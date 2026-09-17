# CHANGELOG

Vše, co se od posledního stavu změnilo, a hlavně: **co nešlo bez fotoaparátu
ověřit** a jak to poznat při prvním zapnutí s tělem.

## 2026-09-17 — přechod na Touptek TS2600MP-G2 (monokrystal bez zrcátka)

Kolo **bez fotoaparátu** — vše níže je testováno proti MockCamera a FakeHcam
(262 testů), na hardwaru neověřeno. Specifikace přechodu:
`INSTRUCTIONS_TOUPTEK_CAMERA.md`; její §11 je **checklist pro první zapnutí
s pravou kamerou** — každý předpoklad, který fake SDK dělá, je tam bod.
Starý D750/gphoto2/SDK kód je smazán ze `main` a žije na branchi `D750`;
staré NEF projekty se záměrně nenačítají (clean transition).

### 1) Nová data: 16-bit TIFF místo NEFu

`core/rawio.py` přepsán na `tifffile`: archivní snímek je mono uint16 TIFF,
sidecar JSON nezapisuje do RAWu (zkoušeno testem „nikdy nemodifikuje zdroj“).
Rawpy/LibRaw demosaicing z develop pipelineu odešel s mozaikou — monokrystal
žádnou nemá.

### 2) `TouptekCamera` — nativní backend přes vendor SDK

`capture/touptek.py` + vendované `capture/_toupcam/` (toupcam.py +
univerzální dylib 43 MB, x86_64+arm64 — žádný Rosetta helper, na rozdíl od
Nikonu). Jednotky SDK se přepínají na backend kontrakt: shutter µs
(clamp 300 µs–1800 s), gain celá permile (readback po zápisu), teplota a
TECTARGET v 0,1 °C. Ověřeno `tests/test_touptek.py` (33 testů) přes FakeHcam,
který hlídá i to, co SDK jen slibuje: audit voleb po zápisu, Stop-před-
překonfigurováním (E_WRONG_THREAD), drop-oldest frontu, sekvenci Snapu
v RAW módu. **Při prvním zapnutí ověř:** fps při binningu 0x83, chování
Snapu v RAW módu, jednotky ExpoAGain, enumeraci bez napájení, reálné
dosetí TECu, a předpoklad `put_Roi(0,0,W,H)`-jako-vypnutý-ROI.

### 3) Live View je lineární 16-bit data — jedna upřímná cesta

Žádný JPEG, žádné auto-jas, žádná predikce NEFu: proud *je* radiometrie
snímku. `LiveViewWorker` posílá `(normalizovaný 0..1 float, měření)` vždy spolu; histogram, AE i náhled se nikdy neliší v tom, co měří. Predikční
přepínač histogramu (`hist_predict`) i `body_ev` kanál zemřely na nadbytečno.

### 4) Zoom mluví jazykem senzoru — overview 3×3 vs hardware ROI

Značky zoomu (100/200/400 %) jsou px obrazovky na **px senzoru**. Do 3×
ostříme na 3×3-binned overviewu (2074×1389, lineární průměr = poctivé
měření); od 3× vysílá senzor 1:1 ROI 1200×1200 kolem hledaného bodu
(`core/zoom.py::stream_plan`; Stop → překonfigurovat → Start, kvůli
E_WRONG_THREAD). Pozámka pod zoomem se přepočítává každý frame — lhaní
přes „zobrazení ×N“ je ten problém, který widget existuje dělat.
Ověřeno end-to-end proti Mocku (`stream_history`); na kameře viz §11.

### 5) GUI: gain místo ISO, TEC panel, audit z TIFFu

- **Gain, ne ISO:** spinbox 1.00–8.00×, tlačítko „Gain 1.00× (archiv)“.
  Archivní pravidlo přežilo: Capture při jiném gainu odmítne
  (`_capture_block_reason`), AE při zamčeném tlačítku řeší jen časem.
- **Chlazení:** panel s teplotou/cílem (−35…20 °C), TEC přepínač a
  semaphore ● — zelená = „u cíle“ (±2 °C), darky platí. Export projektu
  vrací `(zip, [čísel snímků bez darku ve stejné teplotě ±0,5 °C])` a GUI
  je hlasitě vyjmenuje. Mock modeluje TEC zrychleně ×20.
- **Audit:** `audit_frame` čerstvý TIFF (přímo produkční `rawio`), náhledový
  JPEG se renderuje off-thread; verdikt ≠ ok přepne čas na `×2^EV` a řekne
  „tento snímek zopakuj“.
- Připojovací dialog hlídá **napájení 11–14 V** (bez něj kamera není na USB).

### 6) Test suite přepracován na nové kontrakty (262 testů)

