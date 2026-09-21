# FilmScan Studio

Reproducible digitisation of photographic film on a **Touptek TS2600MP-G2**
mono astro camera (Sony IMX571, 6224×4168, 16-bit, TEC-cooled). Two
deliberately separate modules: **Capture** (camera → 16-bit TIFF + metadata)
and **Developer** (TIFF → developed 16-bit TIFF). Acquisition never influences
development and neither module can corrupt the other's data.

```
uv run filmscan-studio          # Capture GUI (PySide6)
uv run filmscan-studio --mock   #   …with a simulated TS2600MP-G2, no camera needed
uv run filmscan-develop frameNNN.tif ...  # Developer CLI → 16-bit TIFF + sidecar
uv run filmscan-annotate [slozka]  # Anotátor metadat (název, datum, geo, štítky)
uv run pytest                   # tests
```

## Design rules the code enforces

| Rule | Where |
|---|---|
| Metadata lives in JSON sidecars + SQLite, **never** written into the TIFF | `capture/session.py` — raw files are copied, never opened for writing |
| Histogram, auto exposure and preview all read the **same linear sensor stream** — one honest path, they can never disagree | `core/histogram.py`, `core/exposure.py`, `gui/capture_window.py` |
| Auto exposure targets the **99.9th percentile** with 0.4 EV headroom | `capture/autoexposure.py` |
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
16-bit USB3, native 16-bit ADC — no demosaic, no gamma, no auto-brightness;
what the stream measures is what the file holds. Two-stage TEC to ΔT −42 °C.
**Needs external 11–14 V power** — without it the camera may not enumerate on
USB at all (the connect dialog says so). The vendor SDK
(`capture/_touptek/`, universal dylib x86_64+arm64, vendored — see that
directory) is driven directly over USB3.

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
scan the TIFF is **audited** (whole frame, or the red AE rect — converted to
sensor px, exact over the binned overview and over a moved ROI alike) and the
next frame's shutter is corrected; a positive
`frameNNN.jpg` is rendered from the TIFF beside it. The export lists any scan
whose darks sit further than ±0.5 °C away.

**Both rails are loud (2026-09):** overexposure is red everywhere (histogram
bar + `PŘEPAL` flag + `clip: …` on the rail), underexposure is its blue mirror
— histogram bar + `PODEXP` flag with the crushed-pixel %, a blue `černá … %`
in the clip readout, a blue `PODEXP` on the meter line, and the post-capture
audit's `PODEXPOZICOVÁNO` message now names how many percent of the metered
area sit on black.

**Film base / min point (third calibration, 2026-09):** *not* the flat field —
a flat is shot **without** film and divides out vignetting/dust; the min point
measures the held film's **clear base** (the subtraction floor of the
emulsion, raw material for a per-film Hurter–Driffield curve: min from this,
max from each frame). The calibration box has a third row: *Režim min point*
switches the shift-drag rect from red (AE) to blue (base — both rects can
coexist, the inactive one dims; the rect usually covers only a patch of clear
edge, not the whole frame), and two ways to measure it:

* **Měřit z proudu** — mean DN under the blue rect on a fresh Live View frame,
  no new exposure; the frame's own exposure stamp plus shutter/gain/temperature
  travel with the reading.
* **Snímek base** — a real full-size archived capture (`kind: "base"`, sidecar
  like a dark's) with the rect mean measured on it.

Every reading lands in the project's `film_base.json` with its exposure, and
`FilmBaseSample.scaled_above_black()` scales it onto differently exposed
frames — signal by the shutter/gain ratio, pedestal never scaled. The rect
collision (dragged on the 3×3-binned stream, measured on full-size frames) is
resolved the same way as the audit's: the GUI converts stream px to sensor px
before measurement. Nothing is applied to the preview yet; the level is
measurement + archive only.

**Two preview modes:** *RAW View* (display gamma only — judge exposure here)
and *Working Positive* (auto base subtraction, inversion, preview exposure,
Fritsch–Carlson spline filmic with Toe/Gamma/Shoulder — judge the picture
here, it changes nothing on disk). The live Working Positive runs through a
per-channel LUT (`FastPositivePreview`) so the tone curve costs one lookup
per pixel. Mono sensor: no white balance to chase.

**Developer:** `filmscan-develop frameNNN.tif --dark darks/ --flat flats/
--params look.json -o out/ --jpeg`. Pipeline: dark → flat → base subtraction
→ inversion → exposure → filmic → 16-bit TIFF (+ `.develop.json` provenance
with a parameter fingerprint, so any export can be re-generated
bit-identically).

## Developer GUI: jak ladit pozitiv

`uv run filmscan-develop-gui` otevre hustotní archiv a vykreslí pozitiv. Ladění
dělej v tomto pořadí:

1. **Dmin** nech na `z měření film base`, pokud má projekt měření čiré
  podložky. Dmin je černý bod pozitivu; není to nejtmavší motiv fotografie.
2. **Dmax** nejdřív nech na `auto ze snímku (p99,9 + okraj)`. V horním
  histogramu zkontroluj pravý konec hustot. Pokud je Dmax zbytečně daleko za
  skutečnými daty, vypni automatiku a opatrně ho sniž. Tím se roztáhnou
  střední a světlé tóny. Pokud je příliš nízko, světlá místa se oříznou na
  bílou.
3. **Kontrast středu (gamma)** dolaď pro celkový kontrast. Začni přibližně na
  `1,35`; běžný rozsah je `1,35-1,70`. Hodnota `1,00` je neutrální, hodnoty
  nad `2,2` jsou spíše zvláštní případy.
4. **Patka (toe)** komprimuje stíny pozitivu. Vyšší hodnota stíny více slepí;
  pro otevřenější stíny ji sniž.
5. **Rameno (shoulder)** komprimuje světla pozitivu. Vyšší hodnota chrání
  nejsvětlejší tóny před tvrdým ořezem, ale může je slít. Pro více roztažená
  světla ho sniž.
6. **Tolerance pod Dmin** (`shadow_band`) používej jen jako malou rezervu
  měření, obvykle `0,00-0,03 D`. Nevrací skutečně oříznutý detail a vysoká
  hodnota zvedne a vyšedí černou.
7. **Display gamma** nech na `2,2`. Je to technický převod do koukatelného
  gray prostoru, ne fotografický kontrast.

Pravidlo pro rychlé rozhodnutí: **Dmin/Dmax nastavují měřítko filmu, gamma
nastavuje kontrast a toe/shoulder tvarují konce.** Nejprve oprav rozsah dat,
teprve potom dolaďuj vzhled. Horní bílá křivka slouží k posouzení filmové
křivky před display transferem; dolní histogram a náhled ukazují výsledek po
display gamma.

Praktický start pro běžný snímek:

```text
Dmin: auto z film base
Dmax: auto, případně ručně podle pravého okraje D histogramu
toe: 0,10-0,25
gamma: 1,25-1,60
shoulder: 0,10-0,25
shadow_band: 0,01-0,03 D
gamma_display: 2,2
```

## Status

- **This code:** Capture GUI + Touptek backend + test suite (283 tests)
  complete against MockCamera; Developer CLI complete on mono TIFFs.
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
