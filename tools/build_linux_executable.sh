#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_SELECTOR="${PYTHON_SELECTOR:-3.13}"

cd "${ROOT_DIR}"

uv run --python "${PYTHON_SELECTOR}" --with pyinstaller pyinstaller \
  --onefile \
  --name cc5x-helper \
  --paths tools \
  tools/cc5x_setcc_native.py