`test_touptek.py` nový; `test_gui.py` proti ROI/gain/chlazení (d750 třídy
body-crop, predikce NEFu a RAW-media guard smazeny s mizejícími featurami);
`test_capture/quality/zoom/developer` na reálných TIFF výstupech Mocku
(žádná injikace readeru — testy čtou produkční cestou). Testy zachytily
tři reálné bogy: `info.v3.expotime` (V4 obaluje V3 — padal každý capture),
Mock lhal teplotou u nechazené kamery, `display_scale` dělil zoom(scale).

### Odstraněno (nebojte, je to na `D750`)

gphoto2/Nikon backendy + SDK helper, `ptpcamerad` retry, Nikon MAID bindings,
NEF/LibRaw demosaic v developeru, `develop_colour` (color cesta), WB UI
(tlačítka z GUI odešla; WB math zůstává v `core/positive.py` pro barevný
náhledový řetězec a je pod testy determinismu), ISO widgety, body-zoom
(`set_live_view_zoom`), predikce histogramu, kontrola NEF media nastavení.

## 2026-09-16 — čtvercový náhled, celé násobky zoomu, WB, zrcadlo nahoře, ISO 100 tvrdě, audit NEFu

Kolo **bez připojeného těla** — vše níže neověřeno na hardwaru, testováno
proti MockCamera (246 testů). U každého bodu je, jak poznat při prvním
zapnutí, že to na těle nedělá to, co slibuje.

### 1) „Náhled je pořád ořez čtverec — chcu celé políčko 640×424“

**Příčina (ve kódu):** `ZoomView._crop_rect()` a `_clamp_center()` četly
`h, w = shape[1], shape[0]` — numpy `shape` je `(výška, šířka)`, takže se
šířka 640 zaměňovala za výšku. Z proudu 640×424 se řezalo přibližně
424×424. Opraveno na `shape[0], shape[1]`; Fit teď kreslí celý rám.

### 2) Zoom jen celými násobky — „ať není 3.425×“

Nové `ZoomView.source_zoom()`: zobrazení se snapne na **celý násobek
proudu** (×1 ×2 ×3 — 3× se vejde na displej); menší požadavky drží ×1 a
**posouvají se panováním**, nezmenšují. Výjimka: Fit na widgetu menším než
proud drží přesný zlomkový downscale — tam „vidět celý frame“ předčí
celistvost. Interpolace je vždy vypnutá (nearest) — zrno se má jevit ostře.
Poznámka pod zoomem i v obraze teď píše skutečně kreslené `zobrazení ×N`.
Ověřeno testy (`tests/test_gui.py`: snap na ×1/×2, výplň widgetu).

### 3) Oranžový náhled → BGR/RGB fix + Auto WB uzamčený pro film

- **Skutečná příčina oranžové:** `LiveMeter.decode_live_frame` dekoval
  OpenCV JPEG jako **BGR** a celý řetězec pak počítal kanály prohozeně.
  Jeden `cv2.cvtColor` na vstupu — histogram, AE i náhled najednou vidí
  barvy správně. (Tohle nebyl WB; to bylo prohozené pořadí kanálů.)
- **Tlačítko Auto WB** (řádek s režimy náhledu): gains se počítají z
  nejjasnějšího percentile snímku = filmový podklad (u negativu to
  nejjasnější, co rám nese), měřeno uvnitř červeného AE rámečku, je-li
  nastaven. Zisky `(r, 1.0, b)` se **uzamknou pro celý film** — per-frame
  vyvažování by oranžová maska barevného negativu roztančila (dýchající
  kast). WB těla se záměrně nepoužívá: tělo by si tipovalo samo a kamera
  WB nemá ani jak zachytit locked neutral pro celý film. Po stisku se zapne
  Negative náhled, ať je výsledek vidět („ukazovat po vyvážení“). New Film
  gains resetuje.
- K ověření na těle: po Auto WB musí být podklad v náhledu neutrálně šedý;
  při pochybnostech zamíř AE rámeček na proužek čistého podkladu.

### 4) Rychlý live náhled: S-křivka jako LUT („fps mi klesly“)

Celý Working Positive řetězec (WB → base subtract+invert → expozice →
filmic spline) jsou **tři per-channel mapy a nic víc**, takže se
předpočítá do tabulek 4096 úrovní na kanál (`FastPositivePreview` v
`core/positive.py`); spline se vyhodnocuje při změně parametru, ne per
frame. Base se měří jen každý 12. frame (a po změně parametrů) — base,
která by cukala každý frame, se jeví jako blikání. Křivka v tabulce je
*táž* `FilmicProfile` jako při exportu: rychlá cesta je kvantizovaná
verze pomalé, nikdy jiný vzhled (rozdíl držen pod krok tabulky — test).
K ověření na těle: po přepnutí na Negative musí fps zůstat srovnatelné
s RAW View.

