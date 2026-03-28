#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from cc5x_setcc_native_lib.headergen import (
    render_dynamic_config_section,
    render_full_header,
)
from cc5x_setcc_native_lib.packs import (
    discover_atpack_dirs,
    find_device_in_atpacks,
    find_device_in_unpacked_packs,
    list_devices_in_atpacks,
    normalize_device_name,
)
from cc5x_setcc_native_lib.picmeta import load_device_metadata
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
PROJECT_ROOT = Path(__file__).resolve().parent.parent
VALIDATION_ROOT = PROJECT_ROOT / "validation" / "generated"
DEFAULT_RUNNER = Path("/home/gary/apps/cc5x-run.sh")
DEFAULT_COMPILER = PROJECT_ROOT / "cc5x_paid" / "CC5X" / "CC5X.EXE"
DEFAULT_CROSSOVER_BINARIES = [
    Path("/opt/cxoffice/bin/cxrun"),
    Path("/opt/cxoffice/bin/wine"),
]
DEFAULT_CROSSOVER_BOTTLE = Path.home() / ".cxoffice" / "CC5X"


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
                resolved_word = (word.default & ~setting.mask) | value.value
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
        line = f"#pragma config {name} = {state}"
        if option.comment:
            line += f" // {option.comment}"
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
        f" * Managed by cc5x_setcc_native.py from {header_path.name}",
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
        f" * Managed by cc5x_setcc_native.py from {source_label}",
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
    pattern = re.compile(
        rf"(?ms)^.*?//\s*{re.escape(start)}\n.*?^//\s*{re.escape(end)}\s*$"
    )
    if pattern.search(source_text):
        return pattern.sub(block.rstrip("\n"), source_text, count=1), True
    if source_text and not source_text.endswith("\n"):
        source_text += "\n"
    if source_text and not source_text.endswith("\n\n"):
        source_text += "\n"
    return source_text + block, False


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
    active_pack = atpack_result if any(atpack_result.get(key) for key in ("pic", "cfgdata", "ini")) else pack_result
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


