from __future__ import annotations

import hashlib
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import defusedxml.ElementTree as ET

from . import packs

# Verified against the live host: https://packs.download.microchip.com/Microchip.<family>.pdsc
# returns a per-family release history, and .../Microchip.<family>.<version>.atpack serves the
# archive itself (the same naming PACK_STEM_RE already parses for locally discovered packs).
PACKS_HOST = "https://packs.download.microchip.com"
REQUEST_TIMEOUT_SECONDS = 20.0
USER_AGENT = "cc5x-helper/packs-update"

# Real Microchip .pdsc descriptors run under ~2MB; cap generously above that so a
# misbehaving/compromised host cannot force an unbounded read.
MAX_PDSC_BYTES = 16 * 1024 * 1024
# The largest current DFP .atpack is well under 100MB compressed; cap with generous
# headroom so a misbehaving host cannot fill the disk via a single download.
MAX_ATPACK_BYTES = 400 * 1024 * 1024

FAMILY_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


class PackUpdateError(ValueError):
    """Network, parsing, or verification failure while checking/downloading a pack.

    Subclasses ``ValueError`` (not ``RuntimeError``) so it lands in the same
    "expected, clean-message" bucket the CLI's top-level error handling already
    gives file-IO/bad-input failures, instead of surfacing as a raw traceback.
    """


def _require_https(url: str) -> None:
    """Reject anything but https:// before it reaches urlopen.

    Every URL here is built from the hardcoded `PACKS_HOST` constant plus
    percent-encoded path components, so the scheme can never actually be
    attacker-influenced — this check makes that invariant explicit and
    machine-checked rather than "safe by construction only if nobody edits
    `PACKS_HOST` carelessly later" (also satisfies bandit B310, which cannot see
    that guarantee statically).
    """
    if urllib.parse.urlparse(url).scheme != "https":
        raise PackUpdateError(f"refusing non-https URL: {url}")


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    date: str | None

    @property
    def version_key(self) -> tuple[int, ...]:
        return packs.parse_version(self.version)


@dataclass(frozen=True)
class UpdateStatus:
    family: str
    local_version: str | None
    latest_version: str | None
    latest_date: str | None
    update_available: bool


def known_local_families() -> dict[str, str]:
    """CC5X-relevant pack families discovered locally, mapped to their best local version.

    Reuses the same discovery `list-devices`/`describe-device` rely on, so the family set
    here is exactly "packs that actually provide a PIC10F/12F/16F device this tool cares
    about" — never an ARM/AVR/debug-tool pack an MPLAB X install happens to also carry.
    """
    best_key: dict[str, tuple[int, ...]] = {}
    best_version: dict[str, str] = {}
    for entry in packs.list_devices_in_unpacked_packs() + packs.list_devices_in_atpacks():
        family = entry.get("pack_family")
        version = entry.get("pack_version")
        if not family or not version:
            continue
        key = packs.parse_version(version)
        if family not in best_key or key > best_key[family]:
            best_key[family] = key
            best_version[family] = version
    return best_version


def _validate_family(family: str) -> str:
    if not FAMILY_RE.match(family):
        raise PackUpdateError(f"invalid pack family name: {family!r}")
    return family


