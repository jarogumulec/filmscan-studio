# 04 — Plán implementace (pro developera)

**Zatím jen plán — žádný kód.** Odhad řádků je hrubý. Každá fáze = samostatný blok
s testy (`pytest tests/ -q`), v souladu s pravidly projektu (systematičnost, ověření
každého bodu, žádný commit bez výzvy).

## Fáze 0 — rozhodovací předpoklady (žádný kód)

- [ ] **Dmax strategie:** per-frame p99.9 vs globální minimum role (memory filmbase-hd-plan).
      Rozhodnutí později, API nese obojí.
- [ ] Souhlas s novým archivním formátem (float32 D TIFF, `filmscan-density` magic) —
     přibývá typ souboru do katalogu/exportu, ne do akvizice.
- [ ] Potvrdit, že preview zůstává lineální (rychlost Live View) — mění se jen reference.

## Fáze 1 — měřicí jádro (`core/density.py`, nový modul, ~200 ř. + testy)

Funkce (čisté, float64, per-pixel — žádné GUI):

1. `transmittance(scan_sig, flat_sig_at_scan_shutter, gain_floor) -> T`
   — jmenovatel per-pixel flat (ne white_level); T>0 mask.
2. `density(T, t_floor) -> D float32` — `−log10`, **NaN** pro T≤0, mimo osvětlenou
   oblast a pro saturaci rawu.
3. `dmin_from_sample(sample: FilmBaseSample, flat_sig, scan_shutter) -> float`
   — přes `scaled_above_black` → T_base → D (validováno na B1: 0,516/0,523).
4. `dmax_from_density(D, percentile=99.9) -> float` (strategie dle Fáze 0).
5. `write_density_tiff(path, D, provenance)` / `read_density_tiff` — formát dle 01 §4,
   v duchu `rawio.write_frame` (JSON v description, verze, fingerprint).

Testy: syntetické (known T → known D), B1 pevné body (D_base ±0,01, D_med f002 ≈ 1,70),
NaN masky (tma, saturace, tmavý okraj), exposure invariance (stejný signál při 2× shutter).

## Fáze 2 — dark stack na společný čas (`core/calibration.py`, ~30 ř. + testy)

- `stack()` reskaluje darky (above-black) na referenční čas před medianem — fix nalezený
  na B1 (1,000 vs 1,17 ms). Zpětná kompatibilita: stávající volání se stejnými časy
  se chová identicky.

## Fáze 3 — render přepne doménu (`core/positive.py` + `developer/pipeline.py`, ~150 ř. úprav)

- Nová cesta `density → render` dle dok. 03 (sdílená s preview na kalibračním úseku).
- `PositiveParams`: přibude `dmin/dmax` source; `base_level` (lineární percentile)
  zůstává jen jako fallback preview bez měření.
- Export: `write_render_tiff` (vzhledový, křivka baked) + `write_flat_render_tiff`
  (pro Capture One, bez S-ky) — 03 §5.
- `DeveloperParams.fingerprint()` rozšířit o nová pole (SCHEMA/export verze, ne TIF header).
- **Zachovat:** dnešní `develop()` jako „legacy preview" dokud preview plně nepřepne;
  žádný z retirovaných fieldů se nevrací.

## Fáze 4 — H-D registratur (rozhraní, ~120 ř. + testy)

- `core/hdcurves.py`: načítání/validace `curves/hd/*.json` (schema `hd-curve-v1`, 03 §4),
  monotone interpolace (přes `_monotone_interp` z filmic.py), aplikace jako LUT.
- Prázdný adresář = dnes známé chování (režim A). **Žádná emulze se zatím nedodává.**
- `RenderParams.curve_model: "spline" | "print"` — print model = negadoctorova exponentu
  + soft-clip (02 §6), vlastní testy monotónnosti a mezních hodnot.

## Fáze 5 — katalog/UI dotyk (minimální, ~80 ř.)

- Artefakty developu (density archiv, rendery) v `catalog.py` jako odvozené soubory
  (nový `ArtifactKind`, **ne** `FrameKind` — akvizice se nemění).
- UI: česky, bez soft-hyphenu (`grep -rn $'\xc2\xad'` kontrola); žádná nová metadata
  z retirovaného seznamu.

## Vrstvy testování kvality (chybová analýza 01 §3)

1. **Regression proti B1:** fixní expectace D_base/D_med/D_p99 s tolerancemi; data
   nejsou v repu — test skipuje bez `local scan archive/B1` (nebo fixtury generovat).
2. **Detektor anomálií** (B1 nálezy): „žádný film" (median D < Dmin−0,05),
   hluboké stíny pod dark residualem (>x % NaN), záporné čisté hustoty nad šum —
   hlásí, neořezává tiše.
3. Vlastnosti: asociativita/reskalování expozice, idempotence render fingerprintu,
   monotónnost všech krivek náhodnými parametry (hypothesis-style).

## Otevřené otázky (vyžadují uživatele/data)

| # | otázka | odkud |
|---|---|---|
| Q1 | Dmax per-frame vs per-film | filmbase-hd-plan, B1: f001 2,48 vs f006 3,35 (různý obsah!) |
| Q2 | Půjde density archiv sdílet s darktable (negadoctor neumí číst float D — render zůstane náš; darktable jen jako srovnávací nástroj?) | 02 §4 |
| Q3 | Kalibrační terč pro režim C: vlastní expoziční řada na riggu (rotace expozic na jedné masce), až příště | 03 §4 C |
| Q4 | Zdroj tiskové estetiky pro `print` model: Cineon konstanty (0.45–2.19) nebo naladit na BW papír? | 02 §1 |
