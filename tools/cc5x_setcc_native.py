#!/usr/bin/env python3
from __future__ import annotations

import argparse
import functools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

try:
    from cc5x_setcc_native_lib.headergen import (
        render_dynamic_config_section,
        render_full_header,
    )
    from cc5x_setcc_native_lib.packs import (
        discover_atpack_dirs,
        discover_pack_roots,
        find_device_in_atpacks,
        find_device_in_unpacked_packs,
        list_devices_in_atpacks,
        list_devices_in_unpacked_packs,
        mplabx_install_bases,
        mplabx_version_dirs,
        normalize_device_name,
        parse_version,
        device_short_name,
    )
    from cc5x_setcc_native_lib.picmeta import load_device_metadata
    from cc5x_setcc_native_lib.intellisense import (
        DEFAULT_CC5X_VERSION,
        build_intellisense,
    )
    from cc5x_setcc_native_lib.fsutil import atomic_write_text
    from cc5x_setcc_native_lib.project import (
        delete_project_edition,
        default_project_manifest,
        load_project_file,
        project_summary,
        remove_project_edition_config,
        set_project_edition,
        update_project_fields,
        update_project_edition_build_options,
        update_project_edition_config,
        validate_project_file,
        write_project_file,
    )
except ModuleNotFoundError:
    from tools.cc5x_setcc_native_lib.fsutil import atomic_write_text
    from tools.cc5x_setcc_native_lib.headergen import (
        render_dynamic_config_section,
        render_full_header,
    )
    from tools.cc5x_setcc_native_lib.packs import (
        discover_atpack_dirs,
        discover_pack_roots,
        find_device_in_atpacks,
        find_device_in_unpacked_packs,
        list_devices_in_atpacks,
        list_devices_in_unpacked_packs,
        mplabx_install_bases,
        mplabx_version_dirs,
        normalize_device_name,
        parse_version,
        device_short_name,
    )
    from tools.cc5x_setcc_native_lib.picmeta import load_device_metadata
    from tools.cc5x_setcc_native_lib.intellisense import (
        DEFAULT_CC5X_VERSION,
        build_intellisense,
    )
    from tools.cc5x_setcc_native_lib.project import (
        delete_project_edition,
        default_project_manifest,
        load_project_file,
        project_summary,
        remove_project_edition_config,
        set_project_edition,
        update_project_fields,
        update_project_edition_build_options,
        update_project_edition_config,
        validate_project_file,
        write_project_file,
    )


CONFIG_LINE_RE = re.compile(
    r"^#pragma\s+config\s+/(?P<register>\d+)\s+0x(?P<mask>[0-9A-Fa-f]+)\s+"
    r"(?P<name>[A-Za-z0-9_]+)\s*=\s*(?P<state>[A-Za-z0-9_]+)"
    r"(?:\s*//\s*(?P<comment>.*))?$"
)

DEVICE_NAME_RE = re.compile(r"PIC(?:1[0268]|18)[A-Z]*[0-9A-Z]+", re.IGNORECASE)
START_MARKERS = {
    "5x": "START_CONFIG_SETCC_5X.",
    "8e": "START_CONFIG_SETCC_8E.",
}
END_MARKERS = {
    "5x": "END_CONFIG_SETCC_5X.",
    "8e": "END_CONFIG_SETCC_8E.",
}


def _install_root() -> Path:
    """Root of the tree that holds the bundled toolchain (``cc5x_paid/``, the runner).

    Source checkout: the repo root is two levels above this module (``tools/``).
    Frozen (PyInstaller ``--onefile``): ``__file__`` resolves *inside* the throwaway
    ``_MEIPASS`` extraction dir (``/tmp/_MEIxxxx/...``), which never contains ``cc5x_paid``,
    so the defaults must be resolved next to the **executable** instead. The repo ships the
    binary in ``<root>/dist/``, so the root is the dir above it (audit #8 — a bundle must not
    point its compiler default at ``/tmp``).
    """
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        return exe_dir.parent if exe_dir.name == "dist" else exe_dir
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = _install_root()
VALIDATION_ROOT = PROJECT_ROOT / "validation" / "generated"
DEFAULT_COMPILER = PROJECT_ROOT / "cc5x_paid" / "CC5X" / "CC5X.EXE"


def _runner_candidates() -> list[Path]:
    """Install-relative locations of the CrossOver/Wine wrapper, newest-preference first.

    No hardcoded home path (audit #8): the repo historically ships ``cc5x-run.sh`` one level
    above the checkout, so ``PROJECT_ROOT.parent`` discovers it on the dev machine while
    staying portable to any install layout. ``$CC5X_RUNNER`` overrides this everywhere it is
    consumed (build / doctor), so this is only the no-env fallback.
    """
    return [PROJECT_ROOT / "cc5x-run.sh", PROJECT_ROOT.parent / "cc5x-run.sh"]


def default_runner() -> Path:
    """First existing runner candidate, or the first candidate as a points-somewhere fallback."""
    candidates = _runner_candidates()
    return next((candidate for candidate in candidates if candidate.exists()), candidates[0])


DEFAULT_RUNNER = default_runner()
DEFAULT_CROSSOVER_BINARIES = [
    Path("/opt/cxoffice/bin/cxrun"),
    Path("/opt/cxoffice/bin/wine"),
]
DEFAULT_CROSSOVER_BOTTLE = Path.home() / ".cxoffice" / "CC5X"
# IPECMD `-TP` tool-select code; PK4 = MPLAB PICkit 4. See `Readme for IPECMD.htm`.
DEFAULT_PROGRAMMER_TOOL = "PK4"


@dataclass(frozen=True)
class ConfigOption:
    register: int
    mask: int
    name: str
    state: str
    comment: str = ""


@dataclass
class ConfigSymbol:
    name: str
    options: dict[str, ConfigOption] = field(default_factory=dict)

    def add(self, option: ConfigOption) -> None:
        self.options[option.state] = option


def parse_header_config(header_path: Path) -> dict[str, ConfigSymbol]:
    symbols: dict[str, ConfigSymbol] = {}
    for raw_line in header_path.read_text(encoding="latin-1").splitlines():
        line = raw_line.strip()
        match = CONFIG_LINE_RE.match(line)
        if not match:
            continue
        option = ConfigOption(
            register=int(match.group("register")),
            mask=int(match.group("mask"), 16),
            name=match.group("name"),
            state=match.group("state"),
            comment=match.group("comment") or "",
        )
        symbols.setdefault(option.name, ConfigSymbol(name=option.name)).add(option)
    if not symbols:
        raise SystemExit(f"no dynamic config symbols found in {header_path}")
    return symbols


def infer_device_name(path: Path) -> str:
    match = DEVICE_NAME_RE.search(path.stem.upper())
    return match.group(0).upper() if match else path.stem.upper()


def detect_family_from_header(symbols: dict[str, ConfigSymbol], header_path: Path) -> str:
    device_name = infer_device_name(header_path)
    if device_name.startswith("PIC18"):
        return "8e"
    return "5x"


def default_symbol_values(symbols: dict[str, ConfigSymbol]) -> dict[str, str]:
    defaults: dict[str, str] = {}
    for name, symbol in symbols.items():
        best = max(symbol.options.values(), key=lambda opt: opt.mask)
        defaults[name] = best.state
    return defaults


def pack_config_symbols(metadata) -> dict[str, ConfigSymbol]:
    symbols: dict[str, ConfigSymbol] = {}
    for register, word in enumerate(metadata.config_words, start=1):
        for setting in word.settings:
            symbol = symbols.setdefault(setting.name, ConfigSymbol(name=setting.name))
            for value in setting.values:
                # Mask the pack-supplied value to its setting's bits before OR-ing it in, so a
                # malformed CVALUE with stray high bits can't bleed into neighbouring settings
                # of the same word. Matches headergen._render_config_line (audit consistency).
                resolved_word = (word.default & ~setting.mask) | (value.value & setting.mask)
                symbol.add(
                    ConfigOption(
                        register=register,
                        mask=resolved_word,
                        name=setting.name,
                        state=value.name,
                        comment=value.description,
                    )
                )
    return symbols


def default_pack_symbol_values(metadata) -> dict[str, str]:
    defaults: dict[str, str] = {}
    for word in metadata.config_words:
        for setting in word.settings:
            default_value = word.default & setting.mask
            match = next(
                (value for value in setting.values if value.value == default_value),
                None,
            )
            if match is not None:
                defaults[setting.name] = match.name
    return defaults


def _safe_config_comment(text: str) -> str:
    """Collapse a pack-derived config comment to a single, write-safe line.

    Pack metadata is untrusted: a newline would break out of the ``//`` comment, a trailing
    backslash would line-continue and swallow the next generated line, and a non-latin-1
    character would raise ``UnicodeEncodeError`` when the managed block is written into the
    latin-1 source file (truncating it). Strip backslashes, collapse whitespace, and replace
    any non-encodable character so the rendered block is always safe to write.
    """
    collapsed = " ".join(text.replace("\\", " ").split())
    return collapsed.encode("latin-1", "replace").decode("latin-1")


def _safe_block_comment(text: str) -> str:
    """Collapse untrusted text to a single line safe inside a ``/* ... */`` comment.

    The block preamble interpolates a caller-supplied label (a manifest/edition name or
    pack-derived device name). A ``*/`` in that label would close the comment early and
    splice the following text in as live source; a newline would do the same. Collapse
    whitespace, neutralize any ``*/`` sequence, and replace non-latin-1 characters so the
    rendered preamble is always write-safe.
    """
    collapsed = " ".join(text.replace("\\", " ").split()).replace("*/", "* /")
    return collapsed.encode("latin-1", "replace").decode("latin-1")


# Config symbol/state names are restricted CC5X config tokens in real device headers
# (CONFIG_LINE_RE enforces this when parsing; some legal states begin with a digit). Pack
# metadata, however, is untrusted: a
# crafted setting/value name could carry a newline or ``//`` and inject directives into the
# generated ``#pragma config`` block. Validate against this charset and reject anything else
# rather than silently coercing it (a coerced fuse name would set the wrong bits).
_CONFIG_TOKEN_RE = re.compile(r"^[A-Za-z0-9_]+$")


def config_lines_for_settings(
    symbols: dict[str, ConfigSymbol],
    settings: dict[str, str],
) -> list[str]:
    lines: list[str] = []
    for name in sorted(settings):
        state = settings[name]
        symbol = symbols.get(name)
        if symbol is None:
            raise SystemExit(f"unknown config symbol: {name}")
        option = symbol.options.get(state)
        if option is None:
            available = ", ".join(sorted(symbol.options))
            raise SystemExit(
                f"invalid state for {name}: {state}. available: {available}"
            )
        if not _CONFIG_TOKEN_RE.match(name) or not _CONFIG_TOKEN_RE.match(state):
            # Header-parsed symbols always pass (CONFIG_LINE_RE), so this only fires for
            # untrusted pack metadata carrying an unsafe name/state — refuse rather than
            # emit it into the #pragma config block.
            raise SystemExit(
                f"unsafe config symbol/state for {name!r} = {state!r}: "
                "only letters, digits, and underscore are allowed"
            )
        line = f"#pragma config {name} = {state}"
        if option.comment:
            line += f" // {_safe_config_comment(option.comment)}"
        lines.append(line)
    return lines


def render_config_block(
    header_path: Path,
    family: str | None,
    settings: dict[str, str],
    include_defaults: bool,
) -> str:
    symbols = parse_header_config(header_path)
    family = family or detect_family_from_header(symbols, header_path)
    merged = default_symbol_values(symbols) if include_defaults else {}
    merged.update(settings)
    lines = [
        "/*",
        f" * Managed by cc5x_setcc_native.py from {_safe_block_comment(header_path.name)}",
        " * Update this block with the tool instead of editing it manually.",
        " */",
        f"// {START_MARKERS[family]}",
    ]
    lines.extend(config_lines_for_settings(symbols, merged))
    lines.append(f"// {END_MARKERS[family]}")
    return "\n".join(lines) + "\n"


def render_config_block_from_symbols(
    source_label: str,
    family: str,
    symbols: dict[str, ConfigSymbol],
    settings: dict[str, str],
) -> str:
    lines = [
        "/*",
        f" * Managed by cc5x_setcc_native.py from {_safe_block_comment(source_label)}",
        " * Update this block with the tool instead of editing it manually.",
        " */",
        f"// {START_MARKERS[family]}",
    ]
    lines.extend(config_lines_for_settings(symbols, settings))
    lines.append(f"// {END_MARKERS[family]}")
    return "\n".join(lines) + "\n"


