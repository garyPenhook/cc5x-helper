from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path


PACK_STEM_RE = re.compile(
    r"^Microchip\.(?P<family>.+)\.(?P<version>\d+\.\d+\.\d+)$"
)
DEVICE_NAME_IN_MEMBER_RE = re.compile(r"(PIC[0-9A-Z]+)\.(?:PIC|ATDF)$", re.IGNORECASE)
CC5X_DEVICE_PREFIXES = ("PIC10F", "PIC12F", "PIC16F")


@dataclass(frozen=True)
class PackArchiveInfo:
    path: Path
    family: str
    version: str | None

    @property
    def version_key(self) -> tuple[int, ...]:
        if not self.version:
            return ()
        return tuple(int(part) for part in self.version.split("."))


def normalize_device_name(device: str) -> str:
    text = device.strip().upper()
    if text.startswith(("ATTINY", "ATMEGA", "AVR", "SAM", "ATSAM")):
        return text
    if not text.startswith("PIC"):
        text = f"PIC{text}"
    return text


def discover_pack_roots() -> list[Path]:
    return [
        Path.home() / ".mchp_packs/Microchip",
        Path.home() / "apps",
    ]


def unpacked_pack_roots() -> list[Path]:
    roots: list[Path] = []
    for root in discover_pack_roots():
        if root.exists():
            roots.append(root)
    return roots


def discover_atpack_dirs() -> list[Path]:
    dirs = [
        Path.home() / "apps",
        Path.home() / "Downloads",
    ]
    return [path for path in dirs if path.exists()]


def parse_pack_archive_info(path: Path) -> PackArchiveInfo:
    match = PACK_STEM_RE.match(path.stem)
    if match:
        return PackArchiveInfo(
            path=path,
            family=match.group("family"),
            version=match.group("version"),
        )
    return PackArchiveInfo(path=path, family=path.stem, version=None)


def is_cc5x_device(device: str) -> bool:
    normalized = normalize_device_name(device)
    return normalized.startswith(CC5X_DEVICE_PREFIXES)


def _pic_candidates(normalized: str) -> list[str]:
    short = normalized[3:] if normalized.startswith("PIC") else normalized
    return [
        f"edc/{normalized}.PIC",
        f"edc/{short}.PIC",
        f"edc/AC244051_AS_{normalized}.PIC",
        f"edc/AC244052_AS_{normalized}.PIC",
        f"edc/AC244055_AS_{normalized}.PIC",
        f"edc/AC244063_AS_{normalized}.PIC",
        f"edc/AC244064_AS_{normalized}.PIC",
        f"edc/AC244065_AS_{normalized}.PIC",
        f"edc/AC244066_AS_{normalized}.PIC",
    ]


def _ini_candidates(normalized: str) -> list[str]:
    short = normalized[3:] if normalized.startswith("PIC") else normalized
    lower = short.lower()
    return [
        f"xc8/pic/dat/ini/{lower}.ini",
        f"xc8/pic/dat/ini/{normalized.lower()}.ini",
    ]


def _cfg_candidates(normalized: str) -> list[str]:
    short = normalized[3:] if normalized.startswith("PIC") else normalized
    lower = short.lower()
    return [
        f"xc8/pic/dat/cfgdata/{lower}.cfgdata",
        f"xc8/pic/dat/cfgdata/{normalized.lower()}.cfgdata",
        f"xc8/avr/cfgdata/{normalized.lower()}.cfgdata",
        f"xc8/avr/cfgdata/{lower}.cfgdata",
    ]


def _empty_result(normalized: str) -> dict[str, str | None]:
    return {
        "device": normalized,
        "pack_family": None,
        "pack_version": None,
        "pack_root": None,
        "pic": None,
        "atdf": None,
        "cfgdata": None,
        "pdsc": None,
        "ini": None,
    }


def _archive_sort_key(path: Path) -> tuple[tuple[int, ...], str]:
    info = parse_pack_archive_info(path)
    return (info.version_key, path.name)


def _extract_device_names_from_members(member_names: list[str]) -> set[str]:
    devices: set[str] = set()
    for member_name in member_names:
        match = DEVICE_NAME_IN_MEMBER_RE.search(member_name)
        if match:
            devices.add(normalize_device_name(match.group(1)))
    return devices


