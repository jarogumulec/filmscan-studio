# Instrukce: přechod z Nikon D750 (SDK) na Touptek ATR2600M / TS2600MP

Cílová kamera: Touptek ATR2600M (= TS2600MP), Sony IMX571 APS-C mono BSI,
26 MP (6224×4168), nativní 16bit ADC, chlazený TEC senzor, USB3 stream.
SDK: `Touptek_SDK/toupcamsdk.20260908` (nativní macOS `.dylib`, oficiální
`ctypes` Python binding v `python/toupcam.py` — žádný x86_64 helper, žádná
Rosetta, žádný `ptpcamerad` boj jako u Nikonu).

Nikon SDK stav je bezpečně uložený na branchi `D750` (stejný commit je i na
`main` v době psaní tohoto dokumentu) — nic se ztrátou nezničí.

## Shrnutí rozhodnutí

| Otázka | Rozhodnuto |
|---|---|
| Live view vs. capture | Oddělený plný RAW capture, živý náhled běží na jiném (zmenšeném) streamu |
| Chlazení blokuje Capture? | Ne — jen semafor (zelená/červená), varování při Capture, neblokuje |
| Dark/flat teplotní shoda | Ano — hlídat a při Exportu filmu ohlásit nesoulad teplot |
| Barva | Zatím čistě mono, žádný filter wheel |
| Nikon SDK úklid | Smazat z `main` úplně; `Nikon_SDK/` složka (gitignored) i historie zůstávají v branchi `D750` |
| Zoom/geometrie | Přepsat na obecný senzor `6224×4168`; overview downsampleovaný/binned, zoom = hardwarové ROI |

## 1. Co smazat z `main`

Nikon-specific soubory (D750 branch je má bezpečně uložené):

```
src/filmscan_studio/capture/nikon_backend.py
src/filmscan_studio/capture/nikon_sdk.py
src/filmscan_studio/capture/sdk_server.py
src/filmscan_studio/capture/sdk_probe.py
src/filmscan_studio/capture/_filmscan_trampolines.c
src/filmscan_studio/capture/parsing.py         # pokud ho nic jiného nepoužívá
tests/test_nikon_sdk.py
scripts/install_helper.sh
scripts/install_sdk.sh
scripts/probe.sh
```

V `gui/capture_window.py` odstranit: import `NikonSdkBackend`, `_connect_sdk`,
connect-dialog tlačítko „Nikon D750 (Nikon SDK)“, zmínky o SDK zoomu.

`Nikon_SDK/` složku (obsahuje jen dokumentaci/binárky, je v `.gitignore`)
nechat ležet nebo smazat z disku — v gitu na ní stejně nic nezáviselo.

`gphoto2.py` backend a `.venv-x86`/Rosetta infrastrukturu doporučuji smazat
také — nová kamera nemá s Nikonem nic společného a udržovat dva mrtvé
backendy zbytečně zatěžuje kód. `MockCamera` zůstává, ale je potřeba ji
přepracovat pro nový model kamery (viz níže).

## 2. Nový backend: `capture/touptek.py`

Touptek SDK je řádově jednodušší než Nikon MAID3: nativní macOS `.dylib`
(`Touptek_SDK/toupcamsdk.20260908/mac/libtoupcam.dylib`), oficiální
`ctypes`-based Python modul (`python/toupcam.py`) — **žádný x86_64 helper,
žádná Rosetta, žádný `ptpcamerad` boj**. Zkopírovat/vendor `toupcam.py` do
`src/filmscan_studio/capture/_toupcam/` (nebo balit jako submodul) a nad ním
postavit tenký `CameraBackend`.

### Základní tok (z `python/samples/simplest.py`)

```
toupcam.Toupcam.EnumV2() -> seznam zařízení
Toupcam.Open(camId) -> handle
hcam.put_Size(w, h) / put_Roi(...) / put_Option(...)
hcam.StartPullModeWithCallback(callback, ctx)
# callback běží na SDK vlákně; PullImageV4 vytáhne aktuální frame
hcam.Close()
```

### Klíčové `Option` konstanty pro mono 16-bit RAW

- `TOUPCAM_OPTION_RAW = 1` — RAW mód (žádný ISP/demosaic, přímo mosaic/mono
  data)
- `TOUPCAM_OPTION_BITDEPTH = 1` — 16bitová hloubka (senzor má nativní 16bit
  ADC)
- `TOUPCAM_OPTION_RGB = 4` — „16 Bits Grey (only for mono camera when
  bitdepth > 8)“ — přesně pro tento senzor
