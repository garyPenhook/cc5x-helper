from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.cc5x_setcc_native_lib.project import (
    default_project_manifest,
    load_project_file,
    validate_project_file,
    write_project_file,
)


class ProjectFileTests(unittest.TestCase):
    def test_default_project_manifest_uses_generated_header(self) -> None:
        project = default_project_manifest(
            device="16f1509",
            compiler="/opt/cc5x/CC5X.EXE",
            runner="/home/gary/apps/cc5x-run.sh",
            main_source="app.c",
        )
        self.assertEqual(project.device, "PIC16F1509")
        self.assertEqual(project.header_mode, "generated")
        self.assertEqual(project.header_path, "generated_headers/16F1509.H")
        self.assertEqual(sorted(project.editions), ["debug", "production"])

    def test_write_and_load_project_file_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "setcc-native.json"
            project = default_project_manifest(
                device="PIC12F1840",
                compiler="/compiler/CC5X.EXE",
                runner="/runner/cc5x-run.sh",
                main_source="firmware/main.c",
                config_source="firmware/config.c",
                header_mode="existing",
                header_path="include/12F1840.H",
            )
            write_project_file(project, path)
            loaded = load_project_file(path)
            self.assertEqual(loaded.device, "PIC12F1840")
            self.assertEqual(loaded.main_source, "firmware/main.c")
            self.assertEqual(loaded.config_source, "firmware/config.c")
            self.assertEqual(loaded.header_mode, "existing")
            self.assertEqual(loaded.header_path, "include/12F1840.H")

    def test_validate_project_file_rejects_non_cc5x_family(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "setcc-native.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "device": "PIC18F47Q10",
                        "compiler": "/compiler/CC5X.EXE",
                        "runner": "/runner/cc5x-run.sh",
                        "header": {"mode": "generated", "path": "generated_headers/18F47Q10.H"},
                        "config_source": "app.c",
                        "main_source": "app.c",
                        "build_options": [],
                        "editions": {"production": {"config": {}, "build_options": []}},
                    }
                ),
                encoding="utf-8",
            )
            loaded = load_project_file(path)
            errors = validate_project_file(loaded)
            self.assertTrue(any("PIC10F/PIC12F/PIC16F" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
