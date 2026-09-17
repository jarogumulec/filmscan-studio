# FilmScan Studio

Reproducible digitisation of photographic film on a **Touptek TS2600MP-G2**
mono astro camera (Sony IMX571, 6224×4168, 16-bit, TEC-cooled) instead of a
DSLR. Two deliberately separate modules: **Capture** (camera → 16-bit TIFF +
metadata) and **Developer** (TIFF → developed 16-bit TIFF). Acquisition never
influences development and neither module can corrupt the other's data.

The D750 era (PTP/gphoto2 + Nikon SDK) lives on branch `D750`; see
`INSTRUCTIONS_TOUPTEK_CAMERA.md` for the migration spec.

```
uv run filmscan-studio          # Capture GUI (PySide6)
uv run filmscan-studio --mock   #   …with a simulated TS2600MP-G2, no camera needed
uv run filmscan-develop RAW...  # Developer CLI → 16-bit TIFF + sidecar
uv run pytest                   # 262 tests
```

## Design rules the code enforces

| Rule | Where |
|---|---|
| Metadata lives in JSON sidecars + SQLite, **never** written into the TIFF | `capture/session.py` — raw files are copied, never opened for writing |
| Histogram is always **linear sensor data**, in both preview modes — the stream is linear, so one honest path replaced the D750's two disagreeing ones | `core/histogram.py`, `gui/capture_window.py` |
| Auto exposure targets the **99.9th percentile** with 0.4 EV headroom, metered from the linear stream itself | `core/exposure.py`, `capture/autoexposure.py` |
| Working Positive preview (invert + base + curve) **cannot touch the stored raw** | preview is a display-only transform in `core/positive.py` |
| Dark frames rescale by **shutter ratio only** (dark current precedes electronic gain); sensor pedestal is not scaled | `core/calibration.py::rescale_dark` |
| Flat fields need **no matching exposure** — dark-subtracted, then mean-normalised | `developer/pipeline.py::_calibrate_above_black` |
| One thread owns the camera at a time (the SDK is not thread-safe) | `gui/capture_window.py::CameraWorker`, `gui/liveview.py` |
| Archival scans happen at **gain 1.00× only** — capture is blocked at any other sensitivity, exposure lives in the shutter | `core/exposure.py::ARCHIVE_GAIN`, `_capture_block_reason` |
| Every scan is audited after capture (p99.9 target, optional red-rect region); the shutter for the *next* frame is corrected automatically | `capture/quality.py::audit_frame` |
| Export **warns per frame number** when a scan has no dark measured within ±0.5 °C — dark subtraction on a cooled sensor only holds in a narrow temperature window | `capture/session.py::export_project` |
| Display zoom is labelled in **sensor pixels** and drawn at a whole multiple of the delivered stream; ≥3× switches the sensor to a 1:1 hardware ROI (Stop → reconfigure → Start) | `core/zoom.py`, `gui/widgets.py::ZoomView` |
| The zoom note states honestly what one stream pixel is worth (3×3 binned overview vs 1:1 ROI vs interpolation) | `gui/capture_window.py::_update_zoom_note` |

## The camera

TS2600MP-G2 (= ATR2600M): IMX571 APS-C mono, 6224×4168 @ ~6.5 fps full
16-bit USB3, native 16-bit ADC (no demosaic, no gamma, no auto-brightness —
what the stream measures is what the file holds), two-stage TEC to ΔT −42 °C,
**needs external 11–14 V power** — without it the camera may not enumerate on
USB at all (the connect dialog says so). The vendor SDK
(`capture/_toupcam/`, universal dylib x86_64+arm64, vendored — see that
directory) is driven directly; no gphoto2, no PTP, no `ptpcamerad` fights.

Stream design: a 3×3-binned whole-sensor overview (~2074×1389) is the honest
everyday stream — one stream pixel is the mean of 3×3 sensor pixels, so
metering on it is honest. Display zoom ≥3× swaps it for a 1200×1200 1:1
hardware ROI around the point you are looking at (focusing at true pixel
detail); below 3× the overview already shows every real detail and the swap
is refused on principle.

## Workflow

**Capture:** Nový film (metadata dialog) → *Dark Frame* → *Flat Field* →
frame-by-frame *Capture* (auto frame numbering matching the canister) →
*Exportovat projekt*. Each film is a folder: untouched 16-bit TIFFs +
`<raw>.tif.json` sidecars + `catalog.sqlite` + `project.json`. Scans run at
**gain 1.00×** (the Capture button refuses any other sensitivity); *Auto
Exposure* solves with the shutter alone while the archival-gain button is
checked. The cooling panel shows current/target temperature with a
traffic-light semaphore — green means "u cíle — darky platí". After every
scan the TIFF is **audited** (whole frame, or the red AE rect while streaming
the overview) and the next frame's shutter is corrected; a positive
`frameNNN.jpg` is rendered from the TIFF beside it. The export lists any scan
whose darks sit further than ±0.5 °C away.

**Two preview modes:** *RAW View* (display gamma only — judge exposure here)
and *Working Positive* (auto base subtraction, inversion, preview exposure,
Fritsch–Carlson spline filmic with Toe/Gamma/Shoulder — judge the picture
here, it changes nothing on disk). The live Working Positive runs through a
per-channel LUT (`FastPositivePreview`) so the tone curve costs one lookup
per pixel. Mono sensor: no WB to chase, but the WB controls still exist for
the colour preview chain (tested for determinism; currently exposed nowhere).

**Developer:** `filmscan-develop frameNNN.tif --dark darks/ --flat flats/
--params look.json -o out/ --jpeg`. Pipeline: dark → flat → base subtraction
→ inversion → exposure → filmic → 16-bit TIFF (+ `.develop.json` provenance
with a parameter fingerprint, so any export can be re-generated
bit-identically).

## Status

- **v2 (this code):** Capture GUI + Touptek backend + rebuilt test suite
  (262 tests) complete against MockCamera; Developer CLI complete on mono
  TIFFs.
- **Pending hardware verification** (`INSTRUCTIONS_TOUPTEK_CAMERA.md` §11):
  real fps at the 0x83 binning, Snap-in-RAW-mode behaviour, ExpoAGain units,
  enumeration under missing power, real TEC settling, the
  `put_Roi(0,0,W,H)`-as-ROI-off assumption. Every assumption the fake SDK
  (`tests/test_touptek.py::FakeHcam`) makes is listed there as a checklist
  item in waiting.
- Planned: Developer GUI (share `gui/widgets.py` + pipeline), film profiles
  (`{name, toe, gamma, shoulder}` JSON), Linux packaging, Windows.

## Requirements

Python 3.12 via [uv](https://docs.astral.sh/uv/); `uv sync` installs
everything (PySide6, numpy, OpenCV, tifffile, pydantic). The Touptek SDK
dylib is vendored in the repo — nothing to download; the camera needs 11–14 V
DC power and a USB3 port.
