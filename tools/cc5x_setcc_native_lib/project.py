from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from .fsutil import atomic_write_text
from .packs import normalize_device_name


SUPPORTED_HEADER_MODES = {"generated", "supplied", "existing"}
SUPPORTED_BIT_NAME_FORMATS = {"combined", "long", "short"}
DEFAULT_BIT_NAME_FORMAT = "combined"
DEFAULT_PROJECT_VERSION = 1


@dataclass(frozen=True)
class ProjectEdition:
    name: str
    config: dict[str, str] = field(default_factory=dict)
    build_options: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProjectFile:
    version: int
    device: str
    compiler: str
    runner: str | None
    header_mode: str
    header_path: str
    config_source: str
    main_source: str
    header_bit_name_format: str = DEFAULT_BIT_NAME_FORMAT
    base_build_options: list[str] = field(default_factory=list)
    editions: dict[str, ProjectEdition] = field(default_factory=dict)
    mplab_root: str | None = None
    # Opaque pass-through for the `debug-stub` generator's config (debuggen.py validates it).
    # Preserved verbatim across load/edit/write so a mutation command does not erase a
    # hand-added "debug" section. Omitted from the serialized manifest when None.
    debug: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        header_payload: dict[str, object] = {
            "mode": self.header_mode,
            "path": self.header_path,
        }
        if self.header_bit_name_format != DEFAULT_BIT_NAME_FORMAT:
            header_payload["bit_name_format"] = self.header_bit_name_format
        payload: dict[str, object] = {
            "version": self.version,
            "device": self.device,
            "compiler": self.compiler,
            "runner": self.runner,
            "mplab_root": self.mplab_root,
            "header": header_payload,
            "config_source": self.config_source,
            "main_source": self.main_source,
            "build_options": list(self.base_build_options),
            "editions": {
                name: {
                    "config": dict(edition.config),
                    "build_options": list(edition.build_options),
                }
                for name, edition in sorted(self.editions.items())
            },
        }
        if self.debug is not None:
            payload["debug"] = self.debug
        return payload


def default_project_manifest(
    device: str,
    compiler: str,
    runner: str | None,
    main_source: str,
    config_source: str | None = None,
    header_mode: str = "generated",
    header_path: str | None = None,
    header_bit_name_format: str = DEFAULT_BIT_NAME_FORMAT,
    mplab_root: str | None = None,
) -> ProjectFile:
    normalized = normalize_device_name(device)
    short_name = normalized[3:] if normalized.startswith("PIC") else normalized
    if header_path is None and header_mode == "supplied":
        header_path = f"{short_name}.H"
    return ProjectFile(
        version=DEFAULT_PROJECT_VERSION,
        device=normalized,
        compiler=compiler,
        runner=runner,
        header_mode=header_mode,
        header_path=header_path or f"generated_headers/{short_name}.H",
        header_bit_name_format=header_bit_name_format,
        config_source=config_source or main_source,
        main_source=main_source,
        base_build_options=[],
        mplab_root=mplab_root,
        editions={
            "production": ProjectEdition(name="production"),
            "debug": ProjectEdition(name="debug"),
        },
    )


def _require_field(payload: dict, key: str, path: Path) -> object:
    """Fetch a required manifest field, raising a clear error instead of KeyError."""
    if key not in payload:
        raise ValueError(f"{path}: missing required field {key!r}")
    return payload[key]


def _require_str(payload: dict, key: str, path: Path) -> str:
    """Fetch a required string field, rejecting other JSON types instead of coercing them.

    Without this a manifest with ``"main_source": 123`` or ``"device": {...}`` would be
    silently ``str()``-ed into a valid-looking ``"123"`` / ``"{'...'}"`` and only fail much
    later (or build the wrong thing). Reject the wrong type at load time.
    """
    value = _require_field(payload, key, path)
    if not isinstance(value, str):
        raise ValueError(f"{path}: field {key!r} must be a string, got {type(value).__name__}")
    return value


def _optional_str(value: object, label: str, path: Path) -> "str | None":
    """Validate an optional string field, rejecting non-string JSON types."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{path}: {label} must be a string, got {type(value).__name__}")
    return value


def _as_mapping(value: object, label: str, path: Path) -> dict:
    """Coerce an optional manifest field to a dict, rejecting other JSON shapes cleanly."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{path}: {label} must be an object")
    return value