- `TOUPCAM_OPTION_PIXEL_FORMAT` — pro čistě RAW tok lze nechat na výchozí,
  ADC dá `RAW16`

### Chlazení (TEC)

- `TOUPCAM_OPTION_TEC` (0/1 zapnout/vypnout)
- `TOUPCAM_OPTION_TECTARGET` — cílová teplota v 0,1 °C (např. `-100` = −10 °C)
- `TOUPCAM_OPTION_TECTARGET_RANGE` — rozsah podporovaný modelem
- `get_Temperature()` — aktuální teplota senzoru, 0,1 °C
- `TOUPCAM_OPTION_FAN` — otáčky ventilátoru (většina cooled modelů to
  potřebuje i při zapnutém TEC)

## 3. Live view: overview vs. zoom

Toto přesně řeší požadavek „nemusím furt vidět 6224×...“. Touptek na rozdíl
od Nikonu nemá pevnou tabulku zoom-rate — **binning a ROI se nastavují přímo
a přesně**, žádné hádání.

### Overview (výchozí náhled)

- `TOUPCAM_OPTION_BINNING` s hodnotou `0x83` (3×3 average, bitová hloubka se
  nemění) → `6224/3 × 4168/3 ≈ 2075×1389 px` — přesně požadovaná plocha na
  monitoru
- Streamuje se plynule, malý objem dat přes USB3 → vysoký fps i při 16bit
  datech
- Expozici lze i měřit z tohoto binned streamu (je to lineární
  součet/průměr, ne JPEG s auto-brightness jako u Nikonu — mnohem poctivější
  pro metering)

### Zoom (kolečkem myši, ostření)

- Přepnout `BINNING = 0x01` (bez binningu) + `put_Roi(x, y, w, h)` na malý
  výřez kolem bodu zájmu (např. `800×800` nebo `1200×1200`)
- Hardwarové ROI = senzor posílá jen tuto oblast → i při plném rozlišení
  pixelů zůstává vysoký fps, protože se přenáší málo dat
- Souřadnice ROI jsou vždy vůči plnému rozlišení senzoru (SDK to
  garantuje), takže mapování z overview na zoom je přímočaré lineární
  přepočítání, ne aproximace jako u Nikonu

To znamená: **žádná ztráta detailu, žádné hádání crop faktorů** — jde o
skutečné hardwarové řízení, ne o pevnou tabulku jako
`core/zoom.py::BODY_ZOOM_SENSOR_PX_PER_LV_PX`. Tenhle modul lze zjednodušit:
`SensorSize(6224, 4168)`, žádná `BODY_ZOOM_SENSOR_PX_PER_LV_PX` tabulka,
zoom = vlastní ROI výpočet místo dotazu na tělo.

### Přepínání overview ↔ zoom za běhu

Podle dokumentace (`en.html`) `TOUPCAM_OPTION_BINNING` a `put_Roi` je nutné
volat, když kamera neběží (`Toupcam_Stop` → nastavit → znovu spustit pull
mode), případně přímo v pull módu je to omezené („zakázáno volat put_Option
s BINNING v callback kontextu“ — volat z UI/worker vlákna, ne z callbacku).
Návrh:

```
přepnutí zoomu (uživatel točí kolečkem)
→ zastavit stream
→ put_Roi(...) / BINNING podle režimu
→ znovu StartPullModeWithCallback
```

Krátké přerušení streamu (desítky ms) je zanedbatelné, kamera nemá zrcadlo
ani mechaniku, která by se tím rozhýbala.

## 4. Capture (archivní snímek)

Rozhodnuto pro oddělený capture, ne poslední streamovaný frame. Důvod navíc
k tomu, co bylo řečeno: binned/ROI overview by jinak omezoval archivní
snímek na zmenšený/oříznutý formát. Postup:

```
Capture stisknuto
→ zastavit live stream (BINNING=0x01, plné ROI = celý senzor)
→ nastavit expozici/gain podle aktuální hodnoty (long exposure přípustná)
→ StartPullModeWithCallback nebo Snap/SnapR podle toho, co model podporuje
  pro still
→ PullImageV4 → uložit jako 16bit RAW (mosaic, žádný demosaic — stejná
  filozofie jako teď s NEF)
→ obnovit live view (binning/ROI podle posledního zoom stavu)
```

Bez zrcadla a bez mechanické závěrky (pokud kamera nemá mechanickou
závěrku — pokud má, `TOUPCAM_OPTION_MECHANICALSHUTTER` existuje jako volba)
odpadá celý dosavadní problém s vibracemi řešený v Nikon vlákně.

## 5. Datový formát místo NEF