### 5) Červený AE rámeček konečně platí i na SDK cestě

Tělní měřič D750 je globální a LV JPEG je auto-brightness (absolutní
hodnoty nevypovídají nic) — ale **poměr** luminance rámečku ke zbytku
frame uniformní display gain přežije. `_roi_bias()` změří poměr mediánů
(rank statistika JPEGem projde), klampuje ±3 EV a Auto Exposure pak cílí
tělní meter na *rámeček*, ne na celý frame. Cesty bez tělního měřiče
(gphoto2/mock) měří rámeček přímo — stará cesta zůstává. K ověření na
těle: tmavý rámeček uvnitř světlého frame musí vést k delšímu času, ne
ke zkrácení.

### 6) Zrcadlo nahoře: Capture během Live View

Nejzávažnější bod. Dosud Capture LV vypnul → zrcadlo sjelo → expozice →
zrcadlo vyjelo → **třes, svislé rozostření**. Nová cesta: poller se jen
**pozastaví** (`LiveViewWorker.pause_polling()` — tělo LV nikdy neopustilo)
a helper volá `capture_still` při běžícím LV; zrcadlo zůstává nahoře.
Capability `capture_in_live_view`, flag `keep_live_view` prostoupený
session → backend → helper. **Záložka v helperu:** pokud tělo při capture
při LV hodí chybu, zkusí lv_off → capture → lv_on a vrátí `lv_cycled=True`;
GUI to **jednou** poznamená a dál LV před expozicí vypíná rovnou (cyklovat
za každou cenu je přesně ten třes, kterému se uniká). gphoto2 zůstává s
cyklem — PTP při capturePreview still prostě neumí. Záměrně **nepoužity**
režimy MirrorUp/ExposureDelay (existence na D750 neověřena; ExposureDelay
hnutí navíc jen odloží, neodstraní).

**NEJISTĚJŠÍ BOD CELÉHO KYCLE — ověřit první:** jestli D750 vůbec dovolí
expozici při SDK `Capture` za běžícího LV (analýza to čekala, změřeno
není). Známky selhání: hláška „tělo při Capture vypnulo Live View“, nebo
zasekané LV po snímku.

### 7) „When ISO isn't 100, don't let me shoot“ — tvrdé pravidlo archivu

`ARCHIVE_ISO = 100` v `core/exposure.py`. Tlačítko **ISO 100** (přejmenováno
z „ISO min“) pinuje 100; Capture při jakékoli jiné citlivosti **odmítne**
s hláškou „stiskni ISO 100 a exponuj jen časem“ — tlačítko je šedé se
stejným tooltipem. Auto Exposure běží s `iso_lock=True` — řeší **časem**,
ISO nedeforčuje. (Důvod: celý film je jedno měření; měnit citlivost
uprostřed znamená měnit přenosovou funkci.)

### 8) Post-capture audit NEFu + náhledový JPEG (`capture/quality.py`)

Po každém snímku, mimo UI thread a **bez sahání na fotoaparát** (jen čte
soubor):

- **Audit** změří dovezený NEF v oblasti červeného rámečku (přepočet z
  proudu na sensor — **platí jen při Whole zoomu**; při body cropu je
  pozice výřezu na senzoru neznámá, audit pak meterí celý frame). Cíl =
  p99.9 na `white · 2^-0.4`. Verdikt `ok` / `over` (blow = >0.01 % pixelů
  na bílé) / `under`. Při verdiktu ≠ ok **rovnou upraví čas** pro další
  snímek (`choose_shutter`, jen při ISO 100 — jinak radši nic); stavová
  řádka hlásí „tento snímek zopakuj“. Exponovaný snímek zachránit nejde —
  audit existuje proto, aby další seděl.
- **Náhledový JPEG** `frameNNN.jpg` vedle NEFu: render z RAW (LibRaw
  demosaic v polovině velikosti; `use_camera_wb` **vypnuto**, jsou-li gains
  uzamčené — jinak by se korigovalo dvakrát), přes táž
  `to_working_positive`. Nikdy ne z LV JPEGu — auto-brightness by lhal i v
  náhledu archivu.
- K ověření na těle: JPEG musí sedět vedle .NEF, verdikt musí odpovídat
  skutečné expozici a po záměrné podexpozici se musí čas prodloužit.

### 9) SDK zoom enum 7 a 8 (13 %, 17 %) přidány do žebříku

