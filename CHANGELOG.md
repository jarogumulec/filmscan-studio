# CHANGELOG

Vše, co se od posledního stavu změnilo, a hlavně: **co nešlo bez fotoaparátu
ověřit** a jak to poznat při prvním zapnutí s tělem.

## 2026-09-20 — developer GUI: osa histogramu, kurzor pixelu, jemná expozice

**Řezání histogramu vlevo — příčina:** osa byla pevně od **nuly**
(`density_histogram(range=(0.0, xmax))`, `D_HIST_MAX` záruka doleva). Vše
pod nulou — D pod film base (fog, lamp drift, u flatless relativní měření
mínusové od podsvitu) — do histogramu nevlezo a jevilo se to jako uříznutý
levý okraj dat i křivky. **Oprava:** osa `_xmin.._xmax` se nyní počítá
z dat: `xmin = min(0, min(D) - 0.02)`, `xmax = max(xmin + 0.5, p99,9·1,05)`
(sestupná záruka 0,5 D, ať se škála nesrazí na proužek). Křivka i mrtvá
zóna se kreslí přes `_d_to_x` se stejným `_xmin`, takže sedí na data.

**Histogram NENÍ z výřezu** — je z celého snímku (`build_density(crop=False)`),
stejně jako náhled i status; ROI se uplatní až při exportu archivu
(původní záměr, ROI rámeček se kreslí do whole-frame souřadnic).

**Kurzor pixelu (nové):** `DensityView.hovered` signal — jakmile myš pojede
nad náhledem, vyšle `(D, out)` pixelu pod ní (D z on-the-fly Hustotního
podvzorku, `out` z renderu už **po** expozici i křivce; `_subsample` je
stride, takže `display[i,j]` ≡ `sub[i,j]`). V histogramu běhá **oranžová
svislá čára** na D, do status řádku se píše
`pixel D +1,415 · pozitiv 0.441`. Mimo snímek / `leaveEvent` se maže
a status vrací popis měření.

**Jemná expozice:** jezdec míval krok ⅓ EV (test4: skokem ~0,33 EV —
uživatel vnímal ~0,7). Nově `QSlider` ±6 EV v tickách po **0,01 EV**
(singleStep 0,05, pageStep 0,2) + **`QDoubleSpinBox`** s libovolnou hodnotou
(zadáš 0,15 → jezdec jede na 15). Obousměrné propojení, rozlišení shodné
→ žádná oscilace. `lbl_ev` nahrazen spinboxem; `current_params()` čte spin.

**"neprostup" přeclarováno:** byl to +inf D = *hustší než škála* (film
nestihl prosvitovat) → v pozitivu **bílá**, ne černý pod. Popisek ahora
`nad škálu(→bílá)`; přepal (−inf D) ponese `→černá`. Komentář vysvětluje,
že černý pod je opak — nízké konečné D viditelné v histogramu.

Testy: `TestExposureSpin`, `TestPixelProbe`, `TestHistogramAxis`
(+ úprava EV hodnot v existujících GUI testech na nové rozlišení).
Ověřeno headless na `test4`: osa −0,026..2,598, hover píše `pixel D +0,033
· pozitiv 0,441`, `spin 0,15 → slider 15`. 431 testů zelených.

## 2026-09-20 — developer: náhled i bez flat snímku

Složka bez `flat_*.tif` dříve v vyvíječi **nevykreslila nic**:
`build_density` hodil „project has no flat frames", GUI ho chytil jako
„Chyba měření" a náhled zůstal prázdný s textem „Otevři složku projektu" —
i když otevřená byla. Nyní:

* **Fallback reference** (`project.py`): bez flatu nastupuje reference z
  vlastních highlightů snímku (percentil 99,9 above-black signálu, konstantní
  plocha) — design 04 připouští percentilovou base jen jako preview fallback,
  ne jako měření. Hustoty jsou pak **relativní** (nula = nejjasnější místa),
  Dmin z film base bez flatu stejně změřit nejde → zůstává ruční.
* **Poctivý záznam**: `DensityProvenance.flat_fallback` (i v JSON hlavičce
  archivu) — archiv z relativního měření se navenek nevydává za absolutní.
  Přidané additivní pole, schéma beze změny, staré archivy čitelné.
