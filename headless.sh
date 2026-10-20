#!/bin/sh
# comfyui-headless launcher for Linux and macOS. HEADLESS_PYTHON selects the interpreter.
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -n "$HEADLESS_PYTHON" ]; then
    PY=$HEADLESS_PYTHON
elif command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    echo "comfyui-headless: Python 3.11 or newer was not found. Install it or set HEADLESS_PYTHON." >&2
    exit 1
fi
exec "$PY" "$ROOT/headless.py" "$@"