def build_command(
    compiler: str,
    main_file: str,
    options: list[str],
    runner: list[str],
) -> list[str]:
    command = list(runner)
    if not runner or Path(runner[0]).name != "cc5x-run.sh":
        command.append(compiler)
    command.extend(options)
    command.append(main_file)
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
    pack_entries = list_devices_in_atpacks()
    crossover_bins = {str(path): path.exists() for path in DEFAULT_CROSSOVER_BINARIES}
    runner_candidates = [DEFAULT_RUNNER]
    env_runner = os.environ.get("CC5X_RUNNER")
    if env_runner:
        runner_candidates.insert(0, Path(env_runner).expanduser())
    compiler_candidates = [DEFAULT_COMPILER]
    env_compiler = os.environ.get("CC5X_COMPILER")
    if env_compiler:
        compiler_candidates.insert(0, Path(env_compiler).expanduser())
    selected_runner = next((path for path in runner_candidates if path.exists()), None)
    selected_compiler = next((path for path in compiler_candidates if path.exists()), None)
    return {
        "pack_archive_dirs": [str(path) for path in discover_atpack_dirs()],
        "pack_archive_count": len(pack_entries),
        "pack_device_count": len(pack_entries),
        "validated_device_count": len(validated_device_names()),
        "validated_devices": validated_device_names(),
        "runner": str(selected_runner) if selected_runner else None,
        "runner_exists": bool(selected_runner),
        "compiler": str(selected_compiler) if selected_compiler else None,
        "compiler_exists": bool(selected_compiler),
        "crossover_binaries": crossover_bins,
        "crossover_bottle": str(DEFAULT_CROSSOVER_BOTTLE),
        "crossover_bottle_exists": DEFAULT_CROSSOVER_BOTTLE.exists(),
        "ready": bool(pack_entries) and bool(selected_runner) and bool(selected_compiler),
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


def project_metadata(project) -> tuple[dict[str, str | None], object]:
    result = find_device_metadata(project.device, project.mplab_root)
    metadata = load_device_metadata(
        device=result["device"],
        ini_reference=result.get("pack_ini") or result.get("ini"),
        cfgdata_reference=result.get("pack_cfgdata") or result.get("cfgdata"),
        pic_reference=result.get("pic"),
    )
    return result, metadata


def ensure_project_header(project_path: Path, project) -> Path:
    if project.header_mode == "supplied":
        short_name = project.device[3:] if project.device.startswith("PIC") else project.device
        supplied_path = DEFAULT_COMPILER.parent / f"{short_name}.H"
        if not supplied_path.exists():
            raise SystemExit(f"supplied header not found: {supplied_path}")
        return supplied_path
    header_path = project_path_join(project_path, project.header_path)
    if project.header_mode == "generated":
        _, metadata = project_metadata(project)
        header_path.parent.mkdir(parents=True, exist_ok=True)
        header_path.write_text(render_full_header(metadata), encoding="latin-1")
        return header_path
    if header_path.exists():
        return header_path
    raise SystemExit(f"existing header not found: {header_path}")


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
        source_path.write_text(updated, encoding="latin-1")
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
    source_path.write_text(updated, encoding="latin-1")
    action = "updated" if replaced else "appended"
    print(f"{action} managed config block in {source_path}")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    if args.project:
        project_path, project, edition = load_project_and_edition(args.project, args.edition)
        header_path = ensure_project_header(project_path, project)
        runner = shlex.split(project.runner) if project.runner else []
        source_path = project_path_join(project_path, project.main_source)
        device_option = (
            f"-p{project.device[3:]}" if project.device.startswith("PIC") else f"-p{project.device}"
        )
        options = [device_option, f"-I{header_path.parent}"]
        options.extend(project.base_build_options)
        options.extend(edition.build_options)
        command = build_command(
            compiler=project.compiler,
            main_file=str(source_path),
            options=options,
            runner=runner,
        )
        print("command:", shlex.join(command))
        if args.dry_run:
            return 0
        completed = subprocess.run(command, cwd=source_path.parent)
        return completed.returncode
    if not args.compiler or not args.main:
        raise SystemExit("--compiler and --main are required unless --project is used")
    runner = shlex.split(args.runner) if args.runner else []
    command = build_command(
        compiler=args.compiler,
        main_file=args.main,
        options=args.option or [],
        runner=runner,
    )
    print("command:", shlex.join(command))
    if args.dry_run:
        return 0
    completed = subprocess.run(command, cwd=args.cwd or None)
    return completed.returncode


def cmd_project_init(args: argparse.Namespace) -> int:
    project_path = Path(args.project)
    if project_path.exists() and not args.force:
        raise SystemExit(f"project file already exists: {project_path}")
    compiler = args.compiler or str(DEFAULT_COMPILER)
    runner = args.runner if args.runner is not None else str(DEFAULT_RUNNER)
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
    errors = validate_project_file(project)
    if args.json:
        print(json.dumps({"project": str(project_path), "errors": errors}, indent=2))
    else:
        if errors:
            print(f"invalid: {project_path}")
            for error in errors:
                print(f"- {error}")
        else:
            print(f"valid: {project_path}")
            print(f"device: {project.device}")
            print(f"editions: {', '.join(sorted(project.editions))}")
            print(f"header: {project.header_mode} -> {project.header_path}")
    return 0 if not errors else 1


def cmd_project_edit_edition(args: argparse.Namespace) -> int:
    project_path = Path(args.project)
    project = load_project_file(project_path)
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
    write_project_file(project, project_path)
    print(f"{action} edition {args.edition!r} in {project_path}")
    return 0


def cmd_project_set_config(args: argparse.Namespace) -> int:
    project_path = Path(args.project)
    project = load_project_file(project_path)
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
    write_project_file(project, project_path)
    print(f"updated config for edition {args.edition!r} in {project_path}")
    return 0


def cmd_project_set_build_options(args: argparse.Namespace) -> int:
    project_path = Path(args.project)
    project = load_project_file(project_path)
    try:
        project = update_project_edition_build_options(
            project,
            args.edition,
            args.option or [],
        )
    except KeyError as exc:
        missing = exc.args[0]
        raise SystemExit(f"unknown edition {missing!r} in {project_path}") from None
    write_project_file(project, project_path)
    print(f"updated build options for edition {args.edition!r} in {project_path}")
    return 0


def cmd_project_edit(args: argparse.Namespace) -> int:
    project_path = Path(args.project)
    project = load_project_file(project_path)
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
    errors = validate_project_file(project)
    if errors:
        raise SystemExit(
            f"invalid project update for {project_path}:\n- " + "\n- ".join(errors)
        )
    write_project_file(project, project_path)
    print(f"updated project fields in {project_path}")
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


def cmd_list_devices(args: argparse.Namespace) -> int:
    prefixes = tuple(
        f"PIC{family.upper()}" if not family.upper().startswith("PIC") else family.upper()
        for family in (args.family or ["10F", "12F", "16F"])
    )
    devices = list_devices_in_atpacks(prefixes=prefixes)
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
    print(f"pack devices: {report['pack_device_count']}")
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
    describe.set_defaults(func=cmd_describe_device)

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
    list_devices.set_defaults(func=cmd_list_devices)

    doctor = subparsers.add_parser(
        "doctor",
        help="Report whether the local packs and CrossOver-backed CC5X toolchain are usable.",
    )
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    project_init = subparsers.add_parser(
        "project-init",
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
        help="Validate a setcc-native.json manifest.",
    )
    project_validate.add_argument("--project", default="setcc-native.json")
    project_validate.add_argument("--json", action="store_true")
    project_validate.set_defaults(func=cmd_project_validate)

    project_edit = subparsers.add_parser(
        "project-edit",
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
        help="Create, copy, or delete editions in a setcc-native.json manifest.",
    )
    project_edit_edition.add_argument("--project", default="setcc-native.json")
    project_edit_edition.add_argument("--edition", required=True)
    project_edit_edition.add_argument("--copy-from")
    project_edit_edition.add_argument("--delete", action="store_true")
    project_edit_edition.set_defaults(func=cmd_project_edit_edition)

    project_set_config = subparsers.add_parser(
        "project-set-config",
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
        help="Replace the build option list for a named project edition.",
    )
    project_set_build_options.add_argument("--project", default="setcc-native.json")
    project_set_build_options.add_argument("--edition", required=True)
    project_set_build_options.add_argument("--option", action="append")
    project_set_build_options.set_defaults(func=cmd_project_set_build_options)

    project_list_editions = subparsers.add_parser(
        "project-list-editions",
        help="List the editions stored in a setcc-native.json manifest.",
    )
    project_list_editions.add_argument("--project", default="setcc-native.json")
    project_list_editions.add_argument("--json", action="store_true")
    project_list_editions.set_defaults(func=cmd_project_list_editions)

    project_show = subparsers.add_parser(
        "project-show",
        help="Show the stored project manifest or one edition from it.",
    )
    project_show.add_argument("--project", default="setcc-native.json")
    project_show.add_argument("--edition")
    project_show.add_argument("--json", action="store_true")
    project_show.set_defaults(func=cmd_project_show)

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
    list_pack_config.set_defaults(func=cmd_list_pack_config)

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
    build.set_defaults(func=cmd_build)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
