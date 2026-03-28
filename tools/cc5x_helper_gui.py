#!/usr/bin/env python3
from __future__ import annotations

import json
import inspect
import os
import shlex
import subprocess
import sys
import threading
import traceback
from argparse import Namespace
from functools import wraps
from pathlib import Path

if sys.platform.startswith("linux"):
    if "QT_QPA_PLATFORM" not in os.environ:
        if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            os.environ["QT_QPA_PLATFORM"] = "xcb"
        else:
            os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ.setdefault("QT_STYLE_OVERRIDE", "Fusion")
    os.environ.setdefault("QT_QPA_PLATFORMTHEME", "")

from PyQt6.QtCore import QProcess, Qt
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

try:
    from cc5x_setcc_native import (
        DEFAULT_COMPILER,
        DEFAULT_RUNNER,
        build_command,
        default_pack_symbol_values,
        ensure_project_header,
        environment_report,
        find_device_metadata,
        load_project_and_edition,
        pack_config_symbols,
        parse_key_value_pairs,
        project_metadata,
        project_path_join,
        render_config_block_from_symbols,
        render_dynamic_config_section,
        render_full_header,
        update_managed_block,
    )
    from cc5x_setcc_native_lib.packs import list_devices_in_atpacks
    from cc5x_setcc_native_lib.picmeta import load_device_metadata
    from cc5x_setcc_native_lib.project import (
        default_project_manifest,
        delete_project_edition,
        load_project_file,
        project_summary,
        remove_project_edition_config,
        set_project_edition,
        update_project_edition_build_options,
        update_project_edition_config,
        update_project_fields,
        validate_project_file,
        write_project_file,
    )
except ModuleNotFoundError:
    from tools.cc5x_setcc_native import (
        DEFAULT_COMPILER,
        DEFAULT_RUNNER,
        build_command,
        default_pack_symbol_values,
        ensure_project_header,
        environment_report,
        find_device_metadata,
        load_project_and_edition,
        pack_config_symbols,
        parse_key_value_pairs,
        project_metadata,
        project_path_join,
        render_config_block_from_symbols,
        render_dynamic_config_section,
        render_full_header,
        update_managed_block,
    )
    from tools.cc5x_setcc_native_lib.packs import list_devices_in_atpacks
    from tools.cc5x_setcc_native_lib.picmeta import load_device_metadata
    from tools.cc5x_setcc_native_lib.project import (
        default_project_manifest,
        delete_project_edition,
        load_project_file,
        project_summary,
        remove_project_edition_config,
        set_project_edition,
        update_project_edition_build_options,
        update_project_edition_config,
        update_project_fields,
        validate_project_file,
        write_project_file,
    )


def format_json(payload: object) -> str:
    return json.dumps(payload, indent=2)


def candidate_project_paths() -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()

    env_path = os.environ.get("CC5X_HELPER_PROJECT")
    if env_path:
        path = Path(env_path).expanduser()
        candidates.append(path)
        seen.add(path)

    roots = [Path.cwd()]
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    else:
        roots.append(Path(__file__).resolve().parent)

    for root in roots:
        if root.name == "dist":
            candidate = root.parent / "setcc-native.json"
            if candidate not in seen:
                candidates.append(candidate)
                seen.add(candidate)
        for base in [root, *root.parents]:
            candidate = base / "setcc-native.json"
            if candidate not in seen:
                candidates.append(candidate)
                seen.add(candidate)

    return candidates


