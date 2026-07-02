from __future__ import annotations

import os
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path


# Device files (.PIC/.cfgdata/.ini/.pdsc) are at most a few MB; cap reads from
# untrusted `.atpack` archives well above that so a decompression bomb (a tiny
# archive member that inflates to gigabytes) cannot exhaust memory.
MAX_PACK_MEMBER_BYTES = 64 * 1024 * 1024

PACK_STEM_RE = re.compile(
    r"^Microchip\.(?P<family>.+)\.(?P<version>\d+\.\d+\.\d+)$"
)
DEVICE_NAME_IN_MEMBER_RE = re.compile(r"(PIC[0-9A-Z]+)\.(?:PIC|ATDF)$", re.IGNORECASE)
CC5X_DEVICE_PREFIXES = ("PIC10F", "PIC12F", "PIC16F")


def parse_version(version: str | None) -> tuple[int, ...]:
    """Parse a dotted version string into a comparable integer tuple.

    Handles pack versions (``1.29.444``) and MPLAB X install-dir names (``v6.30``;
    an optional leading ``v``/``V`` is stripped). Missing or non-numeric input yields
    an empty tuple, which sorts below any real version. This is the single version
    parser shared by archive, unpacked-pack, and tool discovery so they order
    consistently (e.g. ``v6.30`` newer than ``v6.5``, not lexically older).
    """
    if not version:
        return ()
    text = version[1:] if version[:1] in ("v", "V") else version
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError:
        return ()


@dataclass(frozen=True)
class PackArchiveInfo:
    path: Path
    family: str
    version: str | None

    @property
    def version_key(self) -> tuple[int, ...]:
        return parse_version(self.version)


def normalize_device_name(device: str) -> str:
    text = device.strip().upper()
    # Device names are bare part numbers; a path separator (or NUL) means a crafted
    # value like "/tmp/x" that would escape the pack/metadata search dirs once joined
    # into candidate paths / rglob patterns (e.g. search_dir / "/TMP/X.PIC" collapses
    # to an absolute path). Reject them here so every consumer is protected at the source.
    if any(ch in text for ch in ("/", "\\", "\x00")):
        raise ValueError(f"invalid device name {device!r}: path separators are not allowed")
    if text.startswith(("ATTINY", "ATMEGA", "AVR", "SAM", "ATSAM")):
        return text
    if not text.startswith("PIC"):
        text = f"PIC{text}"
    return text


def device_short_name(device: str) -> str:
    """Device name without the leading ``PIC`` prefix.

    IPECMD and CC5X both expect ``16F1509`` rather than ``PIC16F1509`` for PIC parts,
    while AVR/SAM names (e.g. ``ATSAML11E16A``) are passed through unchanged. This is the
    single source of truth shared by the build/program ``-p``/``-P`` args and the editor's
    per-device ``_<short>`` IntelliSense macro.
    """
    normalized = normalize_device_name(device)
    return normalized[3:] if normalized.startswith("PIC") else normalized


def _env_path_list(*names: str) -> list[Path]:
    """Return paths from the first set environment variable in `names`.

    The value is split on the OS path separator so multiple roots can be supplied
    (e.g. ``CC5X_PACK_ROOTS=/a:/b``).
    """
    for name in names:
        value = os.environ.get(name)
        if value:
            return [Path(part).expanduser() for part in value.split(os.pathsep) if part]
    return []


def mplabx_install_bases() -> list[Path]:
    """Standard MPLAB X install locations across platforms (version-agnostic bases).

    Each base contains per-version subdirectories (e.g. ``v6.30``). Shared by pack-root
    discovery and tool (IPECMD/MDB) discovery so there is one source of truth.
    """
    bases = [
        Path("/opt/microchip/mplabx"),          # Linux default
        Path("/Applications/microchip/mplabx"),  # macOS default
    ]
    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        value = os.environ.get(env_name)
        if value:
            bases.append(Path(value) / "Microchip" / "MPLABX")
    return bases


def mplabx_version_dirs() -> list[Path]:
    """Installed MPLAB X version directories, newest version first within each base.

    Single source for the "walk each install base, order versions numerically (so v6.30
    precedes v6.5)" loop shared by pack-root and IPECMD/MDB tool discovery, so the two
    cannot disagree about which installed MPLAB X is newest.
    """
    dirs: list[Path] = []
    for base in mplabx_install_bases():
        if not base.exists():
            continue
        dirs.extend(
            sorted(
                (path for path in base.iterdir() if path.is_dir()),
                key=lambda path: parse_version(path.name),
                reverse=True,
            )
        )
    return dirs


def _dedup_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    ordered: list[Path] = []
    for path in paths:
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key not in seen:
            seen.add(key)
            ordered.append(path)
    return ordered


