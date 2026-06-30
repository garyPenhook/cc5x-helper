from __future__ import annotations

import json
import os
import subprocess
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

    def test_project_validate_reports_generated_header_failure(self) -> None:
        self._write_manifest()
        (self.dir / "app.c").write_text("void main(void){}\n", encoding="ascii")
        with unittest.mock.patch.object(
            build,
            "render_project_generated_header",
            side_effect=SystemExit("cannot generate header for PIC16F1509: unsupported architecture (none)"),
        ):
            with unittest.mock.patch("builtins.print") as printed:
                rc = build.cmd_project_validate(
                    types.SimpleNamespace(project=str(self.manifest), json=True)
                )
        self.assertEqual(rc, 1)
        payload = json.loads("".join(str(call.args[0]) for call in printed.call_args_list))
        self.assertTrue(
            any("cannot generate header for PIC16F1509" in item for item in payload["build_errors"])
        )
        self.assertFalse((self.dir / "gen" / "16F1509.H").exists())

    def test_project_build_refuses_missing_main_source_before_subprocess(self) -> None:
        self._write_manifest()
        args = types.SimpleNamespace(project=str(self.manifest), edition="production", dry_run=False)
        with unittest.mock.patch.object(build.subprocess, "run") as run:
            with self.assertRaises(SystemExit):
                build.cmd_build(args)
        run.assert_not_called()


class UpdateManagedBlockTests(unittest.TestCase):
    """sync-config must replace only the managed block, never preceding user code."""

    BLOCK = (
        "/*\n"
        " * Managed by cc5x_setcc_native.py from new\n"
        " * Update this block with the tool instead of editing it manually.\n"
        " */\n"
        "// START_CONFIG_SETCC_5X.\n"
        "#pragma config NEW = OFF\n"
        "// END_CONFIG_SETCC_5X.\n"
    )

    def test_replacing_block_preserves_code_before_and_after(self) -> None:
        src = (
            "int before;\n"
            "int keep_me = 2;\n\n"
            "/*\n"
            " * Managed by cc5x_setcc_native.py from old\n"
            " */\n"
            "// START_CONFIG_SETCC_5X.\n"
            "#pragma config OLD = ON\n"
            "// END_CONFIG_SETCC_5X.\n\n"
            "int after;\n"
        )
        updated, replaced = build.update_managed_block(src, self.BLOCK, "5x")
        self.assertTrue(replaced)
        # Regression: the old `^.*?//START` anchor deleted everything before the marker.
        self.assertIn("int before;", updated)
        self.assertIn("int keep_me = 2;", updated)
        self.assertIn("int after;", updated)
        self.assertIn("#pragma config NEW = OFF", updated)
        self.assertNotIn("#pragma config OLD = ON", updated)
        # Exactly one managed block remains.
        self.assertEqual(updated.count("// START_CONFIG_SETCC_5X."), 1)

    def test_replaces_old_block_without_preamble(self) -> None:
        src = (
            "int before;\n"
            "// START_CONFIG_SETCC_5X.\n"
            "#pragma config OLD = ON\n"
            "// END_CONFIG_SETCC_5X.\n"
            "int after;\n"
        )
        updated, replaced = build.update_managed_block(src, self.BLOCK, "5x")
        self.assertTrue(replaced)
        self.assertIn("int before;", updated)
        self.assertIn("int after;", updated)
        self.assertEqual(updated.count("// START_CONFIG_SETCC_5X."), 1)

    def test_does_not_consume_user_comment_above_block(self) -> None:
        src = (
            "int x;\n"
            "/* my own note */\n"
            "/*\n"
            " * Managed by cc5x_setcc_native.py from old\n"
            " */\n"
            "// START_CONFIG_SETCC_5X.\n"
            "#pragma config OLD = ON\n"
            "// END_CONFIG_SETCC_5X.\n"
        )
        updated, _ = build.update_managed_block(src, self.BLOCK, "5x")
        self.assertIn("/* my own note */", updated)
        self.assertIn("int x;", updated)

    def test_appends_when_no_block_present(self) -> None:
        src = "int only;\n"
        updated, replaced = build.update_managed_block(src, self.BLOCK, "5x")
        self.assertFalse(replaced)
        self.assertIn("int only;", updated)
        self.assertIn("// START_CONFIG_SETCC_5X.", updated)

    def test_re_sync_is_idempotent(self) -> None:
        src = "int before;\n" + self.BLOCK + "int after;\n"
        once, _ = build.update_managed_block(src, self.BLOCK, "5x")
        twice, _ = build.update_managed_block(once, self.BLOCK, "5x")
        self.assertEqual(once, twice)
        self.assertEqual(twice.count("// START_CONFIG_SETCC_5X."), 1)


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

    def test_reports_generate_failed_on_unsupported_architecture(self) -> None:
        # Audit #6: headergen now raises ValueError for an unmapped arch; ensure_project_header
        # converts it to a clean build-stopping error that the JSON contract reports.
        self._write_manifest("generated")
        with unittest.mock.patch.object(
            build, "project_metadata", return_value=(None, object())
        ), unittest.mock.patch.object(
            build, "render_full_header", side_effect=ValueError("unsupported architecture")
        ):
            rc, payload = self._run()
        self.assertEqual(rc, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["kind"], "generate_failed")
        self.assertIn("cannot generate header for PIC16F1509", payload["error"]["message"])
        self.assertIn("unsupported architecture", payload["error"]["message"])


def _config_symbol(name: str, *states: str) -> "build.ConfigSymbol":
    symbol = build.ConfigSymbol(name=name)
    for state in states:
        symbol.add(build.ConfigOption(register=1, mask=1, name=name, state=state))
    return symbol


