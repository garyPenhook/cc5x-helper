from __future__ import annotations

import json
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

import cc5x_setcc_native as build
import validate_generated_headers as validate_headers


class HeaderDefinesChipTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, text: str) -> Path:
        path = self.dir / "device.H"
        path.write_text(text, encoding="latin-1")
        return path

    def test_detects_pragma_chip(self) -> None:
        path = self._write("// HEADER FILE\n#pragma chip PIC16F1509, core 14 enh\n")
        self.assertTrue(build.header_defines_chip(path))

    def test_detects_indented_and_spaced_pragma(self) -> None:
        # CC5X accepts whitespace variation; the detector must too.
        path = self._write("   #  pragma   chip  PIC10F200\n")
        self.assertTrue(build.header_defines_chip(path))

    def test_header_without_chip(self) -> None:
        path = self._write("#define FOO 1\nbit D1N @ 0x10.0;\n")
        self.assertFalse(build.header_defines_chip(path))

    def test_pragma_config_is_not_pragma_chip(self) -> None:
        # `#pragma config` must not be mistaken for a chip declaration.
        path = self._write("#pragma config FOSC = INTOSC\n")
        self.assertFalse(build.header_defines_chip(path))

    def test_missing_file_returns_false(self) -> None:
        self.assertFalse(build.header_defines_chip(self.dir / "absent.H"))


class HeaderChipNameTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, text: str) -> Path:
        path = self.dir / "device.H"
        path.write_text(text, encoding="latin-1")
        return path

    def test_extracts_chip_name(self) -> None:
        path = self._write("#pragma chip PIC16F1509, core 14 enh\n")
        self.assertEqual(build.header_chip_name(path), "PIC16F1509")

    def test_returns_none_without_pragma_chip(self) -> None:
        path = self._write("#define FOO 1\n")
        self.assertIsNone(build.header_chip_name(path))

    def test_returns_none_on_missing_file(self) -> None:
        self.assertIsNone(build.header_chip_name(self.dir / "absent.H"))


class BuildOptionsForProjectTests(unittest.TestCase):
    """Audit A1/A4: the shared build-arg builder used by both the CLI and the GUI."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _project(self, device: str = "PIC16F1509", base: list[str] | None = None) -> object:
        return types.SimpleNamespace(device=device, base_build_options=base or [])

    def _edition(self, options: list[str] | None = None) -> object:
        return types.SimpleNamespace(build_options=options or [])

    def _header(self, text: str) -> Path:
        path = self.dir / "device.H"
        path.write_text(text, encoding="latin-1")
        return path

    def test_adds_p_flag_when_header_has_no_chip(self) -> None:
        header = self._header("#define FOO 1\n")
        options = build.build_options_for_project(self._project(), self._edition(), header)
        self.assertEqual(options[0], "-p16F1509")
        self.assertIn(f"-I{header.parent}", options)

    def test_omits_p_flag_when_header_defines_matching_chip(self) -> None:
        header = self._header("#pragma chip PIC16F1509, core 14 enh\n")
        options = build.build_options_for_project(self._project(), self._edition(), header)
        self.assertFalse(any(opt.startswith("-p") for opt in options))

    def test_rejects_chip_device_mismatch(self) -> None:
        header = self._header("#pragma chip PIC16F1939\n")
        with self.assertRaises(SystemExit):
            build.build_options_for_project(self._project("PIC16F1509"), self._edition(), header)

    def test_appends_base_then_edition_options(self) -> None:
        header = self._header("#pragma chip PIC16F1509\n")
        options = build.build_options_for_project(
            self._project(base=["-a"]), self._edition(["-b"]), header
        )
        self.assertEqual(options[-2:], ["-a", "-b"])


class ResolveSuppliedHeaderTests(unittest.TestCase):
    """Audit A7: supplied-header lookup must not depend on filename casing."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_exact_uppercase_name(self) -> None:
        (self.dir / "16F1509.H").write_text("", encoding="latin-1")
        self.assertEqual(
            build._resolve_supplied_header(self.dir, "16F1509"), self.dir / "16F1509.H"
        )

    def test_case_insensitive_fallback(self) -> None:
        (self.dir / "16f1509.h").write_text("", encoding="latin-1")
        resolved = build._resolve_supplied_header(self.dir, "16F1509")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.name, "16f1509.h")

    def test_missing_returns_none(self) -> None:
        self.assertIsNone(build._resolve_supplied_header(self.dir, "16F1509"))


class EnsureProjectHeaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _project(self, header_path: str, compiler: str | None = None) -> object:
        return types.SimpleNamespace(
            device="PIC16F1509",
            header_mode="supplied",
            header_path=header_path,
            compiler=compiler or str(self.dir / "compiler" / "CC5X.EXE"),
        )

    def test_supplied_mode_uses_explicit_manifest_header_path(self) -> None:
        manifest_header = self.dir / "include" / "16F1509.H"
        compiler_header = self.dir / "compiler" / "16F1509.H"
        manifest_header.parent.mkdir()
        compiler_header.parent.mkdir()
        manifest_header.write_text("#pragma chip PIC16F1509\n", encoding="latin-1")
        compiler_header.write_text("#pragma chip PIC16F1939\n", encoding="latin-1")

        resolved = build.ensure_project_header(
            self.dir / "setcc-native.json",
            self._project("include/16F1509.H"),
        )
        self.assertEqual(resolved, manifest_header.resolve())

    def test_supplied_mode_bare_filename_uses_project_compiler_directory(self) -> None:
        compiler = self.dir / "custom-cc5x" / "CC5X.EXE"
        header = compiler.parent / "16f1509.h"
        header.parent.mkdir()
        header.write_text("#pragma chip PIC16F1509\n", encoding="latin-1")

        resolved = build.ensure_project_header(
            self.dir / "setcc-native.json",
            self._project("16F1509.H", compiler=str(compiler)),
        )
        self.assertEqual(resolved, header)


class ProjectBuildReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.compiler = self.dir / "CC5X.EXE"
        self.compiler.write_text("", encoding="ascii")
        self.manifest = self.dir / "setcc-native.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_manifest(self, main_source: str = "app.c") -> None:
        manifest = {
            "version": 1,
            "device": "PIC16F1509",
            "compiler": str(self.compiler),
            "runner": None,
            "header": {"mode": "generated", "path": "gen/16F1509.H"},
            "config_source": main_source,
            "main_source": main_source,
            "build_options": [],
            "editions": {"production": {"config": {}, "build_options": []}},
        }
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")

    def test_project_validate_reports_missing_main_source(self) -> None:
        self._write_manifest()
        with unittest.mock.patch("builtins.print") as printed:
            rc = build.cmd_project_validate(types.SimpleNamespace(project=str(self.manifest), json=True))
        self.assertEqual(rc, 1)
        payload = json.loads("".join(str(call.args[0]) for call in printed.call_args_list))
        self.assertTrue(any("main_source not found" in item for item in payload["build_errors"]))

    def test_project_build_refuses_missing_main_source_before_subprocess(self) -> None:
        self._write_manifest()
        args = types.SimpleNamespace(project=str(self.manifest), edition="production", dry_run=False)
        with unittest.mock.patch.object(build.subprocess, "run") as run:
            with self.assertRaises(SystemExit):
                build.cmd_build(args)
        run.assert_not_called()