* **GUI**: status lišta nese „⚑ bez flat snímku — náhled je relativní
  (k nejjasnějším 0,1 %); Dmin zadej ručně"; prázdný placeholder náhledu
  už nikdy nelže otevřené složce (při reálné chybě měření píše co selhalo);
  Proposal bez měření base nespadne, když relativní dmax ≤ výchozí dmin
  (dmax i tak musí škálu přesáhnout).
* **Ověřeno** na reálné složce `local scan archive/test4` (flat chybí,
  4 snímky): náhled 1245×834 vyjde, status radí, exporty se odblokují.
  Syntetické testy: `TestFlatFallback` (project) +
  `TestFlatlessProject` (GUI). 424 testů zelených.

## 2026-09-20 — GUI: návrat tlačítka Flat Field

Refactor „Kalibrace & číslo snímku" (collapsible box, ~2026-09-15) tichounce
ztratil `btn_flat` — tlačítko se sice vytvořilo a napojilo, ale nikdy se
nepřidalo do layoutu (`addWidget` chybělo v `calib_row`). Tmáře ani film base
se nic nestalo; jen Flat zmizel z UI. Opraveno doplněním `addWidget` +
regresivní test `test_calibration_buttons_are_parented_into_the_box`
(parenting všech kalibračních tlačítek). Logika dark/film base nedotčena.

## 2026-09-20 — capture: LCG + Low noise režimy, oprava jednotek gainu (přímý povel)

418 testů zelených; ověřeno na reálné kameře (end-to-end still s metadaty).
Zásah do capture vrstvy **na výslovný povel uživatele** (checklist 2026-09-20).

**OPRAVA JEDNOTEK GAINU — `GAIN_UNIT` 1000 → 100.** SDK `ExpoAGain` jsou
**procenta** (Gain Value; `toupcam.h` „percent, such as 300",
`TOUPCAM_EXPOGAIN_MIN=100`, hardware readback `(100, 10000, 100)` = 1–100×
dle manuálu). Dřívější permilový dělič znamenal: to, co aplikace i metadata
jmenovaly „archivní gain 1,00×", bylo fyzikálně **GV 1000 = 10×** (+20 dB).
Dopad na staré archivy: DN hodnoty platné, popisek `gain: 1.0` lhal (bylo
10×); UI gain spin teď ukazuje skutečných 1–100× s výchozí 1,00×.

**Konverzní gain + Low noise jako režimy** (`camera.py` `SensorModes`,
`touptek.py` `apply_default_modes`/`set_conversion_gain`/`set_low_noise`):
kamera přetrvává v **HCG** (hw: CG=1 při connectu) — aplikace nyní při
connectu nastaví **LCG + low noise** (optimum skeneru: max full well 51 ke−
a DR ~14,4 stopu; low noise na stillu prokazatelně neškodí — jen poloviční
kadence náhledu). V GUI
zaškrtávátka „LCG" a „Low noise" (implicitně zapnuto, per QSettings,
přepínatelná za chodu — hw to bere na běžícím proudu; CG mění DN stupnici
~2,8×, LN jen živý náhled ~0,83× (still 0,996× — měřeno), status bar
připomíná přeřešit expozici). Obojí se píše do
`AcquisitionMetadata` (`conversion_gain`, `low_noise`) → TIFF JSON i sidecar.
Schéma beze změny (additivní pola, staré sidecary čitelné).

**Hardware-probe (camera_tests (e), `probe_modes.py`):** FLAG_CG i
FLAG_LOW_NOISE přítomny; HDR odmítnuto; fps 6,99→3,57 (LN); LN funguje i s
3×3 overview binningem (3,65 fps — manuálové „jen All Pixel" na tomto kuse
neplatí tvrdě); funkční poměr HCG/LCG DN = 2,81 (manuál 3,01); LN posouvá DN
na ~0,83× **jen v live proudu** — still DN 0,996× a σ beze změny.
Světelná fáze měření šumu (`lcg_hcg_snr.py`): při stejném DN má LCG
σ 1,76× nižší a 3,06× expoziční headroom, oba módy shot-noise-limitované;
tma čeká
provoz.

## 2026-09-19 (2) — capture: konec zámku gainu + kalibraverage 10× (přímé povely)

409 testů zelených.