class JsonErrorBoundaryTests(unittest.TestCase):
    """Phase 1: every --json command emits {ok:false,error} on failure, not a traceback."""

    def _wrapped(self, fn):
        return build.json_error_boundary(fn)

    def test_systemexit_message_becomes_json_error(self) -> None:
        def boom(args):
            raise SystemExit("kaboom")
        args = types.SimpleNamespace(json=True)
        with unittest.mock.patch("builtins.print") as printed:
            rc = self._wrapped(boom)(args)
        self.assertEqual(rc, 1)
        payload = json.loads("".join(str(c.args[0]) for c in printed.call_args_list if c.args))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["message"], "kaboom")

    def test_generic_exception_becomes_json_error(self) -> None:
        def boom(args):
            raise ValueError("bad input")
        with unittest.mock.patch("builtins.print") as printed:
            rc = self._wrapped(boom)(types.SimpleNamespace(json=True))
        self.assertEqual(rc, 1)
        payload = json.loads("".join(str(c.args[0]) for c in printed.call_args_list if c.args))
        self.assertEqual(payload["error"]["kind"], "command_failed")

    def test_int_systemexit_passes_through(self) -> None:
        def boom(args):
            raise SystemExit(2)  # argparse-style numeric exit
        with self.assertRaises(SystemExit) as ctx:
            self._wrapped(boom)(types.SimpleNamespace(json=True))
        self.assertEqual(ctx.exception.code, 2)

    def test_non_json_mode_propagates(self) -> None:
        def boom(args):
            raise SystemExit("text mode error")
        with self.assertRaises(SystemExit):
            self._wrapped(boom)(types.SimpleNamespace(json=False))