Hodnoty potvrzeny v SDK tabulce; `BODY_ZOOM_SENSOR_PX_PER_LV_PX` je
interpretuje jako mělkší crop než 25 % (faktory 1/0.13, 1/0.17 — stejná
interpretace procent jako u zbytku tabulky, tedy **hypotéza** jako celý
zbytek tabulky — změř až pravítkem). Vedlejší oprava: `choose_body_rate`
dřív při neexistenci Whole vrátil `max(rates)` — s enum 7/8, číselně
většími ale mělkšími než 25 %, to vybírá špatně; teď se srovnává podle
skutečných faktorů (`_widest`).

### 10) Film dialog — předvyplnění podle reálné sestavy

Placeholdery: Film ID `K16O03_2025`, Fotoaparát `Nikon FM2` (foťák, co film
exponoval — ne D750, ten má skupina sestava), Objektiv
`Carl Zeiss MC Biometar 2.8/80, F8.0`, Světlo `LED11x15cm panel 4400 K`.
**Zrcadlově (lev/prav) je nově implicitně zapnuto** (skenuješ matnou
stranou k objektivu vždy) — vypne se jen když předchozí film říká opak.

### Drobnosti

- `CAP_SILENT_IMAGE_CAPTURE = 0x8328` **odstraněn** z `nikon_sdk.py` —
  0x8328 je ve skutečnosti WBPreset Protect3; použití by byla past.
- Přepnutí režimu náhledu teď hned překreslí i histogram (dřív kurva
  filmiku přibyla až s dalším frame).
- `audit_nef`, preview LUT i WB mají vlastní testy
  (`tests/test_quality.py`; syntetické „NEFy“ přes monkeypatch — rawpy se
  v testech nevolá).

### Co ověřit při prvním připojení těla (pořadí podle rizika)

1. Capture při LV (bod 6) — zrcadlo má zůstat nahoře, snímek bez chvění;
   při chybě záložka sama přepne na cyklus a GUI to řekne.
2. Audit (bod 8) na reálném NEFu — verdikt, korekce času, JPEG vedle NEFu.
3. Auto WB (bod 3) — podklad musí zůstat neutrální.
4. Zoom 13 %/17 % (bod 9) — pravítkem změř crop factor, oprav tabulku.
5. Celé násobky + Fit (body 1–2) — vizuálně, bez měření.
6. Rychlý LUT náhled (bod 4) — fps v Negative režimu.

## 2026-09-15 (noc) — AE na tělním měřiči, predikce NEFu, RAW guard, ořez, panel

Druhé kolo se živým tělem. Funguje, změřeno na D750: AE konverguje na
**jeden stisk** (`body_ev` 0.000, 1 iterace; po ruční změně času znovu 1
iterace), still je po nastavení Compression Level **skutečný NEF 24,9 MB**
(`smoke_nef.NEF`, 6032×4032, black 600 / white 16383).

### 1) „Nasnímaný NEF má 3008×2008 a 1 MB — celý frame chcu!“

**Příčina (změřeno):** tělo mělo `Compression Level = JPEG Basic` a
`Image Size = S(3008*2008)` — na kartu tedy posílalo **JPEG prevlečený za
.NEF**. Odtud `metadata unreadable … b'Input/output error'` v logu: LibRaw
dostal JPEG a neumí ho (upozornění z flatfieldu byl tenže jev, ne chyba
flatfieldu).

- helper `capture()` čte magii prvních bajtů a hlásí `file_format`
  `jpeg` / `nef` / `unknown`; backend soubor **poctivě přejmenuje** na `.jpg`
  (žádný lživý `.NEF`), session zapíše sidecar `file_format: jpeg` a rozměry
  z SOF markeru (`jpeg_dimensions()` — bez decoderu), radiometrii N/A.
- GUI: při připojení i po každém snímku kontrola Compression Level; chybí-li
  RAW, objeví se tlačítko **„⚠ Tělo posílá JPEG — Nastavit RAW + L“**, které
  napíše enum a **ověří zpětným čtením**. Při JPEG capture navíc dialog s
  výzvou snímek zopakovat.
- **Změřeno:** `Image Size` má při RAW vypnutý `OP_SET` — velikost pak
  konfiguruje jen JPEG doplněk, NEF je vždy L(6016×4016). Kontrola proto
  posuzuje **pouze Compression Level** (vynucovat Size by bylo věčné hučení).
  Při běžícím Live View tělo `Image Size` odmítá i zapsat (-127) — proto se RAW zapíná
  mimo LV nebo znovupokusem; zápis `Compression Level` funguje i v LV.

### 2) Auto Exposure — jedna stisk, tělní měřič (zaslouží si přečíst)

**Proč to předtím lhalo:** náhledový JPEG těla je **automaticky podsvícený**
(Změřeno 2026-09-15: medián 0.03026 lineárně je totožný při 1/2 s i 1/500 s a
ISO 100 i 1600). Regulovat expozici podle něj znamená řídit podle displeje,
ne podle senzoru — odtud „stojím na nejmenším čase“, „ISO se neprojeví“
a prodlužování o jeden stupeň na klik.