class ProjectGenerateHeaderTests(unittest.TestCase):
    """`project-generate-header` writes the generated header and refuses other modes."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.compiler = self.dir / "CC5X.EXE"
        self.compiler.write_text("", encoding="ascii")
        self.manifest = self.dir / "setcc-native.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_manifest(self, mode: str = "generated") -> None:
        manifest = {
            "version": 1,
            "device": "PIC16F1509",
            "compiler": str(self.compiler),
            "runner": None,
            "header": {"mode": mode, "path": "gen/16F1509.H"},
            "config_source": "app.c",
            "main_source": "app.c",
            "build_options": [],
            "editions": {"production": {"config": {}, "build_options": []}},
        }
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")

    def _run(self) -> tuple[int, dict]:
        args = types.SimpleNamespace(project=str(self.manifest), json=True)
        with unittest.mock.patch("builtins.print") as printed:
            rc = build.cmd_project_generate_header(args)
        payload = json.loads("".join(str(call.args[0]) for call in printed.call_args_list))
        return rc, payload

    def test_generates_header_for_generated_mode(self) -> None:
        self._write_manifest("generated")
        # Avoid touching real packs: stub metadata lookup + header render.
        with unittest.mock.patch.object(build, "project_metadata", return_value=(None, object())), \
                unittest.mock.patch.object(
                    build, "render_full_header", return_value="#pragma chip PIC16F1509\n"
                ):
            rc, payload = self._run()
        header = self.dir / "gen" / "16F1509.H"
        self.assertEqual(rc, 0)
        self.assertTrue(payload["ok"])
        self.assertTrue(header.is_file())
        self.assertEqual(Path(payload["header"]), header.resolve())
        self.assertIn("#pragma chip PIC16F1509", header.read_text(encoding="latin-1"))

    def test_refuses_non_generated_mode(self) -> None:
        self._write_manifest("existing")
        rc, payload = self._run()
        self.assertEqual(rc, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["kind"], "not_generated_mode")

    def test_reports_generate_failed_on_metadata_error(self) -> None:
        # A device absent from / malformed in the packs raises ValueError / xml ParseError /
        # zipfile.BadZipFile from project_metadata — none an OSError. The JSON contract must
        # still surface a structured {ok:false, generate_failed} error, not a traceback.
        self._write_manifest("generated")
        with unittest.mock.patch.object(
            build, "project_metadata", side_effect=ValueError("malformed pack")
        ):
            rc, payload = self._run()
        self.assertEqual(rc, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["kind"], "generate_failed")
        self.assertIn("malformed pack", payload["error"]["message"])


class FindDeviceMetadataTests(unittest.TestCase):
    def _result(self, version: str, root: str) -> dict[str, str | None]:
        return {
            "device": "PIC16F1509",
            "pack_family": "PIC16F1xxxx_DFP",
            "pack_version": version,
            "pack_root": root,
            "pic": f"{root}/edc/PIC16F1509.PIC",
            "atdf": None,
            "cfgdata": None,
            "pdsc": None,
            "ini": None,
        }

    def test_uses_highest_version_across_atpack_and_unpacked_packs(self) -> None:
        with unittest.mock.patch.object(build, "find_device_in_atpacks", return_value=self._result("1.0.0", "old.atpack")), \
             unittest.mock.patch.object(build, "find_device_in_unpacked_packs", return_value=self._result("2.0.0", "new")), \
             unittest.mock.patch.object(build, "discover_mplab_roots", return_value=[]):
            result = build.find_device_metadata("PIC16F1509", None)
        self.assertEqual(result["pack_version"], "2.0.0")
        self.assertEqual(result["pack_root"], "new")


class StripJsoncTests(unittest.TestCase):
    """Audit A3: tasks.json is JSONC; json.loads needs comments/trailing commas removed."""

    def test_strips_line_and_block_comments(self) -> None:
        text = '{\n  // a comment\n  "a": 1, /* inline */ "b": 2\n}'
        self.assertEqual(json.loads(build.strip_jsonc(text)), {"a": 1, "b": 2})

    def test_strips_trailing_commas(self) -> None:
        text = '{\n  "tasks": [\n    {"label": "x"},\n  ],\n}'
        self.assertEqual(json.loads(build.strip_jsonc(text)), {"tasks": [{"label": "x"}]})

    def test_preserves_slashes_inside_strings(self) -> None:
        text = '{"url": "http://example.com", "p": "a//b"}'
        self.assertEqual(
            json.loads(build.strip_jsonc(text)), {"url": "http://example.com", "p": "a//b"}
        )

    def test_trailing_comma_with_comment_before_close(self) -> None:
        # VS Code writes `[ {...}, /* note */ ]`; the comma is still trailing once the
        # comment is gone. strip_jsonc must remove comments before scanning for it.
        text = '{"tasks": [ {"a": 1}, /* trailing */ ]}'
        self.assertEqual(json.loads(build.strip_jsonc(text)), {"tasks": [{"a": 1}]})

    def test_trailing_comma_with_line_comment_before_close(self) -> None:
        text = '{\n  "tasks": [\n    {"a": 1}, // last\n  ]\n}'
        self.assertEqual(json.loads(build.strip_jsonc(text)), {"tasks": [{"a": 1}]})


class VscodeTasksMergeTests(unittest.TestCase):
    """Audit A3/A5: merge preserves user tasks even when tasks.json is JSONC."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.manifest = self.dir / "setcc-native.json"
        manifest = {
            "version": 1,
            "device": "PIC16F1509",
            "compiler": "CC5X.EXE",
            "runner": None,
            "header": {"mode": "generated", "path": "gen/16F1509.H"},
            "config_source": "main.c",
            "main_source": "main.c",
            "build_options": [],
            "editions": {"production": {"config": {}, "build_options": []}},
        }
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        self.output = self.dir / ".vscode" / "tasks.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _args(self, **kw: object) -> object:
        defaults = dict(
            project=str(self.manifest),
            manifest="setcc-native.json",
            python="python3",
            helper="tools/cc5x_setcc_native.py",
            tool="PK4",
            problem_matcher=None,
            output=str(self.output),
            stdout=False,
            force=False,
        )
        defaults.update(kw)
        return types.SimpleNamespace(**defaults)

    def test_preserves_user_task_in_jsonc(self) -> None:
        self.output.parent.mkdir(parents=True)
        self.output.write_text(
            '{\n  // user tasks below\n  "version": "2.0.0",\n'
            '  "tasks": [\n    {"label": "My Flash", "type": "shell"},\n  ]\n}',
            encoding="utf-8",
        )
        rc = build.cmd_vscode_tasks(self._args())
        self.assertEqual(rc, 0)
        document = json.loads(self.output.read_text(encoding="utf-8"))
        labels = [t["label"] for t in document["tasks"]]
        self.assertIn("My Flash", labels)
        self.assertIn("CC5X: Build production", labels)

    def test_unparseable_without_force_aborts(self) -> None:
        self.output.parent.mkdir(parents=True)
        self.output.write_text("{ not json at all ", encoding="utf-8")
        with self.assertRaises(SystemExit):
            build.cmd_vscode_tasks(self._args())

    def test_force_backs_up_unparseable(self) -> None:
        self.output.parent.mkdir(parents=True)
        self.output.write_text("{ not json at all ", encoding="utf-8")
        rc = build.cmd_vscode_tasks(self._args(force=True))
        self.assertEqual(rc, 0)
        backup = self.output.with_name(self.output.name + ".bak")
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_text(encoding="utf-8"), "{ not json at all ")