class CollectArtifactsTests(unittest.TestCase):
    """Phase 1: `artifacts --json` enumerates CC5X build outputs (newest first)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_finds_artifact_extensions_and_skips_others(self) -> None:
        (self.dir / "main.hex").write_text("x")
        (self.dir / "main.asm").write_text("y")
        (self.dir / "readme.txt").write_text("not an artifact")
        names = {a["name"] for a in build.collect_artifacts(self.dir)}
        self.assertEqual(names, {"main.hex", "main.asm"})

    def test_recurses_but_skips_node_modules_and_dotdirs(self) -> None:
        (self.dir / "main.hex").write_text("x")
        sub = self.dir / "out"
        sub.mkdir()
        (sub / "deep.cod").write_text("z")
        for skipped in ("node_modules", ".hidden"):
            d = self.dir / skipped
            d.mkdir()
            (d / "junk.hex").write_text("no")
        names = {a["name"] for a in build.collect_artifacts(self.dir)}
        self.assertEqual(names, {"main.hex", "deep.cod"})

    def test_sorted_newest_first(self) -> None:
        old = self.dir / "old.hex"
        old.write_text("x")
        new = self.dir / "new.hex"
        new.write_text("y")
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))
        artifacts = build.collect_artifacts(self.dir)
        self.assertEqual([a["name"] for a in artifacts], ["new.hex", "old.hex"])

    def test_missing_dir_returns_empty(self) -> None:
        self.assertEqual(build.collect_artifacts(self.dir / "nope"), [])

    def test_cmd_artifacts_json_error_on_missing_manifest(self) -> None:
        # --json must stay parseable even when the manifest is missing/malformed.
        args = types.SimpleNamespace(project=str(self.dir / "nope.json"), dir=None, json=True)
        with unittest.mock.patch("builtins.print") as printed:
            rc = build.cmd_artifacts(args)
        self.assertEqual(rc, 1)
        payload = json.loads("".join(str(c.args[0]) for c in printed.call_args_list if c.args))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["kind"], "artifacts_failed")

    def test_symlinked_dir_not_followed(self) -> None:
        # Parity with the extension (Dirent.isDirectory() is false for symlinks).
        real = self.dir / "real"
        real.mkdir()
        (real / "inside.hex").write_text("x")
        link = self.dir / "link"
        try:
            link.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unsupported on this platform")
        # The artifact is found once (via real/), not again through the symlink.
        paths = [a["path"] for a in build.collect_artifacts(self.dir)]
        self.assertTrue(any(p.endswith("real/inside.hex") for p in paths))
        self.assertFalse(any("link/inside.hex" in p for p in paths))

    def test_cmd_artifacts_json_payload(self) -> None:
        (self.dir / "main.hex").write_text("x")
        args = types.SimpleNamespace(project=None, dir=str(self.dir), json=True)
        with unittest.mock.patch("builtins.print") as printed:
            rc = build.cmd_artifacts(args)
        self.assertEqual(rc, 0)
        payload = json.loads("".join(str(c.args[0]) for c in printed.call_args_list if c.args))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["artifacts"][0]["type"], "hex")
        self.assertEqual(payload["search_dir"], str(self.dir))


class Cc5xDiagnosticsParseTests(unittest.TestCase):
    """Phase 1: `build --json-diagnostics` normalizes CC5X output to structured diagnostics."""

    def test_parses_error_and_warning(self) -> None:
        text = "Error main.c 42: No ';' found\nWarning regs.h 17: variable not used\n"
        diags = build.parse_cc5x_diagnostics(text)
        self.assertEqual(diags[0], {"severity": "error", "file": "main.c", "line": 42, "message": "No ';' found"})
        self.assertEqual(diags[1]["severity"], "warning")
        self.assertEqual(diags[1]["line"], 17)

    def test_ignores_non_diagnostic_lines(self) -> None:
        # Must NOT match the summary/option lines (mirrors the $cc5x matcher).
        text = "Warnings (level 1-3): 5\nError options: see manual\nCC5X Version 3.8C\n"
        self.assertEqual(build.parse_cc5x_diagnostics(text), [])

    def test_dry_run_json_payload(self) -> None:
        args = types.SimpleNamespace(
            project=None, edition=None, compiler="/c/CC5X.EXE", main="main.c",
            option=None, runner=None, cwd=None, dry_run=True, json_diagnostics=True,
        )
        with unittest.mock.patch("builtins.print") as printed:
            rc = build.cmd_build(args)
        self.assertEqual(rc, 0)
        payload = json.loads("".join(str(c.args[0]) for c in printed.call_args_list if c.args))
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["diagnostics"], [])
        self.assertIn("/c/CC5X.EXE", payload["command"])

    def test_run_json_payload_captures_diagnostics(self) -> None:
        fake = types.SimpleNamespace(returncode=1, stdout="Error main.c 9: oops\n", stderr="")
        args = types.SimpleNamespace(
            project=None, edition=None, compiler="/c/CC5X.EXE", main="main.c",
            option=None, runner=None, cwd=None, dry_run=False, json_diagnostics=True,
        )
        with unittest.mock.patch.object(build.subprocess, "run", return_value=fake) as run, \
             unittest.mock.patch("builtins.print") as printed:
            rc = build.cmd_build(args)
        self.assertEqual(rc, 1)
        self.assertTrue(run.call_args.kwargs.get("capture_output"))
        payload = json.loads("".join(str(c.args[0]) for c in printed.call_args_list if c.args))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["returncode"], 1)
        self.assertEqual(payload["diagnostics"][0]["message"], "oops")

    def test_run_json_payload_reports_timeout(self) -> None:
        args = types.SimpleNamespace(
            project=None, edition=None, compiler="/c/CC5X.EXE", main="main.c",
            option=None, runner=None, cwd=None, dry_run=False, json_diagnostics=True,
            timeout_seconds=1,
        )
        timeout = subprocess.TimeoutExpired(
            cmd=["/c/CC5X.EXE"],
            timeout=1,
            output="Error main.c 9: timed out late\n",
            stderr="",
        )
        with unittest.mock.patch.object(build.subprocess, "run", side_effect=timeout), \
             unittest.mock.patch("builtins.print") as printed:
            rc = build.cmd_build(args)
        self.assertEqual(rc, 124)
        payload = json.loads("".join(str(c.args[0]) for c in printed.call_args_list if c.args))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["returncode"], None)
        self.assertEqual(payload["error"]["kind"], "timeout")
        self.assertEqual(payload["diagnostics"][0]["message"], "timed out late")


class BuildJsonDiagnosticsErrorTests(unittest.TestCase):
    """--json-diagnostics must keep its JSON contract even when the build can't launch."""

    def test_launch_failure_emits_json_error(self) -> None:
        # An explicit --compiler/--main is missing -> SystemExit in prep -> JSON error, exit 1.
        args = types.SimpleNamespace(
            project=None, edition=None, compiler=None, main=None,
            option=None, runner=None, cwd=None, dry_run=False, json_diagnostics=True,
        )
        with unittest.mock.patch("builtins.print") as printed:
            rc = build.cmd_build(args)
        self.assertEqual(rc, 1)
        payload = json.loads("".join(str(c.args[0]) for c in printed.call_args_list if c.args))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["kind"], "build_not_ready")

    def test_launch_failure_without_json_still_raises(self) -> None:
        args = types.SimpleNamespace(
            project=None, edition=None, compiler=None, main=None,
            option=None, runner=None, cwd=None, dry_run=False, json_diagnostics=False,
        )
        with self.assertRaises(SystemExit):
            build.cmd_build(args)

    def test_dry_run_preview_skips_cwd_existence_check(self) -> None:
        # --dry-run is a pure preview: a not-yet-created --cwd must not abort it.
        args = types.SimpleNamespace(
            project=None, edition=None, compiler="/c/CC5X.EXE", main="main.c",
            option=None, runner=None, cwd="/no/such/dir", dry_run=True, json_diagnostics=False,
        )
        with unittest.mock.patch("builtins.print"):
            rc = build.cmd_build(args)
        self.assertEqual(rc, 0)


class BuildCommandTests(unittest.TestCase):
    """Audit #3: runner is a `{compiler}` command template, not filename-coupled."""

    def test_no_runner_invokes_compiler_directly(self) -> None:
        self.assertEqual(
            build.build_command("/c/CC5X.EXE", "main.c", ["-a"], []),
            ["/c/CC5X.EXE", "-a", "main.c"],
        )

    def test_placeholder_substituted_and_not_appended(self) -> None:
        self.assertEqual(
            build.build_command("/c/CC5X.EXE", "main.c", ["-a"], ["wine", "{compiler}"]),
            ["wine", "/c/CC5X.EXE", "-a", "main.c"],
        )

    def test_bare_interpreter_without_placeholder_errors_loudly(self) -> None:
        # Audit-fix follow-up: a bare `wine` runner must fail with guidance, not silently
        # drop the compiler (which would invoke wine with no program).
        with self.assertRaises(SystemExit) as ctx:
            build.build_command("/c/CC5X.EXE", "main.c", ["-a"], ["wine"])
        self.assertIn("{compiler}", str(ctx.exception))

    def test_self_contained_runner_omits_compiler(self) -> None:
        # A placeholder-free wrapper supplies its own compiler (e.g. cc5x-run.sh).
        self.assertEqual(
            build.build_command("/c/CC5X.EXE", "main.c", ["-a"], ["/x/cc5x-run.sh"]),
            ["/x/cc5x-run.sh", "-a", "main.c"],
        )

    def test_behavior_is_not_filename_coupled(self) -> None:
        # Renaming the self-contained wrapper must not change how the compiler is handled.
        a = build.build_command("/c/CC5X.EXE", "m.c", ["-x"], ["/x/cc5x-run.sh"])
        b = build.build_command("/c/CC5X.EXE", "m.c", ["-x"], ["/x/renamed.sh"])
        self.assertNotIn("/c/CC5X.EXE", a)  # neither appends the compiler
        self.assertNotIn("/c/CC5X.EXE", b)
        self.assertEqual(a[1:], b[1:])  # identical past the runner path


