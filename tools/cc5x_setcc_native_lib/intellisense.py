"""Editor-only IntelliSense support for CC5X projects.

CC5X extends standard C with keywords and intrinsics that a normal C parser
(Microsoft C/C++, or Microchip's clangd) does not recognize, so without help the
editor floods CC5X sources with false "unknown type / undeclared identifier"
errors. This module synthesizes two artifacts that quiet that noise **for the
editor only** — they are never seen by CC5X itself:

* ``cc5x_intellisense.h`` — a shim that ``typedef``/``#define``s the CC5X dialect
  (the extension keywords and inline intrinsics from the CC5X 3.8 manual,
  "Extensions to the standard C keywords" / "internal functions").
* ``compile_commands.json`` — an editor-only compile database that force-includes
  the shim and defines the macros CC5X auto-defines (``__CC5X__``, ``__CoreSet__``,
  the per-device ``_<short>`` macro, ...), so the user's ``#if __CC5X__`` branches
  and the generated device header's dynamic-config block resolve in the editor.

The shim body is guarded by ``__CC5X_INTELLISENSE__`` (which only the generated
compile database defines) so that if it is ever pulled into a real CC5X compile it
is an inert no-op rather than a redefinition of CC5X's own built-in keywords.

Facts confirmed against ``cc5x-38.pdf`` (CC5X 3.8 manual):
  - default ``int`` is 8-bit, ``long`` is 16-bit, ``char`` is unsigned by default;
  - keyword extensions: ``bank0..bank63``, ``bit``, ``DataInW``,
    ``fixed8_8..fixed24_8``, ``float16/24/32``, ``int8/16/24/32``, ``interrupt``,
    ``page0..page15``, ``shrBank``, ``size1``, ``size2``, ``uns8/16/24/32``;
  - inline intrinsics translated to instructions: ``btsc btss clearRAM clrwdt
    decsz incsz nop nop2 retint rl rr sleep skip swap addWFC subWFB lsl lsr asr
    softReset``;
  - auto-defined macros include ``__CC5X__`` (3803 == v3.8C), ``__CoreSet__``
    (1200/1400/1410), ``_16CXX``, ``_16C5X``, ``__EnhancedCore14__`` and the
    per-device ``_<short>`` macro (e.g. ``_16F1509``).

Known residual noise (documented, not fixable by a preprocessor shim): CC5X's
absolute-address declaration syntax ``char PORTA @ 0xC;`` used throughout the
generated device headers has no standard-C equivalent, so the editor still flags
those lines. The CC5X build remains authoritative for correctness.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# CC5X version that auto-defines __CC5X__. 3.8C -> 3803 (main 38; minor C == 03).
# The installed toolchain in this repo is CC5X 3.8C; override via --cc5x-version.
DEFAULT_CC5X_VERSION = 3803

# Generated under the project directory; the shim is reusable, the compile
# database is per-project (it carries the device-specific defines).
INTELLISENSE_SUBDIR = "generated/vscode"
SHIM_NAME = "cc5x_intellisense.h"
COMPILE_COMMANDS_NAME = "compile_commands.json"

# Marker the generated compile database defines so the shim body activates for the
# editor but stays inert under a real CC5X compile (CC5X never sets this).
INTELLISENSE_MARKER = "__CC5X_INTELLISENSE__"

# Inline intrinsics CC5X translates to single instructions / short sequences. The
# editor only needs a declaration so calls do not flag as undeclared; an old-style
# (no-prototype) ``int`` declaration accepts any argument list and any use context.
_INTRINSICS = (
    "btsc", "btss", "clearRAM", "clrwdt", "decsz", "incsz", "nop", "nop2",
    "retint", "rl", "rr", "sleep", "skip", "swap", "addWFC", "subWFB",
    "lsl", "lsr", "asr", "softReset",
)


def _qualifier_defines() -> str:
    """`#define`-away the CC5X placement/storage qualifier keywords."""
    banks = "\n".join(f"#define bank{i}" for i in range(64))
    pages = "\n".join(f"#define page{i}" for i in range(16))
    others = "\n".join(f"#define {name}" for name in ("shrBank", "size1", "size2", "DataInW", "interrupt"))
    return f"{banks}\n{pages}\n{others}"


def render_shim() -> str:
    """The device-independent CC5X dialect shim header text."""
    intrinsics = "\n".join(f"int {name}();" for name in _INTRINSICS)
    return f"""\
/* CC5X dialect IntelliSense shim -- GENERATED, editor-only. DO NOT EDIT.
 *
 * Regenerate with: cc5x_setcc_native.py intellisense --project <manifest>
 * This file is force-included by the generated {COMPILE_COMMANDS_NAME} so that an
 * editor C parser (Microsoft C/C++ or Microchip clangd) understands CC5X's dialect
 * extensions. CC5X itself never includes this file; the {INTELLISENSE_MARKER} guard
 * keeps it an inert no-op if it is ever pulled into a real compile.
 */
#ifndef CC5X_INTELLISENSE_H
#define CC5X_INTELLISENSE_H
#ifdef {INTELLISENSE_MARKER}

/* CC5X integer extension types. Per the CC5X 3.8 manual the default int is 8-bit
 * and long is 16-bit; the 24-bit widths are widened to 32 for the editor only. Mapped to
 * char/short/int (NOT long): on an LP64 host `long` is 8 bytes, so `long`-based 32-bit
 * types would make sizeof checks (e.g. static_assert(sizeof(uns32)==4)) fail in-editor. */
typedef unsigned char  uns8;
typedef unsigned short uns16;
typedef unsigned int   uns24;
typedef unsigned int   uns32;
typedef signed char    int8;
typedef signed short   int16;
typedef signed int     int24;
typedef signed int     int32;

