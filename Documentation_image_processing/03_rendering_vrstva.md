# 03 — Rendering vrstva: z hustoty na pozitiv

Stav: **návrh**. Vstup = archivní float32 D mapa (dok. 01). Výstup = display-referred
pozitiv (16bit TIFF render, JPEG náhled, Live View). Rendering je čistě odvozený operátor
nad archivem — nikdy zpětně ovlivňuje měření.

## 1. Rozhraní (kontrakt)

```
render(D_archiv, Dmin, Dmax, RenderParams) -> image 0..1
```

- `D_archiv` float32 mapa (absolutní D, NaN = neplatný pixel),
- `Dmin` = čistá hustota base+fog `[D]` z `FilmBaseSample` (přepočet na expozici snímku
  přes `scaled_above_black` → T_base → `Dmin = −log10 T_base`), viz 01 §5 — ověřeno ±0,007,
- `Dmax` = čistá hustota nejtmavšího bodu. **Otevřená otázka (z memory filmbase-hd-plan):**
  per-frame (p99.9 z ROI) vs globální minimum pro celou roli — render API nese obojí
  (`dmax_source: "frame" | "film"`), rozhodnutí až po naměření rolí,
- `RenderParams` = níže; fingerprintuje se (stejný vzor jako `DeveloperParams`).

Render je **úplně deterministický**: `(D_archiv, parametre including Dmin/Dmax zdroje)` ⇒
stejné bajty. Sidecar renderu uloží fingerprint archivu i parametrů.

## 2. Parametry (požadavek nástinu: exposure, contrast, gamma, toe, shoulder, black, white)

Všechny definované **v hustotní doméně**, ne v display 0..1:

| parametr | význam | doména |
|---|---|---|
| `exposure_ev` | posun expozice v stopách: `D_eff = D − Dmin + 0.301·ev` (oprava dle kódu, dok. 07 §6). Kladný ev zjasňuje pozitiv: v hustotní definici pozitivního renderu roste jas pozitivu **s hustotou** (vyšší D = světlejší scéna na negativu), a `+ev` posune každý pixel o `+log10(2)` D výš po téže ose. Není to totéž znaménko jako „scan exposure bias" v negadoctoru — ten sedí na lineární transmitanční ose před invertou, kde víc světla znamená větší transmisi; zde je inverze už zabudovaná v definici křivky. Uživatelský kontrakt „+EV = světlejší pozitiv" platí v obou modelech. | `[D]` |
| `black_point` | D, které se mapuje na papírovou černou (0). Default: `Dmin + film_Dmax` z kalibrace | `[D]` |
| `white_point` | D, které se mapuje na papírovou bílou (Dmin, tj. po Dmin korekci 0). Default: `Dmin` | `[D]` |
| `contrast` (grade γ) | strmost střední lineární části **v D** — přímočařejší než negadoctor: `slope = d(out)/d(D)` na středové hladině | bezrozměrné |
| `toe` | komprese nízkých D (stíny pozitivu = řídká místa negativu), 0..1 | bezrozměrné |
| `shoulder` | komprese vysokých D, 0..1, navazuje na soft-clip | bezrozměrné |
| `gamma_display` | koncový display transfer (2.2 / cílový profil) — čistě display záležitost, mimo tónovou mapu | — |

