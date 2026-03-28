from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from .packs import normalize_device_name


SUPPORTED_HEADER_MODES = {"generated", "supplied", "existing"}
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
    base_build_options: list[str] = field(default_factory=list)
    editions: dict[str, ProjectEdition] = field(default_factory=dict)
    mplab_root: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "device": self.device,
            "compiler": self.compiler,
            "runner": self.runner,
            "mplab_root": self.mplab_root,
            "header": {
                "mode": self.header_mode,
                "path": self.header_path,
            },
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


def default_project_manifest(
    device: str,
    compiler: str,
    runner: str | None,
    main_source: str,
    config_source: str | None = None,
    header_mode: str = "generated",
    header_path: str | None = None,
    mplab_root: str | None = None,
) -> ProjectFile:
    normalized = normalize_device_name(device)
    short_name = normalized[3:] if normalized.startswith("PIC") else normalized
    return ProjectFile(
        version=DEFAULT_PROJECT_VERSION,
        device=normalized,
        compiler=compiler,
        runner=runner,
        header_mode=header_mode,
        header_path=header_path or f"generated_headers/{short_name}.H",
        config_source=config_source or main_source,
        main_source=main_source,
        base_build_options=[],
        mplab_root=mplab_root,
        editions={
            "production": ProjectEdition(name="production"),
            "debug": ProjectEdition(name="debug"),
        },
    )


def load_project_file(path: Path) -> ProjectFile:
    payload = json.loads(path.read_text(encoding="utf-8"))
    header = payload.get("header") or {}
    editions_raw = payload.get("editions") or {}
    editions = {
        name: ProjectEdition(
            name=name,
            config=dict(item.get("config") or {}),
            build_options=list(item.get("build_options") or []),
        )
        for name, item in editions_raw.items()
    }
    return ProjectFile(
        version=int(payload.get("version", DEFAULT_PROJECT_VERSION)),
        device=normalize_device_name(payload["device"]),
        compiler=str(payload["compiler"]),
        runner=str(payload["runner"]) if payload.get("runner") is not None else None,
        header_mode=str(header.get("mode", "generated")),
        header_path=str(header["path"]),
        config_source=str(payload["config_source"]),
        main_source=str(payload["main_source"]),
        base_build_options=list(payload.get("build_options") or []),
        editions=editions,
        mplab_root=str(payload["mplab_root"]) if payload.get("mplab_root") is not None else None,
    )


def write_project_file(project: ProjectFile, path: Path) -> None:
    path.write_text(json.dumps(project.to_dict(), indent=2) + "\n", encoding="utf-8")


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
    return {
        "version": project.version,
        "device": project.device,
        "compiler": project.compiler,
        "runner": project.runner,
        "mplab_root": project.mplab_root,
        "header": {
            "mode": project.header_mode,
            "path": project.header_path,
        },
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
        config_source=config_source if config_source is not None else project.config_source,
        main_source=main_source if main_source is not None else project.main_source,
    )