class ProgramJsonCaptureTests(unittest.TestCase):
    """Audit A2: `program --json` must emit pure JSON with IPECMD output captured."""

    def test_json_payload_captures_subprocess_output(self) -> None:
        fake = types.SimpleNamespace(returncode=0, stdout="flash ok\n", stderr="")
        args = types.SimpleNamespace(
            action="program",
            project=None,
            edition=None,
            device="PIC16F1509",
            hex=None,
            tool="PK4",
            ipecmd="/fake/ipecmd.sh",
            release_from_reset=False,
            ipe_arg=None,
            dry_run=False,
            json=True,
        )
        # program needs an image that exists; point at a real temp file.
        with tempfile.TemporaryDirectory() as tmp:
            hexfile = Path(tmp) / "app.hex"
            hexfile.write_text(":00000001FF\n", encoding="ascii")
            args.hex = str(hexfile)
            with unittest.mock.patch.object(build.subprocess, "run", return_value=fake) as run, \
                 unittest.mock.patch("builtins.print") as printed:
                rc = build.cmd_program(args)
        self.assertEqual(rc, 0)
        run.assert_called_once()
        # Captured (not inherited) so stdout stays pure JSON.
        self.assertTrue(run.call_args.kwargs.get("capture_output"))
        printed_text = "".join(str(c.args[0]) for c in printed.call_args_list if c.args)
        payload = json.loads(printed_text)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["stdout"], "flash ok\n")


class DeviceShortNameTests(unittest.TestCase):
    def test_strips_pic_prefix(self) -> None:
        self.assertEqual(build.device_short_name("PIC16F1509"), "16F1509")
        self.assertEqual(build.device_short_name("16f1509"), "16F1509")

    def test_passes_through_non_pic(self) -> None:
        self.assertEqual(build.device_short_name("ATSAML11E16A"), "ATSAML11E16A")


