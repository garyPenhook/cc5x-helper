"""Regression tests for the high+medium audit follow-up fixes.

Each test pins one finding so a future refactor cannot silently reopen it:

* #1 config-block source injection (unsafe pack symbol/state names; ``*/`` in the
  block-comment label).
* #2 malformed/duplicate managed markers must be rejected without deleting user code.
* #3 generated-header truncation: ``atomic_write_text`` must not clobber an existing
  file when encoding fails.
* #4 ``build``/``program`` ``--json`` must keep the JSON contract when process launch
  raises ``OSError``.
* #6 a self-contained runner (no ``{compiler}`` placeholder) must not require the
  compiler file on disk.
* #8 manifest mutation commands must not persist state the validator rejects.
* #9 pack discovery must skip one unreadable entry, not abort.
"""

from __future__ import annotations

import json
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

import cc5x_setcc_native as build
from cc5x_setcc_native import (
    ConfigOption,
    ConfigSymbol,
    atomic_write_text,
    config_lines_for_settings,
    project_build_readiness_errors,
    render_config_block_from_symbols,
    update_managed_block,
)
from cc5x_setcc_native_lib.picmeta import DeviceMetadata
from cc5x_setcc_native_lib.project import default_project_manifest
from cc5x_setcc_native_lib.packs import find_device_in_atpacks, list_devices_in_atpacks


def _symbol(name: str, state: str) -> dict[str, ConfigSymbol]:
    symbol = ConfigSymbol(name=name)
    symbol.add(ConfigOption(register=1, mask=0xFF, name=name, state=state, comment="x"))
    return {name: symbol}


class ConfigInjectionTests(unittest.TestCase):
    def test_safe_symbol_and_state_pass_through(self) -> None:
        lines = config_lines_for_settings(_symbol("FOSC", "INTOSC"), {"FOSC": "INTOSC"})
        self.assertEqual(lines, ["#pragma config FOSC = INTOSC // x"])

    def test_numeric_state_used_by_real_cc5x_headers_passes_through(self) -> None:
        lines = config_lines_for_settings(_symbol("BORV", "19"), {"BORV": "19"})
        self.assertEqual(lines, ["#pragma config BORV = 19 // x"])

    def test_unsafe_symbol_name_is_rejected(self) -> None:
        symbols = _symbol("FOSC\n#pragma config EVIL = ON", "INTOSC")
        with self.assertRaises(SystemExit):
            config_lines_for_settings(symbols, {"FOSC\n#pragma config EVIL = ON": "INTOSC"})

    def test_unsafe_state_value_is_rejected(self) -> None:
        symbols = _symbol("FOSC", "INTOSC // injected")
        with self.assertRaises(SystemExit):
            config_lines_for_settings(symbols, {"FOSC": "INTOSC // injected"})

    def test_source_label_cannot_break_out_of_block_comment(self) -> None:
        rendered = render_config_block_from_symbols(
            source_label="pack */ #pragma config EVIL = ON /*",
            family="5x",
            symbols=_symbol("FOSC", "INTOSC"),
            settings={"FOSC": "INTOSC"},
        )
        label_line = next(line for line in rendered.splitlines() if "Managed by" in line)
        self.assertNotIn("*/", label_line)
        self.assertNotIn("\n#pragma config EVIL", rendered.replace("\\n", ""))


class ManagedMarkerValidationTests(unittest.TestCase):
    BLOCK = (
        "// START_CONFIG_SETCC_5X.\n"
        "#pragma config FOSC = INTOSC\n"
        "// END_CONFIG_SETCC_5X.\n"
    )

    def test_duplicate_start_markers_are_rejected_without_losing_user_code(self) -> None:
        source = (
            "int before;\n"
            "// START_CONFIG_SETCC_5X.\n"
            "int must_survive;\n"
            "// START_CONFIG_SETCC_5X.\n"
            "#pragma config OLD = ON\n"
            "// END_CONFIG_SETCC_5X.\n"
            "int after;\n"
        )
        with self.assertRaisesRegex(ValueError, "expected exactly one of each"):
            update_managed_block(source, self.BLOCK, "5x")
        self.assertIn("int must_survive;", source)

    def test_unmatched_marker_is_rejected(self) -> None:
        source = "int before;\n// START_CONFIG_SETCC_5X.\nint after;\n"
        with self.assertRaisesRegex(ValueError, "expected exactly one of each"):
            update_managed_block(source, self.BLOCK, "5x")

    def test_reversed_marker_pair_is_rejected(self) -> None:
        source = (
            "// END_CONFIG_SETCC_5X.\n"
            "int user_code;\n"
            "// START_CONFIG_SETCC_5X.\n"
        )
        with self.assertRaisesRegex(ValueError, "end marker appears before"):
            update_managed_block(source, self.BLOCK, "5x")