class RunnerExecutableTests(unittest.TestCase):
    """Audit #5: doctor must resolve a runner *template*, not treat it as one path."""

    def test_template_with_placeholder_checks_executable_token(self) -> None:
        # The whole `"<wrapper> {compiler}"` string is not a path; only the first token is.
        with tempfile.TemporaryDirectory() as tmp:
            wrapper = Path(tmp) / "cc5x-run.sh"
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            self.assertEqual(
                build.runner_executable(f"{wrapper} {{compiler}}"),
                wrapper,
            )

    def test_missing_wrapper_returns_none(self) -> None:
        self.assertIsNone(build.runner_executable("/nope/cc5x-run.sh {compiler}"))

    def test_bare_command_resolved_on_path(self) -> None:
        # `wine {compiler}` — a bare interpreter name is found on $PATH, not on disk verbatim.
        with unittest.mock.patch.object(build.shutil, "which", return_value="/usr/bin/wine"):
            self.assertEqual(
                build.runner_executable("wine {compiler}"), Path("/usr/bin/wine")
            )
        with unittest.mock.patch.object(build.shutil, "which", return_value=None):
            self.assertIsNone(build.runner_executable("wine {compiler}"))

    def test_empty_and_unparseable_specs_return_none(self) -> None:
        self.assertIsNone(build.runner_executable(""))
        self.assertIsNone(build.runner_executable('"unterminated'))

    def test_leading_placeholder_has_no_executable(self) -> None:
        self.assertIsNone(build.runner_executable("{compiler} -a"))

    def test_doctor_reports_template_runner_as_present(self) -> None:
        # The end-to-end audit-#5 symptom: a CC5X_RUNNER template reported runner missing.
        with tempfile.TemporaryDirectory() as tmp:
            wrapper = Path(tmp) / "cc5x-run.sh"
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            with unittest.mock.patch.dict(
                os.environ, {"CC5X_RUNNER": f"{wrapper} {{compiler}}"}
            ):
                report = build.environment_report()
        self.assertTrue(report["runner_exists"])
        self.assertEqual(report["runner"], f"{wrapper} {{compiler}}")


class ConfigCommentSanitizationTests(unittest.TestCase):
    """Audit #1: pack-derived comments must never corrupt the rendered managed block."""

    def _symbol_with_comment(self, comment: str) -> "build.ConfigSymbol":
        symbol = build.ConfigSymbol(name="FOO")
        symbol.add(build.ConfigOption(register=1, mask=1, name="FOO", state="ON", comment=comment))
        return symbol

    def test_newline_and_backslash_collapsed(self) -> None:
        symbol = self._symbol_with_comment("line one\nline two\\")
        lines = build.config_lines_for_settings({"FOO": symbol}, {"FOO": "ON"})
        self.assertEqual(len(lines), 1)  # comment did not splice into extra lines
        self.assertNotIn("\\", lines[0])
        self.assertEqual(lines[0], "#pragma config FOO = ON // line one line two")

    def test_non_latin1_replaced(self) -> None:
        symbol = self._symbol_with_comment("µ-current 5µA — ok \U0001F600")
        lines = build.config_lines_for_settings({"FOO": symbol}, {"FOO": "ON"})
        # Whole block must be latin-1 encodable (the source file is written latin-1).
        lines[0].encode("latin-1")