- `AutoExposureController` nyní na backendech s tělním měřičem (`exposure_ev`
  = MAID `ExposureStatus`, změřeno: −3,00 EV přesně pro 3 stopě času, +4,00
  pro 4 stopě ISO) **řeší, ne iteruje odhadem**: přečte, kolik stop chybí,
  *najednou* napíše čas i ISO a ověří proti přečtenému měřiči.
- **Politika pro archiv:** víc světla kupuje **nejdřív čas** (ISO drží na
  dně a roste, až když se žebřík časů vyčerpal); míň světla se odebírá
  **nejdřív snížením ISO** (zdarma), teprve pak zkracuje se čas.
- **Limity jsou teď poctivé:** hlásí konkrétní příčky
  („ani 30 s a ISO 25600 je scéna o X EV tmavší — přidej světlo“) a jen tehdy,
  když je příčka opravdu na doraz. Zbude-li reziduuma kvůli tomu, že tělo má
  jen celý stupeň, hlásí se to jako **dokončeno s poznámkou**
  („zůstává 0,33 EV — jemnější krok tělo nemá“), ne jako limit.
- **Odpověď na otázku „jednou a stačí?“:** Ano — AE je uzavřená smyčka
  vůči aktuální scéně. Stiskni **jednou**. Změníš-li pak ručně čas/ISO nebo
  osvětlení, expozice už neodpovídá tomu, co změřil — stiskni znovu (není co
  „držet“, nic se neztratí). Před každým snímkem AE dělat nemusíš: scéna na
  podavači se mezi snímky nemění. Přepínač čas/ISO proto AE samo nemaže —
  jen se řídí tím, co právě naměříš. Chování i důvod jsou nyní textem v tooltipu
  tlačítka AE.

### 3) Histogram = predikce NEFu

Histogram náhledu nevypovídal o expozici nic (auto-brightness). Nově:
- poller `LiveViewWorker` čte tělní `exposure_ev` (throttlovaně 5×/s) a posílá ho
  s rámem;
- histogram zobrazuje **predikci**: jas náhledu škálovaný o `2**ev`, tj. jak
  dopadne NEF. Popisek nápisek jménem říká *„predikce NEFu (…, tělní meter −2.00
  EV)“*;
- přepínač **„Histogram = predikce NEFu“** (zapnuto) — vypnutím se vrátíš na
  histogram samotného náhledu (hodí se při skládání). Bez tělního měřiče
  (mock/gphoto2) se chová jako dřív.

### 4) „Nedělej ořez“ — nízký zoom ukazuje celý frame

Tělo při **jakékoli** zoomovací rychlosti posílá **640×480** (změřeno;
whole-frame má 640×424) — i ten nejmenší tělní zoom je okno ~43 % × 48 % snímku,
což jsi četl jako „oříznutý čtverec“. Nově se tělní ořez žádá **až od 1:1
dál** (`MIN_ZOOM_FOR_BODY_CROP`); pod 1:1 se interpoluje celý frame — nikdo
při přehledu nehodnotí zrno, ale ořezané okraje ano. Zobrazovací geometrie
už dříve centeruje a letterboxuje; ořez je teď jen tam, kde kupuje reálný
detail.

### 5) Pravý panel — vertikální komprese

- **Čas a ISO vedle sebe** na jednom řádku, `ISO min` vedle korekce EV;
  popisky ovládaných prvků nesou tooltipy (v 360px sloupci žrály šířku).
- Dark/Flat + číslo snímku → složitelný box **„Kalibrace & číslo snímku“**.
- Přepínač negativu se přesunul vedle RAW View / Positive do jednoho řádku
  (zůstal vně sbaleného boxu — dřív vyžadoval tři kliky a zmizel).
- Popisky měření a fáze menší, log omezen na výšku 64 px.
- Celý panel je v `QScrollArea` — i na nízkém okně se nic neořízne.

### Testy

`uv run pytest -q` → **226 passed** (předtím 209 + toto kolo). Nové:
`TestAutoExposureBodyMeter` (7 — jedna-stisk, ISO místo leže, skutečné extrémy,
straddle, oscilace ≠ limit), `test_capture_sniffs_jpeg_masked_as_nef`,
`test_backend_renames_jpeg_to_jpg`, `test_jpeg_disguised_as_nef_is_recorded_honestly`,
`TestJpegDimensions`, `TestHistogramPredictsNef` (3), `test_body_ev_fn_is_throttled_and_delivered`,
upravené `test_zoom` (0,5 → celý frame).

## 2026-09-15 (večer) — RPC zámka, AE/histogram, pan v zoomu, negativní přepínač

První večer s připojeným tělem odhalil chyby, které přes mock neprošly:

