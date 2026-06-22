"""Smoke coverage for the optional PyQt6 GUI module.

These run only when the ``gui`` extra (PyQt6) is installed; otherwise they skip so
the base test suite stays green without the optional dependency.
"""
from __future__ import annotations

import importlib.util

import pytest

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("PyQt6") is None,
    reason="PyQt6 (gui extra) not installed",
)


def test_gui_module_imports() -> None:
    import cc5x_helper_gui

    assert hasattr(cc5x_helper_gui, "main")
    assert hasattr(cc5x_helper_gui, "MainWindow")
    assert hasattr(cc5x_helper_gui, "ProjectTab")


def test_new_project_creates_manifest_and_source_in_user_dir(tmp_path) -> None:
    """New must create the manifest at a user-chosen location and scaffold the main
    source there, so a project in an arbitrary user folder is immediately usable."""
    import os
    from unittest import mock

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    import cc5x_helper_gui

    app = QApplication.instance() or QApplication([])
    try:
        projects = cc5x_helper_gui.ProjectTab(lambda *_: None)
        target = tmp_path / "my project" / "setcc-native.json"
        with mock.patch.object(
            cc5x_helper_gui.QFileDialog, "getSaveFileName", return_value=(str(target), "")
        ):
            projects.new_project()
        assert target.is_file(), "manifest not created at the user-chosen location"
        assert (target.parent / "app.c").is_file(), "main source was not scaffolded"
        assert projects.project_path_edit.text() == str(target)
    finally:
        app.quit()


def test_new_project_cancel_writes_nothing(tmp_path) -> None:
    import os
    from unittest import mock

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    import cc5x_helper_gui

    app = QApplication.instance() or QApplication([])
    try:
        projects = cc5x_helper_gui.ProjectTab(lambda *_: None)
        with mock.patch.object(
            cc5x_helper_gui.QFileDialog, "getSaveFileName", return_value=("", "")
        ):
            projects.new_project()
        assert not any(tmp_path.iterdir()), "cancel must not create any files"
    finally:
        app.quit()


def test_build_lock_buttons_toggle() -> None:
    """The build-state lock list must be populated and toggle together, otherwise
    the "disable controls during a build" safety guard is a no-op."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    import cc5x_helper_gui

    app = QApplication.instance() or QApplication([])
    try:
        projects = cc5x_helper_gui.ProjectTab(lambda *_: None)
        assert projects._build_lock_buttons, "expected build-lock buttons to be registered"
        projects._set_build_running(True)
        assert all(not button.isEnabled() for button in projects._build_lock_buttons)
        projects._set_build_running(False)
        assert all(button.isEnabled() for button in projects._build_lock_buttons)
    finally:
        app.quit()