**Zámek gainu pryč.** Capture nezahlaví snímek při gainu ≠ 1,00× —
`_capture_block_reason` zůstalo jen „není připojen fotoaparát", výhrůžný
tooltip tlačítka je pryč. Důvod: měření (d) ukázalo, že nízký gain s kompenzovanou
expozicí je legitimní (0,1× = +1,44 dB); operátor si vybírá sám.
`ARCHIVE_GAIN` zůstává jako výchozí hodnota (konekt, mock), ne jako pravidlo.

**Kalibrace averageuje implicitně ×10** (`CALIBRATION_AVERAGE`): Dark, Flat
i Film base fotí průměr z 10 expozic do JEDNOHO TIFF (stejná cesta jako
spinbox u skenu, ale nezávisle na jeho hodnotě). Flat byl dřív 3 samostatné
soubory pro median na disku; teď 1 averaging soubor (√10 > √3, median proti
kosmickému záření na tomle senzoru nemá co řešit). Vývojářská vrstva čte
soubory z disku a medianuje je — jedno averageované pole projde beze změny.
Status bar hlásí „Dark: average 3/10…" i u kalibrace. Počet `count` sad
zůstává jako parametr (implicitně 1).

**Doba:** flat/dark/base trvají ~10× déle (u mocky ~8 s; na kameře
10×(expozice + ~1 s readout)). Dark averageuje taky — tlumí readout šum i
kolísání teploty, tmavý reference chce √K stejně jako flat.

## 2026-09-19 — capture: SW clamp na hw dno + averaging snímků (přímé povely)

406 testů zelených. Zásah do capture vrstvy **na výslovný povel uživatele**
(ruší dočasně zákaz sahání do `capture/` ze checklistu).

**Clamp expozice 300 → 100 µs** (`EXPO_TIME_RANGE_US`): SW dno bylo
neprozkoumaný odhad, hw dno změřeno (camera_tests (a)): pod 100 µs firmware
`E_INVALIDARG`. Kotvící test `test_shutter_floor_is_the_hardware_floor`
brání návratu. Pod ~400 µs zůstává kvantování po ~75 µs — readback v
`set_shutter` to operátorovi ukáže.

**Averaging:** vedle tlačítka Capture je spinbox „Average" ×1…×16.
`capture(frames=K)` proběhne K expozic v JEDNÉ still session (K× Snap,
jedna rekonfigurace), uloží se **jediný TIFF = průměr** (float32 akumulace,
žádné whole-DN biasy; dílčí snímky se neukládají — rozhodnutí operátora).
`AcquisitionMetadata.frames_averaged` (default 1 = i staré sidecary, bez
bumpu schématu), do logu poznámka „average: uložen průměr z N expozic",
status bar „average k/n…" přes relay. Dary/flaty zůstávají více-souborové
záměrně — jejich averaging je median stack v kalibraci. Hodnota spinboxu
se persistuje přes QSettings.

**Ověřeno na kameře 2026-09-19 (ATR2600M na rigu):** 10× Snap v jedné still
session proběhne bez zádrhele — flat i dark ×10 trvají 9,5 s (≈ expozice +
~0,9 s readout/download na snímek). Fyzika averageu sedí: σ single 5,7 DN →
σ average(10) 2,0 DN, podíl 2,86 (√10=3,16; zbytek spolkne nestabilita
světla). Sidecary nesou `frames_averaged`, sken při gainu 0,5× proběhl bez
výhrady (zámek pryč). `DevelopProject.open` + calibration + hustota +
developerské GUI (náhled, histogram, slider, export pozitiv/flat/hustota,
reopen `develop_settings.json`) nad hw averageovaným projektem prošly bez
kolize. Poznatek: kamera si po odpojení napájení drží vlastní poslední
nastavení (připojila se na 0,1×/10 ms) — aplikace je poctivě čte a ukáže je;
bez zámku gainu je na operátorovi, aby se podíval na stavový řádek.

## 2026-09-18 večer (2) — vyvolávač: histogram D + vizualizace křivky

397 testů zelených. Čistě `developer/gui.py` + testy; vrstvy neměny.

