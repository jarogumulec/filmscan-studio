#!/usr/bin/env bash
# Install the Nikon SDK binaries where the bundle links them by absolute
# install name (verified with otool): /Library/Application Support/Nikon/...
# Needs sudo once; rsync makes it idempotent.
set -euo pipefail
cd "$(dirname "$0")/.."

SRC="Nikon_SDK/S-SDKD750-011BF-ALLIN/Module/Mac/Binary Files/binary15"
DEST="/Library/Application Support/Nikon/Camera Control Modules"

[ -d "$SRC/Type0015 Module.bundle" ] || {
    echo "CHYBI: $SRC — rozbalte S-SDKD750-011BF-ALLIN do Nikon_SDK/" >&2
    exit 1
}

sudo mkdir -p "$DEST"
sudo rsync -a --delete "$SRC/Type0015 Module.bundle/" "$DEST/Type0015 Module.bundle/"
sudo rsync -a "$SRC/libNkPTPDriver2.dylib" "$DEST/"
sudo rsync -a --delete "$SRC/Royalmile.framework/" "$DEST/Royalmile.framework/"
echo "nainstalováno do $DEST:"
ls -la "$DEST"