### Chyby z logu (vyřešeny)

- **`JSONDecodeError: Extra data / Expecting value`** při zoomu i zápisu času:
  LiveView poller a UI vlak (zoom/čas/ISO) psaly a četly ze stejné dvojice
  pipe helperu současně — každý si přečetl cizí odpověď. `nikon_backend` má
  nově `_rpc_lock` držící *celý* request/reply; test
  `test_concurrent_rpcs_never_interleave` to hlídá (60 volání ze 2 vlaken).
- **`lv_frame: MAID -127`** při Auto Exposure: worker při stopnutí vypnul
  Live View a AE si pak nechal posílat snímky. Cesta AE nově nastaví
  `worker.leave_live_view = True` (ukončí poller, tělo nechá v LV). Pojistka
  navíc: `next_live_frame` vidí -127 → jeden automatický restart LV.
- **Auto Exposure se po 1–2 pokusech zašedilo nastálo**: `_set_actions_busy(False)`
  znovupovolila tlačítka jen když existoval film; AE ale film nepotřebuje.
  Uvolňování nyní deleguje na `_refresh_buttons()` (jediný zdroj pravdy).

### UI požadavky

- **Histogram měří ze stejného červeného výřezu jako AE** (dřív celý snímek).
  Popisek i čísla clipu hlásí rozsah měření (`AE výřez` / `celý snímek`); hodnota
  p99.9 v labelu souhlasí s histogramem. Neplatný výřez mimo snímek →
  padá zpět na celý snímek. Při změně body-zoom se výřez smaže (jeho souřadnice
  platily pro starý proud).
- **Zoom teď vyplňuje okno**: geometrie výpočtu je nově v reálných číslech
  (`QRectF`) — dříve zaoblená velikost výřezu + cíl nechaly mezery a obraz
  „jen od levého horního rohu“; při zoomu menším než okno se Frame
  centrovaný (doposud ukotvený do rohu).
- **Při zoomu lze tažením posouvat po snímku** (pan) — bod pod prstem
  zůstává pod prstem. AE obdélník se kreslí **Shift+tažením** (i tažení bez
  Shiftu v Fit zůstává AE); klik = vycentrovat, pravé tlačítko = zrušit
  výřez. Posun je jen pohled — ořez těla (LiveViewPosition) dál čeká na
  změření (⚠️ bod 2).
- **Přepínač „Náhled negativu (invert + křivka)“**se vrátil do pravého
  panelu nad zoom: jedním kliknutím Working Positive s invertou; nastavení
  křivky zůstává v panelu. Přepínač se synchronizuje s checkboxy RAW View /
  Working Positive.

Testy: 209 passed (z toho 11 nových). Live smoke s D750: LV, 5× změna
body-zoom během pollingu, zápisy času/ISO, re-start LV — vše bez chyb.

## 2026-09-15 — oprava: x86 helper spadl při importu → tichý fallback na gphoto2

**Příčina:** `WARNING … Nikon SDK helper neodpověděl na 'connect'; falling back
to gphoto2` nebyl problém fotoaparátu. Přidal jsem do `sdk_server.py` import
`from …nikon_backend import parse_iso, parse_shutter` a ten přes `camera →
core.exposure` natáhl **numpy** — které v `.venv-x86` (stdlib + cffi) není.
Helper zemřel při importu, stderr šel do `DEVNULL`, a GUI čekalo plných 30 s
na RPC timeout, než vyhodilo nic neříkající hlášku.

**Oprava:**

- `capture/parsing.py` (nový) — `parse_shutter`/`parse_iso` bez jediné
  závislosti; používá je helper i GUI strana (`nikon_backend` je jen
  re-exportuje, aby existující importy a testy šly).
- `sdk_server.py` importuje parserty výhradně z `parsing` — nic z grafického
  řetězce.
- `nikon_backend`: stderr helperu jde do dočasného souboru (ne `DEVNULL`, ne
  roura — ta by se po zaplnění zasekla) a při neúspěšném `connect()` se
  připojí k chybové hlášce: *„… — helper: spadl (exit 1): ModuleNotFoundError:
  …“*. Chyba je hned, bez 30 s čekání (readline na mrtvé rouře vrátí EOF).
- `_rpc()` už u mrtvého helperu hlásí `NotConnectedError` se stejným
  rozlišením; `disconnect()` zavírá log.
- `sdk_server.connect()` při „no device“ projde discovery **podruhé**
  (~1,5 s navíc) — po přepojení USB bývá první výčet prázdný. Záměrně jen
  jednou, aby opravdová absence těla nepozdržela fallback na gphoto2.