def discover_pack_roots() -> list[Path]:
    """System-wide search roots for *unpacked* device-family packs.

    Each returned path is a directory whose children are ``<family>/<version>/...``
    pack trees (e.g. the ``packs/Microchip`` folder of any installed MPLAB X). Order:
    explicit env override, the standalone pack-manager cache, every installed MPLAB X
    version found on the system, then legacy local dirs for back-compat.
    """
    roots: list[Path] = []
    roots.extend(_env_path_list("CC5X_PACK_ROOTS", "MPLABX_PACKS"))
    roots.append(Path.home() / ".mchp_packs/Microchip")
    for version_dir in mplabx_version_dirs():
        pack_root = version_dir / "packs" / "Microchip"
        if pack_root.exists():
            roots.append(pack_root)
    roots.append(Path.home() / "apps")  # legacy local layout
    return _dedup_paths(roots)


def unpacked_pack_roots() -> list[Path]:
    roots: list[Path] = []
    for root in discover_pack_roots():
        if root.exists():
            roots.append(root)
    return roots


# `pack_updates.default_download_dir()` falls back to this same constant (rather than a
# second literal) so the two never drift apart.
DEFAULT_ATPACK_DOWNLOAD_DIR = Path.home() / ".cc5x" / "atpacks"


def discover_atpack_dirs() -> list[Path]:
    dirs = _env_path_list("CC5X_ATPACK_DIRS")
    dirs.extend([
        DEFAULT_ATPACK_DOWNLOAD_DIR,  # `packs-update`'s default download dir
        Path.home() / "apps",
        Path.home() / "Downloads",
    ])
    return [path for path in _dedup_paths(dirs) if path.exists()]


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


def list_devices_in_unpacked_packs(
    roots: list[Path] | None = None,
    prefixes: tuple[str, ...] = CC5X_DEVICE_PREFIXES,
) -> list[dict[str, str | None]]:
    """List CC5X-relevant devices found in *unpacked* pack trees (e.g. MPLAB X packs).

    Mirrors :func:`list_devices_in_atpacks` but walks ``<root>/<family>/<version>/edc``
    directories instead of ``.atpack`` archives, keeping the highest pack version per
    device across all roots.
    """
    roots = roots if roots is not None else unpacked_pack_roots()
    normalized_prefixes = tuple(prefix.upper() for prefix in prefixes)
    best_by_device: dict[str, dict[str, str | None]] = {}
    best_version_by_device: dict[str, tuple[int, ...]] = {}

    for root in roots:
        if not root.exists():
            continue
        for family_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            for version_dir in sorted(
                (path for path in family_dir.iterdir() if path.is_dir()), reverse=True
            ):
                edc_dir = version_dir / "edc"
                if not edc_dir.exists():
                    continue
                version_key = parse_version(version_dir.name)
                member_names = [path.name for path in edc_dir.iterdir() if path.is_file()]
                for device_name in _extract_device_names_from_members(member_names):
                    if normalized_prefixes and not device_name.startswith(normalized_prefixes):
                        continue
                    if device_name in best_version_by_device and (
                        version_key <= best_version_by_device[device_name]
                    ):
                        continue
                    best_version_by_device[device_name] = version_key
                    best_by_device[device_name] = {
                        "device": device_name,
                        "pack_family": family_dir.name,
                        "pack_version": version_dir.name,
                        "pack_root": str(version_dir),
                        "pdsc": None,
                    }

    return [best_by_device[name] for name in sorted(best_by_device)]


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
        except (zipfile.BadZipFile, OSError):
            # One unreadable entry (corrupt zip, a directory matching the glob, a
            # permission error) must not abort discovery of every other pack — skip it.
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
        except (zipfile.BadZipFile, OSError):
            # Matches list_devices_in_atpacks: one unreadable entry (corrupt zip, a
            # directory matching the glob, a permission error) must not abort resolution
            # of every other device — skip it.
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


def _read_zip_member_capped(
    archive: zipfile.ZipFile,
    member_name: str,
    max_bytes: int = MAX_PACK_MEMBER_BYTES,
) -> bytes:
    """Read a single archive member, refusing decompression bombs.

    The declared size is checked first (cheap), then the stream itself is read with a
    hard cap so a member whose ZIP header understates its inflated size still cannot
    blow past the limit. `getinfo` also restricts reads to members the archive really
    lists, so a crafted reference cannot pull in an unrelated path.
    """
    try:
        info = archive.getinfo(member_name)
    except KeyError as exc:
        raise FileNotFoundError(f"archive member not found: {member_name}") from exc
    if info.file_size > max_bytes:
        raise ValueError(
            f"refusing to read oversized archive member {member_name!r} "
            f"({info.file_size} bytes exceeds {max_bytes}-byte limit)"
        )
    with archive.open(info) as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(
            f"archive member {member_name!r} exceeds {max_bytes}-byte limit"
        )
    return data