class AtomicWriteTextTests(unittest.TestCase):
    """Audit #1: a failed write must never truncate the user's existing source file."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_round_trips_content(self) -> None:
        path = self.dir / "app.c"
        build.atomic_write_text(path, "void main(void){}\n")
        self.assertEqual(path.read_text(encoding="latin-1"), "void main(void){}\n")

    def test_encode_failure_leaves_original_intact(self) -> None:
        path = self.dir / "app.c"
        path.write_text("ORIGINAL", encoding="latin-1")
        with self.assertRaises(UnicodeEncodeError):
            build.atomic_write_text(path, "needs \U0001F600 unicode")
        # The destination is untouched (no zero-byte truncation) and no temp left behind.
        self.assertEqual(path.read_text(encoding="latin-1"), "ORIGINAL")
        leftovers = [p for p in self.dir.iterdir() if p.name != "app.c"]
        self.assertEqual(leftovers, [])


class ManifestConfigDiagnosticsTests(unittest.TestCase):
    """`manifest_config_diagnostics` flags unknown symbols / illegal values per edition."""

    def _project(self, config: dict[str, str]) -> object:
        return types.SimpleNamespace(
            device="PIC16F1509",
            editions={"production": types.SimpleNamespace(name="production", config=config)},
        )

    def test_empty_config_skips_metadata_lookup(self) -> None:
        # No config overrides → no pack lookup at all (fast + pack-independent).
        with unittest.mock.patch.object(build, "project_metadata") as metadata:
            diags = build.manifest_config_diagnostics(self._project({}))
        self.assertEqual(diags, [])
        metadata.assert_not_called()

    def test_unknown_symbol_flagged(self) -> None:
        symbols = {"BOREN": _config_symbol("BOREN", "ON", "OFF")}
        with unittest.mock.patch.object(build, "project_metadata", return_value=(None, object())), \
                unittest.mock.patch.object(build, "pack_config_symbols", return_value=symbols):
            diags = build.manifest_config_diagnostics(self._project({"NOPE": "ON"}))
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0]["kind"], "unknown_config_symbol")
        self.assertEqual(diags[0]["symbol"], "NOPE")
        self.assertEqual(diags[0]["edition"], "production")
        self.assertEqual(diags[0]["severity"], "error")

    def test_invalid_value_flagged_with_legal_states(self) -> None:
        symbols = {"BOREN": _config_symbol("BOREN", "ON", "OFF")}
        with unittest.mock.patch.object(build, "project_metadata", return_value=(None, object())), \
                unittest.mock.patch.object(build, "pack_config_symbols", return_value=symbols):
            diags = build.manifest_config_diagnostics(self._project({"BOREN": "MAYBE"}))
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0]["kind"], "invalid_config_value")
        self.assertEqual(diags[0]["symbol"], "BOREN")
        self.assertIn("OFF", diags[0]["message"])
        self.assertIn("ON", diags[0]["message"])

    def test_legal_config_is_clean(self) -> None:
        symbols = {"BOREN": _config_symbol("BOREN", "ON", "OFF")}
        with unittest.mock.patch.object(build, "project_metadata", return_value=(None, object())), \
                unittest.mock.patch.object(build, "pack_config_symbols", return_value=symbols):
            diags = build.manifest_config_diagnostics(self._project({"BOREN": "ON"}))
        self.assertEqual(diags, [])

    def test_metadata_failure_emits_no_false_positives(self) -> None:
        # A missing/malformed pack must not turn every symbol into an "unknown" error.
        with unittest.mock.patch.object(
            build, "project_metadata", side_effect=ValueError("malformed pack")
        ):
            diags = build.manifest_config_diagnostics(self._project({"BOREN": "ON"}))
        self.assertEqual(diags, [])

    def test_no_config_symbols_emits_no_false_positives(self) -> None:
        # Metadata can resolve but expose no config symbols (e.g. cfgdata not found). That
        # must not flag every user entry as unknown — it means "cannot validate", not "wrong".
        with unittest.mock.patch.object(build, "project_metadata", return_value=(None, object())), \
                unittest.mock.patch.object(build, "pack_config_symbols", return_value={}):
            diags = build.manifest_config_diagnostics(self._project({"BOREN": "ON"}))
        self.assertEqual(diags, [])


class ProjectManifestDiagnosticsTests(unittest.TestCase):
    """`project-validate --json` surfaces locatable manifest diagnostics + fails on them."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.compiler = self.dir / "CC5X.EXE"
        self.compiler.write_text("", encoding="ascii")
        self.manifest = self.dir / "setcc-native.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_manifest(self, *, mode: str, header_path: str, config: dict) -> None:
        (self.dir / "app.c").write_text("void main(void){}\n", encoding="ascii")
        manifest = {
            "version": 1,
            "device": "PIC16F1509",
            "compiler": str(self.compiler),
            "runner": None,
            "header": {"mode": mode, "path": header_path},
            "config_source": "app.c",
            "main_source": "app.c",
            "build_options": [],
            "editions": {"production": {"config": config, "build_options": []}},
        }
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")

    def _validate(self) -> tuple[int, dict]:
        args = types.SimpleNamespace(project=str(self.manifest), json=True)
        with unittest.mock.patch("builtins.print") as printed:
            rc = build.cmd_project_validate(args)
        payload = json.loads("".join(str(call.args[0]) for call in printed.call_args_list))
        return rc, payload

    def test_missing_existing_header_reports_diagnostic(self) -> None:
        # `existing` mode + a header path that does not exist → locatable header diagnostic.
        self._write_manifest(mode="existing", header_path="nope.H", config={})
        rc, payload = self._validate()
        self.assertEqual(rc, 1)
        header_diags = [d for d in payload["diagnostics"] if d["kind"] == "missing_header"]
        self.assertEqual(len(header_diags), 1)
        self.assertEqual(header_diags[0]["field"], "header.path")

    def test_invalid_config_value_fails_validation(self) -> None:
        # A valid-shape manifest whose only fault is an illegal config value must still
        # fail (exit 1) and surface a locatable diagnostic, even though `errors` is empty.
        self._write_manifest(mode="generated", header_path="gen/16F1509.H", config={"BOREN": "MAYBE"})
        symbols = {"BOREN": _config_symbol("BOREN", "ON", "OFF")}
        with unittest.mock.patch.object(build, "project_metadata", return_value=(None, object())), \
                unittest.mock.patch.object(build, "render_full_header", return_value="// ok\n"), \
                unittest.mock.patch.object(build, "pack_config_symbols", return_value=symbols):
            rc, payload = self._validate()
        self.assertEqual(rc, 1)
        self.assertEqual(payload["errors"], [])
        bad = [d for d in payload["diagnostics"] if d["kind"] == "invalid_config_value"]
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]["symbol"], "BOREN")

    def test_valid_manifest_has_empty_diagnostics(self) -> None:
        self._write_manifest(mode="generated", header_path="gen/16F1509.H", config={})
        with unittest.mock.patch.object(build, "project_metadata", return_value=(None, object())), \
                unittest.mock.patch.object(build, "render_full_header", return_value="// ok\n"):
            rc, payload = self._validate()
        self.assertEqual(rc, 0)
        self.assertEqual(payload["diagnostics"], [])


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
            timeout_seconds=0,
            yes=True,
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

    def test_json_payload_reports_timeout(self) -> None:
        args = types.SimpleNamespace(
            action="program", project=None, edition=None, device="PIC16F1509", hex=None,
            tool="PK4", ipecmd="/fake/ipecmd.sh", release_from_reset=False, ipe_arg=None,
            dry_run=False, json=True, timeout_seconds=2, yes=True,
        )
        timeout = subprocess.TimeoutExpired(
            cmd=["/fake/ipecmd.sh"],
            timeout=2,
            output="programming...\n",
            stderr="still waiting\n",
        )
        with tempfile.TemporaryDirectory() as tmp:
            hexfile = Path(tmp) / "app.hex"
            hexfile.write_text(":00000001FF\n", encoding="ascii")
            args.hex = str(hexfile)
            with unittest.mock.patch.object(build.subprocess, "run", side_effect=timeout), \
                 unittest.mock.patch("builtins.print") as printed:
                rc = build.cmd_program(args)
        self.assertEqual(rc, 124)
        payload = json.loads("".join(str(c.args[0]) for c in printed.call_args_list if c.args))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["returncode"], None)
        self.assertEqual(payload["error"]["kind"], "timeout")
        self.assertEqual(payload["stdout"], "programming...\n")


