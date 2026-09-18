# 01 — Fyzikální model: měřicí vrstva

Stav: **návrh, neimplementováno**. Vstupy i kalibrační matematiku předpokládáme tak, jak je
dnes dodává akvizice (ta se nemění — ROI s obrazem, tmavý okraj, dark/flat hlídány teplotou).

## 1. Účel a oddělení vrstev

```
MĚŘICÍ VRSTVA (objektivní, bez estetiky)          RENDERING VRSTVA (interpretace)
raw DN ──dark──► flat ──► T ──► D ──► ARCHIV  ──►  Dmin korekce ──► inverze ──►
     (fotonový signál)  (transmittance) (hustota)                    tónová mapa ──► pozitiv
```

- Negativ → hustota je **měření**; hustota → pozitiv je **interpretace** (vztahuje se na
  nástin z poznámkového bloku i na uživatelské rozhodnutí 2026-09-18).
- Archivní artefakt = **optická hustota D, float32 TIFF**. Pozitivní 16bit TIFF je odvozený
  rendering s fingerprintem parametrů; kdykoliv regenerovatelný z archivu.
- Rendering vrstva je samostatný dokument [03_rendering_vrstva.md](03_rendering_vrstva.md).

## 2. Řetězec měřicí vrstvy (přesné definice)

Vše v lineálních DN, float64, per-pixel. Označení: `S(x)` snímek, `Dk(x)` dark,
`F(x)` flat, vše po medianovém stacku více frameů.

### 2.1 Dark korekce (už implementováno, `core/calibration.py`)

```
S' = S − [pedestal + (Dk − pedestal) · (t_S / t_Dk)]
```

- Pedestal (zde 0 — Touptek/manual exposure mode) se **neškáluje**; škáluje se jen temový
  proud. To současný kód dělá správně (`rescale_dark`).
- **Nález z dat B1:** stack se počítá medianem přes darky s různými časy
  (B1: 1,000 ms + 4× 1,170 ms) — `stack()` je před median **neškáluje na společný čas**
  (`calibration.py:61-76`). U darku s ~260 DN/1,17 ms to není katastrofa, ale je to
  systematika řádově srovnatelná s šumem darku (spread p50 ≈ 20 DN). **Doporučení:**
  škálovat darky na referenční čas *před* stackem, nebo vyžadovat stejný čas.

### 2.2 Flat korekce (už implementováno)

```
F' = median(F_i) po subtrakci darku na čase darku, Gaussian blur σ=64
g  = F' / mean(F')            (unit-mean gain mapa; floor 0.05)
S'' = S' / g
```

Flat se darkuje **před** normalizací (pedestal nařídil by gain mapu) — `pipeline.py`
`_calibrate_above_black` to dělá. Na B1: gain map v osvětlené oblasti ±5–10 %,
v tmavém okraji klesá ke 0.001 (tam korekce záměrně neaplikujeme).

### 2.3 Transmittance

Klíčový rozdíl oproti dnešku: **nejmenovat T až po normalizaci na `white_level`.**
Referenční bílá pro transmittanci je *místní hodnota proudu bez filmu* (flat signál),
ne plný rozsah senzoru:

```
T(x) = S''(x) / F''(x)         kde F'' = flat signál naškálovaný na čas snímku
```

- T = 1 ⇒ místo, kudy prošel celý proud bez filmu (žádná emulze).
- T filmu je všude pod T film base; nikdy se nedotýká full-scale senzoru.
- Dnešní `_calibrate_array` dělí `white−black` (65535), čímž T vychází vztažené k saturaci
  senzoru — to je škála vhodná pro zobrazení, ne pro měření. **Nová měřicí vrstva používá
  jmenovatel z flatu per-pixel.** (Na B1: flat mean v osvětlené oblasti 50 353 DN @0,3 ms;
  plný rozsah 65 535 — rozdíl 1,3×, tedy ~0,11 D systematiky, kdybychom nechali jmenovatel
  podle white_level.)
- Exposure invariance: `S''` i `F''` jsou po darku čisté fotonové signály naškálované na
  čas snímku, poměr je na expozici nezávislý. Zisk (gain) škáluje oba stejně.

### 2.4 Hustota

```
D(x) = −log10( max(T(x), T_floor) )
```