def list_devices_in_atpacks(
    archive_dirs: list[Path] | None = None,
    prefixes: tuple[str, ...] = CC5X_DEVICE_PREFIXES,
) -> list[dict[str, str | None]]:
    dirs = archive_dirs if archive_dirs is not None else discover_atpack_dirs()
    archive_paths: list[Path] = []
    for archive_dir in dirs:
        archive_paths.extend(sorted(archive_dir.glob("Microchip*.atpack")))
    archive_paths.sort(key=_archive_sort_key, reverse=True)

    best_by_device: dict[str, dict[str, str | None]] = {}
    best_version_by_device: dict[str, tuple[int, ...]] = {}
    normalized_prefixes = tuple(prefix.upper() for prefix in prefixes)

    for archive_path in archive_paths:
        info = parse_pack_archive_info(archive_path)
        try:
            with zipfile.ZipFile(archive_path) as archive:
                member_names = archive.namelist()
        except zipfile.BadZipFile:
            continue

        pdsc_name = next(
            (name for name in member_names if name.lower().endswith(".pdsc")),
            None,
        )
        for device_name in sorted(_extract_device_names_from_members(member_names)):
            if normalized_prefixes and not device_name.startswith(normalized_prefixes):
                continue
            version_key = info.version_key
            if version_key < best_version_by_device.get(device_name, ()):
                continue
            best_version_by_device[device_name] = version_key
            best_by_device[device_name] = {
                "device": device_name,
                "pack_family": info.family,
                "pack_version": info.version,
                "pack_root": str(archive_path),
                "pdsc": f"{archive_path}!/{pdsc_name}" if pdsc_name else None,
            }

    return [best_by_device[name] for name in sorted(best_by_device)]


def find_device_in_atpacks(
    device: str,
    archive_dirs: list[Path] | None = None,
) -> dict[str, str | None]:
    normalized = normalize_device_name(device)
    dirs = archive_dirs if archive_dirs is not None else discover_atpack_dirs()
    archive_paths: list[Path] = []
    for archive_dir in dirs:
        archive_paths.extend(sorted(archive_dir.glob("Microchip*.atpack")))
    archive_paths.sort(key=_archive_sort_key, reverse=True)

    pic_candidates = _pic_candidates(normalized)
    ini_candidates = _ini_candidates(normalized)
    cfg_candidates = _cfg_candidates(normalized)

    for archive_path in archive_paths:
        try:
            with zipfile.ZipFile(archive_path) as archive:
                names = set(archive.namelist())
        except zipfile.BadZipFile:
            continue

        pic_match = next((name for name in pic_candidates if name in names), None)
        ini_match = next((name for name in ini_candidates if name in names), None)
        cfg_match = next((name for name in cfg_candidates if name in names), None)
        if pic_match or ini_match or cfg_match:
            info = parse_pack_archive_info(archive_path)
            pdsc_name = next((name for name in names if name.lower().endswith(".pdsc")), None)
            return {
                "device": normalized,
                "pack_family": info.family,
                "pack_version": info.version,
                "pack_root": str(archive_path),
                "pic": f"{archive_path}!/{pic_match}" if pic_match else None,
                "atdf": None,
                "cfgdata": f"{archive_path}!/{cfg_match}" if cfg_match else None,
                "pdsc": f"{archive_path}!/{pdsc_name}" if pdsc_name else None,
                "ini": f"{archive_path}!/{ini_match}" if ini_match else None,
            }

    return _empty_result(normalized)


def read_text_reference(reference: str, encoding: str = "utf-8") -> str:
    if "!/" in reference:
        archive_name, member_name = reference.split("!/", 1)
        with zipfile.ZipFile(archive_name) as archive:
            return archive.read(member_name).decode(encoding)
    return Path(reference).read_text(encoding=encoding)


def find_device_in_unpacked_packs(
    device: str,
    roots: list[Path] | None = None,
) -> dict[str, str | None]:
    normalized = normalize_device_name(device)
    roots = roots if roots is not None else unpacked_pack_roots()
    filenames = {
        f"{normalized}.PIC",
        f"{normalized[3:]}.PIC" if normalized.startswith("PIC") else f"{normalized}.PIC",
        f"{normalized}.atdf",
        f"{normalized[3:]}.atdf" if normalized.startswith("PIC") else f"{normalized}.atdf",
        f"{normalized.lower()}.cfgdata",
        f"{normalized[3:].lower()}.cfgdata" if normalized.startswith("PIC") else f"{normalized.lower()}.cfgdata",
    }
    best_match: dict[str, str | None] | None = None
    best_version: tuple[int, ...] = ()

    for root in roots:
        for family_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            version_dirs = sorted((path for path in family_dir.iterdir() if path.is_dir()), reverse=True)
            for version_dir in version_dirs:
                version_key = _archive_sort_key(Path(f"Microchip.{family_dir.name}.{version_dir.name}.atpack"))[0]
                for rel_dir in ("edc", "atdf", "xc8/avr/cfgdata", "xc8/pic/dat/cfgdata", "cfgdata"):
                    search_dir = version_dir / rel_dir
                    if not search_dir.exists():
                        continue
                    for filename in filenames:
                        candidate = search_dir / filename
                        if candidate.exists() and version_key >= best_version:
                            best_version = version_key
                            best_match = {
                                "device": normalized,
                                "pack_family": family_dir.name,
                                "pack_version": version_dir.name,
                                "pack_root": str(version_dir),
                                "pic": str(candidate) if candidate.suffix.upper() == ".PIC" else None,
                                "atdf": str(candidate) if candidate.suffix.lower() == ".atdf" else None,
                                "cfgdata": str(candidate) if candidate.suffix.lower() == ".cfgdata" else None,
                                "pdsc": None,
                                "ini": None,
                            }
    if best_match:
        return best_match
    return _empty_result(normalized)