def update_managed_block(
    source_text: str,
    block: str,
    family: str,
) -> tuple[str, bool]:
    start = START_MARKERS[family]
    end = END_MARKERS[family]
    start_pattern = re.compile(
        rf"(?m)^[ \t]*//[ \t]*{re.escape(start)}[ \t]*$"
    )
    end_pattern = re.compile(
        rf"(?m)^[ \t]*//[ \t]*{re.escape(end)}[ \t]*$"
    )
    start_matches = list(start_pattern.finditer(source_text))
    end_matches = list(end_pattern.finditer(source_text))
    if not start_matches and not end_matches:
        if source_text and not source_text.endswith("\n"):
            source_text += "\n"
        if source_text and not source_text.endswith("\n\n"):
            source_text += "\n"
        return source_text + block, False
    if len(start_matches) != 1 or len(end_matches) != 1:
        raise ValueError(
            f"malformed managed config markers for family {family!r}: "
            f"found {len(start_matches)} start marker(s) and {len(end_matches)} end marker(s); "
            "expected exactly one of each"
        )
    if start_matches[0].start() >= end_matches[0].start():
        raise ValueError(
            f"malformed managed config markers for family {family!r}: "
            "the end marker appears before the start marker"
        )

    # Match only the managed region (an optional tool-written preamble immediately above the
    # start marker, then start..end), NOT from the beginning of the file. The previous
    # `^.*?//START` form anchored at file start, so replacing a block deleted every line
    # preceding the start marker — a silent data-loss bug.
    # MULTILINE only (no DOTALL): line-oriented parts use [^\n] so they cannot run past a
    # line, and only the block body uses [\s\S]*? to span lines lazily up to the end marker.
    # This avoids cross-line over-matching / backtracking that a global `.` (DOTALL) invites.
    managed_preamble = (
        r"(?:^/\*\n"
        r"[ \t]*\* Managed by cc5x_setcc_native\.py\b[^\n]*\n"
        r"(?:[ \t]*\*[^\n]*\n)*?"
        r"[ \t]*\*/\n)?"
    )
    pattern = re.compile(
        r"(?m)"
        + managed_preamble
        + rf"^[ \t]*//[ \t]*{re.escape(start)}[ \t]*\n"
        + r"[\s\S]*?"
        + rf"^[ \t]*//[ \t]*{re.escape(end)}[ \t]*$"
    )
    if not pattern.search(source_text):
        raise ValueError(
            f"malformed managed config block for family {family!r}: "
            "the marker pair could not be parsed safely"
        )
    return pattern.sub(block.rstrip("\n"), source_text, count=1), True


