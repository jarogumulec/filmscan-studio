#!/usr/bin/env bash
# Create the x86_64 helper venv (Rosetta) for the Nikon Type0015 module.
# The SDK binary is x86_64-only; the arm64 GUI process cannot dlopen it.
set -euo pipefail
cd "$(dirname "$0")/.."

REQ=cpython-3.12-macos-x86_64-none
uv python install "$REQ" >/dev/null 2>&1 || true
if [ ! -x .venv-x86/bin/python ]; then
    uv venv --python "$REQ" .venv-x86
fi
uv pip install --python .venv-x86/bin/python cffi
.venv-x86/bin/python - <<'EOF'
import platform
assert platform.machine() == "x86_64", platform.machine()
import cffi
print(f"helper venv OK: python {platform.python_version()} ({platform.machine()}), cffi {cffi.__version__}")
EOF
