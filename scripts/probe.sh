#!/usr/bin/env bash
# Run the Nikon SDK hardware probe under Rosetta (x86_64 venv).
# Camera: zapnout, USB, PTP. Live view needs no memory card.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -x .venv-x86/bin/python ] || { echo "spusť nejdřív scripts/install_helper.sh" >&2; exit 1; }
PYTHONPATH=src exec .venv-x86/bin/python -m filmscan_studio.capture.sdk_probe "$@"
