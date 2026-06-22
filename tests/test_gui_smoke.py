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