- Testy: `test_crashed_helper_fails_fast_and_names_the_reason` (mrtvý helper →
  immediate error citující stderr) a `TestHelperImports`, který **v reálné
  `.venv-x86`** změří, co `sdk_server` importuje — příští numpy-sem-táhl-import
  spadne v CI, ne v terénu.

## 2026-09-15 — zoom v pixelech senzoru, AE výřez, metadata filmu, layout

### Zoom (hlavní změna)

- Žebříček zoomu je nově v **pixelech senzoru**: `Fit`, `12,5 %`, `25 %`,
  `50 %`, `100 % — 1:1 pixel NEFu`, `200 %`. 100 % = jeden pixel monitoru na
  jeden pixel fullframe obrazu (6016×4016), ne Pixel náhledového okna.
- Při volbě zoomu okno požádá tělo o odpovídající výřez proudu
  (`LiveViewImageZoomRate`, jen Nikon SDK backend). Gphoto2 nic takového
  neumí — chová se jako dosud (pouze digitální zvětšení 640 px proudu)
  a okno to při připojení napíše do logu.
- **Honestní badge**: pod comboem zoomu (a překryvně v náhledu při zoomu)
  stojí, kolik pixelů senzoru jeden proudový pixel ve skutečnosti zastupuje a
  kolikrát se interpoluje. Důvod: D750 posílá Live View pořád ~640 px
  (měřeno: whole 640×424, 100% zoom 640×480), takže i při body-zoomu 100 % je
  proudový pixel 1:1 s pixel senzoru, ale jich je jen 640 z 6016 — detail
  roste ~9× oproti whole, ne 9.4× na plný fullframe. **Plné 1:1 rozlišení NEFu
  přes Live View fyzicky nejde**; jediné, co ji dá, je ostrý capture + výřez
  z NEFu (viz TODO).
- Přepnutí zoomu mění i body-rate; Fit vždy vyslal Whole (celý frame).

### Auto Exposure z výřezu + report přeplácnutí

- Tažení myší v náhledu vykreslí **tenký červený rámeček**; Auto Exposure
  pak měří jen uvnitř něj (pravé tlačítko výřez zruší, krátké kliknutí stále
  přecentruje). Bílý okraj filmu/přeexponované nebe tedy expozici nezkreslí.
- Report přeplácnutých pixelů: pod histogramem číselně
  (`clip: bílá x.xxx % (červeně) · černá x.xxx % (modře)`), na histogramu
  samotném červený sloupec na pravé liště a modrý na levé.
- Měření AE i histogram počítají z plného proudu; výřez se aplikuje jen na AE.

### Metadata „Nový film“

- Sloučené: **Film (výrobce + typ)** na jedno pole („Fomapan 100 Classic“),
  **Vyvolání** na jeden řádek („R 09 1:50 8 min @22C“).
- Nová pole: Fotoaparát, Datum start / Datum konec (text! „asi 12/25“ projde),
  Obsah filmu, **Zrcadlově (lev/prav)** — flag, Objektiv / Světlo / Držák
  sestavy, Datum digitalizace (předvoleno dnešní).
- Při „Nový film“ se **předplní jen sestava** (fotoaparát, objektiv, světlo,
  držák, formát, třída, zrcadlově, operátor) z posledního filmu. Jméno filmu,
  ID, vývojka, data a obsah se **záměrně nepřenášejí**.
- Staré sidecary (schema v1: `manufacturer`+`film_type`, 3 vývojkové sloupce)
  se při čtení automaticky sloučí — data na disku se nepřepisují.
- Flag zrcadlově se **zatím jen ukládá** do metadat (sidecar + katalog).
  Převracení obrazů nikde neběží — viz TODO.

### Expozice na dvě vrstvy

- „Náhled — expozice & křivka“ je **svinutý panel** a nesmí naznačovat, že
  sahá na expozici focení (filmic exposure, stíny/středy/světla, invertace).
- „Expozice fotoaparátu“: editovatelný **čas** (text, snapne na žebříček těla),
  **ISO** dropdown, tlačítko **ISO na minimum (kvalita)** — pro digitalizaci
  ISO 100 a níž, a **Korekce expozice** (EV, ExposureComp — nastavuje se jen
  pokud tělo dovolí; v režimu A na těle ISO/čas nastavitelné není, to oznamuje
  stávající chyba z pomocníka).
- Clona: beze změny, nefunguje a nefungovat nebude — manuální sklo
  v kompendiu; okno to po připojení napíše.

### Layout

- Live View je středové okno přes téměř celou plochu; histogram, expozice,
  zoom, akční tlačítka a vývojové info jsou v jednom pruhu vpravo (360 px).

### Oprava textu, který mátl