def default_project_path() -> Path:
    candidates = candidate_project_paths()
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def help_sections() -> list[tuple[str, str]]:
    return [
        (
            "Welcome",
            """
            <h1>cc5x-helper GUI Help</h1>
            <p>This GUI is a front end for the pack-first CC5X helper workflow. It targets BKND CC5X projects for PIC10F, PIC12F, and PIC16F devices, using modern Microchip device packs where possible.</p>
            <h2>What the app does</h2>
            <ul>
              <li>Checks your environment and installed device packs.</li>
              <li>Inspects device metadata directly from Microchip packs.</li>
              <li>Generates CC5X-style headers and managed <code>#pragma config</code> blocks.</li>
              <li>Creates and edits a project manifest named <code>setcc-native.json</code>.</li>
              <li>Builds projects through CC5X, optionally through CrossOver or Wine.</li>
            </ul>
            <h2>Where to start</h2>
            <ol>
              <li>Open <b>Environment</b> and run <b>Doctor</b>.</li>
              <li>Open <b>Devices</b> and probe your target part.</li>
              <li>Open <b>Projects</b>, create a new manifest, then add at least one edition.</li>
              <li>Use <b>Sync Config</b> before a real build if your source file should contain a managed config block.</li>
              <li>Use <b>Dry Run Build</b> first, then <b>Build</b>.</li>
            </ol>
            <h2>Manifest location</h2>
            <p>The default manifest file is <code>setcc-native.json</code>. The GUI tries to find it in sensible places, including the project root above <code>dist/</code>. You can also force a manifest path with the <code>CC5X_HELPER_PROJECT</code> environment variable.</p>
            """,
        ),
        (
            "Environment",
            """
            <h1>Environment Tab</h1>
            <p>This tab answers a simple question: is this machine ready to use cc5x-helper?</p>
            <h2>Controls</h2>
            <ul>
              <li><b>Family</b>: filters the device inventory to PIC10F, PIC12F, PIC16F, or all supported families.</li>
              <li><b>Doctor</b>: shows a readiness report. Use this first on a new machine.</li>
              <li><b>List Devices</b>: lists locally discoverable devices from installed Microchip packs.</li>
            </ul>
            <h2>How to read Doctor output</h2>
            <ul>
              <li>Pack availability tells you whether the machine has source metadata for supported devices.</li>
              <li>Compiler and runner paths tell you whether the configured CC5X toolchain exists.</li>
              <li>Validated devices indicate which parts have already passed the real compiler-validation flow in this repo.</li>
            </ul>
            <h2>When to use this tab</h2>
            <ul>
              <li>First run on a new machine.</li>
              <li>After installing or moving Microchip device packs.</li>
              <li>After changing your CrossOver, Wine, or CC5X installation.</li>
            </ul>
            """,
        ),
        (
            "Devices",
            """
            <h1>Devices Tab</h1>
            <p>This tab is for raw device exploration without needing a project manifest.</p>
            <h2>Fields</h2>
            <ul>
              <li><b>Device</b>: the target part, for example <code>PIC16F1509</code>.</li>
              <li><b>MPLAB Root</b>: optional override for older MPLAB metadata locations. Leave it blank unless you have a specific legacy install to use.</li>
            </ul>
            <h2>Actions</h2>
            <ul>
              <li><b>Probe</b>: shows where metadata for the device was found, such as pack files, <code>.PIC</code>, <code>.ini</code>, and <code>cfgdata</code>.</li>
              <li><b>Describe</b>: shows normalized device metadata parsed from those sources.</li>
              <li><b>List Config</b>: lists available config symbols and their legal values.</li>
              <li><b>Render Header</b>: generates a CC5X-style header from pack metadata.</li>
              <li><b>Render Config</b>: generates a managed config block using default symbol values.</li>
            </ul>
            <h2>Typical use</h2>
            <ol>
              <li>Enter the device name.</li>
              <li>Run <b>Probe</b> to confirm the device is actually available locally.</li>
              <li>Run <b>Describe</b> to inspect parsed metadata.</li>
              <li>Run <b>List Config</b> before deciding per-edition config values.</li>
              <li>Run <b>Render Header</b> or <b>Render Config</b> when you need ad hoc output.</li>
            </ol>
            <h2>What this tab does not do</h2>
            <p>It does not save project state. Use the <b>Projects</b> tab for repeatable builds and edition management.</p>
            """,
        ),
        (
            "Projects",
            """
            <h1>Projects Tab</h1>
            <p>This is the main working area for repeatable builds. It manages a JSON manifest, edition-specific config values, build options, header generation, and actual builds.</p>
            <h2>Project area</h2>
            <ul>
              <li><b>Project</b>: path to <code>setcc-native.json</code>.</li>
              <li><b>Browse</b>: opens an existing manifest.</li>
              <li><b>New</b>: creates a new manifest at the current path using the visible project fields.</li>
              <li><b>Load</b>: loads the selected manifest into the editor.</li>
            </ul>
            <h2>Project fields</h2>
            <ul>
              <li><b>Device</b>: target MCU, such as <code>PIC12F1840</code> or <code>PIC16F15313</code>.</li>
              <li><b>Compiler</b>: CC5X executable path.</li>
              <li><b>Runner</b>: optional wrapper command, such as CrossOver or Wine.</li>
              <li><b>MPLAB Root</b>: optional legacy metadata override.</li>
              <li><b>Header Mode</b>: <code>generated</code>, <code>supplied</code>, or <code>existing</code>.</li>
              <li><b>Header Path</b>: path used for the selected header mode.</li>
              <li><b>Main Source</b>: source file passed to the compiler.</li>
              <li><b>Config Source</b>: source file that will receive the managed config block.</li>
            </ul>
            <h2>Editions</h2>
            <p>An edition is a named variant of the same project. Typical examples are <code>production</code>, <code>debug</code>, and <code>qa</code>.</p>
            <ul>
              <li><b>Add</b>: creates a new edition. If another edition is selected, the new one copies that edition.</li>
              <li><b>Delete</b>: removes the selected edition.</li>
              <li><b>Edition Config</b>: one <code>NAME=VALUE</code> pair per line. Example: <code>FOSC=INTOSC</code>.</li>
              <li><b>Edition Build Options</b>: one compiler option per line. Example: <code>-a</code>.</li>
            </ul>
            <h2>Actions</h2>
            <ul>
              <li><b>Save Project</b>: validates and writes the project fields back to the manifest.</li>
              <li><b>Show Summary</b>: prints a normalized summary of the manifest.</li>
              <li><b>Show Edition</b>: shows the current edition's config and build options.</li>
              <li><b>List Editions</b>: lists all editions with counts.</li>
              <li><b>Sync Config</b>: writes or updates the managed config block in the configured source file.</li>
              <li><b>Dry Run Build</b>: shows the exact build command without executing it.</li>
              <li><b>Build</b>: starts an actual compiler run with live output.</li>
              <li><b>Render Header</b>: generates or resolves the header and shows its text.</li>
              <li><b>Render Config</b>: previews the config block for the selected edition.</li>
            </ul>
            """,
        ),
        (
            "Header Modes",
            """
            <h1>Header Modes</h1>
            <ul>
              <li><b>generated</b>: preferred for pack-first workflows. The tool generates the header from Microchip pack metadata.</li>
              <li><b>supplied</b>: use a specific header file path supplied by the project manifest.</li>
              <li><b>existing</b>: use an already-existing header path directly, without generation.</li>
            </ul>
            <h2>When to use each mode</h2>
            <ul>
              <li>Use <b>generated</b> for newer devices or when BKND did not ship a header.</li>
              <li>Use <b>supplied</b> when you want to keep a generated header under version control at a specific path.</li>
              <li>Use <b>existing</b> when you already maintain the header yourself.</li>
            </ul>
            """,
        ),
        (
            "Config Workflow",
            """
            <h1>Managed Config Workflow</h1>
            <p>cc5x-helper treats configuration as generated content derived from device metadata and edition-level overrides.</p>
            <h2>Recommended flow</h2>
            <ol>
              <li>Use the <b>Devices</b> tab to inspect available config symbols.</li>
              <li>Create or load a project manifest.</li>
              <li>Add at least one edition.</li>
              <li>Enter edition config values as <code>NAME=VALUE</code> pairs.</li>
              <li>Preview with <b>Render Config</b>.</li>
              <li>Write the managed block with <b>Sync Config</b>.</li>
            </ol>
            <h2>Important behavior</h2>
            <ul>
              <li><b>Render Config</b> is preview only.</li>
              <li><b>Sync Config</b> modifies the configured source file.</li>
              <li>The tool merges pack defaults with edition overrides, with the edition winning.</li>
            </ul>
            """,
        ),
        (
            "Build Workflow",
            """
            <h1>Build Workflow</h1>
            <h2>What Build does</h2>
            <ul>
              <li>Loads the manifest and selected edition.</li>
              <li>Ensures the required header is available.</li>
              <li>Builds the CC5X command line using the project device, include path, base options, and edition options.</li>
              <li>Runs the command directly or through the configured runner.</li>
              <li>Streams compiler output into the right-hand output pane.</li>
            </ul>
            <h2>Recommended sequence</h2>
            <ol>
              <li>Save the project.</li>
              <li>Render or sync config if needed.</li>
              <li>Use <b>Dry Run Build</b> to inspect the command line.</li>
              <li>Use <b>Build</b> for a real compile.</li>
            </ol>
            <h2>One active build at a time</h2>
            <p>The GUI prevents launching another build while one is already running.</p>
            """,
        ),
        (
            "Troubleshooting",
            """
            <h1>Troubleshooting</h1>
            <h2>Common issues</h2>
            <ul>
              <li><b>project file not found</b>: use <b>New</b> to create a manifest or <b>Browse</b> to select an existing one.</li>
              <li><b>no edition selected</b>: create an edition and select it before edition-specific actions.</li>
              <li><b>device metadata not found</b>: verify the device exists in installed packs, then run <b>Doctor</b> and <b>Probe</b>.</li>
              <li><b>build fails immediately</b>: check the compiler path, runner path, and build command via <b>Dry Run Build</b>.</li>
              <li><b>packaged GUI launched from dist</b>: the GUI now prefers the project root above <code>dist/</code>, but you can also set <code>CC5X_HELPER_PROJECT</code> explicitly.</li>
            </ul>
            <h2>Error handling</h2>
            <p>The GUI wraps button actions and application-level exceptions. Normal user errors should appear as dialogs instead of crashing the process.</p>
            <h2>When to inspect raw output</h2>
            <p>Use the output pane whenever a command succeeds but the result is not what you expected. The app shows raw JSON, header text, config blocks, and compiler output there.</p>
            """,
        ),
    ]