class IpecmdCommandTests(unittest.TestCase):
    IPECMD = Path("ipecmd.sh")
    HEX = Path("app.hex")

    def test_program_command(self) -> None:
        cmd = build.ipecmd_command(self.IPECMD, "PIC16F1509", "PK4", "program", self.HEX)
        self.assertEqual(
            cmd, [str(self.IPECMD), "-P16F1509", "-TPPK4", f"-F{self.HEX}", "-M"]
        )

    def test_verify_command(self) -> None:
        cmd = build.ipecmd_command(self.IPECMD, "PIC16F1509", "PK5", "verify", self.HEX)
        self.assertEqual(cmd[-2:], [f"-F{self.HEX}", "-Y"])
        self.assertIn("-TPPK5", cmd)

    def test_erase_command_has_no_image(self) -> None:
        cmd = build.ipecmd_command(self.IPECMD, "PIC16F1509", "PK4", "erase", None)
        self.assertEqual(cmd, [str(self.IPECMD), "-P16F1509", "-TPPK4", "-E"])

    def test_blank_check_command(self) -> None:
        cmd = build.ipecmd_command(self.IPECMD, "PIC10F200", "PK4", "blank-check", None)
        self.assertEqual(cmd[-1], "-C")

    def test_program_requires_image(self) -> None:
        with self.assertRaises(SystemExit):
            build.ipecmd_command(self.IPECMD, "PIC16F1509", "PK4", "program", None)

    def test_release_from_reset_and_extra_args(self) -> None:
        cmd = build.ipecmd_command(
            self.IPECMD,
            "PIC16F1509",
            "PK4",
            "program",
            self.HEX,
            release_from_reset=True,
            extra_args=["-W2.5"],
        )
        self.assertEqual(cmd[-2:], ["-OL", "-W2.5"])

    def test_unknown_action_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            build.ipecmd_command(self.IPECMD, "PIC16F1509", "PK4", "read", None)


class GenerateVscodeTasksTests(unittest.TestCase):
    def _project(self) -> object:
        import types

        return types.SimpleNamespace(editions={"production": {}, "debug": {}})

    def _tasks(self, **kwargs: object) -> list[dict]:
        defaults = dict(
            project=self._project(),
            manifest="setcc-native.json",
            python="python3",
            helper="tools/cc5x_setcc_native.py",
            tool="PK4",
            problem_matcher=[],
        )
        defaults.update(kwargs)
        return build.generate_vscode_tasks(**defaults)

    def test_task_count_and_labels(self) -> None:
        tasks = self._tasks()
        labels = [t["label"] for t in tasks]
        # 3 per edition (build, program, verify) + erase + blank-check
        self.assertEqual(len(tasks), 2 * 3 + 2)
        self.assertTrue(all(label.startswith("CC5X:") for label in labels))
        self.assertIn("CC5X: Erase (PK4)", labels)
        self.assertIn("CC5X: Blank Check (PK4)", labels)

    def test_first_build_is_default(self) -> None:
        tasks = self._tasks()
        builds = [t for t in tasks if t["label"].startswith("CC5X: Build")]
        self.assertEqual(builds[0]["group"], {"kind": "build", "isDefault": True})
        self.assertEqual(builds[1]["group"], {"kind": "build", "isDefault": False})

    def test_program_depends_on_its_build(self) -> None:
        tasks = self._tasks()
        program = next(t for t in tasks if t["label"] == "CC5X: Program debug (PK4)")
        self.assertEqual(program["dependsOn"], "CC5X: Build debug")
        self.assertEqual(program["dependsOrder"], "sequence")

    def test_process_type_and_args_threading(self) -> None:
        tasks = self._tasks(tool="PK5")
        program = next(t for t in tasks if t["label"] == "CC5X: Program production (PK5)")
        self.assertEqual(program["type"], "process")
        self.assertEqual(program["command"], "python3")
        self.assertEqual(
            program["args"],
            [
                "tools/cc5x_setcc_native.py", "program", "--project", "setcc-native.json",
                "--edition", "production", "--tool", "PK5",
            ],
        )

    def test_problem_matcher_passthrough(self) -> None:
        tasks = self._tasks(problem_matcher="$cc5x")
        build_task = next(t for t in tasks if t["label"] == "CC5X: Build production")
        self.assertEqual(build_task["problemMatcher"], "$cc5x")


class ValidateGeneratedHeadersTests(unittest.TestCase):
    def test_compile_result_reports_actual_header_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "16f1509_gen.c"
            header = root / "16F1509.H"
            source.write_text('#include "16F1509.H"\nvoid main(void) {}\n', encoding="ascii")
            header.write_text("#pragma chip PIC16F1509\n", encoding="latin-1")
            source.with_suffix(".hex").write_text(":00000001FF\n", encoding="ascii")
            fake = types.SimpleNamespace(returncode=0, stdout="", stderr="")
            with unittest.mock.patch.object(validate_headers.subprocess, "run", return_value=fake):
                result = validate_headers.run_compile(
                    runner=Path("runner"),
                    include_dir=root,
                    source_path=source,
                    header_path=header,
                    label="generated",
                    device="PIC16F1509",
                )
        self.assertEqual(result.header_path, str(header))


if __name__ == "__main__":
    unittest.main()