class IpecmdFailureGuidanceTests(unittest.TestCase):
    """Phase 5: a non-zero IPECMD run yields actionable, tool-aware guidance."""

    def test_always_returns_full_checklist(self) -> None:
        hints = build.ipecmd_failure_guidance("", "", "PK4")
        self.assertGreaterEqual(len(hints), 3)
        joined = "\n".join(hints).lower()
        self.assertIn("usb permission", joined)  # Linux perms covered
        self.assertIn("pk4", joined)  # mentions the actual tool code
        self.assertIn("device", joined)  # unsupported-device covered

    def test_connection_failure_leads_checklist(self) -> None:
        hints = build.ipecmd_failure_guidance("Unable to connect to tool", "", "PPK5")
        self.assertIn("could not talk to the programmer", hints[0].lower())

    def test_unsupported_device_hint(self) -> None:
        hints = build.ipecmd_failure_guidance("Device not found in this version", "", "SNAP")
        self.assertTrue(any("not be supported" in h for h in hints))

    def test_no_duplicate_when_match_overlaps_checklist(self) -> None:
        hints = build.ipecmd_failure_guidance("no tool detected", "", "PK4")
        self.assertEqual(len(hints), len(set(hints)))


class ProgramFailureGuidanceTests(unittest.TestCase):
    """A failing IPECMD run surfaces a structured error + guidance in the JSON payload."""

    def test_nonzero_exit_emits_error_and_guidance(self) -> None:
        fake = types.SimpleNamespace(returncode=1, stdout="Unable to connect to tool\n", stderr="")
        args = types.SimpleNamespace(
            action="program", project=None, edition=None, device="PIC16F1509", hex=None,
            tool="PK4", ipecmd="/fake/ipecmd.sh", release_from_reset=False, ipe_arg=None,
            dry_run=False, json=True, yes=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            hexfile = Path(tmp) / "app.hex"
            hexfile.write_text(":00000001FF\n", encoding="ascii")
            args.hex = str(hexfile)
            with unittest.mock.patch.object(build.subprocess, "run", return_value=fake), \
                 unittest.mock.patch("builtins.print") as printed:
                rc = build.cmd_program(args)
        self.assertEqual(rc, 1)
        payload = json.loads("".join(str(c.args[0]) for c in printed.call_args_list if c.args))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["returncode"], 1)
        self.assertEqual(payload["error"]["kind"], "ipecmd_failed")
        self.assertTrue(payload["guidance"])
        self.assertIn("could not talk to the programmer", payload["guidance"][0].lower())


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
    def test_measurement_harness_includes_device_header_and_stub(self) -> None:
        h = validate_headers._measurement_harness(
            "16F1509.H", "cdl_monitor_16F1509.c",
            "void cdl_init(void);\nvoid cdl_trace(uns8 ch, uns16 v);\nvoid cdl_poll(void);\n")
        self.assertIn('#include "16F1509.H"', h)
        self.assertIn('#include "cdl_monitor_16F1509.c"', h)
        self.assertIn("void main(void)", h)

    def test_measurement_harness_only_calls_declared_entry_points(self) -> None:
        # A trace-tier header declares no cdl_bp -> the harness must not reference it
        # (an undeclared call would fail to compile), but must call what is declared.
        stub_h = "void cdl_init(void);\nvoid cdl_trace(uns8 ch, uns16 v);\n"
        h = validate_headers._measurement_harness("d.H", "stub.c", stub_h)
        self.assertIn("cdl_init();", h)
        self.assertIn("cdl_trace(0, 0);", h)
        self.assertNotIn("cdl_bp(", h)
        self.assertNotIn("cdl_poll(", h)

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
                    runner=["runner"],
                    include_dir=root,
                    source_path=source,
                    header_path=header,
                    label="generated",
                    device="PIC16F1509",
                    timeout=None,
                )
        self.assertEqual(result.header_path, str(header))

    def test_compile_timeout_reports_failed_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "16f1509_gen.c"
            header = root / "16F1509.H"
            source.write_text('#include "16F1509.H"\nvoid main(void) {}\n', encoding="ascii")
            header.write_text("#pragma chip PIC16F1509\n", encoding="latin-1")
            timeout = subprocess.TimeoutExpired(
                cmd=["runner"],
                timeout=1,
                output="compiling\n",
                stderr="",
            )
            with unittest.mock.patch.object(validate_headers.subprocess, "run", side_effect=timeout):
                result = validate_headers.run_compile(
                    runner=["runner"],
                    include_dir=root,
                    source_path=source,
                    header_path=header,
                    label="generated",
                    device="PIC16F1509",
                    timeout=1,
                )
        self.assertEqual(result.returncode, 124)
        self.assertFalse(result.succeeded)
        self.assertIn("timed out", result.stderr)

    def test_runner_command_expands_compiler_placeholder(self) -> None:
        command = validate_headers.runner_command('wine "{compiler}"')
        self.assertEqual(command, ["wine", str(validate_headers.DEFAULT_COMPILER)])

    def test_runner_command_rejects_bare_interpreter(self) -> None:
        # Same guard as the main CLI: a bare "wine" with no {compiler} would drop the
        # compiler path, so the validation helper must reject it rather than build a
        # command that runs wine with the flags as its program.
        with self.assertRaises(SystemExit) as ctx:
            validate_headers.runner_command("wine")
        self.assertIn("{compiler}", str(ctx.exception))