def parse_multiline_pairs(text: str) -> dict[str, str]:
    items = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        items.append(line)
    return parse_key_value_pairs(items)


def parse_multiline_options(text: str) -> list[str]:
    options: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        options.append(line)
    return options


def gui_action(method):
    signature = inspect.signature(method)
    positional_params = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    accepts_varargs = any(
        parameter.kind == inspect.Parameter.VAR_POSITIONAL
        for parameter in signature.parameters.values()
    )
    max_args = max(0, len(positional_params) - 1)

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        try:
            if accepts_varargs:
                return method(self, *args, **kwargs)
            return method(self, *args[:max_args], **kwargs)
        except SystemExit as exc:
            self.show_error(str(exc) or method.__name__)
        except Exception as exc:
            self.show_error(f"{type(exc).__name__}: {exc}")
        return None

    return wrapped


def format_exception_message(exc_type, exc_value, exc_traceback) -> str:
    summary = "".join(traceback.format_exception_only(exc_type, exc_value)).strip()
    details = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    return summary, details


def show_global_error(summary: str, details: str) -> None:
    print(details, file=sys.stderr, flush=True)
    app = QApplication.instance()
    if app is None:
        return
    active = app.activeWindow()
    box = QMessageBox(active)
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle("cc5x-helper")
    box.setText(summary)
    box.setDetailedText(details)
    box.exec()