**`DensityHistogramWidget`** pod náhledem: osa x je **hustota v [D]** (doména,
ve které se rozhoduje), ne jas výstupu. Odpověď na „v jakém rozsahu fotka je"
je histogram nad hustotou — data (konečné D) se počítají na plném rozlišení
při výběru/ROI snímku, ±inf (směr, ne hodnota) do nich nepatří a kreslí se
jako sloupce na kolejnicích: bílá vlevo = přepal (−inf), modrá vpravo =
neprostupno (+inf); NaN („bez světla") jen číselně do stavového textu.

Tři vrstvy nad jednou osou: **histogram** (log. výška, ať mása není nevidit;
plň i linka), **body stupnice** Dmin (cyan) / Dmax (oranžová) tečkovaně —
a pole mimo ně se stmívá (červeně pod Dmin = mrtvá zóna pod tiskovou černí,
modře nad Dmax), takže posun slideru je sofort vidět jako „kolik obrazu
zahazuji", a **bílá křivka** = promítnutí `RenderParams` (toe/gamma/shoulder
+ expozice): kde který D dopadne na tisku. Osa se automaticky roztáhne, aby
Dmax i pravé konto vešly. Reaguje na každý slider přes `rerender()`.

## 2026-09-18 večer — vyvolávač: pozitiv, per-snímkové nastavení, orientace

393 testů zelených. Zásah jen do developerské vrstvy (`core/render.py`,
`core/density.py`, `developer/*`) — capture pipeline se nezměnila.

**Inverze na pozitiv.** `render_density` dosud počítal `out = 1 - y`, což je
**negativ znovu** (u negativu hustota roste tam, kde byla scéna jasná —
pozitiv musí být světlý TAM TÉŽ). Nyní `out = y`: base (nejméně zákalu) →
černá, dmax → bílá; expozice +EV jasně zjasňuje. Testy přepsány na fyzikální
invarianty (bázový black, dmax white, monotónnost D↑ → jasnější).

**Přepaly a neprostupné hustoty už nejsou černé tečky.** Dřív
saturace i `T <= 0` splývaly v NaN → export je zalil černou a náhled kreslil
purpurem. Nyní `density()` rozlišuje *směr*: saturace (film světlejší než
změřitelný) = **−inf D** → po renderu černá (spodek stupnice);
`T <= 0` (hustší než tmavé reziduum) = **+inf D** → bílá. NaN zůstává jen
„nedosvětlo" (mimo ozářenou oblast) — purpur v náhledu, černá v exportu,
a to je poctivá „neměřeno". Nová `illuminated_mask()` odděluje masku
ozáření od masky měřitelnosti; `valid_mask()` nadále platí pro audit.
Na test2: 0,45 % pixelů +inf, 0,02 % −inf, 15,3 % skutečné NaN (okraje).

**Per-snímkové parametry + persistence.** Slidery (Dmin/Dmax, expozice,
křivka) se pamatují zvlášť pro každý snímek do `develop_settings.json`
ve složce projektu; při přepnutí snímku se nahrají zpět, ROI rámčky také.
Tlačítko **Proposal** vrátí auto body (Dmin z film base, Dmax z p99,9)
s lineární křivkou — nový snímek bez historie startuje na Proposalu,
žádné volitelné S-ko jako výchozina.

**Orientace filmu.** `DevelopProject` čte `mirrored_horizontal` /
`mirrored_vertical` / `rotated_180` z `project.json` (vynáší capture apka;
test2 má H+V). Překlopí se náhled i oba exporty; **hustotní archiv zůstává
v surové orientaci senzoru** (data se nepřepisují, jen se zobrazují).
Orientace je vidět v levém sloupci.

## 2026-09-18 — fix: vývojářské GUI spadlo na paintEvent

`DensityView` měl `@property rect`, který **clonoval `QWidget.rect()`** —
každé `self.rect()` v `paintEvent` (i uvnitř Qt) pak zabalovalo ROI tuple
→ `TypeError: 'tuple' object is not callable`, nekonečné přes kreslení.
Property se jmenuje `roi` (nikdy ne `rect` — komentář v kódu to hlídá);
testy v `tests/test_developer_gui.py` přejmenovány. 11 testů zelených.

## 2026-09-18 — červený rámeček se ukládá jako pokyn k ořezu

375 testů zelených. GUI-only + metadata cesta; žádná změna kamery.