class InstallRootDefaultsTests(unittest.TestCase):
    """Frozen-aware toolchain defaults (audit #8): a PyInstaller bundle must not point its
    compiler default into the /tmp _MEIPASS extraction dir, and the runner default must not
    be a hardcoded home path."""

    def test_source_mode_root_is_repo_root(self) -> None:
        with unittest.mock.patch.object(build.sys, "frozen", False, create=True):
            root = build._install_root()
        self.assertEqual(root, Path(build.__file__).resolve().parent.parent)

    def test_frozen_onefile_in_dist_resolves_to_dir_above_dist(self) -> None:
        # Binary ships at <root>/dist/<bin>; the root (with cc5x_paid/) is the dir above dist.
        with unittest.mock.patch.object(build.sys, "frozen", True, create=True), \
                unittest.mock.patch.object(build.sys, "executable", "/opt/app/dist/cc5x-helper"):
            self.assertEqual(build._install_root(), Path("/opt/app"))

    def test_frozen_binary_outside_dist_uses_executable_dir(self) -> None:
        with unittest.mock.patch.object(build.sys, "frozen", True, create=True), \
                unittest.mock.patch.object(build.sys, "executable", "/usr/local/bin/cc5x-helper"):
            self.assertEqual(build._install_root(), Path("/usr/local/bin"))

    def test_runner_default_has_no_hardcoded_home_path(self) -> None:
        # Guard against re-introducing a machine-specific literal like /home/gary/...
        import inspect

        self.assertNotIn("/home/", inspect.getsource(build._runner_candidates))