Tím je splněn požadavek „parametrizovatelný a vhodný pro další úpravy v Capture One":
všechny páčky mají význam v `[D]`, tedy fyzikálně čitelné („toe začíná ve 0,3 D nad base").

## 3. Tónová mapa — struktura

```
D_eff   = D − Dmin + 0.301·exposure_ev + shadow_band   # čistá hustota + pás pod base
x       = D_eff / (span + shadow_band)   # normalizace do 0..1; base na band/(span+band)
y       = spline_toe_shoulder(x; toe, gamma, shoulder) # monotone Hermite (filmic.py)
out     = y                                        # lineární pozitiv 0..1
display = transfer(out, gamma_display)             # gamma 2,2 dána ICC profilem
```

- **Stávající `FilmicProfile` (monotone cubic Hermite, Fritsch–Carlson) zůstává páteří** —
  už je otestovaný, monotónní, s nezávislými toe/gamma/shoulder. Mění jen doménu vstupu:
  dnes „linear 0..1 po invertu", nově normalizované čisté hustoty.
- **Nově volitelný exponenciální režim** (z negadoctoru, dok. 02 §6):
  `y = (1 − 10^(−D_eff/D_scale))^γ` s `soft_clip` gloss — pro uživatele, kteří chtějí
  Cineon/papírovou estetiku. Obě mapy vedle sebe jako `curve_model: "spline" | "print"`.
- Inverze je implicitní v definici čistých hustot (vyšší D = tmavší pozitiv), žádný
  separátní invert krok — oproti dnešnímu `subtract_base` mizí dvojí škálování.

## 4. Režimy tónového mapování (rozhodnutí: A teď, B/C rozhraní)

### Režim A — univerzální (first release)

Parametrická mapa §3. Auto-návrh hodnot: `white_point = Dmin`, `black_point = D_p99.9`,
`contrast` z rozptylu histogramu D, toe/shoulder defaulty dnešního `FilmicProfile`.

### Režim B — emulze emulzí (rozhraní, naplnit po Decision)

```json
// curves/hd/fomapan100-r09-20c.json
{ "schema": "hd-curve-v1", "film": "Fomapan 100 Classic",
  "developer": "R 09 1:50 20C", "source": "manufacturer datasheet, graph-digitized",
  "units": "log10( Relative exposure ) vs net D",
  "points": [[-3.0, 0.02], [-2.0, 0.09], ..., [1.0, 2.9]],
  "dmin": 0.23, "gamma_avg": 0.58 }
```

- Načítací registr: adresář `curves/hd/*.json`, načtení bez zásahu do jádra
  (požadavek nátinu). Křivka je monotonní LUT (interpolační vrstva shodná s profilem).
- Aplikace: `D_eff` se mapuje *na* předlohu H-D (hledání expozice, která by dala danou D)
  → výsledná expozice se pak tiskne stejnou display mapou.
- Data pro Fomapan/HP5+/Tri-X: **úkol až po rozhodnutí** (digitalizace grafů z datasheetů;
  pozor, datasheet H-D je při standardním vyvolání — push/pull se škáluje přes
  `pushed_stops` v `FilmMetadata`, což je odhad, ne emulze).

### Režim C — vlastní měření (rozhraní)

Stejný formát jako B, `source: "measured"`, navíc provenance (terč, expoziční řada,
densitometr). Formát definovat teď, sběr dat až příště — rig už umí kvantitativní D,
kalibrační expozice by šla udělat stupnicí neutrálních filtrů / clonou objektivu
(pozn.: clona je z UI vyloučena, ale pro kalibrační měření mimo produkt může posloužit
jako fyzická pomůcka — nerozširuje se tím do produkčních metadat).

## 5. Output pro externí úpravy (Capture One)

Kromě vzhledového renderu definovat **flat render**:
`invert + Dmin korekce + normalizace black/white point` bez S-ky, float/16bit,
lineární nebo s volitelným profilem. Úpravy pak začínají od netrénovaného pozitivu.
(Na rozdíl od dnešku, kde 16bit export má křivku „vypálenou" — `write_tiff` to dělá
správně pro vzhledový render; flat render je nová cesta.)

## 6. Shoda náhled ↔ export

- `FastPositivePreview` (Live View) zůstává LUT-based, ale LUT se začne stavět nad
  stejnou definicí čistých hustot (posun o Dmin, normalizace black/white) — jediná
  věc, která se v náhledu aproximuje, je percentile auto-base (dnes) →
  ve výsledku škálovaný `FilmBaseSample` (uživatelsky daný) nebo per-frame fallback.
- Kalibrační úsek (dark/flat, per-pixel flat jmenovatel) musí být **táž implementace**
  jako u archivu, jinak náhled lhal o expozici (princip positive.py: sdílená cesta).

## 7. Co se nemění / zakazy (kontrola proti pravidlům projektu)

- Žádný WB, clona, mirrored, lens/focus metadata — render vrstva s nimi nepracuje.
- H-D plná parametrizace po složkách je vědomě odložená (memory filmbase-hd-plan):
  tento dokument ji *připravuje rozhraním* (formát křivek, Dmin/Dmax metadata,
  per-frame vs film Dmax přepínač), ale nedefinuje fit tvaru — ten až po datech.
- UI texty česky, bez soft-hyphenu.
