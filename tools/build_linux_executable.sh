#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_SELECTOR="${PYTHON_SELECTOR:-3.13}"
MODE="${1:-cli}"

case "${MODE}" in
  cli)
    OUTPUT_NAME="cc5x-helper"
    ENTRYPOINT="tools/cc5x_setcc_native.py"
    ;;
  gui)
    OUTPUT_NAME="cc5x-helper-gui"
    ENTRYPOINT="tools/cc5x_helper_gui.py"
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
  "${ENTRYPOINT}"
