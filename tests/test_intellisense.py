from __future__ import annotations

import json
import os
import tempfile
import types
import unittest
from pathlib import Path

from cc5x_setcc_native_lib import intellisense


class RenderShimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.shim = intellisense.render_shim()

    def test_defines_extension_types(self) -> None:
        for name in ("uns8", "uns16", "uns24", "uns32", "int8", "int16", "int24", "int32"):
            self.assertIn(f" {name};", self.shim, name)
        self.assertIn("typedef unsigned char bit;", self.shim)

    def test_defines_qualifier_keywords(self) -> None:
        # bank0..bank63 and page0..page15 must all be defined away.
        self.assertIn("#define bank0\n", self.shim)
        self.assertIn("#define bank63\n", self.shim)
        self.assertIn("#define page0\n", self.shim)
        self.assertIn("#define page15\n", self.shim)
        for name in ("shrBank", "size1", "size2", "DataInW", "interrupt"):
            self.assertIn(f"#define {name}\n", self.shim, name)

    def test_declares_intrinsics(self) -> None:
        for name in ("nop", "clrwdt", "sleep", "skip", "softReset", "btsc"):
            self.assertIn(f"int {name}();", self.shim, name)

    def test_body_guarded_by_marker(self) -> None:
        # The dialect body activates only when the compile database defines the marker,
        # so an accidental include in a real CC5X compile is an inert no-op.
        self.assertIn(f"#ifdef {intellisense.INTELLISENSE_MARKER}", self.shim)

    def test_persistent_is_not_a_cc5x_keyword(self) -> None:
        # Confirmed against the CC5X 3.8 manual: `persistent` is XC8, not CC5X. The shim
        # must not invent it (it would mask a genuine unknown-identifier).
        self.assertNotIn("persistent", self.shim)


class IntellisenseDefinesTests(unittest.TestCase):
    def _meta(self, arch: str | None) -> object:
        return types.SimpleNamespace(ini_arch=arch)

    def test_enhanced_14bit_core(self) -> None:
        defines = intellisense.intellisense_defines(self._meta("PIC14E"), "PIC16F1509")
        self.assertEqual(defines["__CoreSet__"], "1410")
        self.assertEqual(defines["__EnhancedCore14__"], "1")
        self.assertEqual(defines["_16CXX"], "1")
        self.assertEqual(defines["_16F1509"], "1")
        self.assertEqual(defines[intellisense.INTELLISENSE_MARKER], "1")
        self.assertEqual(defines["__CC5X__"], str(intellisense.DEFAULT_CC5X_VERSION))
        self.assertNotIn("_16C5X", defines)

    def test_baseline_14bit_core(self) -> None:
        defines = intellisense.intellisense_defines(self._meta("PIC14"), "PIC16F628")
        self.assertEqual(defines["__CoreSet__"], "1400")
        self.assertNotIn("__EnhancedCore14__", defines)

    def test_12bit_core_sets_16c5x(self) -> None:
        defines = intellisense.intellisense_defines(self._meta("PIC12"), "PIC10F200")
        self.assertEqual(defines["__CoreSet__"], "1200")
        self.assertEqual(defines["_16C5X"], "1")
        self.assertEqual(defines["_10F200"], "1")

    def test_pic12ie_core_sets_12bit_editor_macros(self) -> None:
        defines = intellisense.intellisense_defines(self._meta("PIC12IE"), "PIC16F527")
        self.assertEqual(defines["__CoreSet__"], "1200")
        self.assertEqual(defines["_16CXX"], "1")
        self.assertEqual(defines["_16C5X"], "1")
        self.assertEqual(defines["_16F527"], "1")
        self.assertNotIn("__EnhancedCore14__", defines)

    def test_unknown_arch_omits_coreset(self) -> None:
        # Never invent a core macro we cannot justify; the marker/version/device still hold.
        defines = intellisense.intellisense_defines(self._meta(None), "PIC18F4550")
        self.assertNotIn("__CoreSet__", defines)
        self.assertNotIn("_16CXX", defines)
        self.assertEqual(defines["_18F4550"], "1")

    def test_version_override(self) -> None:
        defines = intellisense.intellisense_defines(self._meta("PIC14"), "PIC16F1509", cc5x_version=3700)
        self.assertEqual(defines["__CC5X__"], "3700")


class BuildIntellisenseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _project(self, **overrides: object) -> object:
        defaults = dict(
            device="PIC16F1509",
            header_path="generated_headers/16F1509.H",
            main_source="app.c",
            config_source="app.c",
        )
        defaults.update(overrides)
        return types.SimpleNamespace(**defaults)

    def _meta(self, arch: str = "PIC14E") -> object:
        return types.SimpleNamespace(ini_arch=arch)

    def _header(self, rel: str = "generated_headers/16F1509.H") -> Path:
        # A resolved header path (as ensure_project_header would return) under self.dir.
        path = self.dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// header\n", encoding="latin-1")
        return path

    def test_writes_shim_and_compile_commands(self) -> None:
        result = intellisense.build_intellisense(self.dir, self._project(), self._meta(), self._header())
        self.assertTrue(result["ok"])
        shim = Path(result["shim"])
        cc = Path(result["compile_commands"])
        self.assertTrue(shim.is_file())
        self.assertTrue(cc.is_file())
        # Paths are absolute but NOT symlink-resolved (so they match what the editor opens).
        self.assertEqual(
            shim.parent,
            Path(os.path.abspath(self.dir / intellisense.INTELLISENSE_SUBDIR)),
        )

    def test_compile_commands_entry_shape(self) -> None:
        result = intellisense.build_intellisense(self.dir, self._project(), self._meta(), self._header())
        entries = json.loads(Path(result["compile_commands"]).read_text())
        self.assertEqual(len(entries), 1)  # main_source == config_source -> deduped
        entry = entries[0]
        self.assertEqual(entry["file"], os.path.abspath(self.dir / "app.c"))
        args = entry["arguments"]
        self.assertIn("-include", args)
        self.assertIn(result["shim"], args)
        self.assertIn("-D__CC5X__=3803", args)
        self.assertIn("-D_16F1509=1", args)
        self.assertIn("-ffreestanding", args)
        # The device header's directory is on the include path so #include "<dev>.H" navigates.
        self.assertIn(os.path.abspath(self.dir / "generated_headers"), args)

    def test_supplied_header_dir_uses_resolved_path(self) -> None:
        # A supplied header beside the compiler is NOT under the manifest's header.path; the
        # include dir must follow the resolved header, not the manifest-relative path.
        supplied = self.dir / "compiler" / "16F1509.H"
        supplied.parent.mkdir(parents=True, exist_ok=True)
        supplied.write_text("// supplied\n", encoding="latin-1")
        result = intellisense.build_intellisense(self.dir, self._project(), self._meta(), supplied)
        args = json.loads(Path(result["compile_commands"]).read_text())[0]["arguments"]
        self.assertIn(os.path.abspath(supplied.parent), args)
        self.assertNotIn(os.path.abspath(self.dir / "generated_headers"), args)

    def test_distinct_sources_get_separate_entries(self) -> None:
        project = self._project(main_source="main.c", config_source="config.c")
        result = intellisense.build_intellisense(self.dir, project, self._meta(), self._header())
        entries = json.loads(Path(result["compile_commands"]).read_text())
        self.assertEqual(len(entries), 2)
        files = {Path(e["file"]).name for e in entries}
        self.assertEqual(files, {"main.c", "config.c"})

    def test_absolute_source_path_is_honored(self) -> None:
        # An absolute main_source/config_source must be used as-is (os.path.join semantics),
        # not re-rooted under the manifest directory.
        abs_src = str((self.dir / "elsewhere" / "main.c"))
        project = self._project(main_source=abs_src, config_source=abs_src)
        result = intellisense.build_intellisense(self.dir, project, self._meta(), self._header())
        entries = json.loads(Path(result["compile_commands"]).read_text())
        self.assertEqual(entries[0]["file"], os.path.abspath(abs_src))

    def test_shim_is_byte_identical_across_devices(self) -> None:
        # The shim is device-independent (device specifics live in compile_commands).
        a = intellisense.build_intellisense(self.dir, self._project(), self._meta(), self._header())
        text = Path(a["shim"]).read_text()
        self.assertEqual(text, intellisense.render_shim())


if __name__ == "__main__":
    unittest.main()
