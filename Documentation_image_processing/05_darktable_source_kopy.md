# 05 — Kopie relevantních zdrojových souborů z darktable

Překopírováno z lokálního checkoutu `local darktable checkout (github.com/darktable-org/darktable)`
(verze `nightly-4-g2c42f30809`, naposledy změněno v commitu `06e85df4` z 2026-08-06)
dne 2026-09-18. Účel: fixovat zdrojovou pravdu, ze které vychází
[02_rozbor_negadoctor.md](02_rozbor_negadoctor.md) a tento dokument, proti budoucímu
posunu upstreamu.

## Adresář `darktable_source/`

| Soubor | Řádků | Proč je tady |
|---|---|---|
| `negadoctor.c` | 1078 | kompletní zdrojový kód modulu — dokumentace (l. 40–53), parametry (l. 70–91), jádro `_process_pixel` (l. 268–322), auto-kalibrace ze 2 bodů (l. 635–804), presety (l. 392–423) |
| `negadoctor.cl` | 53 | OpenCL kernel — nejkompaktnější zápis celé rovnice na jednom místě (l. 32–52) |
| `spektrafilm.c` | 5429 | dokument „druhé půlky otázky": kde v darktable *jsou* film-specific profily a proč je-nejsou součástí negadoctoru |
| `darktable_LICENSE` | — | GPL v3 — viz oddíl Licence níže |

Původní cesty v upstream repozitáři: `src/iop/negadoctor.c`,
`data/kernels/negadoctor.cl`, `src/iop/spektrafilm.c`.

## Shrnutí z analýzy (detail v 02)

### Monokanál redukce negadoctoru

Pro ČB negativ s monochrom kamerou se celý modul redukuje na (pseudocode,
odvozeno z `_process_pixel`, RGB gymnastika je identita):

```
τ   = t / Dmin                          # relativní propustnost vs. báze
P   = exposure · (1 + black − τ^(1/D_max))   # „tisk na papír", clamp ≥ 0
out = softclip(P^gamma, soft_clip)      # grade papíru + Cineon rolloff
```

Pracuje v **doméně hustot** (`−log10(Dmin/t)`) — přirozený domov H-D křivky,
kde je lineární část lineární v log expozici. (Lineární `subtract_base`
v našem `positive.py` je sensitometricky jen aproximace.)

### Mapa parametrů na naše veličiny

| negadoctor | význam | náš protějšek |
|---|---|---|
| `Dmin` | hustotní páka = barva/úroveň báze (pro NB forcovaná monochromatická, l. 253–254) | měřený min point / `film_base.json` |
| `D_max` | max densita filmu; auto = `log10(Dmin / nejtmavší pixel)` (l. 655–676) | „max z fotky" — po dark+flat je tmavý pixel poctivá transmise |
| `offset` | scan exposure bias v dB — aditivní posun v log prostoru | expozice **před** křivkou; v log doméně neruší páku báze |
| `black` | paper black — jak černá je báze po inversi (auto odvozen z pick pointů, l. 756–778) | náš „histogram black" |
| `gamma` | grade papíru; komentář l. 986: „use a high grade for high D max" | kontrast odvozený z D_max, ne volný parametr |
| `exposure` | expoziční multiplikátor tisku (GUI v EV, l. 1037–1040) | 1:1 |
| `soft_clip` | Cineon exponenciální rolloff nad prahem (l. 317–321) | alternativní shoulder k našemu splineu, monotónní bez Fritsch-Carlsonu |
| `THRESHOLD` | clamp `max(t, 2.33e-10)` = −32 EV před log (l. 56) | nutná hygiena pro jakoukoli log doménu |

### Auto-kalibrace (l. 635–804) — recept pro naše naměřená data

Pořadí `Dmin → D_max → offset → WB_low → WB_high → black → exposure` je kompletní
dvoubodová kalibrace: z báze + nejtmavšího pixelu dopočítá vše tak, aby výstup
kryl [0; 0.96] bez ořezů. Přesně scénář, až doběhne měření min pointů
(plán `filmbase-hd-plan`): parametry H-D z dat, ne odhadem.