Kamera nemá Bayer barevný senzor (je mono), takže žádné demosaicování,
žádný `color_desc`, žádný `rawpy`. Návrh:

- Ukládat přímo 16bit `TIFF` (grayscale) nebo surová binární data + minimal
  header, s metadaty v sidecaru JSON jako dosud
- `core/rawio.py` nahradit modulem, který čte tento formát místo
  `rawpy.imread` — aktuální `RawFrame` dataclass (`data`, `black_level`,
  `white_level`, `color_desc`) lze zachovat, jen `color_desc` bude vždy
  „mono“ a čtení souboru bude přes `tifffile`/`numpy`, ne přes LibRaw
- `developer/pipeline.py` (`demosaic_linear`) se pro mono zjednoduší —
  žádný LibRaw krok, jen přímé škálování dat

## 6. Expozice: gain místo ISO

Touptek nemá ISO ladder, ale:

- `get_ExpoTime()` / `put_ExpoTime()` — mikrosekundy
- `get_ExpoAGain()` / `put_ExpoAGain()` / `get_ExpoAGainRange()` —
  analogový gain, obvykle v promile (1000 = 1×)

Návrh na `core/exposure.py`:

- Přidat `ExposureSettings.gain: float | None = None` vedle `iso` (ponechat
  `iso=None` pro tuto kameru; katalogové schéma i `AcquisitionMetadata` mají
  pole `iso` — buď přidat `gain` sloupec vedle, nebo `iso` přejmenovat na
  obecnější „gain_code“ — doporučeno **přidat nové pole, ne přejmenovávat**,
  ať zůstane zpětná kompatibilita se starými NEF projekty)
- Auto Exposure Controller (`capture/autoexposure.py`) nahradit tělní
  `ExposureStatus` meter přímým měřením z binned live streamu (žádné
  auto-brightness zkreslení jako u Nikonu — tenhle senzor posílá lineární
  data) — to je ve skutečnosti jednodušší a přesnější než u D750
- Aktuátorová politika: prodlužovat čas první (gain zvyšuje šum), gain
  zvyšovat jen když čas dosáhne praktického maxima (desítky sekund) —
  stejná filozofie jako „ISO stays at noise floor“, jen s gainem místo ISO

## 7. Cooling UX

- Semafor vedle capture tlačítka: zelené =
  `|aktuální − cílová teplota| < tolerance`, červené = mimo toleranci —
  **needitovatelné blokování**, jen vizuální
- V `CaptureRecord`/sidecaru přidat pole `sensor_temperature_c` u každého
  snímku (dark, flat, scan)
- Při „Export projektu“ (nebo tlačítku, co dnes dělá `_export_project`)
  přidat validaci: pro každý scan najít odpovídající dark v podobné teplotě
  (tolerance např. ±0,5 °C); pokud chybí, vypsat jasné varování se seznamem
  problematických frame čísel — přesně „zařve, že není light dark“

## 8. Vláknový model

Zachovat současné pravidlo „jedno vlákno vlastní kameru“
(`CameraWorker`/`ResultRelay`). Rozdíl oproti Nikonu: Touptek SDK sám
spouští vlastní vnitřní vlákno pro `StartPullModeWithCallback` a volá tvůj
callback z něj. Doporučení:

- Callback pouze zapisuje nejnovější frame do `threading.Lock`-chráněné
  proměnné nebo `queue.Queue(maxsize=1)`
- `LiveViewWorker` (Qt `QThread`) frame vyzvedává, ne že by čekal na SDK
  callback přímo — zamezí to volání Qt signálů z cizího vlákna
- Operace jako `put_Roi`, `put_Option(BINNING)`, `Snap` musí jít přes
  stejnou frontu jako dnes `CameraWorker.submit(...)`, protože dokumentace
  explicitně zakazuje volat `BITDEPTH`/`BINNING`/`PIXEL_FORMAT`/`TRIGGER`/
  `ROTATE` uvnitř callbacku (`E_WRONG_THREAD`)

## 9. GUI úpravy (přehled, ne detailní diff)

- Connect dialog: jedna volba „Touptek ATR2600M / TS2600MP“ + Mock, žádný
  Nikon/gphoto2 výběr
- Nahradit ISO combobox gain sliderem/comboboxem (rozsah z
  `get_ExpoAGainRange()`)
- Přidat cooling panel: aktuální teplota, cílová teplota (spinbox →
  `put_Temperature`), TEC on/off, semafor
- Zoom UI: kolečko myši místo pevného comboboxu úrovní
  (`core/zoom.ZOOM_LEVELS` nahradit plynulým/ROI-based zoomem), overview vs.
  zoom už neřeší „interpolace“ text jako u Nikonu, protože ROI dá skutečné
  pixely 1:1