class ResolveProjectManifestTests(unittest.TestCase):
    """--workspace-root / default --project discovery for setcc-native.json."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "setcc-native.json").write_text("{}", encoding="utf-8")
        self.sub = self.root / "a" / "b"
        self.sub.mkdir(parents=True)
        # Never let an ambient env override leak in from the runner's environment.
        self._env = unittest.mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("CC5X_HELPER_PROJECT", None)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_bare_name_discovered_by_walking_up(self) -> None:
        # From a deep subdirectory, the bare default name finds the ancestor manifest.
        resolved = build.resolve_project_manifest("setcc-native.json", str(self.sub))
        self.assertEqual(resolved, self.root / "setcc-native.json")

    def test_absolute_path_is_used_verbatim(self) -> None:
        absolute = str(Path(self._tmp.name).resolve() / "elsewhere" / "x.json")
        self.assertEqual(
            str(build.resolve_project_manifest(absolute, str(self.sub))), absolute
        )

    def test_relative_with_directory_component_anchors_to_root(self) -> None:
        resolved = build.resolve_project_manifest("cfg/p.json", str(self.root))
        self.assertEqual(resolved, self.root / "cfg" / "p.json")

    def test_not_found_returns_conventional_location(self) -> None:
        # A workspace with no manifest anywhere up the tree -> <root>/<name>. Use an
        # independent temp dir so no ancestor (incl. self.root) carries a manifest.
        with tempfile.TemporaryDirectory() as empty:
            empty_root = Path(empty).resolve()
            resolved = build.resolve_project_manifest("setcc-native.json", str(empty_root))
            self.assertEqual(resolved, empty_root / "setcc-native.json")

    def test_init_mode_skips_upward_walk(self) -> None:
        # project-init must create at the anchored dir, not bind onto an ancestor manifest.
        resolved = build.resolve_project_manifest(
            "setcc-native.json", str(self.sub), discover=False
        )
        self.assertEqual(resolved, self.sub / "setcc-native.json")

    def test_env_override_wins_for_bare_name(self) -> None:
        os.environ["CC5X_HELPER_PROJECT"] = "/env/pinned.json"
        resolved = build.resolve_project_manifest("setcc-native.json", str(self.sub))
        self.assertEqual(str(resolved), "/env/pinned.json")

    def test_explicit_absolute_beats_env_override(self) -> None:
        os.environ["CC5X_HELPER_PROJECT"] = "/env/pinned.json"
        resolved = build.resolve_project_manifest("/explicit/x.json", str(self.sub))
        self.assertEqual(str(resolved), "/explicit/x.json")

    def test_init_mode_ignores_env_override(self) -> None:
        # project-init (discover=False) must create at the anchored dir even when the GUI's
        # CC5X_HELPER_PROJECT pin is set, not redirect the new file elsewhere.
        os.environ["CC5X_HELPER_PROJECT"] = "/env/pinned.json"
        resolved = build.resolve_project_manifest(
            "setcc-native.json", str(self.sub), discover=False
        )
        self.assertEqual(resolved, self.sub / "setcc-native.json")

    def test_bad_tilde_user_raises_runtimeerror(self) -> None:
        # main() converts this to a clean SystemExit; the resolver itself surfaces it.
        with self.assertRaises(RuntimeError):
            build.resolve_project_manifest("~nouser_zzz9999/x.json", str(self.root))

    def test_main_empty_project_string_stays_standalone(self) -> None:
        # `--project ''` must remain falsy so optional-project commands keep standalone
        # mode; central resolution must not rewrite it into the workspace directory.
        import contextlib
        import io
        import sys

        argv = ["prog", "artifacts", "--project", "", "--dir", str(self.root), "--json"]
        buf = io.StringIO()
        with unittest.mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(buf):
            rc = build.main()
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["search_dir"], str(self.root))

    def test_workspace_root_discovers_for_optional_project_command(self) -> None:
        # build/sync-config/artifacts/program have no --project default; an explicit
        # --workspace-root must still discover a manifest there instead of being a no-op.
        import contextlib
        import io
        import sys

        ws = self.root / "ws"
        ws.mkdir()
        (ws / "app.c").write_text("", encoding="latin-1")
        manifest = {
            "version": 1,
            "device": "PIC16F1509",
            "compiler": str(self.root / "CC5X.EXE"),
            "runner": None,
            "header": {"mode": "generated", "path": "gen/16F1509.H"},
            "config_source": "app.c",
            "main_source": "app.c",
            "build_options": [],
            "editions": {"production": {"config": {}, "build_options": []}},
        }
        (ws / "setcc-native.json").write_text(json.dumps(manifest), encoding="utf-8")

        argv = ["prog", "artifacts", "--workspace-root", str(ws), "--json"]
        buf = io.StringIO()
        with unittest.mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(buf):
            rc = build.main()
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["ok"])
        # Discovered the manifest and searched its main-source dir (not standalone error).
        self.assertEqual(payload["search_dir"], str(ws))

    def test_workspace_root_without_manifest_stays_standalone(self) -> None:
        # No manifest under the workspace root -> stay in standalone mode, not a crash.
        import contextlib
        import io
        import sys

        with tempfile.TemporaryDirectory() as empty:
            empty_root = Path(empty).resolve()
            (empty_root / "data").mkdir()
            argv = [
                "prog", "artifacts", "--workspace-root", str(empty_root),
                "--dir", str(empty_root / "data"), "--json",
            ]
            buf = io.StringIO()
            with unittest.mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(buf):
                rc = build.main()
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["search_dir"], str(empty_root / "data"))

    def test_parser_accepts_workspace_root_on_project_command(self) -> None:
        parser = build.build_parser()
        args = parser.parse_args(
            ["project-show", "--workspace-root", str(self.sub), "--json"]
        )
        self.assertEqual(args.workspace_root, str(self.sub))
        self.assertEqual(args.project, "setcc-native.json")


class MeasureGateCodexFixes(unittest.TestCase):
    """Regressions for the two Codex findings on the P2 measure gate. The compile
    step is CrossOver-gated, so these exercise the pure-Python parts: provisional
    tier selection and the harness's entry-point call set."""

    class _Stop(Exception):
        pass

    def _run_capture_provisional(self, debug_config: dict) -> list:
        """Run measure_debug_stub far enough to capture the tier of the *provisional*
        generate_debug_stub call, then stop at run_measure_gate."""
        seen: list = []

        def rec_gen(metadata, payload):
            seen.append(payload.get("tier"))
            return types.SimpleNamespace(decision=object(), caps=object())

        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(validate_headers, "VALIDATION_ROOT", Path(tmp)), \
                unittest.mock.patch.object(validate_headers, "_metadata_for", return_value=object()), \
                unittest.mock.patch.object(validate_headers, "generate_device_header", return_value=""), \
                unittest.mock.patch.object(validate_headers.debuggen, "generate_debug_stub",
                                           side_effect=rec_gen), \
                unittest.mock.patch.object(validate_headers.debuggen, "run_measure_gate",
                                           side_effect=self._Stop):
            with self.assertRaises(self._Stop):
                validate_headers.measure_debug_stub("PIC16F15244", ["runner"], None, debug_config)
        return seen

    def test_provisional_honors_forced_tier(self):
        # A project forcing 'trace' must be measured as 'trace', not re-auto-selected.
        seen = self._run_capture_provisional({"tier": "trace", "transport": {"brg": 25}})
        self.assertEqual(seen[0], "trace")

    def test_provisional_defaults_to_auto_when_unset(self):
        seen = self._run_capture_provisional({"transport": {"brg": 25}})
        self.assertEqual(seen[0], "auto")

    def test_toggle_header_gets_cdl_mark_call(self):
        harness = validate_headers._measurement_harness(
            "d.H", "stub.c", "void cdl_init(void);\nvoid cdl_mark(uns8 id);\n")
        self.assertIn("cdl_mark(0);", harness)
        self.assertIn("cdl_init();", harness)

    def test_full_header_has_no_mark_call(self):
        harness = validate_headers._measurement_harness(
            "d.H", "stub.c",
            "void cdl_init(void);\nvoid cdl_trace(uns8 ch, uns16 value);\nvoid cdl_poll(void);\n")
        self.assertNotIn("cdl_mark", harness)
        self.assertIn("cdl_trace(0, 0);", harness)


if __name__ == "__main__":
    unittest.main()