/* CC5X fixed-point and reduced-width float extension types -- approximated as
 * float/double for the editor (CC5X stores them in fewer bytes). */
typedef float  fixed8_8;
typedef float  fixed16_8;
typedef float  fixed24_8;
typedef float  float16;
typedef float  float24;
typedef float  float32;

/* CC5X 1-bit type. sizeof(bit) is 0 in CC5X; the nearest editor type is a byte. */
typedef unsigned char bit;

/* Placement / storage qualifier keywords -- no editor semantics, define away.
 * (bank0..bank63, page0..page15, shrBank, size1, size2, DataInW, interrupt) */
{_qualifier_defines()}

/* Inline-instruction intrinsics -- declared so calls are not flagged undefined. */
{intrinsics}

/* NOTE: CC5X's absolute-address declaration syntax `char PORTA @ 0xC;` (used by the
 * generated device headers) has no standard-C form and is NOT fixable here; the
 * editor still flags those lines. The CC5X build stays authoritative. */

#endif /* {INTELLISENSE_MARKER} */
#endif /* CC5X_INTELLISENSE_H */
"""


def intellisense_defines(metadata, device: str, cc5x_version: int = DEFAULT_CC5X_VERSION) -> dict[str, str]:
    """Macros the editor must define to mirror what CC5X auto-defines for ``device``.

    ``metadata`` is the resolved :class:`DeviceMetadata` (for ``ini_arch``); ``device``
    is the manifest device name (``PIC16F1509``). Returns an ordered name->value map.
    """
    from .packs import device_short_name

    defines: dict[str, str] = {
        INTELLISENSE_MARKER: "1",
        "__CC5X__": str(cc5x_version),
    }

    # Per-device macro CC5X auto-defines, e.g. PIC16F1509 -> _16F1509.
    short = device_short_name(device)
    if short:
        # Per-device macro CC5X auto-defines, e.g. PIC16F1509 -> _16F1509.
        defines[f"_{short}"] = "1"

    arch = (getattr(metadata, "ini_arch", None) or "").upper()
    if arch in {"PIC12", "PIC12IE", "PIC14", "PIC14E", "PIC14EX"}:
        defines["_16CXX"] = "1"  # always defined for the 12- and 14-bit cores
    if arch in {"PIC12", "PIC12IE"}:
        defines["__CoreSet__"] = "1200"
        defines["_16C5X"] = "1"
    elif arch == "PIC14":
        defines["__CoreSet__"] = "1400"
    elif arch in {"PIC14E", "PIC14EX"}:
        defines["__CoreSet__"] = "1410"
        defines["__EnhancedCore14__"] = "1"
    return defines


def _compile_arguments(shim_path: Path, header_dir: Path, defines: dict[str, str], source: Path) -> list[str]:
    # -ffreestanding so the editor accepts the embedded-idiomatic `void main(void)`
    # (hosted C requires `int main`); -ferror-limit=0 keeps later diagnostics flowing.
    args = ["clang", "-x", "c", "-std=c99", "-ffreestanding", "-ferror-limit=0"]
    for name, value in defines.items():
        args.append(f"-D{name}={value}")
    args.extend(["-include", str(shim_path)])
    if header_dir:
        args.extend(["-I", str(header_dir)])
    args.append(str(source))
    return args


def build_intellisense(
    project_dir: Path,
    project,
    metadata,
    header_path: Path,
    cc5x_version: int = DEFAULT_CC5X_VERSION,
) -> dict[str, object]:
    """Generate the shim + compile database for ``project`` and return a JSON summary.

    Writes ``generated/vscode/cc5x_intellisense.h`` and ``compile_commands.json`` under
    ``project_dir``. ``header_path`` is the *resolved* device header (from
    ``ensure_project_header``), so the include path is correct for supplied-mode headers
    that live beside the compiler — not just the manifest-relative ``header.path``. Compile-
    database entries cover the project's main source and config source. Returns the structured
    payload the CLI/extension consume; raises on I/O errors (the caller maps those to the
    ``{ok:false}`` JSON contract).
    """
    # Emit absolute paths WITHOUT resolving symlinks: os.path.abspath normalizes ``..``/``.``
    # but, unlike Path.resolve(), preserves any symlinked workspace path so the
    # compile_commands ``file`` entries match the path the editor actually opens (a resolved
    # path would silently fail to match for a symlinked workspace, leaving IntelliSense noisy).
    def _abs(*parts: str) -> Path:
        return Path(os.path.abspath(os.path.join(*parts)))

    project_root = _abs(str(project_dir))
    out_dir = _abs(str(project_dir), INTELLISENSE_SUBDIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    shim_path = out_dir / SHIM_NAME
    shim_path.write_text(render_shim(), encoding="utf-8")

    defines = intellisense_defines(metadata, project.device, cc5x_version)

    # The resolved device header's directory on the include path so #include "<dev>.H"
    # navigates — correct even for a supplied header that lives beside the compiler.
    header_dir = _abs(str(header_path)).parent

    # One entry per distinct source file; dedupe while preserving order.
    sources: list[Path] = []
    seen: set[Path] = set()
    for rel in (project.main_source, project.config_source):
        if not rel:
            continue
        resolved = _abs(str(project_dir), rel)
        if resolved not in seen:
            seen.add(resolved)
            sources.append(resolved)

    entries = [
        {
            "directory": str(project_root),
            "file": str(source),
            "arguments": _compile_arguments(shim_path, header_dir, defines, source),
        }
        for source in sources
    ]

    compile_commands_path = out_dir / COMPILE_COMMANDS_NAME
    compile_commands_path.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")

    return {
        "ok": True,
        "device": project.device,
        "shim": str(shim_path),
        "compile_commands": str(compile_commands_path),
        "defines": defines,
        "sources": [str(source) for source in sources],
    }
