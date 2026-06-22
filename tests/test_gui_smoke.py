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


def test_device_field_substring_completer_narrows() -> None:
    """The device field offers a case-insensitive substring dropdown so typing part of a
    device name (e.g. '1509') narrows to matching CC5X devices."""
    import os
    from unittest import mock

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication, QLineEdit
    from PyQt6.QtCore import Qt

    import cc5x_helper_gui

    app = QApplication.instance() or QApplication([])
    try:
        cc5x_helper_gui.cc5x_device_names.cache_clear()
        with mock.patch.object(
            cc5x_helper_gui,
            "merged_device_list",
            return_value=[{"device": "PIC16F1509"}, {"device": "PIC12F1840"}],
        ):
            edit = QLineEdit()
            cc5x_helper_gui.attach_device_completer(edit)
        completer = edit.completer()
        assert completer is not None, "device field should have a completer"
        assert completer.filterMode() == Qt.MatchFlag.MatchContains
        assert completer.caseSensitivity() == Qt.CaseSensitivity.CaseInsensitive
        completer.setCompletionPrefix("1509")  # substring, not prefix
        model = completer.completionModel()
        matches = [model.index(i, 0).data() for i in range(model.rowCount())]
        assert matches == ["PIC16F1509"], matches
    finally:
        cc5x_helper_gui.cc5x_device_names.cache_clear()
        app.quit()


def test_device_field_no_completer_when_no_devices() -> None:
    import os
    from unittest import mock

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication, QLineEdit

    import cc5x_helper_gui

    app = QApplication.instance() or QApplication([])
    try:
        cc5x_helper_gui.cc5x_device_names.cache_clear()
        with mock.patch.object(cc5x_helper_gui, "merged_device_list", return_value=[]):
            edit = QLineEdit()
            cc5x_helper_gui.attach_device_completer(edit)
        assert edit.completer() is None, "no devices -> plain text field, no completer"
    finally:
        cc5x_helper_gui.cc5x_device_names.cache_clear()
        app.quit()


def test_browse_compiler_sets_absolute_path_outside_project(tmp_path) -> None:
    """A compiler chosen outside the project dir is stored as an absolute path (so a
    system toolchain location is preserved verbatim)."""
    import os
    from unittest import mock

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    import cc5x_helper_gui

    app = QApplication.instance() or QApplication([])
    try:
        projects = cc5x_helper_gui.ProjectTab(lambda *_: None)
        projects.project_path_edit.setText(str(tmp_path / "proj" / "setcc-native.json"))
        external = tmp_path / "toolchain" / "CC5X.EXE"
        with mock.patch.object(
            cc5x_helper_gui.QFileDialog, "getOpenFileName", return_value=(str(external), "")
        ):
            projects.browse_compiler()
        assert projects.compiler_edit.text() == str(external)
    finally:
        app.quit()


def test_browse_header_stores_project_relative_path(tmp_path) -> None:
    """A header chosen inside the project dir is stored relative to the manifest, keeping
    the manifest portable (matching how project_path_join resolves it at build time)."""
    import os
    from unittest import mock

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    import cc5x_helper_gui

    app = QApplication.instance() or QApplication([])
    try:
        proj_dir = tmp_path / "proj"
        proj_dir.mkdir()
        projects = cc5x_helper_gui.ProjectTab(lambda *_: None)
        projects.project_path_edit.setText(str(proj_dir / "setcc-native.json"))
        header = proj_dir / "generated_headers" / "16F1509.H"
        with mock.patch.object(
            cc5x_helper_gui.QFileDialog, "getSaveFileName", return_value=(str(header), "")
        ):
            projects.browse_header_path()
        assert projects.header_path_edit.text() == os.path.join("generated_headers", "16F1509.H")
    finally:
        app.quit()


def test_browse_compiler_inside_project_stays_absolute(tmp_path) -> None:
    """Toolchain fields (compiler/runner/mplab) must NOT be manifest-relative: the CLI and
    extension resolve them against the process CWD, so a relative value would break outside
    the GUI. Even a compiler living inside the project dir is stored absolute."""
    import os
    from unittest import mock

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    import cc5x_helper_gui

    app = QApplication.instance() or QApplication([])
    try:
        proj_dir = tmp_path / "proj"
        proj_dir.mkdir()
        projects = cc5x_helper_gui.ProjectTab(lambda *_: None)
        projects.project_path_edit.setText(str(proj_dir / "setcc-native.json"))
        compiler = proj_dir / "toolchain" / "CC5X.EXE"
        with mock.patch.object(
            cc5x_helper_gui.QFileDialog, "getOpenFileName", return_value=(str(compiler), "")
        ):
            projects.browse_compiler()
        # Absolute, NOT relativized to "toolchain/CC5X.EXE".
        assert projects.compiler_edit.text() == str(compiler)
    finally:
        app.quit()


def test_new_project_appended_suffix_confirms_before_overwrite(tmp_path) -> None:
    """Typing a name without an extension where <name>.json already exists must re-confirm
    before overwriting (the native Save dialog only confirmed the typed name)."""
    import os
    from unittest import mock

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    import cc5x_helper_gui

    app = QApplication.instance() or QApplication([])
    try:
        existing = tmp_path / "proj.json"
        existing.write_text('{"keep": true}', encoding="utf-8")
        projects = cc5x_helper_gui.ProjectTab(lambda *_: None)
        with mock.patch.object(
            cc5x_helper_gui.QFileDialog,
            "getSaveFileName",
            return_value=(str(tmp_path / "proj"), ""),  # no extension -> code appends .json
        ), mock.patch.object(
            cc5x_helper_gui.QMessageBox,
            "question",
            return_value=cc5x_helper_gui.QMessageBox.StandardButton.No,
        ):
            projects.new_project()
        # Declining the overwrite must leave the existing manifest untouched.
        assert existing.read_text(encoding="utf-8") == '{"keep": true}'
    finally:
        app.quit()


def test_browse_cancel_leaves_field_unchanged(tmp_path) -> None:
    """Cancelling a browse dialog must not overwrite the existing field value."""
    import os
    from unittest import mock

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    import cc5x_helper_gui

    app = QApplication.instance() or QApplication([])
    try:
        projects = cc5x_helper_gui.ProjectTab(lambda *_: None)
        before = projects.runner_edit.text()
        with mock.patch.object(
            cc5x_helper_gui.QFileDialog, "getOpenFileName", return_value=("", "")
        ):
            projects.browse_runner()
        assert projects.runner_edit.text() == before
    finally:
        app.quit()


def test_browse_with_empty_project_path_stores_absolute(tmp_path) -> None:
    """With an empty project-path field there is no manifest dir, so a browsed path must
    be stored absolute (never relativized against the parent of the cwd) and the dialog
    must not crash computing a project dir."""
    import os
    from unittest import mock

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    import cc5x_helper_gui

    app = QApplication.instance() or QApplication([])
    try:
        projects = cc5x_helper_gui.ProjectTab(lambda *_: None)
        projects.project_path_edit.clear()
        assert projects._project_dir() is None
        chosen = tmp_path / "anywhere" / "app.c"
        with mock.patch.object(
            cc5x_helper_gui.QFileDialog, "getOpenFileName", return_value=(str(chosen), "")
        ):
            projects.browse_main_source()
        # Must be the absolute path, not something relativized to the cwd's parent.
        assert projects.main_source_edit.text() == str(chosen)
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