def _open_regular_file_rdonly(path: Path) -> int:
    """Open ``path`` for reading, refusing anything but a regular file.

    A pack root can be attacker-writable, and ``candidate.exists()`` (and any
    name-based check) follows symlinks, so the path can resolve to a FIFO or a
    block/character device node. Performing a normal ``open()`` on such a target is
    itself the side effect we must avoid: a device open can block or touch hardware
    before any type check runs, and ``O_NONBLOCK`` does not make arbitrary device
    opens side-effect-free.

    On Linux we acquire the descriptor with ``O_PATH``, which references the resolved
    inode *without* performing an I/O open (no device interaction, no blocking). We
    ``fstat`` that descriptor; only a regular file is then upgraded to a readable fd
    by reopening ``/proc/self/fd/<n>``, which is bound to the already-opened inode and
    therefore cannot be swapped for a different file (no TOCTOU re-resolution of the
    user-supplied path). Where ``O_PATH``/procfs is unavailable, fall back to an
    ``O_NONBLOCK`` open validated by ``fstat`` (covers the FIFO case at least).
    """
    o_path = getattr(os, "O_PATH", 0)
    o_cloexec = getattr(os, "O_CLOEXEC", 0)
    if o_path and os.path.isdir("/proc/self/fd"):
        path_fd = os.open(path, os.O_RDONLY | o_path | o_cloexec)
        try:
            if not stat.S_ISREG(os.fstat(path_fd).st_mode):
                raise ValueError(f"not a regular file: {path}")
            # Reopen the same inode for actual reading; the magic procfs symlink
            # resolves to what path_fd already points at, not the original path.
            return os.open(f"/proc/self/fd/{path_fd}", os.O_RDONLY | o_cloexec)
        finally:
            os.close(path_fd)

    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | o_cloexec
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"not a regular file: {path}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_text_file_capped(
    path: Path,
    encoding: str,
    max_bytes: int = MAX_PACK_MEMBER_BYTES,
) -> str:
    """Read a plain device file with a hard byte cap.

    Validates that the target is a regular file *before* any I/O open of a special
    file (see ``_open_regular_file_rdonly``), and caps the read so a regular file that
    grows after the open still cannot exhaust memory.
    """
    fd = _open_regular_file_rdonly(path)
    with os.fdopen(fd, "rb") as handle:  # fdopen owns fd and closes it on exit
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(
            f"refusing to read oversized device file {str(path)!r} "
            f"(exceeds {max_bytes}-byte limit)"
        )
    return data.decode(encoding)


def read_text_reference(reference: str, encoding: str = "utf-8") -> str:
    if "!/" in reference:
        archive_name, member_name = reference.split("!/", 1)
        with zipfile.ZipFile(archive_name) as archive:
            return _read_zip_member_capped(archive, member_name).decode(encoding)
    return _read_text_file_capped(Path(reference), encoding)


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
        f"{normalized.lower()}.ini",
        f"{normalized[3:].lower()}.ini" if normalized.startswith("PIC") else f"{normalized.lower()}.ini",
    }
    best_match: dict[str, str | None] | None = None
    best_version: tuple[int, ...] | None = None

    for root in roots:
        if not root.exists():
            continue
        for family_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            version_dirs = sorted((path for path in family_dir.iterdir() if path.is_dir()), reverse=True)
            for version_dir in version_dirs:
                version_key = parse_version(version_dir.name)
                for rel_dir in (
                    "edc", "atdf", "xc8/avr/cfgdata", "xc8/pic/dat/cfgdata", "cfgdata",
                    "xc8/pic/dat/ini", "ini",
                ):
                    search_dir = version_dir / rel_dir
                    if not search_dir.exists():
                        continue
                    # Iterate candidates in a stable (sorted) order: ``filenames`` is a set, so
                    # set iteration order varies with the hash seed across runs, which could let
                    # two same-suffix candidates in one dir fill a slot nondeterministically
                    # (audit: metadata nondeterminism).
                    for filename in sorted(filenames):
                        candidate = search_dir / filename
                        if not candidate.exists():
                            continue
                        # A strictly newer version wins outright and resets the record;
                        # ties keep the first version dir seen (dirs are sorted newest
                        # first, roots in priority order).
                        if best_version is None or version_key > best_version:
                            best_version = version_key
                            best_match = {
                                "device": normalized,
                                "pack_family": family_dir.name,
                                "pack_version": version_dir.name,
                                "pack_root": str(version_dir),
                                "pic": None,
                                "atdf": None,
                                "cfgdata": None,
                                "pdsc": None,
                                "ini": None,
                            }
                        # Merge files from the same winning version dir so a later
                        # .cfgdata match does not clobber an earlier .PIC reference.
                        if best_match is None or best_match["pack_root"] != str(version_dir):
                            continue
                        suffix = candidate.suffix
                        if suffix.upper() == ".PIC" and best_match["pic"] is None:
                            best_match["pic"] = str(candidate)
                        elif suffix.lower() == ".atdf" and best_match["atdf"] is None:
                            best_match["atdf"] = str(candidate)
                        elif suffix.lower() == ".cfgdata" and best_match["cfgdata"] is None:
                            best_match["cfgdata"] = str(candidate)
                        elif suffix.lower() == ".ini" and best_match["ini"] is None:
                            # The .ini carries ARCH/SFR/RAM metadata; without it a pack-cache-only
                            # install would generate headers missing the device architecture.
                            best_match["ini"] = str(candidate)
    if best_match:
        return best_match
    return _empty_result(normalized)