def install_exception_hooks() -> None:
    def handle_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        summary, details = format_exception_message(exc_type, exc_value, exc_traceback)
        show_global_error(summary, details)

    def handle_thread_exception(args):
        summary, details = format_exception_message(
            args.exc_type,
            args.exc_value,
            args.exc_traceback,
        )
        show_global_error(summary, details)

    sys.excepthook = handle_exception
    threading.excepthook = handle_thread_exception


class SafeApplication(QApplication):
    def notify(self, receiver, event):
        try:
            return super().notify(receiver, event)
        except Exception:
            summary, details = format_exception_message(*sys.exc_info())
            show_global_error(summary, details)
            return False


class OutputPane(QPlainTextEdit):
    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

    def write_text(self, text: str) -> None:
        self.setPlainText(text)

    def append_text(self, text: str) -> None:
        cursor = self.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()


class HelpDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("cc5x-helper Help")
        self.resize(1000, 760)

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        for title, html in help_sections():
            page = QTextEdit()
            page.setReadOnly(True)
            page.setHtml(html)
            tabs.addTab(page, title)
        layout.addWidget(tabs)
        self.tabs = tabs

    def show_section(self, title: str) -> None:
        for index in range(self.tabs.count()):
            if self.tabs.tabText(index) == title:
                self.tabs.setCurrentIndex(index)
                break
        self.show()
        self.raise_()
        self.activateWindow()


