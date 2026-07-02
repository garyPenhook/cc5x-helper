from __future__ import annotations

import hashlib
import io
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

try:
    from cc5x_setcc_native_lib import pack_updates
    from cc5x_setcc_native_lib import packs
except ModuleNotFoundError:
    from tools.cc5x_setcc_native_lib import pack_updates
    from tools.cc5x_setcc_native_lib import packs


PDSC_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<package>
  <vendor>Microchip</vendor>
  <name>{family}</name>
  <releases>
    <release version="{v1}" date="2026-04-23">First</release>
    <release version="{v2}" date="2024-01-01">Older</release>
  </releases>
</package>
"""


class _FakeResponse:
    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        self._buffer = io.BytesIO(body)
        self.headers = headers

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


class _StallingResponse:
    """Serves one chunk successfully, then times out - simulates a stall mid-download.

    urlopen's own `timeout=` only wraps connection-establishment failures as
    URLError; a stall once the response is already open raises a bare
    TimeoutError from response.read() instead.
    """

    def __init__(self, first_chunk: bytes, headers: dict[str, str] | None = None) -> None:
        self._first_chunk = first_chunk
        self._served_first = False
        self.headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        if not self._served_first:
            self._served_first = True
            return self._first_chunk
        raise TimeoutError("timed out")

    def __enter__(self) -> "_StallingResponse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


class _TimeoutResponse:
    """Times out on the very first read() - simulates a stall reading a small response."""

    headers: dict[str, str] = {}

    def read(self, size: int = -1) -> bytes:
        raise TimeoutError("timed out")

    def __enter__(self) -> "_TimeoutResponse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


class FetchLatestReleaseTests(unittest.TestCase):
    def test_picks_highest_version_regardless_of_document_order(self) -> None:
        body = PDSC_TEMPLATE.format(family="PIC16F1xxxx_DFP", v1="1.9.0", v2="1.31.465").encode()
        with unittest.mock.patch.object(
            pack_updates.urllib.request, "urlopen", return_value=_FakeResponse(body, {})
        ):
            release = pack_updates.fetch_latest_release("PIC16F1xxxx_DFP")
        self.assertEqual(release.version, "1.31.465")
        self.assertEqual(release.date, "2024-01-01")

    def test_rejects_invalid_family_name(self) -> None:
        with self.assertRaises(pack_updates.PackUpdateError):
            pack_updates.fetch_latest_release("../etc/passwd")

    def test_raises_clean_error_on_malformed_xml(self) -> None:
        with unittest.mock.patch.object(
            pack_updates.urllib.request, "urlopen", return_value=_FakeResponse(b"<not-xml", {})
        ):
            with self.assertRaises(pack_updates.PackUpdateError):
                pack_updates.fetch_latest_release("PIC16F1xxxx_DFP")

    def test_pack_update_error_is_a_value_error(self) -> None:
        # main() only converts (ValueError, OSError) into a clean CLI message; anything
        # else would surface as a raw traceback instead of "error: ...".
        self.assertTrue(issubclass(pack_updates.PackUpdateError, ValueError))


class RequireHttpsTests(unittest.TestCase):
    def test_accepts_https(self) -> None:
        pack_updates._require_https("https://packs.download.microchip.com/x")

    def test_rejects_non_https_schemes(self) -> None:
        for url in ("http://packs.download.microchip.com/x", "file:///etc/passwd", "ftp://x/y"):
            with self.assertRaises(pack_updates.PackUpdateError):
                pack_updates._require_https(url)


class CheckForUpdatesTests(unittest.TestCase):
    def test_flags_update_available_only_when_remote_is_newer(self) -> None:
        releases = {
            "FamilyA": pack_updates.ReleaseInfo(version="2.0.0", date="2026-01-01"),
            "FamilyB": pack_updates.ReleaseInfo(version="1.0.0", date="2026-01-01"),
        }
        with unittest.mock.patch.object(
            pack_updates, "fetch_latest_release", side_effect=lambda family: releases[family]
        ):
            results = pack_updates.check_for_updates({"FamilyA": "1.0.0", "FamilyB": "1.0.0"})
        by_family = {r.family: r for r in results}
        self.assertTrue(by_family["FamilyA"].update_available)
        self.assertFalse(by_family["FamilyB"].update_available)

    def test_missing_local_version_still_reports_latest(self) -> None:
        latest = pack_updates.ReleaseInfo(version="1.0.0", date="2026-01-01")
        with unittest.mock.patch.object(pack_updates, "fetch_latest_release", return_value=latest):
            results = pack_updates.check_for_updates({"NewFamily": None})
        self.assertEqual(results[0].local_version, None)
        self.assertTrue(results[0].update_available)


class DownloadPackTests(unittest.TestCase):
    def test_downloads_and_verifies_checksum(self) -> None:
        body = b"fake atpack contents"
        digest = hashlib.sha256(body).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            dest_dir = Path(tmp)
            with unittest.mock.patch.object(
                pack_updates.urllib.request,
                "urlopen",
                return_value=_FakeResponse(body, {"x-amz-meta-sha256": digest}),
            ):
                path = pack_updates.download_pack("PIC16F1xxxx_DFP", "1.31.465", dest_dir)
            self.assertEqual(path.name, "Microchip.PIC16F1xxxx_DFP.1.31.465.atpack")
            self.assertEqual(path.read_bytes(), body)
            # no leftover .part temp file
            self.assertEqual(list(dest_dir.iterdir()), [path])

    def test_checksum_mismatch_raises_and_leaves_no_partial_file(self) -> None:
        body = b"fake atpack contents"
        with tempfile.TemporaryDirectory() as tmp:
            dest_dir = Path(tmp)
            with unittest.mock.patch.object(
                pack_updates.urllib.request,
                "urlopen",
                return_value=_FakeResponse(body, {"x-amz-meta-sha256": "0" * 64}),
            ):
                with self.assertRaises(pack_updates.PackUpdateError):
                    pack_updates.download_pack("PIC16F1xxxx_DFP", "1.31.465", dest_dir)
            self.assertEqual(list(dest_dir.iterdir()), [])

    def test_missing_checksum_header_does_not_fail_download(self) -> None:
        body = b"fake atpack contents"
        with tempfile.TemporaryDirectory() as tmp:
            dest_dir = Path(tmp)
            with unittest.mock.patch.object(
                pack_updates.urllib.request, "urlopen", return_value=_FakeResponse(body, {})
            ):
                path = pack_updates.download_pack("PIC16F1xxxx_DFP", "1.31.465", dest_dir)
            self.assertEqual(path.read_bytes(), body)

    def test_oversized_response_is_rejected_and_cleaned_up(self) -> None:
        body = b"x" * 1024
        with tempfile.TemporaryDirectory() as tmp:
            dest_dir = Path(tmp)
            with unittest.mock.patch.object(pack_updates, "MAX_ATPACK_BYTES", 16):
                with unittest.mock.patch.object(
                    pack_updates.urllib.request, "urlopen", return_value=_FakeResponse(body, {})
                ):
                    with self.assertRaises(pack_updates.PackUpdateError):
                        pack_updates.download_pack("PIC16F1xxxx_DFP", "1.31.465", dest_dir)
            self.assertEqual(list(dest_dir.iterdir()), [])

    def test_rejects_invalid_family_and_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest_dir = Path(tmp)
            with self.assertRaises(pack_updates.PackUpdateError):
                pack_updates.download_pack("../escape", "1.0.0", dest_dir)
            with self.assertRaises(pack_updates.PackUpdateError):
                pack_updates.download_pack("PIC16F1xxxx_DFP", "not-a-version", dest_dir)

    def test_mid_download_timeout_is_wrapped_not_raw(self) -> None:
        # Regression test: a bare TimeoutError from a stalled response.read() must come
        # out as PackUpdateError, or cmd_packs_update's per-family `except
        # PackUpdateError` misses it and aborts the whole batch instead of marking just
        # this family failed.
        with tempfile.TemporaryDirectory() as tmp:
            dest_dir = Path(tmp)
            with unittest.mock.patch.object(
                pack_updates.urllib.request,
                "urlopen",
                return_value=_StallingResponse(b"partial-bytes"),
            ):
                with self.assertRaises(pack_updates.PackUpdateError):
                    pack_updates.download_pack("PIC16F1xxxx_DFP", "1.31.465", dest_dir)
            self.assertEqual(list(dest_dir.iterdir()), [])  # .part cleaned up, not left behind


class FetchLatestReleaseTimeoutTests(unittest.TestCase):
    def test_mid_fetch_timeout_is_wrapped_not_raw(self) -> None:
        with unittest.mock.patch.object(
            pack_updates.urllib.request, "urlopen", return_value=_TimeoutResponse()
        ):
            with self.assertRaises(pack_updates.PackUpdateError):
                pack_updates.fetch_latest_release("PIC16F1xxxx_DFP")


class DefaultDownloadDirTests(unittest.TestCase):
    def test_defaults_to_the_constant_packs_discovery_also_uses(self) -> None:
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CC5X_ATPACK_DIRS", None)
            self.assertEqual(pack_updates.default_download_dir(), packs.DEFAULT_ATPACK_DOWNLOAD_DIR)

    def test_honors_cc5x_atpack_dirs_override(self) -> None:
        with tempfile.TemporaryDirectory() as custom, tempfile.TemporaryDirectory() as other:
            with unittest.mock.patch.dict(os.environ, {"CC5X_ATPACK_DIRS": f"{custom}{os.pathsep}{other}"}):
                self.assertEqual(pack_updates.default_download_dir(), Path(custom))


class FilenameConventionTests(unittest.TestCase):
    def test_download_filename_matches_pack_stem_re(self) -> None:
        # download_pack() builds "Microchip.<family>.<version>.atpack" independently of
        # packs.PACK_STEM_RE, which is what parses that same convention back out of
        # locally discovered .atpack files. Pin the round-trip so the two can't drift
        # apart silently.
        family, version = "PIC16F1xxxx_DFP", "1.31.465"
        stem = Path(f"Microchip.{family}.{version}.atpack").stem
        match = packs.PACK_STEM_RE.match(stem)
        self.assertIsNotNone(match)
        self.assertEqual(match.group("family"), family)
        self.assertEqual(match.group("version"), version)


class KnownLocalFamiliesTests(unittest.TestCase):
    def test_reuses_existing_device_discovery_and_keeps_highest_version(self) -> None:
        fake_unpacked = [
            {"device": "PIC16F1509", "pack_family": "PIC12-16F1xxx_DFP", "pack_version": "1.8.254"},
        ]
        fake_atpacks = [
            {"device": "PIC16F1509", "pack_family": "PIC12-16F1xxx_DFP", "pack_version": "1.9.258"},
            {"device": "PIC10F200", "pack_family": "PIC10-12Fxxx_DFP", "pack_version": "1.8.184"},
        ]
        with unittest.mock.patch.object(
            pack_updates.packs, "list_devices_in_unpacked_packs", return_value=fake_unpacked
        ), unittest.mock.patch.object(
            pack_updates.packs, "list_devices_in_atpacks", return_value=fake_atpacks
        ):
            families = pack_updates.known_local_families()
        self.assertEqual(families["PIC12-16F1xxx_DFP"], "1.9.258")  # highest of the two
        self.assertEqual(families["PIC10-12Fxxx_DFP"], "1.8.184")


if __name__ == "__main__":
    unittest.main()