**`CaptureRecord.crop_rect`** — nový volitelný field `(x0, y0, x1, y1)` v **px
plného rámce** (grid TIFF souboru, nikdy ne 3×3 binovaný proud). Při snímání
(`_capture(scan=True)`) se červený měřicí rámeček transformuje existujícím
`_ae_rect_in_sensor_px()` — přesně stejná konverze, jakou používá audit i film
base (overview: škála ×3; ROI: posun o origin) — a přes `capture_scan(crop_rect=…)`
přijde do sidecaru i katalogu (blob se round-tripuje celý). Developer GUI pak
ořízne podle `crop_rect`; `null` = bez ořezu, frame stojí jak pořízen.
Old sidecars bez klíče zůstávají čitelné (default None, `SCHEMA_VERSION`
nezvýšen — jen přidán nepovinný klíč). Napověď pod ovladačem i stavový řádek
po tažení říkají, že se rámeček ukládá jako ořez.

## 2026-09-18 (noc) — úklid UI + atributy orientace filmu

315 testů zelených (`.venv/bin/python -m pytest tests/ -q`). Pět bodů přímého
povelu; žádné změny kamery (hardwarově neověřováno — UI/model-only zásah).

### 1) Tlačítko „Gain 1.00× (archiv)" pryč

Manuální režim: gain se vrací na 1.00× ručně v gain spinu. **Archivní pravidlo
zůstává** — `_capture_block_reason` čte gain přímo z kamery a Capture při
gain ≠ 1,00× pořád odmítne (zmizil jen odkaz na tlačítko v hlášce, ta teď
říká „ vrať gain na 1.00× a exponuj jen časem").

### 2) Tlačítko „Auto Exposure" pryč

`_auto_exposure` / `_on_auto_exposure_done` / `_meter_source` z okna fuč.
**Motor `capture/autoexposure.py` zůstává** i s testy (uživatel: „nech ať se
rozbi") — `LiveMeter` a `fresh_live_frame` z něj používá Live View i měření
film base z proudu. Červený měřicí rámeček zůstává a dál omezuje histogram +
post-capture audit; přejmenován z „AE výřez" na „měřicí výřez / rámeček",
aby texty neodkazovaly na mrté tlačítko.

### 3) Přepínač náhledu: ze tří boxů dva

Byl problém: klik na **Negativ** odškrnul RAW View (správně) ale **zaškrnul
i Positive** — dva režimy naráz. Positive navíc nastavil `invert=False` a
přesto šel do stejné Working Positive větve, takže kreslil jinak než Negativ.
**Positive odstraněn úplně** (na žádost): zbyly dva stavové boxy
`RAW View` | `Negativ`, vzájemně výlučné, vždy právě jeden zaškrtnutý
(`_set_mode` synchronizuje oba přes `blockSignals`).

### 4) kratší časy

`SHUTTER_PRESETS` pokračují pod 1/10: `1/200, 1/100, 1/50, 1/25` před `0.1`.
Senzor zvládá od 300 µs. Volný vstup (`1/125`) fungoval i dřív.

### 5) Nový film: atributy orientace

Do `FilmMetadata` (a dialogu, skupina Digitalizační sestava) tři volně
kombinovatelné přepínače: **zrcadlit vodorovně / zrcadlit svisle / rotace
180°**. Zaznamenávají se, nepoužívají (postprodukce). **H+V JE rotace 180** —
proto všechny tři najednou model i dialog zakazují (vyrušily by se v
identitu); dva najednou zůstávají platné. Jsou to **nová jména polí**, ne
obnovení starého `mirrored` — to se pořád maže při načtení
(`RETIRED_FILM_FIELDS`), staré archivy zůstávají čitelné, `SCHEMA_VERSION`
se nemění. Orientace se nedědí z předchozího filmu přes rig (patří k
proužku, ne k sestavě).

## 2026-09-18 (večer) — hardwarové ověření obou „ne-testnuto" commitů

`76647ed` (manuální expozice) a `b43b55c` (histogram black + film base)
odtestovány na připojeném ATR2600M. Sondu drží `scripts/manual_filmbase_probe.py`
(15/15 PASS, 308 testů zelených).

### 1) Kamera **neoznačuje snímky expozicí** — fallback na settings je jediná cesta

`info.v3.expotime` je na reálné kameře **vždy 0**, ať live rámec nebo still.
Dvojice předchozích „ověřit na těle" se tak zavřela překvapivě: stamp proudu,
na který spoléhaly `fresh_live_frame` i měření film base z proudu, kamera
nedodává nikdy. Obě cesty padají na požadované `settings` — což je chování,
na které jsou napsané a na hardwaru fyzikálně sedí (signál roste 3,86× se
4× časem; expozice stillu u 60 s trvala 60 s). FakeHcam byl **spořádanější
než hardware**: stamp dodával. Nyní ve výchozím stavu napodobuje realitu
(expotime=0 → `LiveFrame.expotime_us=None`); stampování je opt-in pro test,
který forwardování drží pod krytím (`test_frame_carries_reported_expotime`,
nový `test_real_camera_stamp_is_none`).

### 2) CURVE na mono kameře neexistuje — poznámka „odmítnuto" mizí z každého snímku

ATR2600M odpovídá na `put_Option(CURVE)` E_INVALIDARG (0x80070057): mono tělo
nemá tónovou křivku, které by se dalo vypnout. RAW/BITDEPTH/LINEAR kontrakt
zůstává, CURVE přesunuto do `CURVE_OPTION` a ptá se jen u barevných těl.
Ověřeno na kameře: capture notes jsou nyní **prázdné** — dřív se „vestavěný
křivkový tone-mapping: odmítnuto" lepila na každý still i connect.

### 3) Odškrtáno na hardwaru (bez změny kódu)

- **Strop expozice:** firmware žádný pod 200 s nemá — `set_shutter(200)` se
  vrací 200 s a still při 60 s expozici dorazil po 61 s (ne po readoutu),
  tzn. expozice se skutečně/exponují celá. Prvotní „>50 s oříznuté" z prvního
  dne byla tehdejší hardwarová AE, ne strop; readback disciplína platí dál.
- **Kamera si expozici napříč spojeními nepamatuje:** po `set_gain(2.5)` +
  3 s a novém connectu vrátí kamera **0,1× / 10 ms**. GUI tomu čelí správně —
  `_refresh_settings` po connectu čte skutečný stav (tlačítko archivu se
  odškrtne) a Capture při gain ≠ 1,00× odmítne.
- **Stop při 8 s expozici:** `next_live_frame` se vrátil do 0,5 s — krácení
  pollu z `76647ed` na hardwaru sedí.
- **Obnova binningu po capture** se zastaveným proudem: další Live View je
  2074×1388, ne 26 Mpx — původní zmrazující bug doopravdy zavřen.
- **ROI proud:** 1200×1200 na [1000, 800] dorazil přesně 1:1 — geometrie pro
  převody AE/min-point rámečků platí i v ROI režimu.
- **`capture_base` end-to-end:** still → TIFF → sidecar se skutečným
  časem/gainem/teplotou → `film_base.json`; `region_mean` na archivu sedí se
  vzorkem do 0,1 DN a měření je v rozsahu (3712 DN @ 0,5 ms, bez filmu).
- **Kadence proudu:** readout floor ~0,29 s (3,4 fps); nad ním kadence
  sleduje shutter (0,40 s @ 0,4 s).

## 2026-09-18 — podexpozice modře + film base / min point (třetí kalibrace)

(307 testů zelených.)

### 1) Underexposure warning — zrcadlovka k over

Kde byl červený PŘEPAL, je teď modrý PODEXP: histogram kreslí modrou lajnu na
černé liště s `PODEXP` + %, clip readout hlásí `černá X %` modře (a bílá
červeně, barevně HTML), stavová řádka měření dostala modrou vlajku
`PODEXP` (`MeterReading.crushed`, zrcadlo `clipped`). Audit snímku hlásí u
`PODEXPOZICOVÁNO` i kolik procent měřené plochy leží na černi
(`AuditResult.black_fraction`). Nové `Histogram.crushing_warning` /
`clipped_low_fraction` / `clipped_high_fraction`.

### 2) Film base / min point — nové `FrameKind.BASE`, `core/filmbase.py`

Výslovně NE flat: flat se fotí bez filmu a dělí vinětaci/prach; min point se
měří **skrz držený film** na čirou základnu (obvykle jen část snímku — proto
vlastní modrý obdélník, repurposed red-rect drag: `ZoomView.set_rect_mode`
„ae"/„base", oba rámečky mohou koexistovat, pravé tlačítko ruší jen aktivní
režim). Třetí řádka v Kalibraci:

* **Režim min point** — přepne SHIFT-tažení na modrý rámeček (červený AE dál
  měří expozici).
* **Měřit z proudu** — průměr DN pod rámečkem z čerstvého Live View frame
  (žádná nová expozice); expozice frame (jeho vlastní `expotime_us` stamp) +
  gain + teplota se ukládají s hodnotou, aby šla přepočítat na jinak
  exponované snímky.
* **Snímek base** — plnohodnotná archivace (`kind: "base"`, TIFF + sidecar se
  záznamem expozice jako dark/flat) + měření rámečku na full-size datech.

Srážka souřadnic (taženo na 3×3 binnovaném proudu, měřeno na full-size) se
řeší stejně jako audit: GUI převede stream px na sensor px
(`_rect_in_sensor_px`) a měření probíhá 1:1. Výsledky: `film_base.json` ve
složce filmu (historie vzorků, append), `CaptureSession.capture_base()`,
`FilmBaseSample.scaled_above_black()` — škálování signálu poměrem
shutter/gain, pedestal se neškáluje (stejná disciplína jako u dark/flat).
**Zatím se nic neaplikuje na náhled** — jen měření a archiv; H-D/S-curve
parametrizace z naměřených min/max hodnot je příští úkol až budou data.

Návratové testy: `tests/test_filmbase.py` (region mean, škálování, JSON
historie), `test_base_capture_archives_and_measures`,
`TestFilmBaseMinPoint` (rect módy, měření z proudu i snímku, conversion),
`test_under_message_reports_crushed_percentage`, `test_crushing_is_the_mirror_flag`.

**Co nešlo bez těla ověřit:** že `expotime_us` stamp proudu sedí s nastaveným
časem i při běžícím Live View (mock ho nedává → cestou jsou settings); chování
modrého rámečku při přepnutí na ROI proud (clear rect po změně streamu platí
pro oba rámečky, ale na těle ověřit až s filmem v držáku).

## 2026-09-18 — zmrazení po dlouhé expozici: příčina nalezena a zavřena

Dlouhé trápení: po Auto Exposure + expozici v desítkách sekund se
**celá aplikace zmrazila** a pomohl jen kill. (283 testů zelených.)

### 1) Zmrazení — tři chyby najednou, všechny v `touptek.py`

- `capture()` vracel režim senzoru (binning/ROI) jen když Live View
  **běžel**. GUI ho ale před každým capture vypíná, takže se `_binning`
  navždy nechal v NO_BINNING → **každá další Live View streamovala
  26 Mpx senzor donekonečna**. Přesně „špatně se vypíná plný režim
  zobrazování" z uživatelova tipu.
- `next_live_frame()` čekal na jeden `queue.get(timeout=shutter+2 s)`,
  který **ignoroval Stop()** — starý poller závodil s dalším Snapem
  a zablokoval CameraWorker (žádná kamera akce se už nedostala na řadu).
  Nyní se čekání krájí po 0,25 s a kontroluje `_live_view`; po Stop()
  se vrací `None` do čtvrtiny sekundy.
- Výjimka ze `Stop()` uvnitř starého `finally` přeskočila obnovu režimu.
  Nové `_force_stream_off()` nikdy nevyhazuje; obnova režimu je
  bezpodmínečná.

Návratové testy: `test_capture_restores_binning_when_stream_was_stopped_first`,
`test_stopped_stream_returns_promptly_not_after_full_exposure`,
`test_capture_error_still_restores_binning`.

### 2) Záchrana bez killu: tlačítko „Restartovat proud"

I kdyby se proud zasekl, aplikace neumře: toolbar má **Restartovat proud**
a stejnou volbu nabízí chybová hláška Live View. Přepojí SDK na worker
vlákně (klidný `Close()` nezmrazí GUI) a `session.camera` se přesměruje —
rozpracovaný film přežije.

### 3) RAW View a Negative už se nikdy nemohou zapnout současně

Zaškrtnutí Negative ponechalo `filmic.invert` (zápojný invert náhledu)
v rozporu s režimem. `_set_mode` ho nyní vždy synchronizuje; dva
regresní testy (+ test, že selhání auditu nesmí zabít náhled).

### 4) Retirovaná metadata — WB, objektiv, clona, zrcadlo — PRYČ

Monokrystal IMX571 nemá white balance; rig má pevný manuální objektiv;
zrcadlo se opravuje v postprodukci. Z metadat i UI odešly:
`white_balance`, `raw_developer`, `lens`, `lens_serial`, `f_number`,
`focus_distance_m` (`AcquisitionMetadata`), `mirrored` (`FilmMetadata`
+ dialog + log), a **clona úplně všude** — i z `ExposureSettings`,
`CalibrationStack` a `CameraCapabilities` (vždy byla `None`; AE
matematika ji potřebovat nemohla). `digitising_lens` ZŮSTÁVÁ (přenáší
se mezi filmy). `ev100()` zemřel s `f_number`.

**Staré archivy zůstávají čitelné**: `SCHEMA_VERSION` se nezvyšuje —
místo migrace mode-before validator retirované klíče při načtení
**zahodí** (`_strip_retired`) a validace proběhne na čistých datech.
Skutečně překlepený klíč pořád padá nahlas. Ověřeno na reálném
archivu (sidecar i katalog se otevírají, `mirrored`/`f_number` zmizely).

### 5) „52 s → 50 s“ a tiché ořeznutí — konečně s hlasem

Auditový verdikt pro další snímek počítá `shutter · 2^EV` a firmware
kamery má **vlastní strop expozice** — dřív se oříznutí ztratilo
stmlky v combo boxu (scéna chtěla 52 s+, kamera dala 50 s, uživatel
viděl skok bez vysvětlení). `set_shutter` nyní čte přijatý čas
*zpět* (stejná disciplína jako u gainu) a stavový řádek pojmenuje
obě čísla: *„scéna chtěla 56.6, kamera umí max 50“*. NaN shutter už neprojde
`__post_init__` (kontrola `not > 0` místo `<= 0`) a round-trip
`shutter_string ↔ parse_shutter` drží nový test přes celou ladder
i pod 1 ms.

### 6) AE výřez mluví jednou řečí — pixely senzoru

Říkal „px proudu“, audit ale měřil v px senzoru — a nad ROI se naopak
vzdal celého rámu. GUI nyní převádí obdélník na **px senzoru** přesně
u obou režimů (overview ×3, ROI + offset) a audit dostává škálu 1:1:
**AE výřez nad posunutým ROI ahora měří to, co vidíš**. Stavový řádek
i nápověda pod tlačítky to říkají stejně. AE converged do přeepáleného
rámu už není ticho — `result.clipped` konečně oznamuje dialog.

## 2026-09-17 (večer) — první světlo: reálný ATR2600M

Checklist §11 instrukcí odškrtán na hardware. Tři kontrakty, které fake
hodoval špatně, přepsány podle měření (commit `1b49348`):

- **`get_Size` lže** — vždy vrací rozlišení senzoru, ať je BINNING nebo
  ROI jakkoli. Skutečná velikost rámu je v info záznamu každého rámu.
  Podle `get_Size` se overview 2074×1388 přepisoval jako devět full-size
  rámu nad sebou → **záhada „Mosaik“ v GUI je vyřešena**, hardware
  potvrzuje čisté 2074×1388 @ 3,4 fps a ROI 1200×1200 @ ~11,4 fps.
  (Pozor: binning zaokrouhluje sudě — 1388, ne 4168/3.)
- **buffery jsou `c_char_p`** — ndarray i `POINTER(c_ubyte)` odmítnuté na
  volání; `create_string_buffer` + `np.frombuffer` výběr rámu.
- **still nepřišel přes `WaitImageV4`** (vždy `E_UNEXPECTED`) —
  `capture()` nyní čeká na `TOUPCAM_EVENT_STILLIMAGE` a sahá pro
  `PullStillImageV2`. Full-size still 6224×4168: 2,6 s, TIFF round-trip
  ok, teplota senzoru zaznamenána.

Dodatečná měření: `get_ExpoAGainRange` = **0,1–10×** (mock i instrukce
opraveny; pro archiv zůstává 1,00× — 16bit ADC, <1× jen tłumí),
TEC projede **32 → −5 °C za ~3 min** (semafor ±2 °C se zavře zhruba za
tři minuty od nastavení cíle). FakeHcam teď napodobuje všechna tři
odchylky SDK od dokumentace, takže regrese padají i bez hardwaru
(263 testů zelených).

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