def _as_str_list(value: object, label: str, path: Path) -> list[str]:
    """Coerce an optional manifest field to a list of strings.

    Guards against a bare string being treated as an iterable of characters (e.g.
    "build_options": "-a" silently becoming ["-", "a"]).
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{path}: {label} must be an array of strings")
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{path}: {label} must be an array of strings (got {type(item).__name__})")
    return list(value)


def load_project_file(path: Path) -> ProjectFile:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: manifest must be a JSON object")
    header = payload.get("header") or {}
    if not isinstance(header, dict):
        raise ValueError(f"{path}: 'header' must be an object")
    editions_raw = _as_mapping(payload.get("editions"), "'editions'", path)
    editions = {}
    for name, item in editions_raw.items():
        if not isinstance(item, dict):
            raise ValueError(f"{path}: edition {name!r} must be an object")
        config_raw = _as_mapping(item.get("config"), f"edition {name!r} 'config'", path)
        config: dict[str, str] = {}
        for key, value in config_raw.items():
            if not isinstance(value, str):
                raise ValueError(
                    f"{path}: edition {name!r} config value for {key!r} must be a string, "
                    f"got {type(value).__name__}"
                )
            config[str(key)] = value
        editions[name] = ProjectEdition(
            name=name,
            config=config,
            build_options=_as_str_list(
                item.get("build_options"), f"edition {name!r} 'build_options'", path
            ),
        )
    if "path" not in header:
        raise ValueError(f"{path}: missing required field 'header.path'")
    if not isinstance(header["path"], str):
        # header.path is required; a null/numeric value would otherwise become None (or a
        # bogus "None" path) and blow up later in project_path_join with a raw TypeError.
        raise ValueError(
            f"{path}: 'header.path' must be a string, got {type(header['path']).__name__}"
        )
    version = payload.get("version", DEFAULT_PROJECT_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError(f"{path}: field 'version' must be an integer, got {type(version).__name__}")
    header_mode = _optional_str(header.get("mode"), "'header.mode'", path) or "generated"
    header_bit_name_format = (
        _optional_str(header.get("bit_name_format"), "'header.bit_name_format'", path)
        or DEFAULT_BIT_NAME_FORMAT
    )
    debug_section = payload.get("debug")
    if debug_section is not None and not isinstance(debug_section, dict):
        raise ValueError(f"{path}: 'debug' must be an object")
    return ProjectFile(
        version=version,
        device=normalize_device_name(_require_str(payload, "device", path)),
        compiler=_require_str(payload, "compiler", path),
        runner=_optional_str(payload.get("runner"), "'runner'", path),
        header_mode=header_mode,
        header_path=_optional_str(header["path"], "'header.path'", path),
        header_bit_name_format=header_bit_name_format,
        config_source=_require_str(payload, "config_source", path),
        main_source=_require_str(payload, "main_source", path),
        base_build_options=_as_str_list(payload.get("build_options"), "'build_options'", path),
        editions=editions,
        mplab_root=_optional_str(payload.get("mplab_root"), "'mplab_root'", path),
        debug=debug_section,
    )


def write_project_file(project: ProjectFile, path: Path) -> None:
    # Atomic write: the manifest holds the device, header config, and every edition, so a
    # crash / disk-full mid-write must not truncate it. Mirrors the source/header writes
    # rather than the old plain write_text that truncated before serializing (audit).
    atomic_write_text(path, json.dumps(project.to_dict(), indent=2) + "\n", encoding="utf-8")


def validate_project_file(project: ProjectFile) -> list[str]:
    errors: list[str] = []
    if project.version != DEFAULT_PROJECT_VERSION:
        errors.append(
            f"unsupported project version {project.version}; expected {DEFAULT_PROJECT_VERSION}"
        )
    if project.header_mode not in SUPPORTED_HEADER_MODES:
        errors.append(
            f"unsupported header.mode {project.header_mode!r}; expected one of {sorted(SUPPORTED_HEADER_MODES)}"
        )
    if project.header_bit_name_format not in SUPPORTED_BIT_NAME_FORMATS:
        errors.append(
            f"unsupported header.bit_name_format {project.header_bit_name_format!r}; "
            f"expected one of {sorted(SUPPORTED_BIT_NAME_FORMATS)}"
        )
    if not project.device.startswith(("PIC10F", "PIC12F", "PIC16F")):
        errors.append("device must be a CC5X-target PIC10F/PIC12F/PIC16F part")
    if not project.compiler:
        errors.append("compiler is required")
    if not project.header_path:
        errors.append("header.path is required")
    if not project.main_source:
        errors.append("main_source is required")
    if not project.config_source:
        errors.append("config_source is required")
    if not project.editions:
        errors.append("at least one edition is required")
    for name, edition in sorted(project.editions.items()):
        if not name:
            errors.append("edition name cannot be empty")
        for key, value in sorted(edition.config.items()):
            if not key or not value:
                errors.append(f"edition {name!r} has an empty config entry")
        for option in edition.build_options:
            if not option:
                errors.append(f"edition {name!r} contains an empty build option")
    return errors


def set_project_edition(
    project: ProjectFile,
    name: str,
    *,
    from_edition: str | None = None,
) -> ProjectFile:
    editions = dict(project.editions)
    if from_edition is not None:
        source = editions.get(from_edition)
        if source is None:
            raise KeyError(from_edition)
        editions[name] = ProjectEdition(
            name=name,
            config=dict(source.config),
            build_options=list(source.build_options),
        )
    else:
        editions[name] = editions.get(name, ProjectEdition(name=name))
    return replace(project, editions=editions)


def delete_project_edition(project: ProjectFile, name: str) -> ProjectFile:
    editions = dict(project.editions)
    if name not in editions:
        raise KeyError(name)
    if len(editions) == 1:
        raise ValueError("cannot delete the last edition")
    del editions[name]
    return replace(project, editions=editions)


def update_project_edition_config(
    project: ProjectFile,
    edition_name: str,
    updates: dict[str, str],
    *,
    clear: bool = False,
) -> ProjectFile:
    edition = project.editions.get(edition_name)
    if edition is None:
        raise KeyError(edition_name)
    config = {} if clear else dict(edition.config)
    config.update(updates)
    editions = dict(project.editions)
    editions[edition_name] = replace(edition, config=config)
    return replace(project, editions=editions)


def remove_project_edition_config(
    project: ProjectFile,
    edition_name: str,
    names: list[str],
) -> ProjectFile:
    edition = project.editions.get(edition_name)
    if edition is None:
        raise KeyError(edition_name)
    config = dict(edition.config)
    for name in names:
        config.pop(name, None)
    editions = dict(project.editions)
    editions[edition_name] = replace(edition, config=config)
    return replace(project, editions=editions)


def update_project_edition_build_options(
    project: ProjectFile,
    edition_name: str,
    options: list[str],
) -> ProjectFile:
    edition = project.editions.get(edition_name)
    if edition is None:
        raise KeyError(edition_name)
    editions = dict(project.editions)
    editions[edition_name] = replace(edition, build_options=list(options))
    return replace(project, editions=editions)


def project_summary(project: ProjectFile) -> dict[str, object]:
    header: dict[str, object] = {
        "mode": project.header_mode,
        "path": project.header_path,
    }
    if project.header_bit_name_format != DEFAULT_BIT_NAME_FORMAT:
        header["bit_name_format"] = project.header_bit_name_format
    return {
        "version": project.version,
        "device": project.device,
        "compiler": project.compiler,
        "runner": project.runner,
        "mplab_root": project.mplab_root,
        "header": header,
        "config_source": project.config_source,
        "main_source": project.main_source,
        "build_options": list(project.base_build_options),
        "editions": {
            name: {
                "config": dict(edition.config),
                "build_options": list(edition.build_options),
            }
            for name, edition in sorted(project.editions.items())
        },
    }


def update_project_fields(
    project: ProjectFile,
    *,
    device: str | None = None,
    compiler: str | None = None,
    runner: str | None = None,
    mplab_root: str | None = None,
    header_mode: str | None = None,
    header_path: str | None = None,
    header_bit_name_format: str | None = None,
    config_source: str | None = None,
    main_source: str | None = None,
    clear_runner: bool = False,
    clear_mplab_root: bool = False,
) -> ProjectFile:
    return replace(
        project,
        device=normalize_device_name(device) if device is not None else project.device,
        compiler=compiler if compiler is not None else project.compiler,
        runner=None if clear_runner else (runner if runner is not None else project.runner),
        mplab_root=None if clear_mplab_root else (mplab_root if mplab_root is not None else project.mplab_root),
        header_mode=header_mode if header_mode is not None else project.header_mode,
        header_path=header_path if header_path is not None else project.header_path,
        header_bit_name_format=(
            header_bit_name_format
            if header_bit_name_format is not None
            else project.header_bit_name_format
        ),
        config_source=config_source if config_source is not None else project.config_source,
        main_source=main_source if main_source is not None else project.main_source,
    )
