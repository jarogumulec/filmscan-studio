# Vendored Touptek SDK

Vendored from `Touptek_SDK/toupcamsdk.20260908/` (local, gitignored — the
downloaded vendor archive), 2026-09-17. Deliberately **unmodified vendor
code** — when updating, replace all three files together and update the
version below.

| file | source | role |
|---|---|---|
| `toupcam.py` | `python/toupcam.py` | official ctypes wrapper, **version 60.32549.20260908** |
| `libtoupcam.dylib` | `mac/libtoupcam.dylib` | native library, universal (x86_64 + arm64), 43 MB |
| `toupcam.h` | `inc/toupcam.h` | C header — reference for option constants, not imported |

`toupcam.py` loads `libtoupcam.dylib` from *its own directory* first
(`__initlib`), so vendoring the dylib next to it is the whole install
story — no `LD_LIBRARY_PATH`, no helper process, no Rosetta: the dylib is
native arm64 (the old x86_64-only Nikon SDK problem is gone).

Update recipe:

    SRC=/path/to/toupcamsdk.YYYYMMDD
    cp "$SRC/python/toupcam.py" "$SRC/mac/libtoupcam.dylib" "$SRC/inc/toupcam.h" \
       src/filmscan_studio/capture/_toupcam/
    # check the version line at toupcam.py:1 and update this README

Nothing outside `capture/touptek.py` imports this package; `capture.touptek`
re-exports `Toupcam` lazily so environments without the camera never touch
the dylib.