- `T_floor` definovat per-frame z poměru šumu: tam, kde `S''` není statisticky nad darkem,
  je D jen šum. Prakticky: pixely s `T ≤ 0` (dark over-subtrakce) a pixely mimo osvětlenou
  oblast (gain < floor) dostanou **NaN** — float32 archiv to umí vyjádřit, uint16 by musel
  ořezávat. (To je jedna z hmotných výhod float32 archivu oproti uint16 T/D.)
- **Do archivu se Dmin odečítat NEBUDE.** Archivuje se surová absolutní hustota včetně
  base+fog. Dmin je metadata (viz §4), která se odečítají až při renderingu — díky tomu
  přebaseování (nové měření base, výměna lampy) nevyžaduje rescanning.

### 2.5 Proč float32 D (zdůvodnění volby, měřeno 2026-09-18)

Kvantizační chyba při archivaci v uint16 T, propagováno `σ_D = log10(e)·σ_T/T`:

| T | D | σ_D z 1 LSB uint16 T |
|---|---|---|
| 1.0 | 0.00 | 0.00001 |
| 0.1 | 1.00 | 0.00007 |
| 0.01 | 2.00 | 0.00066 |
| 0.004 | 2.40 | 0.00166 |
| 0.001 | 3.00 | 0.00663 |

Kvantizace uint16 T by *statisticky* stačila (vše pod šumem skeneru). Float32 D se přesto
volí pro **významové** důvody:

1. **Záporné čisté hustoty** — po Dmin korekci existují hodnoty D < Dmin (viz anomálie B1,
   §5); float je zachová, pevný formát by je ořízl nebo vyžadoval offset.
2. **NaN maska** pro nekorigovatelné pixely místo ořezávání.
3. **H-D křivka je lineární v D** — float má v celém rozsahu relativní přesnost 1e-7,
   žádné rozmytí volby měřítka/faktoru; faktor (scale) je zátěž pro reprodukovatelnost.
4. Formát je sobě popsaný (viz §4), ztráta čitelnosti oproti uint16 žádná —
   `tifffile` umí obojí, `docs` v headeru.

Velikost: 6224×4168 float32 ≈ 104 MB/frame (neztraceně; deflate ~2×). U 6 frameů/trh zanedbatelné.

## 3. Chybová analýza (co limituje přesnost)

`σ_D = log10(e) · σ_S''/S''` — hustotová chyba je dána **relativním** šumem signálu:

| zdroj | velikost na B1 | dopad na D |
|---|---|---|
| sčítací šum (shot) | `σ_D ≈ 0.434/√N_fotonů(DN)`; při D=2 (S''≈2 240 DN @1,33 ms): 0,009 | dominuje v D≳2 |
| temný proud / jeho reskalování | dark ≈260 DN@1,17 ms; mismatch časů dark stacku ~17 % ⇒ ~44 DN systematika | při D=2: 0,009; při D=3 (S''≈224 DN): **0,085 — dominuje** |
| quantizace 12/16bit | zanedbatelná (tabulka §2.5) | — |
| flat nehomogenita po korekci | ±pár % zbytky (blur σ=64 nezachytí prach) | lokální, řádově 0,002–0,01 |
| stabilita lampy (drift mezi flat/base/scan) | B1: ±1,6 % srovnání base měření (viz §5) | ~0,007 D |

Závěr pro návrh: **největní páka na přesnost v D>2,5 není formát archivu, ale (a) stejné
časy darků a (b) blízkost dark/flat/base/scan měření v čase** (teplota hlídána, lampa
se zahřívá). Měřicí vrstva to má podpořit: archiv uloží, ze kterých souborů/časů kalibrace
byla (provenance, §4) — render pak jde zopakovat s jinou kalibrací a porovnat.

## 4. Formát hustotního archivu

Nový typ souboru vedle raw TIFFu (raw archivy se nemění):

- **data:** float32, single-plane mono, `tifffile.imwrite(..., description=JSON)`
  — stejný self-describing vzor jako `core/rawio.py` (magic `filmscan-density`,
  `schema_version`).
- **hodnoty:** absolutní D (bez Dmin korekce); NaN mimo osvětlenou oblast;
  pixely dotčené saturací raw snímku (DN ≥ white_level) taktéž **NaN** (T je
  tam only dolní odhad — nelže se do archivu, render je ukáže jako masku).
