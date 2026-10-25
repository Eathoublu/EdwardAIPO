#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
python3 installer/build_pyz.py
if [ "$(uname -s)" = "Darwin" ]; then
  python3 installer/build_macos_app.py
fi