Otázka per-frame vs. globální D_max: model vstupuje jen jako jmenovatel exponentu
`τ^(1/D_max)`, takže globální (roll-min) hodnota je legitimní a stabilnější;
per-frame by znamenala dýchající kontrast mezi snímky.

### Žádné film-specific křivky v negadoctoru — ověřeno

- V kódu nejsou žádné profily filmů. Jediné „výchozí hodnoty" jsou dva **generické**
  presety (`init_presets`, l. 392–423): color a BW — a BW preset
  (`D_max=2.2, gamma=5.0, Dmin=1.0`) není profil konkrétní emulze, jen sada čísel.
- Záměr je explicitní v hlavičce (l. 40–53): Cineon/Kodak densitometrie jako
  **fyzikální model** (reference: Kodak Sensitometry Workbook, Cineon spec,
  openexr-devel). Konkrétní film = tři naměřená čísla, ne název v dropdownu.

### Kde film-specific profily v darktable *jsou*: `spektrafilm`

- Nový modul (`src/iop/spektrafilm.c`), engine portu spektrafilm
  (<https://github.com/andreavolpato/spektrafilm>, GPL v3, data CC BY-SA 4.0).
- Načítá **profily filmů a papírů** z externího datového packu
  (repo `darktable-org/darktable-spektrafilm`: `pack.json`, `spectra_lut.f32`,
  `profiles/*.json`) — spektrální citlivosti, DIR coupler, zrnitost, halaci,
  simulaci enlargeru i papíru. Existuje i B&W model
  (`channel_model == "bw"`, l. 352/901; `sf_sim_film_bw()`, l. 1805 — achromatické zrno).
- Je to **kreativní emulace** (scene-to-display view transform, nahrazuje
  filmic/agx): simuluje film, který jsi nenafotil. Pro digitalizaci reálného
  negativu, kde známe bázi, D_max i expozici měřením, je emulace cizí
  charakteristické křivky kontraproduktivní; na mono vstupu z veškeré spektrální
  mašinérie zbyde 1-D D-logE křivka — a tu máme měřenou lépe než kterýkoli pack.

### Závěr pro filmscan-studio

- Model „obecná H-D parametrizace + vlastní měření" je správně — struktura
  darktable to potvrzuje: korekční modul (negadoctor) záměrně **bez** databáze
  filmů, kreativní emulace (spektrafilm) **s** databází, odděleně.
- Film-specific věc, kterou stojí za to později zvážit, není LUT, ale
  **ukládat naměřenou trojici (Dmin, D_max, gamma) per film stock**
  do `film_base.json` — předání parametrů při dalším skenu stejné emulze.
- Z negadoctoru dále přímo přenositelné: 1-D LUT kompatibilita celé mono
  rovnice (sedí do `FastPositivePreview`), Dmin jako per-pixel mapa díky
  flat-frame (což negadoctor neumí — jeho Dmin je skalár), a WARNING v l. 207–209
  (gcc AVX alignment při kopírování polí) jako připomínka, proč přepisovat
  do NumPy, ne překládat cizí C.

## Co z negadoctoru **nebrat** (severné zkazy)

- `wb_low` / `wb_high` (dvoustupňový WB stíny/paprsek), barevný Dmin picker,
  oranžová maska — vše pro barevný negativ. Na IMX571 mrtvá zátěž a dle
  pravidla projektu (WB retired) se nesmí vracet ani jako idea do UI.
  Pro NB film se ostatně modul sám redukuje na monochromatický Dmin.
- GUI / GTK pickers / OpenCL část — nezajímá (vlastní Python developer).

## Licence

Kopované soubory jsou GPL **v3** darktable (soubor `darktable_LICENSE`).
Kopie slouží jako **interní dokumentace/reference** v této vývojové repozitáři —
nejsou začleněny do kódu `filmscan-studio` (matematika se přepisuje vlastním
NumPy/Pythonem, nikoli kopirováním), takže status derivative work nenastává.
Pokud by se kdy část kódu (i adaptovaná) stala součástí distribuce
filmscan-studio, aplikace by podléhala GPL v3 — to je důvod, proč zůstáváme
u „přečti princip, přepiš si sám".