def _fetch_capped(url: str, max_bytes: int) -> bytes:
    _require_https(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        # Scheme already pinned to https by _require_https() above.
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # nosec B310
            data = response.read(max_bytes + 1)
    except OSError as exc:
        # Covers urllib.error.URLError/HTTPError as well as a bare TimeoutError from a
        # stall mid-read() — see the matching comment in download_pack() for why urlopen's
        # own timeout= does not wrap that case as a URLError.
        raise PackUpdateError(f"request to {url} failed: {exc}") from exc
    if len(data) > max_bytes:
        raise PackUpdateError(f"response from {url} exceeded {max_bytes}-byte limit")
    return data


def fetch_latest_release(family: str) -> ReleaseInfo:
    """Query Microchip's pack host for the newest published release of `family`.

    `family` must be the exact pack directory name (e.g. ``PIC16F1xxxx_DFP``) as seen
    under a local pack cache / `.atpack` filename — the same string used as `pack_family`
    everywhere else in this codebase.
    """
    _validate_family(family)
    url = f"{PACKS_HOST}/Microchip.{urllib.parse.quote(family)}.pdsc"
    data = _fetch_capped(url, MAX_PDSC_BYTES)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise PackUpdateError(f"could not parse pdsc for {family}: {exc}") from exc

    best: ReleaseInfo | None = None
    for release in root.findall("./releases/release"):
        version = release.get("version")
        if not version:
            continue
        candidate = ReleaseInfo(version=version, date=release.get("date"))
        if best is None or candidate.version_key > best.version_key:
            best = candidate
    if best is None:
        raise PackUpdateError(f"no releases found in pdsc for {family}")
    return best


def check_for_updates(families: dict[str, str] | None = None) -> list[UpdateStatus]:
    """Compare locally known pack versions against the latest published release.

    `families` maps family name -> local version string; defaults to
    `known_local_families()`. A network/parse failure for one family aborts the whole
    check rather than silently reporting a partial result.
    """
    families = families if families is not None else known_local_families()
    results: list[UpdateStatus] = []
    for family in sorted(families):
        local_version = families[family]
        latest = fetch_latest_release(family)
        results.append(
            UpdateStatus(
                family=family,
                local_version=local_version,
                latest_version=latest.version,
                latest_date=latest.date,
                update_available=latest.version_key > packs.parse_version(local_version),
            )
        )
    return results


def default_download_dir() -> Path:
    """Where `packs-update` saves new .atpack archives by default.

    Every other path-like setting in this codebase (``CC5X_ATPACK_DIRS``,
    ``CC5X_PACK_ROOTS``/``MPLABX_PACKS``, ``CC5X_IPECMD``) is env-var overridable;
    this matches that convention rather than only accepting ``--dest``. If
    ``CC5X_ATPACK_DIRS`` is set, downloads go to its first entry instead of the
    default, so a user who already points that at a custom location doesn't end
    up with packs split across two directories.
    """
    configured = packs._env_path_list("CC5X_ATPACK_DIRS")
    if configured:
        return configured[0]
    return packs.DEFAULT_ATPACK_DOWNLOAD_DIR


def download_pack(family: str, version: str, dest_dir: Path) -> Path:
    """Download and verify ``Microchip.<family>.<version>.atpack`` into `dest_dir`.

    Streams to a ``.part`` file (deleted on any failure) then atomically renames on
    success, so a half-downloaded file is never mistaken for a complete pack by later
    discovery. Verification uses the ``x-amz-meta-sha256`` response header Microchip's
    CDN has been observed to publish for every pack; if a future response omits it the
    download still succeeds (the header is an implementation detail of their S3-backed
    host, not a documented contract), it just isn't checksum-verified.
    """
    _validate_family(family)
    if not VERSION_RE.match(version):
        raise PackUpdateError(f"invalid pack version: {version!r}")

    filename = f"Microchip.{family}.{version}.atpack"
    url = f"{PACKS_HOST}/{urllib.parse.quote(filename)}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    final_path = dest_dir / filename
    tmp_path = dest_dir / f".{filename}.part"

    _require_https(url)
    hasher = hashlib.sha256()
    total = 0
    headers: dict[str, str] = {}
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        # Scheme already pinned to https by _require_https() above.
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # nosec B310
            headers = {key.lower(): value for key, value in response.headers.items()}
            with open(tmp_path, "wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ATPACK_BYTES:
                        raise PackUpdateError(
                            f"download of {filename} exceeded {MAX_ATPACK_BYTES}-byte limit"
                        )
                    hasher.update(chunk)
                    handle.write(chunk)
    except PackUpdateError:
        tmp_path.unlink(missing_ok=True)
        raise
    except OSError as exc:
        # Covers urllib.error.URLError/HTTPError (both OSError subclasses) as well as a
        # bare TimeoutError/ConnectionError from a stall mid-`response.read()` — urlopen's
        # own `timeout=` only wraps connection-establishment failures as URLError; a stall
        # once the response is already open raises a plain TimeoutError instead. Without
        # this catch-all that error would propagate unwrapped past download_pack, and
        # cmd_packs_update's per-family `except PackUpdateError` would miss it, aborting
        # the whole batch instead of reporting just this one family as failed.
        tmp_path.unlink(missing_ok=True)
        raise PackUpdateError(f"request to {url} failed: {exc}") from exc

    expected_sha256 = headers.get("x-amz-meta-sha256")
    if expected_sha256 and expected_sha256.lower() != hasher.hexdigest():
        tmp_path.unlink(missing_ok=True)
        raise PackUpdateError(
            f"checksum mismatch downloading {filename}: "
            f"expected {expected_sha256}, got {hasher.hexdigest()}"
        )

    tmp_path.replace(final_path)
    return final_path