def parse_key_value_pairs(items: Iterable[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"expected NAME=STATE, got: {item}")
        name, value = item.split("=", 1)
        parsed[name.strip()] = value.strip()
    return parsed


def discover_mplab_roots(explicit_root: str | None) -> list[Path]:
    roots: list[Path] = []
    if explicit_root:
        roots.append(Path(explicit_root).expanduser())
    home = Path.home()
    candidates = [
        home / ".wine/drive_c/Program Files/Microchip/MPLABX",
        home / ".wine/drive_c/Program Files (x86)/Microchip/MPLABX",
        home / ".cxoffice" / "MPLABX" / "drive_c/Program Files/Microchip/MPLABX",
        home / "Microchip/MPLABX",
        Path("/opt/microchip/mplabx"),
    ]
    for candidate in candidates:
        if candidate not in roots:
            roots.append(candidate)
    return roots


def find_device_metadata(device: str, mplab_root: str | None) -> dict[str, str | None]:
    normalized = normalize_device_name(device)
    pack_result = find_device_in_unpacked_packs(normalized)
    atpack_result = find_device_in_atpacks(normalized)
    pack_candidates = [
        result
        for result in (atpack_result, pack_result)
        if any(result.get(key) for key in ("pic", "atdf", "cfgdata", "ini"))
    ]
    active_pack = (
        max(pack_candidates, key=lambda item: parse_version(item.get("pack_version")))
        if pack_candidates else pack_result
    )
    stem = normalized[3:].lower()
    roots = [root for root in discover_mplab_roots(mplab_root) if root.exists()]
    ini_path: Path | None = None
    cfg_path: Path | None = None
    for root in roots:
        for path in root.rglob(f"{stem}.ini"):
            ini_path = path
            break
        for path in root.rglob(f"{stem}.cfgdata"):
            cfg_path = path
            break
        if ini_path or cfg_path:
            return {
                "device": normalized,
                "pack_family": active_pack["pack_family"],
                "pack_version": active_pack["pack_version"],
                "pack_root": active_pack["pack_root"],
                "pic": active_pack["pic"],
                "atdf": active_pack["atdf"],
                "pack_cfgdata": active_pack["cfgdata"],
                "pack_ini": active_pack.get("ini"),
                "pdsc": active_pack.get("pdsc"),
                "mplab_root": str(root),
                "ini": str(ini_path) if ini_path else None,
                "cfgdata": str(cfg_path) if cfg_path else None,
            }
    return {
        "device": normalized,
        "pack_family": active_pack["pack_family"],
        "pack_version": active_pack["pack_version"],
        "pack_root": active_pack["pack_root"],
        "pic": active_pack["pic"],
        "atdf": active_pack["atdf"],
        "pack_cfgdata": active_pack["cfgdata"],
        "pack_ini": active_pack.get("ini"),
        "pdsc": active_pack.get("pdsc"),
        "mplab_root": None,
        "ini": None,
        "cfgdata": None,
    }


RUNNER_COMPILER_PLACEHOLDER = "{compiler}"

# Bare interpreters that cannot run CC5X without being handed the compiler path. A
# placeholder-free runner is otherwise assumed self-contained; flagging these by name turns
# the common "runner": "wine" mistake into a loud, actionable error instead of silently
# dropping the compiler (which would invoke the interpreter with no program).
_BARE_INTERPRETERS = {"wine", "wine64", "wine-stable", "wine-development", "cxrun"}


def build_command(
    compiler: str,
    main_file: str,
    options: list[str],
    runner: list[str],
) -> list[str]:
    """Assemble the compiler invocation, treating ``runner`` as a command template.

    The compiler path is inserted via the ``{compiler}`` placeholder so the behavior does
    not depend on the runner's filename (a previous version special-cased ``cc5x-run.sh``,
    which silently changed semantics when the wrapper was renamed):

    * no runner -> invoke the compiler directly;
    * runner contains ``{compiler}`` -> substitute the compiler there (e.g. ``wine {compiler}``);
    * runner without the placeholder -> treated as self-contained (it supplies its own
      compiler, like the CrossOver ``cc5x-run.sh`` wrapper that forwards ``$@`` to a hard-coded
      ``CC5X.EXE``), so only the options + source are passed. A bare interpreter here (e.g.
      ``wine``) is almost certainly a mistake and raises a clear error rather than silently
      dropping the compiler.

    In every case the CC5X options and the source file are appended last.
    """
    if not runner:
        return [compiler, *options, main_file]
    if any(RUNNER_COMPILER_PLACEHOLDER in token for token in runner):
        command = [token.replace(RUNNER_COMPILER_PLACEHOLDER, compiler) for token in runner]
    else:
        if Path(runner[0]).name.lower() in _BARE_INTERPRETERS:
            raise SystemExit(
                f"runner {runner[0]!r} needs the compiler path; add the {{compiler}} "
                f"placeholder, e.g. \"{runner[0]} {{compiler}}\""
            )
        command = list(runner)
    command.extend(options)
    command.append(main_file)
    return command


def runner_executable(runner_spec: str) -> Path | None:
    """Resolve a runner *command template* to the filesystem path of its executable.

    A runner is not a plain path: it may carry arguments and the ``{compiler}`` placeholder
    (``"/x/cc5x-run.sh {compiler}"``, ``"wine {compiler}"``). ``doctor`` must check the
    executable token — the first ``shlex`` word — not the whole string; treating the template
    as one path made ``…/runner {compiler}`` report as missing (audit #5). Returns ``None``
    when the spec is empty/unparseable, leads with the placeholder, or the executable cannot
    be found. An absolute/relative path is checked on disk; a bare command name (``wine``) is
    looked up on ``$PATH``.
    """
    try:
        tokens = shlex.split(runner_spec)
    except ValueError:
        return None
    if not tokens:
        return None
    first = tokens[0]
    if RUNNER_COMPILER_PLACEHOLDER in first:
        return None
    candidate = Path(first).expanduser()
    looks_like_path = (
        candidate.is_absolute()
        or os.sep in first
        or (os.altsep is not None and os.altsep in first)
    )
    if looks_like_path:
        return candidate if candidate.exists() else None
    found = shutil.which(first)
    return Path(found) if found else None


# Matches a CC5X `#pragma chip ...` directive at the start of a (possibly indented)
# line. CC5X rejects a build that selects the device both via `-p<chip>` and via a
# `#pragma chip` in an included header ("Duplicate chip definition"), so the build
# must supply `-p` only when the header does not already define the chip.
_PRAGMA_CHIP_RE = re.compile(r"^\s*#\s*pragma\s+chip\b", re.IGNORECASE)
# Captures the chip name token following `#pragma chip` (e.g. PIC16F1509 from
# "#pragma chip PIC16F1509, core 14 enh"). CC5X chip names are alphanumeric.
_PRAGMA_CHIP_NAME_RE = re.compile(
    r"^\s*#\s*pragma\s+chip\s+(?P<chip>[A-Za-z0-9]+)", re.IGNORECASE
)


def header_defines_chip(header_path: Path) -> bool:
    """Return True if the header declares the target chip via `#pragma chip`.

    Generated and CC5X-supplied headers carry `#pragma chip`; user-provided
    ("existing") headers may or may not. The caller uses this to decide whether to
    also pass `-p<device>` on the command line without provoking a duplicate-chip error.
    """
    try:
        text = header_path.read_text(encoding="latin-1")
    except OSError:
        return False
    return any(_PRAGMA_CHIP_RE.match(line) for line in text.splitlines())


def header_chip_name(header_path: Path) -> str | None:
    """Return the chip named by the header's `#pragma chip`, or None.

    None means either the header has no `#pragma chip` or its argument could not be
    parsed; callers should not treat None as a device mismatch.
    """
    try:
        text = header_path.read_text(encoding="latin-1")
    except OSError:
        return None
    for line in text.splitlines():
        match = _PRAGMA_CHIP_NAME_RE.match(line)
        if match:
            return match.group("chip")
    return None


def build_options_for_project(project, edition, header_path: Path) -> list[str]:
    """Assemble CC5X command-line options for a project edition build.

    Shared by the CLI (`cmd_build`) and the GUI so both make the same `-p`/`#pragma chip`
    decision (audit A1). When the resolved header selects the chip via `#pragma chip`,
    `-p<device>` is omitted (CC5X rejects a duplicate chip definition); when the header
    also names a parseable chip, it must equal the manifest device or the build is
    refused rather than silently targeting the wrong part (audit A4).
    """
    options = [f"-I{header_path.parent}"]
    if header_defines_chip(header_path):
        chip = header_chip_name(header_path)
        if chip is not None:
            expected = normalize_device_name(project.device)
            actual = normalize_device_name(chip)
            if actual != expected:
                raise SystemExit(
                    f"header {header_path} selects chip {actual} but the project device is "
                    f"{expected}; regenerate the header or fix the manifest device before building"
                )
    else:
        options.insert(0, f"-p{device_short_name(project.device)}")
    options.extend(project.base_build_options)
    options.extend(edition.build_options)
    return options


# IPECMD operation flags, verified against `Readme for IPECMD.htm` (MPLAB X v6.30):
#   program -> -M, verify -> -Y, erase -> -E, blank-check -> -C.
# program/verify require a hex image (-F<file>); erase/blank-check do not.
IPECMD_ACTION_FLAG = {
    "program": "-M",
    "verify": "-Y",
    "erase": "-E",
    "blank-check": "-C",
}
IPECMD_ACTIONS_NEEDING_IMAGE = ("program", "verify")
# Actions that modify the connected device. These require explicit confirmation before
# running (audit): the interactive GUI/extension pass --yes after their own modal warning,
# while a generated terminal task (which has no modal) must prompt the user to type 'yes'.
DESTRUCTIVE_PROGRAM_ACTIONS = ("program", "erase")


def discover_ipecmd() -> Path | None:
    """Locate an IPECMD launcher system-wide.

    Order: ``$CC5X_IPECMD`` override, then ``mplab_platform/mplab_ipe/ipecmd.{sh,exe}``
    under each installed MPLAB X version (newest path first).
    """
    override = os.environ.get("CC5X_IPECMD")
    if override:
        path = Path(override).expanduser()
        return path if path.exists() else None
    relative = (
        Path("mplab_platform") / "mplab_ipe" / "ipecmd.sh",
        Path("mplab_platform") / "mplab_ipe" / "ipecmd.exe",
    )
    # Newest installed MPLAB X first (shared walk, numeric version order: v6.30 > v6.5).
    for version_dir in mplabx_version_dirs():
        for rel in relative:
            candidate = version_dir / rel
            if candidate.exists():
                return candidate
    return None


def ipecmd_command(
    ipecmd: Path,
    device: str,
    tool: str,
    action: str,
    image: Path | None,
    release_from_reset: bool = False,
    extra_args: list[str] | None = None,
) -> list[str]:
    """Build an IPECMD command line for a program/erase/verify/blank-check operation."""
    if action not in IPECMD_ACTION_FLAG:
        raise SystemExit(
            f"unknown ipecmd action {action!r}; expected one of {sorted(IPECMD_ACTION_FLAG)}"
        )
    command = [str(ipecmd), f"-P{device_short_name(device)}", f"-TP{tool}"]
    if action in IPECMD_ACTIONS_NEEDING_IMAGE:
        if image is None:
            raise SystemExit(f"action {action!r} requires a hex image (--hex)")
        command.append(f"-F{image}")
    command.append(IPECMD_ACTION_FLAG[action])
    if release_from_reset:
        command.append("-OL")  # release target from reset after the operation
    command.extend(extra_args or [])
    return command


def validated_device_names(validation_root: Path = VALIDATION_ROOT) -> list[str]:
    devices: list[str] = []
    if not validation_root.exists():
        return devices
    for device_dir in sorted(path for path in validation_root.iterdir() if path.is_dir()):
        generated_dir = device_dir / "generated"
        shipped_dir = device_dir / "shipped"
        if any(generated_dir.glob("*.hex")) and any(shipped_dir.glob("*.hex")):
            devices.append(normalize_device_name(device_dir.name))
    return devices


def environment_report() -> dict[str, object]:
    # Compute pack roots and the validated set once and reuse them below; previously the
    # report walked discover_pack_roots() and validated_device_names() twice each (A10).
    archive_dirs = discover_atpack_dirs()
    pack_roots = discover_pack_roots()
    atpack_entries = list_devices_in_atpacks()
    unpacked_entries = list_devices_in_unpacked_packs(
        roots=[root for root in pack_roots if root.exists()]
    )
    validated = validated_device_names()
    # A device may be present in both a downloaded .atpack and an installed MPLAB X
    # pack; count distinct device names so the totals are not double-counted.
    pack_device_names = {
        str(entry["device"])
        for entry in (*atpack_entries, *unpacked_entries)
        if entry.get("device")
    }
    crossover_bins = {str(path): path.exists() for path in DEFAULT_CROSSOVER_BINARIES}
    # A runner is a command template (it may carry args + the {compiler} placeholder), so the
    # spec string and its resolved executable are tracked separately: doctor reports the spec
    # the user configured but checks existence on the executable token (audit #5).
    runner_specs = [str(DEFAULT_RUNNER)]
    env_runner = os.environ.get("CC5X_RUNNER")
    if env_runner:
        runner_specs.insert(0, env_runner)
    compiler_candidates = [DEFAULT_COMPILER]
    env_compiler = os.environ.get("CC5X_COMPILER")
    if env_compiler:
        compiler_candidates.insert(0, Path(env_compiler).expanduser())
    selected_runner = next(
        (spec for spec in runner_specs if runner_executable(spec) is not None), None
    )
    selected_compiler = next((path for path in compiler_candidates if path.exists()), None)
    return {
        "pack_archive_dirs": [str(path) for path in archive_dirs],
        "pack_roots": [str(path) for path in pack_roots],
        "pack_archive_count": len(atpack_entries),
        "pack_unpacked_count": len(unpacked_entries),
        "pack_device_count": len(pack_device_names),
        "validated_device_count": len(validated),
        "validated_devices": validated,
        "runner": str(selected_runner) if selected_runner else None,
        "runner_exists": bool(selected_runner),
        "compiler": str(selected_compiler) if selected_compiler else None,
        "compiler_exists": bool(selected_compiler),
        "crossover_binaries": crossover_bins,
        "crossover_bottle": str(DEFAULT_CROSSOVER_BOTTLE),
        "crossover_bottle_exists": DEFAULT_CROSSOVER_BOTTLE.exists(),
        "ready": bool(pack_device_names) and bool(selected_runner) and bool(selected_compiler),
    }


def load_project_and_edition(project_path: str, edition_name: str | None):
    path = Path(project_path)
    project = load_project_file(path)
    errors = validate_project_file(project)
    if errors:
        raise SystemExit(f"invalid project file {path}:\n- " + "\n- ".join(errors))
    selected_edition = edition_name or next(iter(project.editions))
    edition = project.editions.get(selected_edition)
    if edition is None:
        available = ", ".join(sorted(project.editions))
        raise SystemExit(
            f"unknown edition {selected_edition!r} in {path}. available: {available}"
        )
    return path, project, edition


def project_path_join(project_path: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (project_path.parent / path).resolve()


DEFAULT_PROJECT_MANIFEST = "setcc-native.json"


def resolve_project_manifest(
    project: str,
    workspace_root: str | None = None,
    *,
    discover: bool = True,
) -> Path:
    """Resolve a ``--project`` value to a concrete manifest path.

    Precedence (highest first), so an explicit choice always wins over discovery:

    1. An **absolute** ``--project`` path is used verbatim.
    2. A relative ``--project`` that carries a **directory component** (e.g.
       ``sub/setcc-native.json``) is anchored to the workspace root / current dir.
    3. For a **bare filename** (the default ``setcc-native.json``), when ``discover`` is set:
       ``$CC5X_HELPER_PROJECT`` is honoured if set (the GUI reads the same var), otherwise the
       file is located git-style by walking up from the workspace root (or the current dir),
       so the command works from any project subdirectory; failing that, the conventional
       ``<root>/<name>`` is returned so a not-found error points there.

    NB: the env pin only takes effect when this resolver actually runs. The read/edit commands
    default ``--project`` to a bare name, so it always runs for them. The optional-project
    commands (build/sync-config/artifacts/program) have no default and stay standalone unless
    the user opts into project mode with ``--project`` or ``--workspace-root`` — so for those,
    the env pin applies only once one of those engages resolution (it must not silently force
    project mode and override an explicit standalone ``--compiler``/``--main``).

    ``discover=False`` (used by ``project-init``) neither pins via the env var nor walks up:
    a new manifest is created at the anchored ``<root>/<name>`` rather than resolving onto an
    existing ancestor's — or a pinned, possibly unrelated — manifest.
    """
    start = Path(workspace_root).expanduser() if workspace_root else Path.cwd()
    try:
        start = start.resolve()
    except OSError:  # pragma: no cover - resolve() is non-strict, but be defensive
        pass

    candidate = Path(project).expanduser()
    if candidate.is_absolute():
        return candidate
    # A path carrying a directory component is taken literally (relative to the root).
    if candidate.parent != Path("."):
        return start / candidate

    # Bare filename. Only discovery (read/edit commands) honours the env pin, then walks up;
    # project-init (discover=False) does neither, so a new manifest lands at <root>/<name>.
    name = candidate.name
    if discover:
        env_override = os.environ.get("CC5X_HELPER_PROJECT")
        if env_override:
            return Path(env_override).expanduser()
        for base in [start, *start.parents]:
            found = base / name
            if found.is_file():
                return found
    return start / name


def project_metadata(project) -> tuple[dict[str, str | None], object]:
    result = find_device_metadata(project.device, project.mplab_root)
    metadata = load_device_metadata(
        device=result["device"],
        ini_reference=result.get("pack_ini") or result.get("ini"),
        cfgdata_reference=result.get("pack_cfgdata") or result.get("cfgdata"),
        pic_reference=result.get("pic"),
    )
    return result, metadata


def _resolve_supplied_header(directory: Path, short_name: str) -> Path | None:
    """Find a CC5X-supplied ``<short_name>.H`` header, case-insensitively.

    ``device_short_name`` canonicalises the device to upper case (``16F1509``), but a
    CC5X install on a case-sensitive filesystem may ship the header as ``16f1509.h`` or
    similar. Try the canonical name first, then fall back to a case-insensitive scan so
    the lookup does not depend on the manifest's device casing (audit A7).
    """
    target = f"{short_name}.h".lower()
    for candidate in (directory / f"{short_name}.H", directory / f"{short_name}.h"):
        if candidate.exists():
            return candidate
    try:
        for entry in directory.iterdir():
            if entry.is_file() and entry.name.lower() == target:
                return entry
    except OSError:
        return None
    return None


def resolve_supplied_header(project_path: Path, project) -> Path:
    """Resolve a compiler-supplied header without falling back to global defaults.

    `header.path` may be an explicit file, a directory containing `<device>.H`, or just
    the header filename. A bare filename is looked up next to the project compiler, so
    `compiler` controls which CC5X install supplies the header.
    """
    short_name = device_short_name(project.device)
    manifest_header_path = project_path_join(project_path, project.header_path)
    if manifest_header_path.exists():
        if manifest_header_path.is_dir():
            supplied_path = _resolve_supplied_header(manifest_header_path, short_name)
            if supplied_path is None:
                raise SystemExit(
                    f"supplied header {short_name}.H not found in {manifest_header_path}"
                )
            return supplied_path
        return manifest_header_path

    raw_header_path = Path(project.header_path).expanduser()
    if len(raw_header_path.parts) == 1:
        supplied_path = _resolve_supplied_header(Path(project.compiler).expanduser().parent, short_name)
        if supplied_path is not None:
            return supplied_path
    raise SystemExit(f"supplied header not found: {manifest_header_path}")


def ensure_project_header(project_path: Path, project) -> Path:
    if project.header_mode == "supplied":
        return resolve_supplied_header(project_path, project)
    header_path = project_path_join(project_path, project.header_path)
    if project.header_mode == "generated":
        rendered = render_project_generated_header(project)
        header_path.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically: a non-latin-1 byte (or any error) must not truncate an existing
        # header to zero. atomic_write_text encodes before touching the destination, so a bad
        # pack description fails the write whole rather than leaving a clobbered file.
        atomic_write_text(header_path, rendered, encoding="latin-1")
        return header_path
    if header_path.exists():
        return header_path
    raise SystemExit(f"existing header not found: {header_path}")


def render_project_generated_header(project) -> str:
    """Render a generated-mode project header without writing it to disk."""
    _, metadata = project_metadata(project)
    try:
        return render_full_header(metadata)
    except ValueError as exc:
        # headergen rejects malformed pack metadata (e.g. an unsupported architecture);
        # surface it as a build-stopping error rather than a traceback (audit #6).
        raise SystemExit(f"cannot generate header for {project.device}: {exc}") from None


def project_build_readiness_errors(project_path: Path, project) -> list[str]:
    """Local preflight checks for files needed before launching CC5X."""
    errors: list[str] = []
    source_path = project_path_join(project_path, project.main_source)
    if not source_path.is_file():
        errors.append(f"main_source not found: {source_path}")

    config_source_path = project_path_join(project_path, project.config_source)
    if not config_source_path.is_file():
        errors.append(f"config_source not found: {config_source_path}")

    runner_parts = shlex.split(project.runner) if project.runner else []
    # A runner without the {compiler} placeholder is self-contained: build_command never
    # passes the compiler path to it (see build_command), so a missing compiler file is not a
    # build blocker. Only require the compiler on disk when it will actually be invoked —
    # directly (no runner) or substituted into a {compiler}-placeholder runner (audit #6).
    compiler_used = not runner_parts or any(
        RUNNER_COMPILER_PLACEHOLDER in token for token in runner_parts
    )
    if compiler_used:
        compiler_path = Path(project.compiler).expanduser()
        if not compiler_path.is_file():
            errors.append(f"compiler not found: {compiler_path}")

    if runner_parts:
        runner_path = Path(runner_parts[0]).expanduser()
        if runner_path.is_absolute() and not runner_path.exists():
            errors.append(f"runner not found: {runner_path}")

    if project.header_mode == "generated":
        try:
            render_project_generated_header(project)
        except SystemExit as exc:
            errors.append(str(exc))
        except Exception as exc:
            # Pack metadata can fail before headergen gets a chance to normalize the error
            # (missing/corrupt XML, capped reads, bad archives). Validation should report a
            # build-readiness problem just like the later build would, not leak a traceback.
            errors.append(f"cannot generate header for {project.device}: {exc}")
    elif project.header_mode in {"existing", "supplied"}:
        try:
            ensure_project_header(project_path, project)
        except SystemExit as exc:
            errors.append(str(exc))
    return errors


def manifest_config_diagnostics(project) -> list[dict[str, str]]:
    """Validate each edition's config NAME/STATE against the device's pack metadata.

    Surfaces the two config-level defects schema validation cannot catch: a symbol the
    device does not expose (``unknown_config_symbol``) and a value that is not one of that
    symbol's legal states (``invalid_config_value``). Either would otherwise only fail later,
    when ``sync-config`` renders the managed config block via ``config_lines_for_settings``.

    Best-effort: if the device's pack metadata cannot be resolved (no packs installed,
    unknown/malformed device) or exposes no config symbols, returns ``[]`` rather than
    flagging every symbol as unknown. This never invents a constraint the toolchain would not
    enforce — it only moves an existing ``sync-config`` failure earlier, into the editor.
    """
    # Nothing to check (and no reason to pay for a pack lookup) when no edition overrides
    # any config symbol — keeps the common empty-config manifest fast and pack-independent.
    if not any(edition.config for edition in project.editions.values()):
        return []
    try:
        _, metadata = project_metadata(project)
        symbols = pack_config_symbols(metadata)
    except (SystemExit, Exception):
        # Metadata resolution can raise OSError / ValueError / xml ParseError /
        # zipfile.BadZipFile (see cmd_project_generate_header). Without symbols we cannot
        # tell legal from illegal, so emit nothing instead of false positives.
        return []
    if not symbols:
        # Metadata resolved but exposed no config symbols (e.g. the device's cfgdata was not
        # found in the pack). Treat as "cannot validate" rather than flagging every entry as
        # unknown — that would be a wave of false positives, not real defects.
        return []
    diagnostics: list[dict[str, str]] = []
    for edition_name in sorted(project.editions):
        edition = project.editions[edition_name]
        for name in sorted(edition.config):
            state = edition.config[name]
            symbol = symbols.get(name)
            if symbol is None:
                diagnostics.append(
                    {
                        "severity": "error",
                        "kind": "unknown_config_symbol",
                        "edition": edition_name,
                        "symbol": name,
                        "message": f"unknown config symbol {name!r} for {project.device}",
                    }
                )
                continue
            if state not in symbol.options:
                available = ", ".join(sorted(symbol.options)) or "(none)"
                diagnostics.append(
                    {
                        "severity": "error",
                        "kind": "invalid_config_value",
                        "edition": edition_name,
                        "symbol": name,
                        "message": (
                            f"invalid value {state!r} for {name} in edition "
                            f"{edition_name!r}; expected one of: {available}"
                        ),
                    }
                )
    return diagnostics


def manifest_header_diagnostic(project_path: Path, project) -> dict[str, str] | None:
    """Flag a missing provided header (existing/supplied modes) against ``header.path``.

    Generated mode synthesizes the header on build, so a not-yet-written generated header is
    not "missing"; only existing/supplied modes reference a file the user must provide.
    """
    if project.header_mode not in {"existing", "supplied"}:
        return None
    try:
        ensure_project_header(project_path, project)
    except SystemExit as exc:
        return {
            "severity": "error",
            "kind": "missing_header",
            "field": "header.path",
            "message": str(exc),
        }
    return None


def project_manifest_diagnostics(project_path: Path, project) -> list[dict[str, str]]:
    """Locatable manifest diagnostics for the editor: unknown config symbols, invalid config
    values, and a missing provided header. Each entry carries enough context (``edition`` +
    ``symbol`` or ``field``) for the extension to anchor it to the offending line in the
    manifest. Schema/source/compiler readiness stays in the existing ``*_errors`` lists.
    """
    diagnostics: list[dict[str, str]] = []
    header_diag = manifest_header_diagnostic(project_path, project)
    if header_diag is not None:
        diagnostics.append(header_diag)
    diagnostics.extend(manifest_config_diagnostics(project))
    return diagnostics


def cmd_probe(args: argparse.Namespace) -> int:
    result = find_device_metadata(args.device, args.mplab_root)
    print(json.dumps(result, indent=2))
    return 0 if any(result.get(key) for key in ("pic", "atdf", "pack_cfgdata", "ini", "cfgdata")) else 1


def cmd_describe_device(args: argparse.Namespace) -> int:
    result = find_device_metadata(args.device, args.mplab_root)
    metadata = load_device_metadata(
        device=result["device"],
        ini_reference=result.get("pack_ini") or result.get("ini"),
        cfgdata_reference=result.get("pack_cfgdata") or result.get("cfgdata"),
        pic_reference=result.get("pic"),
    )
    print(metadata.to_json())
    return 0


def cmd_render_pack_config_section(args: argparse.Namespace) -> int:
    result = find_device_metadata(args.device, args.mplab_root)
    metadata = load_device_metadata(
        device=result["device"],
        ini_reference=result.get("pack_ini") or result.get("ini"),
        cfgdata_reference=result.get("pack_cfgdata") or result.get("cfgdata"),
        pic_reference=result.get("pic"),
    )
    sys.stdout.write(render_dynamic_config_section(metadata))
    return 0


def cmd_render_pack_header(args: argparse.Namespace) -> int:
    result = find_device_metadata(args.device, args.mplab_root)
    metadata = load_device_metadata(
        device=result["device"],
        ini_reference=result.get("pack_ini") or result.get("ini"),
        cfgdata_reference=result.get("pack_cfgdata") or result.get("cfgdata"),
        pic_reference=result.get("pic"),
    )
    sys.stdout.write(render_full_header(metadata))
    return 0


def cmd_list_pack_config(args: argparse.Namespace) -> int:
    result = find_device_metadata(args.device, args.mplab_root)
    metadata = load_device_metadata(
        device=result["device"],
        ini_reference=result.get("pack_ini") or result.get("ini"),
        cfgdata_reference=result.get("pack_cfgdata") or result.get("cfgdata"),
        pic_reference=result.get("pic"),
    )
    symbols = pack_config_symbols(metadata)
    payload = {
        name: [
            {
                "register": option.register,
                "mask": f"0x{option.mask:X}",
                "state": option.state,
                "comment": option.comment,
            }
            for option in sorted(
                symbol.options.values(), key=lambda option: (option.register, option.state)
            )
        ]
        for name, symbol in sorted(symbols.items())
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    for name, options in payload.items():
        print(f"{name}:")
        for option in options:
            suffix = f"  // {option['comment']}" if option["comment"] else ""
            print(
                f"  reg{option['register']} mask={option['mask']} state={option['state']}{suffix}"
            )
    return 0


def cmd_render_pack_config(args: argparse.Namespace) -> int:
    result = find_device_metadata(args.device, args.mplab_root)
    metadata = load_device_metadata(
        device=result["device"],
        ini_reference=result.get("pack_ini") or result.get("ini"),
        cfgdata_reference=result.get("pack_cfgdata") or result.get("cfgdata"),
        pic_reference=result.get("pic"),
    )
    symbols = pack_config_symbols(metadata)
    family = "8e" if metadata.device.startswith("PIC18") else "5x"
    merged = default_pack_symbol_values(metadata) if args.include_defaults else {}
    merged.update(parse_key_value_pairs(args.set or []))
    sys.stdout.write(
        render_config_block_from_symbols(
            source_label=f"pack metadata for {metadata.device}",
            family=family,
            symbols=symbols,
            settings=merged,
        )
    )
    return 0


def cmd_list_config(args: argparse.Namespace) -> int:
    symbols = parse_header_config(Path(args.header))
    if args.json:
        payload = {
            name: [
                {
                    "register": option.register,
                    "mask": f"0x{option.mask:X}",
                    "state": option.state,
                    "comment": option.comment,
                }
                for option in sorted(
                    symbol.options.values(), key=lambda option: (option.register, option.state)
                )
            ]
            for name, symbol in sorted(symbols.items())
        }
        print(json.dumps(payload, indent=2))
        return 0

    for name, symbol in sorted(symbols.items()):
        print(f"{name}:")
        for option in sorted(symbol.options.values(), key=lambda option: (option.register, option.state)):
            suffix = f"  // {option.comment}" if option.comment else ""
            print(
                f"  reg{option.register} mask=0x{option.mask:X} state={option.state}{suffix}"
            )
    return 0


def cmd_render_config(args: argparse.Namespace) -> int:
    block = render_config_block(
        header_path=Path(args.header),
        family=args.family,
        settings=parse_key_value_pairs(args.set or []),
        include_defaults=args.include_defaults,
    )
    sys.stdout.write(block)
    return 0


def cmd_sync_config(args: argparse.Namespace) -> int:
    if args.project:
        project_path, project, edition = load_project_and_edition(args.project, args.edition)
        _, metadata = project_metadata(project)
        symbols = pack_config_symbols(metadata)
        family = "8e" if metadata.device.startswith("PIC18") else "5x"
        merged = default_pack_symbol_values(metadata)
        merged.update(edition.config)
        merged.update(parse_key_value_pairs(args.set or []))
        block = render_config_block_from_symbols(
            source_label=f"project {project_path.name} [{edition.name}] for {metadata.device}",
            family=family,
            symbols=symbols,
            settings=merged,
        )
        source_path = project_path_join(project_path, project.config_source)
        original = source_path.read_text(encoding="latin-1")
        updated, replaced = update_managed_block(original, block, family)
        atomic_write_text(source_path, updated)
        action = "updated" if replaced else "appended"
        print(f"{action} managed config block in {source_path}")
        return 0
    if not args.source or not args.header:
        raise SystemExit("--source and --header are required unless --project is used")
    header_path = Path(args.header)
    symbols = parse_header_config(header_path)
    family = args.family or detect_family_from_header(symbols, header_path)
    block = render_config_block(
        header_path=header_path,
        family=family,
        settings=parse_key_value_pairs(args.set or []),
        include_defaults=args.include_defaults,
    )
    source_path = Path(args.source)
    original = source_path.read_text(encoding="latin-1")
    updated, replaced = update_managed_block(original, block, family)
    atomic_write_text(source_path, updated)
    action = "updated" if replaced else "appended"
    print(f"{action} managed config block in {source_path}")
    return 0


# CC5X emits diagnostics as "Error <file> <line>: <msg>" / "Warning <file> <line>: <msg>".
# This mirrors the extension's `$cc5x` problem matcher and src/diagnostics.ts so the
# structured (`--json-diagnostics`) form agrees with the text parsing on both sides.
CC5X_DIAGNOSTIC_RE = re.compile(r"^(Error|Warning)\s+(.+?)\s+(\d+):\s+(.*)$")


def parse_cc5x_diagnostics(text: str) -> list[dict[str, object]]:
    """Normalize CC5X build output into structured diagnostics.

    Returns one entry per matched line: ``severity`` ("error"/"warning"), ``file``, ``line``
    (kept 1-based as CC5X reports it; consumers adjust), and ``message``. Non-diagnostic
    lines (e.g. "Warnings (level 1-3): ...") are ignored, matching the contributed matcher.
    """
    diagnostics: list[dict[str, object]] = []
    for raw in text.splitlines():
        match = CC5X_DIAGNOSTIC_RE.match(raw.strip())
        if not match:
            continue
        severity, file, line, message = match.groups()
        diagnostics.append(
            {"severity": severity.lower(), "file": file, "line": int(line), "message": message}
        )
    return diagnostics


def _timeout_seconds(args: argparse.Namespace) -> float | None:
    timeout = getattr(args, "timeout_seconds", 0) or 0
    return float(timeout) if timeout > 0 else None


def _format_seconds(seconds: float) -> str:
    return f"{seconds:g}"


def _timeout_output(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def _finish_build(command: list[str], run_cwd: object, args: argparse.Namespace) -> int:
    """Run (or dry-run) a build command, emitting text or structured-diagnostics JSON.

    With ``--json-diagnostics`` the only thing on stdout is the JSON payload
    (``ok``/``returncode``/``command``/``diagnostics``/``stdout``/``stderr``), so the
    extension can parse it; the exit code is the compiler's return code (nonzero on failure).
    Without it, the command streams through as before.
    """
    json_mode = getattr(args, "json_diagnostics", False)
    if args.dry_run:
        if json_mode:
            print(json.dumps({"ok": True, "dry_run": True, "command": command, "diagnostics": []}, indent=2))
        else:
            print("command:", shlex.join(command))
        return 0
    if json_mode:
        try:
            completed = subprocess.run(
                command,
                cwd=run_cwd,
                capture_output=True,
                text=True,
                timeout=_timeout_seconds(args),
            )
        except OSError as exc:
            # --json-diagnostics promises parseable JSON on stdout; a launch failure
            # (compiler missing/not executable) must stay JSON, not crash with a traceback
            # and empty stdout (audit #4).
            print(
                json.dumps(
                    {
                        "ok": False,
                        "returncode": None,
                        "command": command,
                        "diagnostics": [],
                        "stdout": "",
                        "stderr": str(exc),
                        "error": {"kind": "launch_failed", "message": str(exc)},
                    },
                    indent=2,
                )
            )
            return 1
        except subprocess.TimeoutExpired as exc:
            stdout = _timeout_output(exc.stdout)
            stderr = _timeout_output(exc.stderr)
            timeout = _timeout_seconds(args) or float(exc.timeout)
            combined = f"{stdout}\n{stderr}"
            print(
                json.dumps(
                    {
                        "ok": False,
                        "returncode": None,
                        "command": command,
                        "diagnostics": parse_cc5x_diagnostics(combined),
                        "stdout": stdout,
                        "stderr": stderr,
                        "error": {
                            "kind": "timeout",
                            "message": f"build timed out after {_format_seconds(timeout)} seconds",
                        },
                    },
                    indent=2,
                )
            )
            return 124
        combined = f"{completed.stdout or ''}\n{completed.stderr or ''}"
        payload = {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "command": command,
            "diagnostics": parse_cc5x_diagnostics(combined),
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
        print(json.dumps(payload, indent=2))
        return completed.returncode
    print("command:", shlex.join(command))
    try:
        completed = subprocess.run(command, cwd=run_cwd, timeout=_timeout_seconds(args))
    except subprocess.TimeoutExpired as exc:
        timeout = _timeout_seconds(args) or float(exc.timeout)
        raise SystemExit(
            f"build timed out after {_format_seconds(timeout)} seconds: {shlex.join(command)}"
        ) from None
    return completed.returncode


def _prepare_project_build(args: argparse.Namespace) -> tuple[list[str], object]:
    """Resolve the build command + working dir for a manifest project (may raise SystemExit)."""
    project_path, project, edition = load_project_and_edition(args.project, args.edition)
    build_errors = project_build_readiness_errors(project_path, project)
    if build_errors:
        raise SystemExit(
            f"project is not ready to build {project_path}:\n- " + "\n- ".join(build_errors)
        )
    header_path = ensure_project_header(project_path, project)
    runner = shlex.split(project.runner) if project.runner else []
    source_path = project_path_join(project_path, project.main_source)
    options = build_options_for_project(project, edition, header_path)
    command = build_command(
        compiler=project.compiler,
        main_file=str(source_path),
        options=options,
        runner=runner,
    )
    return command, source_path.parent


def _prepare_standalone_build(args: argparse.Namespace) -> tuple[list[str], object]:
    """Resolve the build command + working dir for an explicit --compiler/--main build."""
    if not args.compiler or not args.main:
        raise SystemExit("--compiler and --main are required unless --project is used")
    runner = shlex.split(args.runner) if args.runner else []
    command = build_command(
        compiler=args.compiler,
        main_file=args.main,
        options=args.option or [],
        runner=runner,
    )
    # Only a real (non-dry-run) build needs the cwd to exist; --dry-run stays a pure preview.
    if args.cwd and not args.dry_run and not Path(args.cwd).is_dir():
        raise SystemExit(f"--cwd directory does not exist: {args.cwd}")
    return command, args.cwd or None


def cmd_build(args: argparse.Namespace) -> int:
    try:
        if args.project:
            command, run_cwd = _prepare_project_build(args)
        else:
            command, run_cwd = _prepare_standalone_build(args)
    except (SystemExit, ValueError, OSError) as exc:
        # --json-diagnostics promises parseable JSON on stdout; honor the contract for
        # launch failures (not-ready project, missing/mismatched header, bad cwd) AND for
        # bad-input errors raised while resolving the build — a malformed manifest
        # (ValueError/OSError) or unparseable pack .ini (configparser.Error, normalized to
        # ValueError in picmeta) must not escape as a traceback with empty stdout.
        if getattr(args, "json_diagnostics", False):
            print(json.dumps(
                {"ok": False, "error": {"kind": "build_not_ready", "message": str(exc)}}, indent=2
            ))
            return 1
        # A non-SystemExit here would otherwise traceback in text mode too; main() converts
        # ValueError/OSError to a clean message, so only re-raise (SystemExit passes through).
        raise
    return _finish_build(command, run_cwd, args)


# CC5X build outputs, matching the extension's artifact view (src/artifacts.ts).
ARTIFACT_EXTENSIONS = (".hex", ".asm", ".occ", ".var", ".fcs", ".cpr", ".cod", ".cof")


def collect_artifacts(search_dir: Path, max_depth: int = 4) -> list[dict[str, object]]:
    """Find CC5X build artifacts under ``search_dir`` (newest first).

    Mirrors the extension walk: recurse up to ``max_depth`` levels, skip ``node_modules``
    and dotted directories, and collect files with an artifact extension. Each entry carries
    ``path``/``name``/``type``/``size``/``mtime``; results are sorted by mtime descending so
    the freshest build output is first. A missing/unreadable directory yields ``[]``.
    """
    found: dict[str, tuple[float, int]] = {}

    def walk(directory: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = list(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            # Do not follow symlinked directories: matches the extension's Dirent.isDirectory()
            # (false for symlinks) and avoids walking a symlink cycle.
            if entry.is_dir() and not entry.is_symlink():
                if entry.name != "node_modules" and not entry.name.startswith("."):
                    walk(entry, depth + 1)
            elif entry.suffix.lower() in ARTIFACT_EXTENSIONS:
                try:
                    stat = entry.stat()
                    found[str(entry)] = (stat.st_mtime, stat.st_size)
                except OSError:
                    found[str(entry)] = (0.0, 0)

    walk(search_dir, 0)
    items = [
        {
            "path": path,
            "name": Path(path).name,
            "type": Path(path).suffix[1:].lower(),
            "size": size,
            "mtime": mtime,
        }
        for path, (mtime, size) in found.items()
    ]
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items


def cmd_artifacts(args: argparse.Namespace) -> int:
    """List CC5X build artifacts for a project (or an explicit --dir) as text or JSON."""
    try:
        if args.project:
            project_path = Path(args.project)
            project = load_project_file(project_path)
            search_dir = project_path_join(project_path, project.main_source).parent
        elif args.dir:
            search_dir = Path(args.dir).expanduser()
        else:
            raise SystemExit("artifacts requires --project or --dir")
    except (SystemExit, OSError, ValueError) as exc:
        # Honor the --json contract for a missing/malformed manifest too (parseable JSON on
        # stdout), mirroring program --json and build --json-diagnostics.
        if args.json:
            print(json.dumps(
                {"ok": False, "error": {"kind": "artifacts_failed", "message": str(exc)}}, indent=2
            ))
            return 1
        raise
    artifacts = collect_artifacts(search_dir)
    if args.json:
        print(json.dumps({"ok": True, "search_dir": str(search_dir), "artifacts": artifacts}, indent=2))
        return 0
    if not artifacts:
        print(f"no artifacts in {search_dir}")
        return 0
    for artifact in artifacts:
        print(f"{artifact['type'].upper():4} {artifact['path']}")
    return 0


def _emit_program_payload(payload: dict[str, object], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2))
        return
    if payload.get("ok") is False and payload.get("error"):
        error = payload["error"]
        print(f"error: {error.get('kind')}: {error.get('message')}")
        return
    print(f"action: {payload.get('action')}")
    print(f"device: {payload.get('device')}  tool: {payload.get('tool')}")
    if payload.get("image"):
        print(f"image: {payload.get('image')}")
    print("command:", shlex.join([str(part) for part in payload.get("command", [])]))


def _json_error_payload(kind: str, message: str, **details: object) -> dict[str, object]:
    """The standard structured error: ``{ok:false, error:{kind, message[, details]}}``."""
    payload: dict[str, object] = {"ok": False, "error": {"kind": kind, "message": message}}
    if details:
        payload["error"]["details"] = details  # type: ignore[index]
    return payload


def _program_error(kind: str, message: str, as_json: bool, **details: object) -> int:
    _emit_program_payload(_json_error_payload(kind, message, **details), as_json)
    return 1


def json_error_boundary(func):
    """Wrap a ``cmd_*`` function so the ``--json`` contract holds on failure.

    In ``--json`` mode a ``SystemExit("<message>")`` or unexpected error becomes the standard
    ``{ok:false, error:{kind, message}}`` payload on stdout (exit 1), instead of a stderr
    message / traceback an extension JSON consumer cannot parse. A ``SystemExit`` carrying an
    int code (argparse, or a command's own chosen exit code) passes through unchanged, and
    non-JSON mode is never altered.
    """

    @functools.wraps(func)
    def wrapper(args: argparse.Namespace) -> int:
        if not getattr(args, "json", False):
            return func(args)
        try:
            return func(args)
        except SystemExit as exc:
            if exc.code is None or isinstance(exc.code, int):
                raise
            print(json.dumps(_json_error_payload("command_failed", str(exc.code)), indent=2))
            return 1
        except Exception as exc:  # JSON boundary: never leak a traceback to a JSON consumer
            print(json.dumps(_json_error_payload("command_failed", str(exc)), indent=2))
            return 1

    return wrapper


def ipecmd_failure_guidance(stdout: str, stderr: str, tool: str) -> list[str]:
    """Actionable troubleshooting hints for a non-zero IPECMD run.

    IPECMD returns 0 on success and a non-zero exit code on failure; its detailed code
    table is PM3-specific, so we classify on the captured output text (best-effort) and
    always append a generic checklist covering the three common Linux failure modes the
    integration cares about: no tool detected, missing USB permissions, and an
    unsupported/mismatched device. The matched-cause hint (if any) is listed first.
    """
    blob = f"{stdout}\n{stderr}".lower()
    hints: list[str] = []
    # Conservative substring heuristics — only to *order* the checklist, never the sole
    # source of help (the full checklist always follows).
    if any(token in blob for token in ("unable to connect", "failed to get device id", "no tool", "target device id")):
        hints.append(
            f"IPECMD could not talk to the programmer. Confirm a {tool} is plugged in, the "
            "target is powered, and the ICSP wiring (PGC/PGD/MCLR/VDD/VSS) is correct."
        )
    if any(token in blob for token in ("not supported", "invalid device", "device not found")):
        hints.append(
            "The selected device may not be supported by this tool/IPECMD. Check the "
            "manifest device name and that its pack is installed (run `doctor`)."
        )
    # Generic checklist (deduped against the leading matched hint).
    checklist = [
        f"Tool: a {tool} must be connected and the target powered; verify the `-TP` tool "
        "code matches your programmer (PK4/PK5/SNAP/ICD4/PKOB4/...).",
        "USB permissions (Linux): your user needs access to the programmer's USB device — "
        "install the MPLAB X udev rules and re-plug the tool (a permission error here looks "
        "like 'no tool detected').",
        "Device: confirm the manifest device is supported and its pack is installed "
        "(`doctor` reports the discovered device count).",
    ]
    for item in checklist:
        if item not in hints:
            hints.append(item)
    return hints


def _confirm_destructive_action(action: str, device: str, args: argparse.Namespace) -> bool:
    """Return whether a device-modifying action is authorized to run.

    ``--yes`` authorizes unconditionally (the GUI/extension pass it after their own modal
    "writes to hardware" warning). Otherwise, only an interactive TTY is offered a typed
    confirmation — a generated VS Code task running in the integrated terminal hits this and
    must type 'yes', so it can no longer flash/erase silently. A machine consumer (``--json``)
    or non-TTY is never prompted; it must pass ``--yes`` explicitly.
    """
    if getattr(args, "yes", False):
        return True
    if getattr(args, "json", False) or not (sys.stdin and sys.stdin.isatty()):
        return False
    prompt = (
        f"{action!r} will modify {normalize_device_name(device)} via {args.tool}. "
        "Type 'yes' to proceed: "
    )
    try:
        reply = input(prompt)
    except EOFError:
        return False
    return reply.strip().lower() == "yes"


def cmd_program(args: argparse.Namespace) -> int:
    """Drive MPLAB IPECMD to program/erase/verify/blank-check a device.

    Resolves the device and (for program/verify) the hex image from an explicit flag or
    from a project manifest, locates an IPECMD launcher, and builds + runs the command.
    """
    action = args.action
    ipecmd = Path(args.ipecmd).expanduser() if args.ipecmd else discover_ipecmd()
    device = args.device
    image = Path(args.hex).expanduser() if args.hex else None

    if args.project:
        project_path, project, _ = load_project_and_edition(args.project, args.edition)
        device = device or project.device
        if image is None and action in IPECMD_ACTIONS_NEEDING_IMAGE:
            source_path = project_path_join(project_path, project.main_source)
            image = source_path.with_suffix(".hex")

    if not device:
        return _program_error(
            "missing_device", "device is required (use --device or --project)", args.json
        )
    if ipecmd is None:
        return _program_error(
            "missing_ipecmd",
            "IPECMD launcher not found; set $CC5X_IPECMD or install MPLAB X",
            args.json,
            searched=[str(base) for base in mplabx_install_bases()],
        )
    if action in IPECMD_ACTIONS_NEEDING_IMAGE:
        if image is None:
            return _program_error(
                "missing_image", f"action {action!r} requires a hex image (--hex)", args.json
            )
        if not args.dry_run and not image.exists():
            return _program_error(
                "image_not_found", f"hex image not found: {image}", args.json, image=str(image)
            )

    command = ipecmd_command(
        ipecmd=ipecmd,
        device=device,
        tool=args.tool,
        action=action,
        image=image,
        release_from_reset=args.release_from_reset,
        extra_args=args.ipe_arg,
    )
    payload: dict[str, object] = {
        "ok": True,
        "action": action,
        "device": normalize_device_name(device),
        "tool": args.tool,
        "ipecmd": str(ipecmd),
        "image": str(image) if image else None,
        "command": [str(part) for part in command],
    }
    if args.dry_run:
        payload["dry_run"] = True
        _emit_program_payload(payload, args.json)
        return 0

    # Gate device-modifying actions behind confirmation so a generated task (or any caller)
    # cannot flash/erase silently. Checked after the dry-run preview so --dry-run stays free.
    if action in DESTRUCTIVE_PROGRAM_ACTIONS and not _confirm_destructive_action(
        action, device, args
    ):
        return _program_error(
            "confirmation_required",
            f"action {action!r} modifies the device; pass --yes (or type 'yes' when prompted)",
            args.json,
        )

    # In --json mode the only thing on stdout must be the JSON object, so the extension
    # (and any other machine consumer) can parse it. IPECMD writes its own progress to
    # stdout/stderr, which would otherwise corrupt the payload (audit A2) — capture it
    # and fold it into the JSON. In human mode, let IPECMD stream through as before.
    try:
        if args.json:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=_timeout_seconds(args),
            )
            payload["stdout"] = completed.stdout
            payload["stderr"] = completed.stderr
        else:
            completed = subprocess.run(command, timeout=_timeout_seconds(args))
    except OSError as exc:
        # IPECMD missing/not executable: keep the --json contract (structured error on
        # stdout) instead of a traceback with empty stdout (audit #4).
        return _program_error(
            "launch_failed", f"could not launch IPECMD: {exc}", args.json, ipecmd=str(ipecmd)
        )
    except subprocess.TimeoutExpired as exc:
        timeout = _timeout_seconds(args) or float(exc.timeout)
        payload["ok"] = False
        payload["returncode"] = None
        if args.json:
            payload["stdout"] = _timeout_output(exc.stdout)
            payload["stderr"] = _timeout_output(exc.stderr)
        payload["error"] = {
            "kind": "timeout",
            "message": (
                f"IPECMD action {action!r} timed out after "
                f"{_format_seconds(timeout)} seconds"
            ),
        }
        _emit_program_payload(payload, args.json)
        return 124
    payload["ok"] = completed.returncode == 0
    payload["returncode"] = completed.returncode
    if completed.returncode != 0:
        # Surface the exit code as a structured error plus actionable guidance the extension
        # can show in the Problems/output channel (TODO Phase 5).
        guidance = ipecmd_failure_guidance(
            getattr(completed, "stdout", "") or "",
            getattr(completed, "stderr", "") or "",
            args.tool,
        )
        payload["error"] = {
            "kind": "ipecmd_failed",
            "message": f"IPECMD exited {completed.returncode} for action {action!r}",
        }
        payload["guidance"] = guidance
        if not args.json:
            # _emit_program_payload prints the `error:` line below; add the hints to stderr.
            print("troubleshooting:", file=sys.stderr)
            for hint in guidance:
                print(f"  - {hint}", file=sys.stderr)
    _emit_program_payload(payload, args.json)
    return completed.returncode


CC5X_TASK_LABEL_PREFIX = "CC5X:"


def generate_vscode_tasks(
    project,
    manifest: str,
    python: str,
    helper: str,
    tool: str,
    problem_matcher: object,
) -> list[dict[str, object]]:
    """Build the list of CC5X VS Code task definitions for a project manifest.

    One build task per edition (the first is the default build task), each paired with a
    program/verify task that runs after it, plus device-level erase and blank-check tasks.
    Tasks use ``type: process`` so args are passed without shell quoting.
    """
    cwd = "${workspaceFolder}"
    presentation = {"reveal": "always", "panel": "shared", "group": "cc5x"}

    def helper_task(label: str, helper_args: list[str], extra: dict[str, object]) -> dict[str, object]:
        task: dict[str, object] = {
            "label": label,
            "type": "process",
            "command": python,
            "args": [helper, *helper_args],
            "options": {"cwd": cwd},
            "problemMatcher": problem_matcher,
            "presentation": presentation,
        }
        task.update(extra)
        return task

    tasks: list[dict[str, object]] = []
    editions = sorted(project.editions)
    for index, edition in enumerate(editions):
        build_label = f"{CC5X_TASK_LABEL_PREFIX} Build {edition}"
        tasks.append(
            helper_task(
                build_label,
                ["build", "--project", manifest, "--edition", edition],
                {"group": {"kind": "build", "isDefault": index == 0}},
            )
        )
        tasks.append(
            helper_task(
                f"{CC5X_TASK_LABEL_PREFIX} Program {edition} ({tool})",
                ["program", "--project", manifest, "--edition", edition, "--tool", tool],
                {"dependsOn": build_label, "dependsOrder": "sequence"},
            )
        )
        tasks.append(
            helper_task(
                f"{CC5X_TASK_LABEL_PREFIX} Verify {edition} ({tool})",
                [
                    "program", "--action", "verify",
                    "--project", manifest, "--edition", edition, "--tool", tool,
                ],
                {"dependsOn": build_label, "dependsOrder": "sequence"},
            )
        )

    tasks.append(
        helper_task(
            f"{CC5X_TASK_LABEL_PREFIX} Erase ({tool})",
            ["program", "--action", "erase", "--project", manifest, "--tool", tool],
            {},
        )
    )
    tasks.append(
        helper_task(
            f"{CC5X_TASK_LABEL_PREFIX} Blank Check ({tool})",
            ["program", "--action", "blank-check", "--project", manifest, "--tool", tool],
            {},
        )
    )
    return tasks


def _strip_jsonc_comments(text: str) -> str:
    """Remove ``//`` line and ``/* */`` block comments, ignoring those inside strings."""
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_trailing_commas(text: str) -> str:
    """Drop commas immediately before a closing ``}``/``]`` (ignoring strings)."""
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def strip_jsonc(text: str) -> str:
    """Normalize JSONC (the dialect VS Code writes for tasks.json) into strict JSON.

    Removes ``//`` line comments, ``/* */`` block comments, and trailing commas, while
    leaving the contents of double-quoted strings untouched. ``json.loads`` rejects all
    three, so without this the merge throws on any real tasks.json and (with --force)
    used to discard every user task (audit A3).

    Comments are stripped first and trailing commas second, in two passes: a comment can
    sit between a comma and the closing ``}``/``]`` (``[ {...}, /* note */ ]``), so the
    trailing-comma scan must run after comments are gone or it would miss that comma.
    """
    return _strip_trailing_commas(_strip_jsonc_comments(text))


def cmd_vscode_tasks(args: argparse.Namespace) -> int:
    project_path, project, _ = load_project_and_edition(args.project, None)
    manifest = args.manifest or project_path.name
    problem_matcher: object = args.problem_matcher if args.problem_matcher else []
    generated = generate_vscode_tasks(
        project=project,
        manifest=manifest,
        python=args.python,
        helper=args.helper,
        tool=args.tool,
        problem_matcher=problem_matcher,
    )

    output = Path(args.output) if args.output else project_path.parent / ".vscode" / "tasks.json"

    # Merge: preserve the user's own tasks, replace only our CC5X-labelled ones.
    preserved: list[dict[str, object]] = []
    if output.exists():
        existing: object = {}
        try:
            raw = output.read_text(encoding="utf-8")
        except OSError as exc:
            raise SystemExit(f"cannot read {output}: {exc}")
        try:
            # VS Code writes JSONC (comments, trailing commas); normalize before parsing.
            existing = json.loads(strip_jsonc(raw))
        except json.JSONDecodeError as exc:
            if not args.force:
                raise SystemExit(
                    f"{output} exists but is not valid JSON ({exc}); pass --force to overwrite "
                    f"or --stdout to print instead"
                )
            # Even with --force, never silently drop the user's tasks: back the file up
            # first so an unparseable tasks.json can be recovered (audit A3).
            # Pick a backup name that does not already exist so a previous good
            # backup is never overwritten.
            backup = output.with_name(output.name + ".bak")
            counter = 1
            while backup.exists():
                backup = output.with_name(f"{output.name}.bak.{counter}")
                counter += 1
            backup.write_text(raw, encoding="utf-8")
            print(f"warning: {output} was not valid JSON; backed it up to {backup} before overwriting")
            existing = {}
        for task in existing.get("tasks", []) if isinstance(existing, dict) else []:
            label = task.get("label", "") if isinstance(task, dict) else ""
            if not str(label).startswith(CC5X_TASK_LABEL_PREFIX):
                preserved.append(task)

    document = {"version": "2.0.0", "tasks": [*preserved, *generated]}
    rendered = json.dumps(document, indent=2) + "\n"

    if args.stdout:
        sys.stdout.write(rendered)
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    print(
        f"wrote {len(generated)} CC5X tasks to {output} "
        f"(preserved {len(preserved)} existing task(s))"
    )
    return 0


def cmd_project_init(args: argparse.Namespace) -> int:
    project_path = Path(args.project)
    if project_path.exists() and not args.force:
        raise SystemExit(f"project file already exists: {project_path}")
    # Honor the documented CC5X_COMPILER / CC5X_RUNNER environment overrides (README) so a
    # manifest is seeded with the user's toolchain, not just the hard-coded repo defaults.
    # Explicit --compiler/--runner flags still win.
    compiler = args.compiler or os.environ.get("CC5X_COMPILER") or str(DEFAULT_COMPILER)
    if args.runner is not None:
        runner = args.runner
    else:
        runner = os.environ.get("CC5X_RUNNER") or str(DEFAULT_RUNNER)
    project = default_project_manifest(
        device=args.device,
        compiler=compiler,
        runner=runner,
        main_source=args.main,
        config_source=args.config_source,
        header_mode=args.header_mode,
        header_path=args.header_path,
        mplab_root=args.mplab_root,
    )
    write_project_file(project, project_path)
    print(f"wrote project file {project_path}")
    return 0


def cmd_project_validate(args: argparse.Namespace) -> int:
    project_path = Path(args.project)
    project = load_project_file(project_path)
    schema_errors = validate_project_file(project)
    build_errors = [] if schema_errors else project_build_readiness_errors(project_path, project)
    errors = [*schema_errors, *build_errors]
    # Locatable, structured diagnostics for the editor (unknown config symbols, invalid
    # config values, missing provided header). Only meaningful once the manifest shape is
    # valid, so skip when schema_errors already exist. An error-severity diagnostic that is
    # not already in `errors` (a bad config symbol/value) still makes validation fail.
    diagnostics = [] if schema_errors else project_manifest_diagnostics(project_path, project)
    has_error_diag = any(item.get("severity") == "error" for item in diagnostics)
    if args.json:
        print(
            json.dumps(
                {
                    "project": str(project_path),
                    "schema_errors": schema_errors,
                    "build_errors": build_errors,
                    "errors": errors,
                    "diagnostics": diagnostics,
                },
                indent=2,
            )
        )
    else:
        if errors or has_error_diag:
            print(f"invalid: {project_path}")
            if schema_errors:
                print("schema:")
                for error in schema_errors:
                    print(f"- {error}")
            if build_errors:
                print("build readiness:")
                for error in build_errors:
                    print(f"- {error}")
            config_diags = [d for d in diagnostics if d.get("kind") != "missing_header"]
            if config_diags:
                print("config:")
                for diag in config_diags:
                    print(f"- {diag['message']}")
        else:
            print(f"valid: {project_path}")
            print(f"device: {project.device}")
            print(f"editions: {', '.join(sorted(project.editions))}")
            print(f"header: {project.header_mode} -> {project.header_path}")
    return 0 if not errors and not has_error_diag else 1


def _persist_mutated_project(
    project, project_path: Path, baseline_errors: list[str], label: str
) -> None:
    """Write a mutated manifest, rejecting only validation errors the edit introduced.

    Mutation commands must not persist state ``validate_project_file`` rejects — e.g.
    ``project-set-config --set FOO=`` storing an empty config value (audit #8). But a manifest
    can load yet already fail validation on an unrelated field (a future ``version``, a drifted
    device): blocking on *every* error would lock the user out of editing it. So compare against
    the errors the freshly-loaded manifest already had and refuse only the newly-introduced ones.
    """
    introduced = [e for e in validate_project_file(project) if e not in baseline_errors]
    if introduced:
        raise SystemExit(
            f"invalid {label} for {project_path}:\n- " + "\n- ".join(introduced)
        )
    write_project_file(project, project_path)


def cmd_project_edit_edition(args: argparse.Namespace) -> int:
    project_path = Path(args.project)
    project = load_project_file(project_path)
    baseline_errors = validate_project_file(project)
    existed = args.edition in project.editions
    try:
        if args.delete:
            project = delete_project_edition(project, args.edition)
            action = "deleted"
        else:
            project = set_project_edition(
                project,
                args.edition,
                from_edition=args.copy_from,
            )
            action = "updated" if existed else "created"
    except KeyError as exc:
        missing = exc.args[0]
        raise SystemExit(f"unknown edition {missing!r} in {project_path}") from None
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    _persist_mutated_project(project, project_path, baseline_errors, "edition update")
    print(f"{action} edition {args.edition!r} in {project_path}")
    return 0


def cmd_project_set_config(args: argparse.Namespace) -> int:
    project_path = Path(args.project)
    project = load_project_file(project_path)
    baseline_errors = validate_project_file(project)
    updates = parse_key_value_pairs(args.set or [])
    try:
        if args.remove:
            project = remove_project_edition_config(project, args.edition, args.remove)
        if updates or args.clear:
            project = update_project_edition_config(
                project,
                args.edition,
                updates,
                clear=args.clear,
            )
    except KeyError as exc:
        missing = exc.args[0]
        raise SystemExit(f"unknown edition {missing!r} in {project_path}") from None
    _persist_mutated_project(project, project_path, baseline_errors, "config update")
    print(f"updated config for edition {args.edition!r} in {project_path}")
    return 0


def cmd_project_set_build_options(args: argparse.Namespace) -> int:
    project_path = Path(args.project)
    project = load_project_file(project_path)
    baseline_errors = validate_project_file(project)
    try:
        project = update_project_edition_build_options(
            project,
            args.edition,
            args.option or [],
        )
    except KeyError as exc:
        missing = exc.args[0]
        raise SystemExit(f"unknown edition {missing!r} in {project_path}") from None
    _persist_mutated_project(project, project_path, baseline_errors, "build-options update")
    print(f"updated build options for edition {args.edition!r} in {project_path}")
    return 0


def cmd_project_edit(args: argparse.Namespace) -> int:
    project_path = Path(args.project)
    project = load_project_file(project_path)
    baseline_errors = validate_project_file(project)
    project = update_project_fields(
        project,
        device=args.device,
        compiler=args.compiler,
        runner=args.runner,
        mplab_root=args.mplab_root,
        header_mode=args.header_mode,
        header_path=args.header_path,
        config_source=args.config_source,
        main_source=args.main_source,
        clear_runner=args.clear_runner,
        clear_mplab_root=args.clear_mplab_root,
    )
    _persist_mutated_project(project, project_path, baseline_errors, "project update")
    print(f"updated project fields in {project_path}")
    return 0


def cmd_project_generate_header(args: argparse.Namespace) -> int:
    """Synthesize the device header for a generated-mode project and write it to header.path.

    Reuses ensure_project_header so the file written here is byte-identical to what `build`
    would generate. Only meaningful for header.mode == "generated"; supplied/existing modes
    use a provided header and have nothing to synthesize.
    """
    project_path = Path(args.project)
    project = load_project_file(project_path)
    if project.header_mode != "generated":
        return _program_error(
            "not_generated_mode",
            f"header.mode is {project.header_mode!r}; only 'generated' projects "
            "synthesize a header (supplied/existing modes use a provided file)",
            args.json,
            mode=project.header_mode,
        )
    try:
        header_path = ensure_project_header(project_path, project)
    except (SystemExit, Exception) as exc:
        # This is the extension-facing JSON boundary: turn any header-generation failure
        # into the structured {ok:false} contract instead of a traceback/SystemExit. The
        # device metadata path can raise OSError, ValueError (capped reads), xml ParseError,
        # or zipfile.BadZipFile for a missing/malformed/corrupt pack — all reported uniformly.
        return _program_error("generate_failed", str(exc), args.json)
    payload = {
        "ok": True,
        "mode": project.header_mode,
        "device": project.device,
        "header": str(header_path),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"generated header for {project.device}: {header_path}")
    return 0


def cmd_intellisense(args: argparse.Namespace) -> int:
    """Generate the editor-only IntelliSense shim + compile database for a project.

    Writes ``generated/vscode/cc5x_intellisense.h`` and ``compile_commands.json`` next
    to the manifest so a C/C++ editor understands the CC5X dialect. Works for any header
    mode (the device header directory is added to the include path). Resolves device
    metadata for the auto-defined macros (``__CC5X__``, ``__CoreSet__``, ``_<short>``).
    """
    project_path = Path(args.project)
    project = load_project_file(project_path)
    try:
        _, metadata = project_metadata(project)
        # Resolve (and, for generated mode, write) the device header the same way `build`
        # does, so the include path is correct for supplied headers beside the compiler and
        # the header actually exists for the editor to include (existing-mode missing header
        # raises here -> {ok:false}, instead of reporting a misleading success).
        header_path = ensure_project_header(project_path, project)
        # Pass the manifest's directory absolute but un-resolved (no symlink follow) so the
        # generated compile_commands paths match the path the editor opens; build_intellisense
        # normalizes the rest.
        result = build_intellisense(
            Path(os.path.abspath(args.project)).parent,
            project,
            metadata,
            header_path,
            cc5x_version=args.cc5x_version,
        )
    except (SystemExit, Exception) as exc:
        # Extension-facing JSON boundary: a missing/malformed pack (or unresolvable
        # device — OSError, ValueError, xml ParseError, zipfile.BadZipFile) becomes the
        # structured {ok:false} contract, not a traceback. Mirrors cmd_project_generate_header.
        return _program_error("intellisense_failed", str(exc), args.json)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"IntelliSense for {result['device']}:")
        print(f"  shim:             {result['shim']}")
        print(f"  compile_commands: {result['compile_commands']}")
    return 0


def cmd_project_list_editions(args: argparse.Namespace) -> int:
    project_path = Path(args.project)
    project = load_project_file(project_path)
    if args.json:
        payload = [
            {
                "name": name,
                "config_count": len(edition.config),
                "build_option_count": len(edition.build_options),
            }
            for name, edition in sorted(project.editions.items())
        ]
        print(json.dumps(payload, indent=2))
        return 0
    for name, edition in sorted(project.editions.items()):
        print(
            f"{name}: config={len(edition.config)} build_options={len(edition.build_options)}"
        )
    return 0


def cmd_project_show(args: argparse.Namespace) -> int:
    project_path = Path(args.project)
    project = load_project_file(project_path)
    if args.json:
        if args.edition:
            edition = project.editions.get(args.edition)
            if edition is None:
                raise SystemExit(f"unknown edition {args.edition!r} in {project_path}")
            print(
                json.dumps(
                    {
                        "name": args.edition,
                        "config": dict(edition.config),
                        "build_options": list(edition.build_options),
                    },
                    indent=2,
                )
            )
            return 0
        print(json.dumps(project_summary(project), indent=2))
        return 0
    if args.edition:
        edition = project.editions.get(args.edition)
        if edition is None:
            raise SystemExit(f"unknown edition {args.edition!r} in {project_path}")
        print(f"edition: {args.edition}")
        print("config:")
        if edition.config:
            for name, value in sorted(edition.config.items()):
                print(f"  {name}={value}")
        else:
            print("  (none)")
        print("build options:")
        if edition.build_options:
            for option in edition.build_options:
                print(f"  {option}")
        else:
            print("  (none)")
        return 0
    summary = project_summary(project)
    print(f"project: {project_path}")
    print(f"device: {summary['device']}")
    print(f"header: {project.header_mode} -> {project.header_path}")
    print(f"config source: {project.config_source}")
    print(f"main source: {project.main_source}")
    print(f"editions: {', '.join(sorted(project.editions))}")
    return 0


def _pack_version_key(entry: dict[str, str | None]) -> tuple[int, ...]:
    return parse_version(entry.get("pack_version"))


def merged_device_list(prefixes: tuple[str, ...]) -> list[dict[str, str | None]]:
    """Devices from downloaded .atpack archives and installed (unpacked) MPLAB X packs,
    merged by device name keeping the highest pack version. Shared by the CLI and GUI so
    both report the same system-wide set (audit A1)."""
    by_name: dict[str, dict[str, str | None]] = {}
    for item in (
        *list_devices_in_atpacks(prefixes=prefixes),
        *list_devices_in_unpacked_packs(prefixes=prefixes),
    ):
        name = str(item["device"])
        existing = by_name.get(name)
        if existing is None or _pack_version_key(item) > _pack_version_key(existing):
            by_name[name] = item
    return [by_name[name] for name in sorted(by_name)]


def cmd_list_devices(args: argparse.Namespace) -> int:
    prefixes = tuple(
        f"PIC{family.upper()}" if not family.upper().startswith("PIC") else family.upper()
        for family in (args.family or ["10F", "12F", "16F"])
    )
    devices = merged_device_list(prefixes)
    if args.json:
        print(json.dumps(devices, indent=2))
        return 0
    for item in devices:
        print(
            f"{item['device']}: {item['pack_family']} {item['pack_version']} "
            f"({Path(item['pack_root']).name})"
        )
    return 0 if devices else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    report = environment_report()
    if args.json:
        print(json.dumps(report, indent=2))
        return 0 if report["ready"] else 1
    print(f"ready: {'yes' if report['ready'] else 'no'}")
    print(f"pack archive dirs: {', '.join(report['pack_archive_dirs']) or '(none)'}")
    print(f"pack roots: {', '.join(report['pack_roots']) or '(none)'}")
    print(
        f"pack devices: {report['pack_device_count']} "
        f"(archive {report['pack_archive_count']}, installed {report['pack_unpacked_count']})"
    )
    print(f"validated devices: {report['validated_device_count']}")
    if report["validated_devices"]:
        print(f"validated set: {', '.join(report['validated_devices'])}")
    print(f"runner: {report['runner'] or '(missing)'}")
    print(f"compiler: {report['compiler'] or '(missing)'}")
    print(
        f"crossover bottle: {report['crossover_bottle']} "
        f"({'present' if report['crossover_bottle_exists'] else 'missing'})"
    )
    for path, exists in report["crossover_binaries"].items():
        print(f"tool: {path} ({'present' if exists else 'missing'})")
    return 0 if report["ready"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Native Linux helper for CC5X workflows that SETCC.EXE currently covers."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Shared by every project-manifest command: where to resolve/discover --project from.
    workspace_parent = argparse.ArgumentParser(add_help=False)
    workspace_parent.add_argument(
        "--workspace-root",
        help=(
            "Directory used to resolve/discover the --project manifest (default: current "
            "directory). A bare --project name (the default setcc-native.json) is found by "
            "walking up from here, so a command works from any project subdirectory."
        ),
    )

    probe = subparsers.add_parser("probe", help="Locate MPLAB X .ini/.cfgdata files for a device.")
    probe.add_argument("--device", required=True)
    probe.add_argument("--mplab-root")
    probe.set_defaults(func=cmd_probe)

    describe = subparsers.add_parser(
        "describe-device",
        help="Load and summarize device metadata from packs or legacy MPLAB sources.",
    )
    describe.add_argument("--device", required=True)
    describe.add_argument("--mplab-root")
    # describe-device always emits JSON (metadata.to_json) but has no --json flag; set the
    # attribute so the error boundary engages and a bad device yields {ok:false} JSON too.
    describe.set_defaults(func=json_error_boundary(cmd_describe_device), json=True)

    list_devices = subparsers.add_parser(
        "list-devices",
        help="List locally discoverable PIC10F/PIC12F/PIC16F devices from installed packs.",
    )
    list_devices.add_argument(
        "--family",
        action="append",
        choices=["10F", "12F", "16F", "PIC10F", "PIC12F", "PIC16F"],
        help="Restrict the output to one or more CC5X-supported families.",
    )
    list_devices.add_argument("--json", action="store_true")
    list_devices.set_defaults(func=json_error_boundary(cmd_list_devices))

    doctor = subparsers.add_parser(
        "doctor",
        help="Report whether the local packs and CrossOver-backed CC5X toolchain are usable.",
    )
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=json_error_boundary(cmd_doctor))

    project_init = subparsers.add_parser(
        "project-init",
        parents=[workspace_parent],
        help="Create a checked-in setcc-native.json manifest instead of relying on setcc.pxk state.",
    )
    project_init.add_argument("--project", default="setcc-native.json")
    project_init.add_argument("--device", required=True)
    project_init.add_argument("--main", required=True)
    project_init.add_argument("--config-source")
    project_init.add_argument("--compiler")
    project_init.add_argument("--runner")
    project_init.add_argument("--mplab-root")
    project_init.add_argument(
        "--header-mode",
        choices=["generated", "supplied", "existing"],
        default="generated",
    )
    project_init.add_argument("--header-path")
    project_init.add_argument("--force", action="store_true")
    project_init.set_defaults(func=cmd_project_init)

    project_validate = subparsers.add_parser(
        "project-validate",
        parents=[workspace_parent],
        help="Validate a setcc-native.json manifest.",
    )
    project_validate.add_argument("--project", default="setcc-native.json")
    project_validate.add_argument("--json", action="store_true")
    project_validate.set_defaults(func=json_error_boundary(cmd_project_validate))

    project_edit = subparsers.add_parser(
        "project-edit",
        parents=[workspace_parent],
        help="Update top-level fields in a setcc-native.json manifest.",
    )
    project_edit.add_argument("--project", default="setcc-native.json")
    project_edit.add_argument("--device")
    project_edit.add_argument("--compiler")
    project_edit.add_argument("--runner")
    project_edit.add_argument("--clear-runner", action="store_true")
    project_edit.add_argument("--mplab-root")
    project_edit.add_argument("--clear-mplab-root", action="store_true")
    project_edit.add_argument("--header-mode", choices=["generated", "supplied", "existing"])
    project_edit.add_argument("--header-path")
    project_edit.add_argument("--config-source")
    project_edit.add_argument("--main-source")
    project_edit.set_defaults(func=cmd_project_edit)

    project_edit_edition = subparsers.add_parser(
        "project-edit-edition",
        parents=[workspace_parent],
        help="Create, copy, or delete editions in a setcc-native.json manifest.",
    )
    project_edit_edition.add_argument("--project", default="setcc-native.json")
    project_edit_edition.add_argument("--edition", required=True)
    project_edit_edition.add_argument("--copy-from")
    project_edit_edition.add_argument("--delete", action="store_true")
    project_edit_edition.set_defaults(func=cmd_project_edit_edition)

    project_set_config = subparsers.add_parser(
        "project-set-config",
        parents=[workspace_parent],
        help="Set or remove config symbol values for a named project edition.",
    )
    project_set_config.add_argument("--project", default="setcc-native.json")
    project_set_config.add_argument("--edition", required=True)
    project_set_config.add_argument("--set", action="append")
    project_set_config.add_argument("--remove", action="append")
    project_set_config.add_argument("--clear", action="store_true")
    project_set_config.set_defaults(func=cmd_project_set_config)

    project_set_build_options = subparsers.add_parser(
        "project-set-build-options",
        parents=[workspace_parent],
        help="Replace the build option list for a named project edition.",
    )
    project_set_build_options.add_argument("--project", default="setcc-native.json")
    project_set_build_options.add_argument("--edition", required=True)
    project_set_build_options.add_argument("--option", action="append")
    project_set_build_options.set_defaults(func=cmd_project_set_build_options)

    project_generate_header = subparsers.add_parser(
        "project-generate-header",
        parents=[workspace_parent],
        help="Synthesize and write the device header for a generated-mode project.",
    )
    project_generate_header.add_argument("--project", default="setcc-native.json")
    project_generate_header.add_argument("--json", action="store_true")
    # json_error_boundary: a missing/malformed manifest or bad pack metadata is loaded before
    # the command's own try/except, so wrap it to keep the --json contract (audit JSON-contract).
    project_generate_header.set_defaults(func=json_error_boundary(cmd_project_generate_header))

    intellisense = subparsers.add_parser(
        "intellisense",
        parents=[workspace_parent],
        help="Generate the editor-only IntelliSense shim + compile_commands.json for a project.",
    )
    intellisense.add_argument("--project", default="setcc-native.json")
    intellisense.add_argument(
        "--cc5x-version",
        type=int,
        default=DEFAULT_CC5X_VERSION,
        help=f"Integer __CC5X__ value to define for the editor (default {DEFAULT_CC5X_VERSION} == v3.8C).",
    )
    intellisense.add_argument("--json", action="store_true")
    intellisense.set_defaults(func=json_error_boundary(cmd_intellisense))

    project_list_editions = subparsers.add_parser(
        "project-list-editions",
        parents=[workspace_parent],
        help="List the editions stored in a setcc-native.json manifest.",
    )
    project_list_editions.add_argument("--project", default="setcc-native.json")
    project_list_editions.add_argument("--json", action="store_true")
    project_list_editions.set_defaults(func=json_error_boundary(cmd_project_list_editions))

    project_show = subparsers.add_parser(
        "project-show",
        parents=[workspace_parent],
        help="Show the stored project manifest or one edition from it.",
    )
    project_show.add_argument("--project", default="setcc-native.json")
    project_show.add_argument("--edition")
    project_show.add_argument("--json", action="store_true")
    project_show.set_defaults(func=json_error_boundary(cmd_project_show))

    render_pack = subparsers.add_parser(
        "render-pack-config-section",
        help="Render a CC5X dynamic config section from pack or legacy device metadata.",
    )
    render_pack.add_argument("--device", required=True)
    render_pack.add_argument("--mplab-root")
    render_pack.set_defaults(func=cmd_render_pack_config_section)

    render_header = subparsers.add_parser(
        "render-pack-header",
        help="Render a CC5X-style header skeleton from pack or legacy device metadata.",
    )
    render_header.add_argument("--device", required=True)
    render_header.add_argument("--mplab-root")
    render_header.set_defaults(func=cmd_render_pack_header)

    list_pack_config = subparsers.add_parser(
        "list-pack-config",
        help="List dynamic config symbols directly from pack or legacy metadata.",
    )
    list_pack_config.add_argument("--device", required=True)
    list_pack_config.add_argument("--mplab-root")
    list_pack_config.add_argument("--json", action="store_true")
    list_pack_config.set_defaults(func=json_error_boundary(cmd_list_pack_config))

    render_pack_config = subparsers.add_parser(
        "render-pack-config",
        help="Render a managed config block directly from pack or legacy metadata.",
    )
    render_pack_config.add_argument("--device", required=True)
    render_pack_config.add_argument("--mplab-root")
    render_pack_config.add_argument("--set", action="append")
    render_pack_config.add_argument("--include-defaults", action="store_true")
    render_pack_config.set_defaults(func=cmd_render_pack_config)

    list_config = subparsers.add_parser(
        "list-config", help="List dynamic config symbols from a CC5X header."
    )
    list_config.add_argument("--header", required=True)
    list_config.add_argument("--json", action="store_true")
    list_config.set_defaults(func=cmd_list_config)

    render = subparsers.add_parser(
        "render-config",
        help="Render a managed config block from a CC5X header and NAME=STATE pairs.",
    )
    render.add_argument("--header", required=True)
    render.add_argument("--family", choices=sorted(START_MARKERS))
    render.add_argument("--set", action="append")
    render.add_argument("--include-defaults", action="store_true")
    render.set_defaults(func=cmd_render_config)

    sync = subparsers.add_parser(
        "sync-config",
        parents=[workspace_parent],
        help="Update or append a managed config block in a C source file.",
    )
    sync.add_argument("--project")
    sync.add_argument("--edition")
    sync.add_argument("--source")
    sync.add_argument("--header")
    sync.add_argument("--family", choices=sorted(START_MARKERS))
    sync.add_argument("--set", action="append")
    sync.add_argument("--include-defaults", action="store_true")
    sync.set_defaults(func=cmd_sync_config)

    build = subparsers.add_parser(
        "build",
        parents=[workspace_parent],
        help="Run CC5X directly from Linux instead of going through the SETCC GUI.",
    )
    build.add_argument("--project")
    build.add_argument("--edition")
    build.add_argument("--compiler")
    build.add_argument("--main")
    build.add_argument("--option", action="append")
    build.add_argument("--runner", help='Optional launcher, for example: "wine"')
    build.add_argument("--cwd")
    build.add_argument("--dry-run", action="store_true")
    build.add_argument(
        "--timeout-seconds",
        type=float,
        default=0,
        help="Abort the compiler process after this many seconds (0 disables; default).",
    )
    build.add_argument(
        "--json-diagnostics",
        action="store_true",
        help="Emit a JSON payload with CC5X output normalized to structured diagnostics.",
    )
    build.set_defaults(func=cmd_build)

    artifacts = subparsers.add_parser(
        "artifacts",
        parents=[workspace_parent],
        help="List CC5X build artifacts (.hex/.asm/.occ/.var/.fcs/.cpr/.cod/.cof) for a project.",
    )
    artifacts.add_argument("--project", help="Manifest; artifacts are searched under its main source dir.")
    artifacts.add_argument("--dir", help="Explicit directory to search instead of a project.")
    artifacts.add_argument("--json", action="store_true")
    artifacts.set_defaults(func=cmd_artifacts)

    program = subparsers.add_parser(
        "program",
        parents=[workspace_parent],
        help="Program/erase/verify/blank-check a device via MPLAB IPECMD (PICkit 4/5, etc.).",
    )
    program.add_argument(
        "--action",
        choices=sorted(IPECMD_ACTION_FLAG),
        default="program",
        help="IPECMD operation (default: program).",
    )
    program.add_argument("--project", help="setcc-native.json manifest to read device/image from.")
    program.add_argument("--edition")
    program.add_argument("--device", help="Override device, e.g. PIC16F1509.")
    program.add_argument("--hex", help="Hex image for program/verify (default: derived from project).")
    program.add_argument(
        "--tool",
        default=DEFAULT_PROGRAMMER_TOOL,
        help="IPECMD -TP tool code: PK4=PICkit 4, PK5=PICkit 5, SNAP, ICD4 (default: PK4).",
    )
    program.add_argument("--ipecmd", help="Path to ipecmd.sh/.exe (default: auto-discover, $CC5X_IPECMD).")
    program.add_argument(
        "--release-from-reset",
        action="store_true",
        help="Add -OL so the target runs after the operation.",
    )
    program.add_argument(
        "--ipe-arg",
        action="append",
        help="Extra raw IPECMD argument (repeatable), e.g. --ipe-arg=-W2.5 to power the target.",
    )
    program.add_argument("--dry-run", action="store_true", help="Print the command without running it.")
    program.add_argument(
        "--yes",
        action="store_true",
        help="Authorize device-modifying actions (program/erase) without an interactive prompt. "
        "The GUI/extension pass this after their own confirmation; omit it for terminal tasks.",
    )
    program.add_argument(
        "--timeout-seconds",
        type=float,
        default=0,
        help="Abort IPECMD after this many seconds (0 disables; default).",
    )
    program.add_argument("--json", action="store_true")
    # json_error_boundary catches a pre-handler manifest load error (missing/bad --project)
    # before cmd_program's internal _program_error handling can run, keeping --json parseable.
    program.set_defaults(func=json_error_boundary(cmd_program))

    vscode_tasks = subparsers.add_parser(
        "vscode-tasks",
        parents=[workspace_parent],
        help="Generate/merge .vscode/tasks.json build + program tasks from a project manifest.",
    )
    vscode_tasks.add_argument("--project", required=True, help="setcc-native.json manifest.")
    vscode_tasks.add_argument(
        "--manifest",
        help="Manifest path embedded in the tasks (default: manifest file name).",
    )
    vscode_tasks.add_argument("--python", default="python3", help="Python interpreter (default: python3).")
    vscode_tasks.add_argument(
        "--helper",
        default="tools/cc5x_setcc_native.py",
        help="Workspace-relative path to this helper (default: tools/cc5x_setcc_native.py).",
    )
    vscode_tasks.add_argument(
        "--tool",
        default=DEFAULT_PROGRAMMER_TOOL,
        help="IPECMD -TP tool code for program/erase tasks (default: PK4).",
    )
    vscode_tasks.add_argument(
        "--problem-matcher",
        help="Problem matcher name for build tasks (default: none). E.g. $cc5x if the extension defines it.",
    )
    vscode_tasks.add_argument("--output", help="Output path (default: <manifest dir>/.vscode/tasks.json).")
    vscode_tasks.add_argument("--stdout", action="store_true", help="Print instead of writing the file.")
    vscode_tasks.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing tasks.json even if it is not valid JSON.",
    )
    vscode_tasks.set_defaults(func=cmd_vscode_tasks)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    # Resolve --project (discover setcc-native.json by walking up from --workspace-root /
    # cwd) once, centrally, so every project command shares one resolution policy. Skip the
    # upward walk for project-init, which creates a manifest at the anchored location rather
    # than binding onto an existing ancestor's. Only a *truthy* --project is resolved: an
    # empty string keeps the falsy "standalone, no manifest" mode the optional-project
    # commands (build/sync-config/artifacts/program) rely on. expanduser() can raise
    # RuntimeError (e.g. an unknown ``~user``), so convert it to a clean error here — the
    # try/except below wraps only args.func.
    try:
        if getattr(args, "project", None):
            args.project = str(
                resolve_project_manifest(
                    args.project,
                    getattr(args, "workspace_root", None),
                    discover=(getattr(args, "command", None) != "project-init"),
                )
            )
        elif getattr(args, "workspace_root", None) and hasattr(args, "project"):
            # Optional-project commands (build/sync-config/artifacts/program) have no
            # --project default, so an explicit --workspace-root would otherwise be a no-op.
            # Treat it as a request to discover a manifest there; fall back to standalone
            # mode (project stays None) when none is found, so non-project use still works.
            discovered = resolve_project_manifest(
                DEFAULT_PROJECT_MANIFEST, args.workspace_root
            )
            if discovered.is_file():
                args.project = str(discovered)
    except (ValueError, OSError, RuntimeError) as exc:
        raise SystemExit(f"error: {exc}")
    try:
        return args.func(args)
    except (ValueError, OSError) as exc:
        # Turn expected bad-input / file-IO failures (malformed manifest, missing or
        # unreadable source/header files) into a clean message instead of a traceback.
        raise SystemExit(f"error: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