- **JSON header (minimum):**
  `black/white_level zdroje`, `shutter`, `gain`, `teplota`, identifikace zdroje;
  `calibration`: cesty + sha256 dark stacku, flat stacku (včetně σ blur), `T_floor`;
  `filmbase`: odkaz na vzorec z `film_base.json` (index/čas) použitý jako Dmin metadata
  (pouze informativně — do pixelů nezasáhl);
  `pipeline_version` + `fingerprint` (stejný styl jako `DeveloperParams.fingerprint()`).
- **sidecar** `*.density.json` volitelně totéž lidštěji; primární zdroj je header uvnitř.
- Nový `FrameKind`? **Ne** — archiv není capture frame; jde o odvozený soubor
  (jako dnešní exporty). Katalog ho eviduje jako artefak develop fáze.

## 5. Validace na datech B1 (změřeno 2026-09-18, `~/scans/B1`)

Postup: dark stack (5), flat stack σ64, per-pixel jmenovatel z flatu, D = −log10 T.

- **Reprodukovatelnost Dmin (opraveno při implementaci, 2026-09-18):** prvních pět
  měření base z `film_base.json` dá po správném poměru signál/flat **Dmin = 0,427 až
  0,432** (±0,005 D) — výborný pevný bod pro tento film. Původní hodnoty 0,516/0,523
  byly chyba mé analytické sondy (dělení gain-correctované base raw flatem; flat-field
  korekce patří do T, ne do poměru base/flat — vignetting se v malém rectu vykrátí).
  Implementace v `core/density.py` počítá obě veličiny stejně; test
  `TestB1FixedPoints::test_dmin_reproducible` to drží.
- **D rozsah skenů** (jen osvětlená oblast): f001 p1/p50/p99 = 0,36/1,35/2,48;
  f002 0,30/1,70/2,03; f005 0,21/1,60/2,48; f006 0,21/1,60/3,35.
- **Anomálie, které návrh musí ošetřit (ne zamést):**
  1. `frame004` (0,303 ms) má D ≈ 0,005 všude — ve snímku není film (nebo prázdný holder).
     Pipeline má detekovat „žádný film“ (median D < D_min měřený − rezerva) a varovat,
     ne z toho renderovat bílý pozitiv.
  2. Skeny f001/f005/f006 mají v masce i D < měřeného D_base (až 0,21): buď maska
     (plocha `flat > 60 %`) sahá i tam, kde ve skenu není film (mezizubová okénka,
     převislá hrana), nebo lampa mezi měřeními (19:16 base vs 19:19 flat — B1 export
     navíc hlásí `scans_without_temperature_matched_dark: [4, 5]`) mírně plynula.
     → Měřicí vrstva musí **umožnit D < Dmin** (záporná čistá hustota), render ořízne
     až na hraně interpretace, a archiv má povinné provenance, aby se dal anomálie
     dohledat (teplota, čas, soubory).
  3. `frame003` (25 ms): 12 % pixelů s T ≤ 0 v masce, p99 D ≥ 9 — deep shadows jsou
     pod úrovní dark residualu. Přesně případ, kdy NaN-maska a honesty archivu
     (žádné klamné D) odůvodňují float32 design.
- Saturace raw (DN ≥ 65535): 0,03–0,71 % pixelů na f001–f006 → v archivu NaN.

## 6. Co se mění oproti současnému kódu (shrnutí pro plan)

| dnes (`pipeline.py`) | návrh |
|---|---|
| jmenovatel `white−black` (full-scale) | jmenovatel per-pixel flat načasovaný na snímek |
| `subtract_base` v lineární doméně (inverze ~ `1−T`) | D = −log10 T; inverze = posun/zobrazení v hustotách |
| base z percentile (`estimate_base`) | base z `FilmBaseSample` (měřeno, škálováno `scaled_above_black`); percentile jen fallback bez měření |
| archiv = 16bit pozitiv s vypálenou křivkou | archiv = float32 D; pozitiv = render (viz 03) |
| dark stack bez časového sjednocení | sjednotit časy před medianem (nebo vyžadovat stejné) |

Preview (`FastPositivePreview`) zůstává v lineární doméně — je to náhled, ne měření;
musí se ale shodovat na kalibračním úseku (dark/flat), jinak by náhled lhal o expozici.