class AtomicWriteTests(unittest.TestCase):
    def test_encode_error_leaves_existing_file_intact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "16F1509.H"
            path.write_text("// original header\n", encoding="latin-1")
            with self.assertRaises(UnicodeEncodeError):
                atomic_write_text(path, "header with emoji \U0001f600\n", encoding="latin-1")
            # The destination must NOT have been truncated by the failed write.
            self.assertEqual(path.read_text(encoding="latin-1"), "// original header\n")
            # No leftover temp files in the directory.
            self.assertEqual([p.name for p in Path(tmpdir).iterdir()], ["16F1509.H"])

    def test_generated_header_write_failure_preserves_existing_header(self) -> None:
        metadata = DeviceMetadata(
            device="PIC16F1509",
            ini_arch="PIC14E",
            ini_procid=None,
            rom_size_words=1,
            banks=1,
            bank_size=1,
            sfr_count=0,
            sfr_field_count=0,
            config_word_count=0,
            config_setting_count=0,
            config_value_count=0,
            config_words=[],
            sfrs=[],
            sfr_fields=[],
            ram_ranges=[],
            common_ranges=[],
            icd_ram_ranges=[],
            pic_summary=None,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "generated_headers" / "16F1509.H"
            header.parent.mkdir()
            header.write_text("// original header\n", encoding="latin-1")
            project = default_project_manifest(
                device="PIC16F1509",
                compiler="CC5X.EXE",
                runner=None,
                main_source="app.c",
            )
            with unittest.mock.patch.object(
                build, "project_metadata", return_value=({}, metadata)
            ), unittest.mock.patch.object(
                build, "render_full_header", return_value="header with emoji \U0001f600\n"
            ):
                with self.assertRaises(UnicodeEncodeError):
                    build.ensure_project_header(root / "setcc-native.json", project)
            self.assertEqual(header.read_text(encoding="latin-1"), "// original header\n")


class BuildJsonLaunchFailureTests(unittest.TestCase):
    def test_finish_build_json_emits_error_payload_on_oserror(self) -> None:
        args = types.SimpleNamespace(dry_run=False, json_diagnostics=True)
        with unittest.mock.patch.object(
            build.subprocess, "run", side_effect=FileNotFoundError("no compiler")
        ):
            with unittest.mock.patch("builtins.print") as printed:
                rc = build._finish_build(["/no/such/CC5X.EXE"], None, args)
        self.assertEqual(rc, 1)
        payload = json.loads("".join(str(c.args[0]) for c in printed.call_args_list))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["kind"], "launch_failed")
        self.assertEqual(payload["diagnostics"], [])
        self.assertIn("no compiler", payload["stderr"])


class ProgramJsonLaunchFailureTests(unittest.TestCase):
    def test_program_json_emits_error_payload_on_oserror(self) -> None:
        args = types.SimpleNamespace(
            action="blank-check",
            ipecmd="/no/such/ipecmd",
            device="PIC16F1509",
            hex=None,
            project=None,
            edition=None,
            tool="PK4",
            release_from_reset=False,
            ipe_arg=[],
            dry_run=False,
            json=True,
        )
        with unittest.mock.patch.object(
            build.subprocess, "run", side_effect=FileNotFoundError("no ipecmd")
        ):
            with unittest.mock.patch("builtins.print") as printed:
                rc = build.cmd_program(args)
        self.assertEqual(rc, 1)
        payload = json.loads("".join(str(c.args[0]) for c in printed.call_args_list))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["kind"], "launch_failed")