class DeviceTab(QWidget):
    def __init__(self, open_help=None) -> None:
        super().__init__()
        self.open_help = open_help
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.device_edit = QLineEdit("PIC16F1509")
        self.mplab_root_edit = QLineEdit()
        form.addRow("Device", self.device_edit)
        form.addRow("MPLAB Root", self.mplab_root_edit)
        layout.addLayout(form)

        button_row = QHBoxLayout()
        for label, handler in (
            ("Probe", self.run_probe),
            ("Describe", self.run_describe),
            ("List Config", self.run_list_pack_config),
            ("Render Header", self.run_render_header),
            ("Render Config", self.run_render_config),
        ):
            button = QPushButton(label)
            button.clicked.connect(handler)
            button_row.addWidget(button)
        help_button = QPushButton("Help")
        help_button.clicked.connect(self.show_help)
        button_row.addWidget(help_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self.output = OutputPane()
        layout.addWidget(self.output, 1)

    def _device(self) -> str:
        return self.device_edit.text().strip()

    def _mplab_root(self) -> str | None:
        text = self.mplab_root_edit.text().strip()
        return text or None

    def _metadata(self):
        result = find_device_metadata(self._device(), self._mplab_root())
        metadata = load_device_metadata(
            device=result["device"],
            ini_reference=result.get("pack_ini") or result.get("ini"),
            cfgdata_reference=result.get("pack_cfgdata") or result.get("cfgdata"),
            pic_reference=result.get("pic"),
        )
        return result, metadata

    def show_error(self, message: str) -> None:
        QMessageBox.critical(self, "cc5x-helper", message)

    @gui_action
    def show_help(self) -> None:
        if self.open_help is not None:
            self.open_help("Devices")

    @gui_action
    def run_probe(self) -> None:
        result = find_device_metadata(self._device(), self._mplab_root())
        self.output.write_text(format_json(result))

    @gui_action
    def run_describe(self) -> None:
        _, metadata = self._metadata()
        self.output.write_text(metadata.to_json())

    @gui_action
    def run_list_pack_config(self) -> None:
        _, metadata = self._metadata()
        payload = {
            name: sorted(symbol.options)
            for name, symbol in sorted(pack_config_symbols(metadata).items())
        }
        self.output.write_text(format_json(payload))

    @gui_action
    def run_render_header(self) -> None:
        _, metadata = self._metadata()
        self.output.write_text(render_full_header(metadata))

    @gui_action
    def run_render_config(self) -> None:
        _, metadata = self._metadata()
        symbols = pack_config_symbols(metadata)
        settings = default_pack_symbol_values(metadata)
        self.output.write_text(
            render_config_block_from_symbols(
                source_label=f"pack metadata for {metadata.device}",
                family="5x",
                symbols=symbols,
                settings=settings,
            )
        )


class EnvironmentTab(QWidget):
    def __init__(self, open_help=None) -> None:
        super().__init__()
        self.open_help = open_help
        layout = QVBoxLayout(self)

        controls = QHBoxLayout()
        self.family_combo = QComboBox()
        self.family_combo.addItems(["All", "PIC10F", "PIC12F", "PIC16F"])
        controls.addWidget(QLabel("Family"))
        controls.addWidget(self.family_combo)

        doctor_button = QPushButton("Doctor")
        doctor_button.clicked.connect(self.show_doctor)
        controls.addWidget(doctor_button)

        list_button = QPushButton("List Devices")
        list_button.clicked.connect(self.show_devices)
        controls.addWidget(list_button)
        help_button = QPushButton("Help")
        help_button.clicked.connect(self.show_help)
        controls.addWidget(help_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.output = OutputPane()
        layout.addWidget(self.output, 1)

    def show_error(self, message: str) -> None:
        QMessageBox.critical(self, "cc5x-helper", message)

    @gui_action
    def show_help(self) -> None:
        if self.open_help is not None:
            self.open_help("Environment")

    @gui_action
    def show_doctor(self) -> None:
        self.output.write_text(format_json(environment_report()))

    @gui_action
    def show_devices(self) -> None:
        family = self.family_combo.currentText()
        prefixes = () if family == "All" else (family,)
        self.output.write_text(format_json(list_devices_in_atpacks(prefixes=prefixes)))


class ProjectTab(QWidget):
    def __init__(self, open_help=None) -> None:
        super().__init__()
        self.open_help = open_help
        self.process: QProcess | None = None

        root_layout = QVBoxLayout(self)
        path_row = QHBoxLayout()
        self.project_path_edit = QLineEdit(str(default_project_path()))
        path_row.addWidget(QLabel("Project"))
        path_row.addWidget(self.project_path_edit, 1)
        browse_button = QPushButton("Browse")
        browse_button.clicked.connect(self.browse_project)
        path_row.addWidget(browse_button)
        new_button = QPushButton("New")
        new_button.clicked.connect(self.new_project)
        path_row.addWidget(new_button)
        load_button = QPushButton("Load")
        load_button.clicked.connect(self.load_project)
        path_row.addWidget(load_button)
        help_button = QPushButton("Help")
        help_button.clicked.connect(self.show_help)
        path_row.addWidget(help_button)
        root_layout.addLayout(path_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root_layout.addWidget(splitter, 1)

        left = QWidget()
        left_layout = QVBoxLayout(left)

        project_group = QGroupBox("Project")
        project_form = QFormLayout(project_group)
        self.device_edit = QLineEdit()
        self.compiler_edit = QLineEdit()
        self.runner_edit = QLineEdit()
        self.mplab_root_edit = QLineEdit()
        self.header_mode_combo = QComboBox()
        self.header_mode_combo.addItems(["generated", "supplied", "existing"])
        self.header_path_edit = QLineEdit()
        self.main_source_edit = QLineEdit()
        self.config_source_edit = QLineEdit()
        project_form.addRow("Device", self.device_edit)
        project_form.addRow("Compiler", self.compiler_edit)
        project_form.addRow("Runner", self.runner_edit)
        project_form.addRow("MPLAB Root", self.mplab_root_edit)
        project_form.addRow("Header Mode", self.header_mode_combo)
        project_form.addRow("Header Path", self.header_path_edit)
        project_form.addRow("Main Source", self.main_source_edit)
        project_form.addRow("Config Source", self.config_source_edit)
        left_layout.addWidget(project_group)

        project_buttons = QHBoxLayout()
        save_project_button = QPushButton("Save Project")
        save_project_button.clicked.connect(self.save_project_fields)
        project_buttons.addWidget(save_project_button)
        show_project_button = QPushButton("Show Summary")
        show_project_button.clicked.connect(self.show_project_summary)
        project_buttons.addWidget(show_project_button)
        left_layout.addLayout(project_buttons)

        edition_group = QGroupBox("Editions")
        edition_layout = QVBoxLayout(edition_group)
        edition_row = QHBoxLayout()
        self.edition_list = QListWidget()
        self.edition_list.currentTextChanged.connect(self.load_selected_edition)
        edition_layout.addWidget(self.edition_list)
        self.new_edition_edit = QLineEdit()
        self.new_edition_edit.setPlaceholderText("new edition name")
        edition_row.addWidget(self.new_edition_edit, 1)
        add_edition_button = QPushButton("Add")
        add_edition_button.clicked.connect(self.add_edition)
        edition_row.addWidget(add_edition_button)
        delete_edition_button = QPushButton("Delete")
        delete_edition_button.clicked.connect(self.delete_edition)
        edition_row.addWidget(delete_edition_button)
        edition_layout.addLayout(edition_row)
        left_layout.addWidget(edition_group, 1)
        splitter.addWidget(left)

        middle = QWidget()
        middle_layout = QVBoxLayout(middle)

        config_group = QGroupBox("Edition Config")
        config_layout = QVBoxLayout(config_group)
        self.config_edit = QPlainTextEdit()
        self.config_edit.setPlaceholderText("FOSC=INTOSC\nWDTE=OFF")
        config_layout.addWidget(self.config_edit)
        config_buttons = QHBoxLayout()
        save_config_button = QPushButton("Save Config")
        save_config_button.clicked.connect(self.save_edition_config)
        config_buttons.addWidget(save_config_button)
        remove_config_button = QPushButton("Clear Config")
        remove_config_button.clicked.connect(self.clear_edition_config)
        config_buttons.addWidget(remove_config_button)
        config_layout.addLayout(config_buttons)
        middle_layout.addWidget(config_group, 1)

        build_group = QGroupBox("Edition Build Options")
        build_layout = QVBoxLayout(build_group)
        self.build_options_edit = QPlainTextEdit()
        self.build_options_edit.setPlaceholderText("-a\n-k")
        build_layout.addWidget(self.build_options_edit)
        save_build_button = QPushButton("Save Build Options")
        save_build_button.clicked.connect(self.save_build_options)
        build_layout.addWidget(save_build_button)
        middle_layout.addWidget(build_group, 1)
        splitter.addWidget(middle)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        action_grid = QGridLayout()
        actions = [
            ("Show Edition", self.show_selected_edition),
            ("List Editions", self.show_editions),
            ("Sync Config", self.sync_config),
            ("Dry Run Build", self.build_dry_run),
            ("Build", self.build_project),
            ("Render Header", self.render_header),
            ("Render Config", self.render_config),
        ]
        for index, (label, handler) in enumerate(actions):
            button = QPushButton(label)
            button.clicked.connect(handler)
            action_grid.addWidget(button, index // 2, index % 2)
        right_layout.addLayout(action_grid)

        self.output = OutputPane()
        right_layout.addWidget(self.output, 1)
        splitter.addWidget(right)
        splitter.setSizes([360, 320, 520])

        self._set_defaults()

    def _set_defaults(self) -> None:
        self.device_edit.setText("PIC16F1509")
        self.compiler_edit.setText(str(DEFAULT_COMPILER))
        self.runner_edit.setText(str(DEFAULT_RUNNER))
        self.header_mode_combo.setCurrentText("generated")
        self.header_path_edit.setText("generated_headers/16F1509.H")
        self.main_source_edit.setText("app.c")
        self.config_source_edit.setText("app.c")

    def project_path(self) -> Path:
        return Path(self.project_path_edit.text().strip())

    def require_project_path(self) -> Path:
        path = self.project_path()
        if not path.exists():
            raise SystemExit(
                f"project file not found: {path}\nUse New to create one or Browse to open an existing manifest."
            )
        return path

    def show_error(self, message: str) -> None:
        QMessageBox.critical(self, "cc5x-helper", message)

    @gui_action
    def show_help(self) -> None:
        if self.open_help is not None:
            self.open_help("Projects")

    def current_edition(self) -> str:
        item = self.edition_list.currentItem()
        if item is None:
            raise SystemExit("no edition selected")
        return item.text()

    @gui_action
    def browse_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Project Manifest",
            str(self.project_path()),
            "JSON Files (*.json);;All Files (*)",
        )
        if path:
            self.project_path_edit.setText(path)
            self.load_project()

    @gui_action
    def new_project(self) -> None:
        project_path = self.project_path()
        project = default_project_manifest(
            device=self.device_edit.text().strip() or "PIC16F1509",
            compiler=self.compiler_edit.text().strip() or str(DEFAULT_COMPILER),
            runner=self.runner_edit.text().strip() or None,
            main_source=self.main_source_edit.text().strip() or "app.c",
            config_source=self.config_source_edit.text().strip() or "app.c",
            header_mode=self.header_mode_combo.currentText(),
            header_path=self.header_path_edit.text().strip() or None,
            mplab_root=self.mplab_root_edit.text().strip() or None,
        )
        project_path.parent.mkdir(parents=True, exist_ok=True)
        write_project_file(project, project_path)
        self.load_project()
        self.output.write_text(f"created {project_path}")

    @gui_action
    def load_project(self) -> None:
        project = load_project_file(self.require_project_path())
        self.device_edit.setText(project.device)
        self.compiler_edit.setText(project.compiler)
        self.runner_edit.setText(project.runner or "")
        self.mplab_root_edit.setText(project.mplab_root or "")
        self.header_mode_combo.setCurrentText(project.header_mode)
        self.header_path_edit.setText(project.header_path)
        self.main_source_edit.setText(project.main_source)
        self.config_source_edit.setText(project.config_source)
        self.edition_list.clear()
        for name in sorted(project.editions):
            self.edition_list.addItem(name)
        if self.edition_list.count():
            self.edition_list.setCurrentRow(0)
        self.output.write_text(format_json(project_summary(project)))

    @gui_action
    def save_project_fields(self) -> None:
        project = load_project_file(self.require_project_path())
        project = update_project_fields(
            project,
            device=self.device_edit.text().strip(),
            compiler=self.compiler_edit.text().strip(),
            runner=self.runner_edit.text().strip() or None,
            mplab_root=self.mplab_root_edit.text().strip() or None,
            header_mode=self.header_mode_combo.currentText(),
            header_path=self.header_path_edit.text().strip(),
            config_source=self.config_source_edit.text().strip(),
            main_source=self.main_source_edit.text().strip(),
            clear_runner=not bool(self.runner_edit.text().strip()),
            clear_mplab_root=not bool(self.mplab_root_edit.text().strip()),
        )
        errors = validate_project_file(project)
        if errors:
            raise SystemExit("\n".join(errors))
        write_project_file(project, self.project_path())
        self.load_project()

    @gui_action
    def add_edition(self) -> None:
        name = self.new_edition_edit.text().strip()
        if not name:
            raise SystemExit("edition name is required")
        project = load_project_file(self.require_project_path())
        source_name = self.edition_list.currentItem().text() if self.edition_list.currentItem() else None
        project = set_project_edition(project, name, from_edition=source_name)
        write_project_file(project, self.project_path())
        self.new_edition_edit.clear()
        self.load_project()

    @gui_action
    def delete_edition(self) -> None:
        name = self.current_edition()
        project = load_project_file(self.require_project_path())
        project = delete_project_edition(project, name)
        write_project_file(project, self.project_path())
        self.load_project()

    @gui_action
    def load_selected_edition(self, name: str) -> None:
        if not name:
            return
        project = load_project_file(self.require_project_path())
        edition = project.editions[name]
        self.config_edit.setPlainText(
            "\n".join(f"{key}={value}" for key, value in sorted(edition.config.items()))
        )
        self.build_options_edit.setPlainText("\n".join(edition.build_options))

    @gui_action
    def save_edition_config(self) -> None:
        project = load_project_file(self.require_project_path())
        updates = parse_multiline_pairs(self.config_edit.toPlainText())
        project = update_project_edition_config(project, self.current_edition(), updates, clear=True)
        write_project_file(project, self.project_path())
        self.output.write_text(f"updated config for {self.current_edition()}")

    @gui_action
    def clear_edition_config(self) -> None:
        project = load_project_file(self.require_project_path())
        names = list(project.editions[self.current_edition()].config)
        project = remove_project_edition_config(project, self.current_edition(), names)
        write_project_file(project, self.project_path())
        self.config_edit.clear()
        self.output.write_text(f"cleared config for {self.current_edition()}")

    @gui_action
    def save_build_options(self) -> None:
        project = load_project_file(self.require_project_path())
        options = parse_multiline_options(self.build_options_edit.toPlainText())
        project = update_project_edition_build_options(project, self.current_edition(), options)
        write_project_file(project, self.project_path())
        self.output.write_text(f"updated build options for {self.current_edition()}")

    @gui_action
    def show_project_summary(self) -> None:
        project = load_project_file(self.require_project_path())
        self.output.write_text(format_json(project_summary(project)))

    @gui_action
    def show_editions(self) -> None:
        project = load_project_file(self.require_project_path())
        payload = [
            {
                "name": name,
                "config_count": len(edition.config),
                "build_option_count": len(edition.build_options),
            }
            for name, edition in sorted(project.editions.items())
        ]
        self.output.write_text(format_json(payload))

    @gui_action
    def show_selected_edition(self) -> None:
        project = load_project_file(self.require_project_path())
        edition = project.editions[self.current_edition()]
        self.output.write_text(
            format_json(
                {
                    "name": self.current_edition(),
                    "config": dict(edition.config),
                    "build_options": list(edition.build_options),
                }
            )
        )

    def _project_and_metadata(self):
        project = load_project_file(self.require_project_path())
        _, metadata = project_metadata(project)
        return project, metadata

    @gui_action
    def render_header(self) -> None:
        project, _ = self._project_and_metadata()
        header_path = ensure_project_header(self.project_path(), project)
        self.output.write_text(header_path.read_text(encoding="latin-1"))

    @gui_action
    def render_config(self) -> None:
        project_path, project, edition = load_project_and_edition(str(self.project_path()), self.current_edition())
        _, metadata = project_metadata(project)
        symbols = pack_config_symbols(metadata)
        settings = default_pack_symbol_values(metadata)
        settings.update(edition.config)
        self.output.write_text(
            render_config_block_from_symbols(
                source_label=f"project {project_path.name} [{edition.name}] for {project.device}",
                family="5x",
                symbols=symbols,
                settings=settings,
            )
        )

    @gui_action
    def sync_config(self) -> None:
        project_path, project, edition = load_project_and_edition(str(self.project_path()), self.current_edition())
        _, metadata = project_metadata(project)
        symbols = pack_config_symbols(metadata)
        settings = default_pack_symbol_values(metadata)
        settings.update(edition.config)
        block = render_config_block_from_symbols(
            source_label=f"project {project_path.name} [{edition.name}] for {project.device}",
            family="5x",
            symbols=symbols,
            settings=settings,
        )
        source_path = project_path_join(project_path, project.config_source)
        original = source_path.read_text(encoding="latin-1")
        updated, replaced = update_managed_block(original, block, "5x")
        source_path.write_text(updated, encoding="latin-1")
        self.output.write_text(
            f"{'updated' if replaced else 'appended'} managed config block in {source_path}"
        )

    @gui_action
    def build_dry_run(self) -> None:
        self._run_build(dry_run=True)

    @gui_action
    def build_project(self) -> None:
        self._run_build(dry_run=False)

    def _run_build(self, dry_run: bool) -> None:
        project_path, project, edition = load_project_and_edition(str(self.project_path()), self.current_edition())
        header_path = ensure_project_header(project_path, project)
        command = build_command(
            compiler=project.compiler,
            main_file=str(project_path_join(project_path, project.main_source)),
            options=[
                f"-p{project.device[3:]}",
                f"-I{header_path.parent}",
                *project.base_build_options,
                *edition.build_options,
            ],
            runner=(shlex.split(project.runner) if project.runner else []),
        )
        if dry_run:
            self.output.write_text("command: " + subprocess.list2cmdline(command))
            return
        if self.process is not None and self.process.state() != QProcess.ProcessState.NotRunning:
            raise SystemExit("a build is already running")
        self.process = QProcess(self)
        self.process.setProgram(command[0])
        self.process.setArguments(command[1:])
        self.process.setWorkingDirectory(str(project_path.parent))
        self.process.readyReadStandardOutput.connect(
            lambda: self.output.append_text(bytes(self.process.readAllStandardOutput()).decode(errors="replace"))
        )
        self.process.readyReadStandardError.connect(
            lambda: self.output.append_text(bytes(self.process.readAllStandardError()).decode(errors="replace"))
        )
        self.process.finished.connect(
            lambda code, _status: self.output.append_text(f"\nprocess exited with code {code}\n")
        )
        self.output.write_text("command: " + subprocess.list2cmdline(command) + "\n\n")
        self.process.start()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.help_dialog: HelpDialog | None = None
        self.setWindowTitle("cc5x-helper")
        self.resize(1400, 880)
        self.setStyleSheet(
            """
            QWidget { background: #f4efe7; color: #231f1a; font-size: 13px; }
            QGroupBox {
                border: 1px solid #cdbda7;
                border-radius: 8px;
                margin-top: 12px;
                padding-top: 12px;
                font-weight: 600;
                background: #fbf7f2;
            }
            QPushButton {
                background: #b84f2d;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 7px 12px;
            }
            QPushButton:hover { background: #9d4325; }
            QLineEdit, QPlainTextEdit, QTextEdit, QListWidget, QComboBox {
                background: white;
                border: 1px solid #d6c9b8;
                border-radius: 6px;
                padding: 4px;
            }
            QTabWidget::pane { border: none; }
            QTabBar::tab {
                background: #e6dccf;
                border-radius: 6px;
                padding: 8px 14px;
                margin-right: 4px;
            }
            QTabBar::tab:selected { background: #231f1a; color: white; }
            """
        )

        self.tabs = QTabWidget()
        self.tabs.addTab(EnvironmentTab(self.show_help_section), "Environment")
        self.tabs.addTab(DeviceTab(self.show_help_section), "Devices")
        self.tabs.addTab(ProjectTab(self.show_help_section), "Projects")
        self.setCentralWidget(self.tabs)

        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.close)
        help_action = QAction("Help Contents", self)
        help_action.setShortcut("F1")
        help_action.triggered.connect(self.show_help)
        current_help_action = QAction("Help For Current Tab", self)
        current_help_action.setShortcut("Shift+F1")
        current_help_action.triggered.connect(self.show_current_tab_help)

        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("File")
        file_menu.addAction(quit_action)
        help_menu = menu_bar.addMenu("Help")
        help_menu.addAction(help_action)
        help_menu.addAction(current_help_action)

    def show_help(self) -> None:
        self.show_help_section("Welcome")

    def show_current_tab_help(self) -> None:
        self.show_help_section(self.tabs.tabText(self.tabs.currentIndex()))

    def show_help_section(self, title: str) -> None:
        if self.help_dialog is None:
            self.help_dialog = HelpDialog(self)
        self.help_dialog.show_section(title)


def main() -> int:
    install_exception_hooks()
    app = SafeApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
