"""Phase 1: unit tests for each JSON command's output payload.

The extension/GUI consume these `--json` contracts, so each read command's success
payload (shape, keys, exit code) is pinned here. The data sources that need real packs
(`environment_report`, `merged_device_list`, the device-metadata pipeline) are mocked so
the tests are deterministic and run in CI without an MPLAB X install. The error contract
for these commands is covered separately by `JsonErrorBoundaryTests` in test_build.py.
"""

from __future__ import annotations

import json
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

import cc5x_setcc_native as build

from test_golden import build_synthetic_metadata


def _run(handler, args: types.SimpleNamespace):
    """Invoke a cmd_* handler, returning (exit_code, parsed_json_payload)."""
    with unittest.mock.patch("builtins.print") as printed:
        rc = handler(args)
    text = "".join(str(call.args[0]) if call.args else "" for call in printed.call_args_list)
    payload = json.loads(text) if text.strip() else None
    return rc, payload


def _write_manifest(path: Path, *, editions: dict | None = None) -> None:
    manifest = {
        "version": 1,
        "device": "PIC16F1509",
        "compiler": "/x/CC5X.EXE",
        "runner": "wine {compiler}",
        "mplab_root": None,
        "header": {"mode": "generated", "path": "gen/16F1509.H"},
        "config_source": "app.c",
        "main_source": "app.c",
        "build_options": ["-a"],
        "editions": editions
        or {
            "debug": {"config": {"FOSC": "INTOSC"}, "build_options": ["-g"]},
            "production": {"config": {}, "build_options": []},
        },
    }
    path.write_text(json.dumps(manifest), encoding="utf-8")


class DoctorJsonTests(unittest.TestCase):
    def test_ready_report_exits_zero(self) -> None:
        report = {"ready": True, "runner": "wine {compiler}", "compiler": "/x/CC5X.EXE"}
        with unittest.mock.patch.object(build, "environment_report", return_value=report):
            rc, payload = _run(build.cmd_doctor, types.SimpleNamespace(json=True))
        self.assertEqual(rc, 0)
        self.assertEqual(payload, report)

    def test_not_ready_report_exits_one(self) -> None:
        report = {"ready": False, "runner": None, "compiler": None}
        with unittest.mock.patch.object(build, "environment_report", return_value=report):
            rc, payload = _run(build.cmd_doctor, types.SimpleNamespace(json=True))
        self.assertEqual(rc, 1)
        self.assertFalse(payload["ready"])


class ListDevicesJsonTests(unittest.TestCase):
    def test_default_families_map_to_pic_prefixes(self) -> None:
        devices = [{"device": "PIC16F1509", "pack_family": "PIC16Fxxx", "pack_version": "1.0"}]
        with unittest.mock.patch.object(
            build, "merged_device_list", return_value=devices
        ) as merged:
            rc, payload = _run(
                build.cmd_list_devices, types.SimpleNamespace(json=True, family=None)
            )
        self.assertEqual(rc, 0)
        self.assertEqual(payload, devices)
        merged.assert_called_once_with(("PIC10F", "PIC12F", "PIC16F"))

    def test_explicit_family_is_prefixed_once(self) -> None:
        with unittest.mock.patch.object(
            build, "merged_device_list", return_value=[]
        ) as merged:
            rc, payload = _run(
                build.cmd_list_devices,
                types.SimpleNamespace(json=True, family=["16F", "PIC10F"]),
            )
        # A bare family gains the PIC prefix; an already-prefixed one is left intact.
        merged.assert_called_once_with(("PIC16F", "PIC10F"))
        self.assertEqual(payload, [])
        self.assertEqual(rc, 0)  # --json always exits 0; the empty-list exit 1 is human-mode


class ProjectShowJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.manifest = Path(self._tmp.name) / "setcc-native.json"
        _write_manifest(self.manifest)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_full_summary_payload(self) -> None:
        args = types.SimpleNamespace(project=str(self.manifest), json=True, edition=None)
        rc, payload = _run(build.cmd_project_show, args)
        self.assertEqual(rc, 0)
        self.assertEqual(payload["device"], "PIC16F1509")
        self.assertEqual(payload["header"], {"mode": "generated", "path": "gen/16F1509.H"})
        self.assertEqual(payload["build_options"], ["-a"])
        self.assertEqual(set(payload["editions"]), {"debug", "production"})
        self.assertEqual(payload["editions"]["debug"]["config"], {"FOSC": "INTOSC"})

    def test_single_edition_payload(self) -> None:
        args = types.SimpleNamespace(project=str(self.manifest), json=True, edition="debug")
        rc, payload = _run(build.cmd_project_show, args)
        self.assertEqual(rc, 0)
        self.assertEqual(
            payload, {"name": "debug", "config": {"FOSC": "INTOSC"}, "build_options": ["-g"]}
        )

    def test_unknown_edition_raises(self) -> None:
        args = types.SimpleNamespace(project=str(self.manifest), json=True, edition="nope")
        with self.assertRaises(SystemExit):
            build.cmd_project_show(args)


class ProjectListEditionsJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.manifest = Path(self._tmp.name) / "setcc-native.json"
        _write_manifest(self.manifest)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_editions_payload_counts(self) -> None:
        args = types.SimpleNamespace(project=str(self.manifest), json=True)
        rc, payload = _run(build.cmd_project_list_editions, args)
        self.assertEqual(rc, 0)
        # Sorted by name; counts reflect the manifest.
        self.assertEqual(
            payload,
            [
                {"name": "debug", "config_count": 1, "build_option_count": 1},
                {"name": "production", "config_count": 0, "build_option_count": 0},
            ],
        )


class ListPackConfigJsonTests(unittest.TestCase):
    def test_pack_config_payload_shape(self) -> None:
        metadata = build_synthetic_metadata()
        args = types.SimpleNamespace(
            device="PIC16FSYN1", mplab_root=None, json=True
        )
        with unittest.mock.patch.object(
            build, "find_device_metadata", return_value={"device": "PIC16FSYN1"}
        ), unittest.mock.patch.object(
            build, "load_device_metadata", return_value=metadata
        ):
            rc, payload = _run(build.cmd_list_pack_config, args)
        self.assertEqual(rc, 0)
        # Synthetic device exposes FOSC and BBSIZE config symbols.
        self.assertEqual(set(payload), {"FOSC", "BBSIZE"})
        fosc = payload["FOSC"]
        self.assertTrue(all({"register", "mask", "state", "comment"} <= set(o) for o in fosc))
        self.assertTrue(all(o["mask"].startswith("0x") for o in fosc))


class ListConfigJsonTests(unittest.TestCase):
    def test_parses_header_pragmas_into_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            header = Path(tmp) / "dev.h"
            header.write_text(
                "// HEADER FILE\n"
                "#pragma config /1 0x3FFC FOSC = INTOSC // internal oscillator\n"
                "#pragma config /1 0x3FFF FOSC = LP\n"
                "#pragma config /2 0x00FF WDTE = OFF // watchdog off\n",
                encoding="latin-1",
            )
            args = types.SimpleNamespace(header=str(header), json=True)
            rc, payload = _run(build.cmd_list_config, args)
        self.assertEqual(rc, 0)
        self.assertEqual(set(payload), {"FOSC", "WDTE"})
        # Two FOSC states, sorted by (register, state); masks rendered as 0x-hex.
        self.assertEqual([o["state"] for o in payload["FOSC"]], ["INTOSC", "LP"])
        self.assertEqual(payload["FOSC"][0]["mask"], "0x3FFC")
        self.assertEqual(payload["FOSC"][0]["comment"], "internal oscillator")
        self.assertEqual(payload["WDTE"][0]["register"], 2)


class DescribeDeviceJsonTests(unittest.TestCase):
    def test_describe_emits_metadata_json(self) -> None:
        metadata = build_synthetic_metadata()
        args = types.SimpleNamespace(device="PIC16FSYN1", mplab_root=None)
        with unittest.mock.patch.object(
            build, "find_device_metadata", return_value={"device": "PIC16FSYN1"}
        ), unittest.mock.patch.object(
            build, "load_device_metadata", return_value=metadata
        ):
            rc, payload = _run(build.cmd_describe_device, args)
        self.assertEqual(rc, 0)
        self.assertEqual(payload["device"], "PIC16FSYN1")
        self.assertEqual(payload["ini_arch"], "PIC14E")


if __name__ == "__main__":
    unittest.main()
