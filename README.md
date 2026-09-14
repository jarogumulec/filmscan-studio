# FilmScan Studio

Reproducible digitisation of photographic film via DSLR (Nikon D750).
Two deliberately separate modules: **Capture** (camera → RAW + metadata) and
**Developer** (RAW → 16-bit TIFF). Acquisition never influences development and
neither module can corrupt the other's data.

```
uv run filmscan-studio          # Capture GUI (PySide6)
uv run filmscan-studio --mock   #   …with a simulated D750, no camera needed
uv run filmscan-develop RAW...  # Developer CLI → 16-bit TIFF + sidecar
uv run pytest                   # 130 tests
```

## Design rules the code enforces

| Rule | Where |
|---|---|
| Metadata lives in JSON sidecars + SQLite, **never** written into the DNG/NEF | `capture/session.py` — raw files are copied, never opened for writing |
| Histogram is always **linear sensor data**, in both preview modes | `core/histogram.py`, `gui/capture_window.py` |
| Auto exposure targets the **99.9th percentile** with 0.4 EV headroom, on linear data only, never on the inverted preview | `core/exposure.py`, `capture/autoexposure.py` |
| Working Positive preview (invert + base + curve) **cannot touch the stored RAW** | preview is a display-only transform in `core/positive.py` |
| Dark frames rescale by **shutter ratio only** (dark current precedes electronic gain); sensor pedestal is not scaled | `core/calibration.py::rescale_dark` |
| Flat fields need **no matching exposure** — dark-subtracted, then mean-normalised | `developer/pipeline.py::_calibrate_above_black` |
| One thread owns the camera at a time (libgphoto2 is not thread-safe) | `gui/capture_window.py::CameraWorker`, `gui/liveview.py` |
| No custom demosaicing — LibRaw interpolates the calibrated mosaic via the rawpy buffer trick | `developer/pipeline.py::develop_colour` |

## The macOS `ptpcamerad` problem

macOS runs `ptpcamerad`, which claims the PTP interface and makes libgphoto2
fail with `-53 Cannot allocate USB device`. It cannot be disabled (SIP) and
respawns when killed. `GPhoto2Backend.connect()` therefore runs a retry loop
that `pkill -9`s the daemon between attempts; connection normally succeeds in
about 1–2 s. Close Photos/Image Capture manually before connecting — they hold
the device differently and the daemon kill will not free them.

## Measured on real hardware (D750, this machine)

- Live View via python-gphoto2 bindings: **~38–43 fps** at 640×424 JPEG —
  the Nikon SDK is unnecessary (the gphoto2 *CLI* only manages ~1–8 fps;
  persistent-session bindings are the difference).
- Connect with retry: ~1.2 s. Still capture + NEF download: ~1.7 s.
- 29 ISO choices, 52 shutter choices; **no aperture control** over USB
  (manual AI lens — the app says so in the UI instead of faking it).
- `viewfinder` config needs integer `1`, not `'1'`; choice lists are localised
  („Paměťová karta") and matched by substring.

## Workflow

**Capture:** Nový film (metadata dialog) → *Dark Frame* → *Flat Field* →
frame-by-frame *Capture* (auto frame numbering matching the canister) →
*Exportovat projekt*. Each film is a folder: untouched camera files +
`<raw>.json` sidecars + `catalog.sqlite` + `project.json`.

**Two preview modes:** *RAW View* (display gamma only — judge exposure here)
and *Working Positive* (auto base subtraction, inversion, preview exposure,
Fritsch–Carlson spline filmic with Toe/Gamma/Shoulder — judge the picture
here, it changes nothing on disk).

**Developer:** `filmscan-develop scan.NEF --dark darks/ --flat flats/
--params look.json -o out/ --jpeg`. Pipeline: dark → flat → demosaic (LibRaw)
→ base subtraction → inversion → exposure → filmic → 16-bit TIFF (+
`.develop.json` provenance with a parameter fingerprint, so any export can be
re-generated bit-identically).

## Status

- **v1 (this code):** Capture GUI complete against MockCamera; Developer CLI
  complete against a real NEF; 152 tests.
- Live D750: connect, settings, metering and 38 fps Live View verified
  end-to-end through the app's own backend. *Still capture* pending on the
  camera's absent/unformatted memory card (software path proven previously).
- **Nikon SDK track — viable, backend wired:** MAID3 bindings
  (`capture/nikon_sdk.py`) + hardware probe (`capture/sdk_probe.py`) +
  `NikonSdkBackend` served by an x86_64 JSON-RPC helper
  (`capture/nikon_backend.py` ↔ `capture/sdk_server.py`). The probe on the
  real D750: module loads under Rosetta on macOS 15.5 (official support ends
  at 14), camera-side Live View zoom works (640×480 crop at 100 % vs
  640×424 whole frame), ~6.5 fresh fps, `ExposureStatus` (Float) readable
  during LV, NEF capture + download in ~2 s, MfDrive absent (manual lens —
  as expected). Caveat found on hardware: with the mode dial on **A** the
  body refuses shutter/exposure-mode writes — set the dial to **M** (or S)
  for scripted exposure control; ISO + ExposureComp work in any mode.
  Setup: `scripts/install_helper.sh`, `scripts/install_sdk.sh` (sudo once),
  verify with `scripts/probe.sh --capture`. The GUI connect dialog offers
  "Nikon D750 (Nikon SDK)" first and falls back to gphoto2 automatically.
- Planned: Developer GUI (share `gui/widgets.py` + pipeline), film profiles
  (`{name, toe, gamma, shoulder}` JSON, loaded by `FilmicProfile.from_dict`),
  Linux packaging, Windows.

## Requirements

Python 3.12 via [uv](https://docs.astral.sh/uv/); `uv sync` installs
everything (PySide6, rawpy/LibRaw, OpenCV, gphoto2 bindings 2.6.4 — the system
needs no libgphoto2, the wheel bundles it).
