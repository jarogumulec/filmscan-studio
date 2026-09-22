# Contributor / agent instructions — filmscan-studio

Rules and design invariants this codebase deliberately enforces. Read before
contributing; several features were **deliberately retired** and must not
resurface. UI texts are Czech; metadata/EXIF values are English.

## Design rules the code enforces

| Rule | Where |
|---|---|
| Metadata lives in JSON sidecars + SQLite, **never** written into the raw TIFF | `capture/session.py` — raw files are copied, never opened for writing |
| Histogram, preview and audit all read the **same linear sensor stream** — one honest path, they can never disagree | `core/histogram.py`, `gui/capture_window.py` |
| Flat fields need **no matching exposure** — dark-subtracted, then mean-normalised | `developer/pipeline.py::_calibrate_above_black` |
| Dark frames rescale by **shutter ratio only** (dark current precedes electronic gain); sensor pedestal is not scaled | `core/calibration.py::rescale_dark` |
| One thread owns the camera at a time (the SDK is not thread-safe) | `gui/capture_window.py::CameraWorker`, `gui/liveview.py` |
| Every scan is audited after capture (metering rect; over/under-exposure flagged on both rails); the shutter for the *next* frame is corrected automatically | `capture/quality.py::audit_frame` |
| Export **warns per frame number** when a scan has no dark measured within ±0.5 °C — dark subtraction on a cooled sensor only holds in a narrow temperature window | `capture/session.py::export_project` |
| The archived preview JPEG is **always the positive** for a negative film — the live-view RAW View/Negativ switch must never reach it | `gui/capture_window.py::_post_capture_check` |
| Display zoom is labelled in **sensor pixels**; ≥3× switches the sensor to a 1:1 hardware ROI, below 3× the 3×3-binned overview already shows every real detail | `core/zoom.py`, `gui/widgets.py::ZoomView` |
| Metering/audit rects are dragged on the binned stream but measured on full-size frames — the GUI converts stream px → sensor px | same conversion in audit + film-base measurement |
| Density archive is **float32 absolute D, uncropped by interpretation**: NaN = "no light", saturation = −inf → black; the positive is a derived render with a parameter fingerprint | `core/density.py`, `core/render.py`, docs 01/03 |
| Display gamma 2.2 is defined by the ICC profile, not a user control; the gamma lands in exported pixels **exactly once** — profiles are interpretation wrappers | `core/icc.py`, doc 07 |
| ICC profiles must survive **both** ColorSync and lcms2 readers: manufacturer = 0 in the header, ICC.1 BCD display date (`tests/test_icc.py::TestColorSyncCompat` runs real `sips`) | `core/icc.py` |
| A metadata (EXIF/XMP) error must **never** drop an export; empty metadata = byte-identical export as before | `developer/gui.py::_export_metadata` |
| EXIF contract: description → `ImageDescription`, title → XMP `dc:title`; camera make/model are stored split (`camera_make`/`camera_model`), rotation is baked into pixels, EXIF `Orientation` is **not** written | doc 09, `core/exportmeta.py` |

## Retired features — do NOT reintroduce (code, metadata, or UI)

- **White balance** — the IMX571 is mono; there is no WB. (A WB step survives
  only inside the Working Positive *preview*, which is not acquisition WB.)
- **Aperture / f_number** — physically set on the lens; removed from
  `AcquisitionMetadata`, `ExposureSettings`, `CalibrationStack` and the UI.
- **`lens`, `lens_serial`, `focus_distance_m`, `raw_developer`** in acquisition
  metadata — gone. `digitising_lens` in `FilmMetadata` stays (the rig is shared
  between films), as do the annotator's `shooting_lens` / `camera` fields.
- **Capture UI controls removed by direct operator order:** the
  "Gain 1.00× (archive)" lock (gain is a free exposure control), the
  "Auto Exposure" button (the rig is operated manually; the
  `capture/autoexposure.py` motor itself stays for Live View/metering), and the
  old "Positive" preview toggle (modes are exactly `RAW View` | `Negativ`).
- **Paper-black lift after the curve** — rejected by the operator ("what has
  merged into black stays black"). Replaced by `shadow_band` (extends the
  curve's domain *below* Dmin, before the curve). Do not re-add a post-curve
  black lift.
- Magenta NaN mask in the preview — removed; NaN renders black, an
  "Exposure warning" checkbox overlays clipping instead.

Retired metadata keys are stripped on load (`_strip_retired`,
`RETIRED_FILM_FIELDS`) so old archives stay readable without a schema bump.

## Process rules (operator-mandated)

- Never change the capture/acquisition pipeline from image-processing work;
  acquisition changes need an explicit direct order.
- Read the *current* code before editing (not old notes/plans); run
  `.venv/bin/python -m pytest tests/ -q` after every block of changes.
- Commit only when explicitly asked.
- UI strings in Czech; never a soft hyphen U+00AD in any string
  (`grep -rn $'\xc2\xad'`).