## 10. Testy

Mirror současného přístupu (`tests/test_nikon_sdk.py`) →
`tests/test_touptek.py`:

- Mockovat `ctypes.CDLL` podobně jako se dřív mockoval Nikon modul, nebo
  rovnou postavit `MockTouptekCamera` v `capture/mock.py` s
  binning/ROI/gain/temperature simulací, aby GUI testy (`test_gui.py`)
  běžely bez hardwaru
- Ověřit v testech: binning matematiku (`6224//3 × 4168//3`), ROI
  souřadnicové transformace overview↔zoom, teplotní tolerance validaci při
  exportu

## 11. Co musí příští Copilot ověřit na reálném hardwaru

> **2026-09-17: ověřeno na reálném ATR2600M** (macOS, USB3, napájení 12 V).
> Výsledky k jednotlivým bodům jsou poznámenky níže; navíc zjistěno:
> `get_Size` lže (vždy rozlišení senzoru, i při BINNING/ROI — velikost
> rámu je v `info.v3.width/height` každého rámku), `PullImageV4`/
> `PullStillImageV2` berou `c_char_p` buffer (přes `create_string_buffer`,
> ndarray i POINTER odmítnou), a `WaitImageV4` na Snap'd still vrací
> `E_UNEXPECTED` — still chodí přes `TOUPCAM_EVENT_STILLIMAGE` +
> `PullStillImageV2`. Gain range `get_ExpoAGainRange` = 0,1–10×.

1. Skutečný max fps při `BINNING=0x83` overview (16bit, USB3) —
   dokumentace dává vzorec jen orientačně
   (`TOUPCAM_OPTION_MAX_PRECISE_FRAMERATE`, závisí na
   bandwidth/ROI/bitdepth)
   → **✅ 3,4 fps** při 2074×1388 (senzor dodává i při binning sudé
   zaokrouhlení 1388, ne 4168/3=1389 — appka nesmí předpokládat přesné
   //3). ROI 1200×1200 bez binningu: **~11,4 fps**, doručuje přesně
   požadovanou velikost.
2. Zda `Snap`/`SnapR` na tomto modelu funguje v RAW módu, nebo je nutné
   dělat capture přes dočasné `Stop → put_Size(full) → Start →
   PullImageV4` (viz `demostillraw.cpp`/`demoraw.cpp` — chování se liší
   model od modelu)
   → **✅ Snap funguje**, ale still nedorazí přes `WaitImageV4`
   (vždy `E_UNEXPECTED` 0x8000FFFF). Funkční flow: callback →
   `Snap(0xFFFFFFFF)` → čekat `TOUPCAM_EVENT_STILLIMAGE` (přijde za
   expozici + ~0,9 s) → `PullStillImageV2`. Full-size still 6224×4168
   trval 2,6 s (1s expozice), TIFF round-trip ok.
3. Skutečné jednotky `ExpoAGain` (dB vs. permile) pro tento konkrétní model
   — SDK je obecné napříč desítkami produktů
   → **✅ permile** (`put_ExpoAGain(1000)` = 1,00×; range 0,1–10×).
   Pro archiv se doporučuje pevně 1,00×: IMX571 má 16bit ADC a plná
   nádrž se vejde do 65535, <1× signál jen tłumí, >1× přidává šum.
4. Zda externí 11–14V napájení je potřeba mít připojené i pro pouhé USB
   enumeraci/streamování bez chlazení (varovná hláška v GUI, pokud
   `EnumV2` selže a napájení není indikováno)
   → napájení připojeno celou dobu, bez něj neměřeno; kamera bez něj
   nesvítí (červená LED = napájení OK). Varovná hláška v GUI zůstává.
5. Reálná teplotní stabilizační doba po `put_Temperature()` — pro UX
   semaforu a tooltip
   → **~3 minuty** z 32 °C na −5 °C (plný výkon TEC, naměřeno
   `scripts/cooling_probe.py`: 32,1 → −3,1 °C za 180 s, dosáhne cíle
   ±0,2 °C). Semafor ±2 °C je tedy po nastavení cíle „closed“ zhruba
   po 3 minutách; při vypnutém chlazení teplota vyleze ke ~25–32 °C.

## Doporučené pořadí implementace

1. Smazat Nikon soubory
2. `touptek.py` backend + Mock
3. `core/zoom.py` zjednodušení
4. `core/exposure.py` gain pole
5. Cooling UI
6. Export teplotní validace
7. Testy
