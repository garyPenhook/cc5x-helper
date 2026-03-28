#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_SELECTOR="${PYTHON_SELECTOR:-3.13}"
MODE="${1:-cli}"

case "${MODE}" in
  cli)
    OUTPUT_NAME="cc5x-helper"
    ENTRYPOINT="tools/cc5x_setcc_native.py"
    EXTRA_ARGS=()
    ;;
  gui)
    OUTPUT_NAME="cc5x-helper-gui"
    ENTRYPOINT="tools/cc5x_helper_gui.py"
    EXTRA_ARGS=(
      --windowed
      --exclude-module pygame
      --exclude-module tkinter
      --exclude-module matplotlib
      --exclude-module numpy
      --exclude-module PIL
      --exclude-module scipy
      --exclude-module PyQt6.QtWebEngineCore
      --exclude-module PyQt6.QtWebEngineWidgets
      --exclude-module PyQt6.QtWebEngineQuick
      --exclude-module PyQt6.QtNetwork
      --exclude-module PyQt6.QtOpenGL
      --exclude-module PyQt6.QtOpenGLWidgets
      --exclude-module PyQt6.QtQml
      --exclude-module PyQt6.QtQuick
      --exclude-module PyQt6.QtQuickWidgets
      --exclude-module PyQt6.QtDesigner
      --exclude-module PyQt6.QtSql
      --exclude-module PyQt6.QtTest
      --exclude-module PyQt6.QtPrintSupport
      --exclude-module PyQt6.QtMultimedia
      --exclude-module PyQt6.QtMultimediaWidgets
      --exclude-module PyQt6.QtPdf
      --exclude-module PyQt6.QtPdfWidgets
      --exclude-module PyQt6.QtPositioning
      --exclude-module PyQt6.QtRemoteObjects
      --exclude-module PyQt6.QtSensors
      --exclude-module PyQt6.QtSerialPort
      --exclude-module PyQt6.QtStateMachine
      --exclude-module PyQt6.QtTextToSpeech
      --exclude-module PyQt6.QtWebChannel
      --exclude-module PyQt6.QtBluetooth
      --exclude-module PyQt6.QtNfc
      --exclude-module KDEPlasmaPlatformTheme6
      --exclude-module breeze6
      --exclude-module qgtk3
    )
    ;;
  *)
    echo "usage: $0 [cli|gui]" >&2
    exit 2
    ;;
esac

cd "${ROOT_DIR}"

uv run --python "${PYTHON_SELECTOR}" --with pyinstaller pyinstaller \
  --onefile \
  --name "${OUTPUT_NAME}" \
  --paths tools \
  "${EXTRA_ARGS[@]}" \
  "${ENTRYPOINT}"