class SelfContainedRunnerReadinessTests(unittest.TestCase):
    def _project(self, tmpdir: Path, runner: str | None, compiler: str):
        (tmpdir / "app.c").write_text("void main(){}\n", encoding="latin-1")
        return build.default_project_manifest(
            device="PIC16F1509",
            compiler=compiler,
            runner=runner,
            main_source="app.c",
            config_source="app.c",
            header_mode="generated",
            header_path="gen/16F1509.H",
        )

    def test_self_contained_runner_does_not_require_compiler_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            project = self._project(tmpdir, runner="/bin/sh", compiler="/nonexistent/CC5X.EXE")
            errors = project_build_readiness_errors(tmpdir / "setcc-native.json", project)
            self.assertFalse(any("compiler not found" in e for e in errors), errors)

    def test_placeholder_runner_still_requires_compiler_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            project = self._project(
                tmpdir, runner="wine {compiler}", compiler="/nonexistent/CC5X.EXE"
            )
            errors = project_build_readiness_errors(tmpdir / "setcc-native.json", project)
            self.assertTrue(any("compiler not found" in e for e in errors), errors)

    def test_no_runner_still_requires_compiler_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            project = self._project(tmpdir, runner=None, compiler="/nonexistent/CC5X.EXE")
            errors = project_build_readiness_errors(tmpdir / "setcc-native.json", project)
            self.assertTrue(any("compiler not found" in e for e in errors), errors)


class MutationValidationTests(unittest.TestCase):
    def _write_manifest(self, path: Path) -> None:
        manifest = {
            "version": 1,
            "device": "PIC16F1509",
            "compiler": "/x/CC5X.EXE",
            "runner": None,
            "header": {"mode": "generated", "path": "gen/16F1509.H"},
            "config_source": "app.c",
            "main_source": "app.c",
            "build_options": [],
            "editions": {"production": {"config": {}, "build_options": []}},
        }
        path.write_text(json.dumps(manifest), encoding="utf-8")

    def test_set_config_rejects_empty_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "setcc-native.json"
            self._write_manifest(path)
            before = path.read_text(encoding="utf-8")
            args = types.SimpleNamespace(
                project=str(path), edition="production", set=["FOSC="], remove=None, clear=False
            )
            with self.assertRaises(SystemExit):
                build.cmd_project_set_config(args)
            # The rejected mutation must not have been persisted.
            self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_set_config_accepts_valid_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "setcc-native.json"
            self._write_manifest(path)
            args = types.SimpleNamespace(
                project=str(path),
                edition="production",
                set=["FOSC=INTOSC"],
                remove=None,
                clear=False,
            )
            with unittest.mock.patch("builtins.print"):
                rc = build.cmd_project_set_config(args)
            self.assertEqual(rc, 0)
            self.assertEqual(
                json.loads(path.read_text())["editions"]["production"]["config"],
                {"FOSC": "INTOSC"},
            )

    def test_set_config_allows_valid_edit_despite_preexisting_error(self) -> None:
        # A manifest that loads but already fails validation on an UNRELATED field (an
        # unsupported version) must still accept a valid config edit — only newly-introduced
        # errors block the write, so the tool never locks a user out of repairing a manifest.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "setcc-native.json"
            self._write_manifest(path)
            data = json.loads(path.read_text())
            data["version"] = 2  # loads fine, but validate_project_file rejects it
            path.write_text(json.dumps(data), encoding="utf-8")
            args = types.SimpleNamespace(
                project=str(path),
                edition="production",
                set=["FOSC=INTOSC"],
                remove=None,
                clear=False,
            )
            with unittest.mock.patch("builtins.print"):
                rc = build.cmd_project_set_config(args)
            self.assertEqual(rc, 0)
            written = json.loads(path.read_text())
            self.assertEqual(written["editions"]["production"]["config"], {"FOSC": "INTOSC"})
            self.assertEqual(written["version"], 2)  # pre-existing error left untouched


class PackDiscoveryResilienceTests(unittest.TestCase):
    def test_directory_matching_glob_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            # A directory whose name matches the *.atpack glob must not abort discovery.
            (tmpdir / "Microchip.PIC16Fxxx_DFP.1.0.0.atpack").mkdir()
            self.assertEqual(list_devices_in_atpacks([tmpdir]), [])

    def test_find_device_skips_directory_matching_glob(self) -> None:
        # The sibling resolver must be equally resilient: a directory matching the glob
        # raises IsADirectoryError (an OSError), which must be skipped, not abort resolution.
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            (tmpdir / "Microchip.PIC16Fxxx_DFP.1.0.0.atpack").mkdir()
            result = find_device_in_atpacks("PIC16F1509", [tmpdir])
            self.assertEqual(result["device"], "PIC16F1509")
            self.assertIsNone(result["pack_root"])


if __name__ == "__main__":
    unittest.main()