- „Nikon D70 · ISO22 · časů 52“ byl display bug: vypisovalo se
  `ISO {len(iso_choices)} · časů {len(shutter_choices)}`, tedy **počty nabízených
  voleb** (22 ISO, 52 časů), ne nastavení. Model „D70“: ten text tiskne
  `manufacturer + model` z backendu — buď byl připojený gphoto2 fallback
  (SDK pomocník nebyl nainstalovaný), nebo tělo hlásilo „D70“; nastavení se
  teď zobrazuje správně (`ISO 100–12800 · časy 1/4000–30s`) a hodnota
  ISO/času je vždy čtená z těla, takže hodnotám v tom starém textu nevěř.

## ⚠️ Neověřeno na hardwaru (spusť `scripts/probe.sh` s fotoaparátem)

1. **Platnost tablelu `core.zoom.BODY_ZOOM_SENSOR_PX_PER_LV_PX`.**
   Předpoklad 25/33/50/66/100/200 % = 4/3/2/1.5/1/0.5 px senzoru na proudový
   pixel je z dokumentace Nikon LV zoomu, **ne z měření**. Nově změřeno
   (2026-09-15, `probe_round3`): whole-frame stream = 640×424, **každá**
   zoomovací rychlost = 640×480 — tj. poměr stran se mění (4:3 proti 3:2),
   tablel čestnosti platí jen pro vodorovný rozměr; svislý cover asi bude
   oříznutý. Probe teď uloží
   `lv_zoom_0.jpg … lv_zoom_6.jpg` — proměř je na pravítku/graph paper a
   hodnoty oprav. Podle toho uprav i `HONEST_INTERPOLATION_LIMIT`.
2. **Kam tělo výřez umístí** (`LiveViewPosition`? caps 0x8240/0x8241 v
   prázdné sondě?) — bez toho klikání na bod při body-zoomu zobrazuje výřez
   tělem vycentrovaný, ne na kliknuté místo. Centrovaní zůstává „přibližně“.
3. **`LiveViewImageSize` (0x8353)**: D750 nabízí prvky `1, 2` — které je
   jaké rozlišení není zjištěno. `set_lv_size` v pomocníkovi existuje, UI ho
   nezasahuje. Změřit: pro každý prvek dekódovat.frame a zapsat rozměry.
   Pokud je „2“ větší než 640, zvýší se reálné detail — pak vyplatí se do
   `start_live_view` přidat automatický maximální výběr.
4. **Exposure preview** (`LiveViewExposurePreview` 0x8333): dle CapInfo v
   sondě GET-only — tělo preview expozice dělá samo a nedá se mu kázat.
   Proto se nastavení náhledové expozice nechává čistě softwarové (filmic).
   Ověřit chováním na těle (záporná EV korekce → ztmavne LV samo?).
5. **Zoom při běžiícím proudu**: nastavení rate probíhá z UI vlákna (helper
   odpovídá v ms, `set_enum_value` má retry na DeviceBusy). Pokud by to při
   reálném LV sekalo/zhadlo, přesunout `set_live_view_zoom` za
   `_pause_live_view` do `CameraWorker` (pomalu, ale bezpečně).
6. **Editace času/ISO z UI vlákna** — stejné riziko jako 5; u SDK helperu
   drobný enum write, u gphoto2 `set_config` trvat může déle.
7. **Textové pole „Korekce expozice“ u gphoto2 backendu**: parsování localizovaných
   voleb `exposurecomp` (`_parse_ev`) netestováno na reálném výčtu.
8. **Round-trip sidecaru s `mirrored`**: starší katalogy (schema v1 DB) se
   otevřou bez migrace (metadata jsou JSON uvnitř), ale `project_export.json`
   zvěčňuje `schema_version` DB 1 — sloučit exporty s různými verzemi zatím
   nic neumí.

9. **RAW zápis při běžícím Live View**: `Compression Level` šel nastavit i v
   LV, `Image Size` odpověděl -127 a po RAW nastavení ztratil i `OP_SET`
   (velikost se vztahuje jen na JPEG doplněk). GUI tlačítko RAW proto
   píše pouze Compression Level. Chování při vybité baterii / jiném
   režimu voliče nezměřeno.


## TODO (vědomě neděláno)

- **Zoom 1:1 z plného rozlišení**: capture + výřez z NEFu (ostření z reálného
  snímku, ne živé). Sloužilo by tlačítko „Ostřit na NEF“ — vyžádá si
  rawpy demosaic a focus panel.
- **Aplikovat `mirrored`**: převrátit export ve vývojářském pipeline (a
  případně náhled) — zatím jen metadata, dle dohody.
- **Auto maximální `LiveViewImageSize`** po změření bodu 3.
- Překlopit `choose_body_rate` na naměřené tabulky po bodu 1.
- Podpora `LiveViewPosition` pro přesné centrovaní při body-zoomu (bod 2).
