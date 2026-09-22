# 09 — Anotace snímků a EXIF kontrakt (filmscan-annotate)

Stav: **DODÁNO 2026-09-21, EXIF export DODĚLÁN 2026-09-22, přepracován
týž den dle provozních připomínek** — anotátor běží
(`filmscan-annotate`), zápis do sidecarů additivní, otestováno na reálném
projektu K16O04 (41 snímků, diff proti originálu: změněny jen bloky
`annotation` / `acquisition` / `film`, ostatní soubory byte shodné).
Developer při exportu JPEG/HEIC vkládá EXIF+XMP (`core/exportmeta.py`,
rozkaz uživatele 2026-09-22: „aby filmscan-develop-gui ta metadata vložil do
exifu… chci tam ta data všechna mít"). Druhý rozkaz tentýž den
(„do ImageDescription dej to, cos dal do usercomment; název patří do Title;
foťák neděl dle mezery — do anotátoru dej make a model; orientation je ok,
jen implementuj a ť exportovaný soubor je takto otočen developerem") je
promítnutý níže — konečné přiřazení tagů je tabulka „Finální kontrakt".

## Proč mezi studiem a developerem

Studio změří *jak* byl negativ nafocen (čas expozice, gain, serial). Co je na
fotce — název, popis, kdy a kde byl snímek pořízen **na filmu**, GPS, hodnocení
— to studio nemůže vědět. To dopisuje operátor později do `*.tif.json` sidecaru
pomocí `filmscan-annotate`. Developer při exportu JPEG tato data promítne do
EXIFu, aby fotky v libovolném prohlížeči seděly na správném místě časové osy.

Aby se strojová progrese a lidský popis nenavzájem nešpinily, je anotace
**samostatný blok** `annotation` v sidecaru (ne v `acquisition`): re-capture
přepíše akvizici a anotaci nechá o samotě.

## Formát: blok `annotation` ve sidecaru

Přidán do `CaptureRecord.annotation` (`core/models.py`, pydantic
`FrameAnnotation`, `extra="forbid"`). `SCHEMA_VERSION` se **nezvyšuje** —
stejný additivní vzor jako `crop_rect` (dok. 06): kdo pole nezná, ignoruje ho;
staré projekty mají `annotation = None`.

| pole | význam |
|---|---|
| `title` | název snímku |
| `note` | popis / komentář (first kus popisu v EXIF `ImageDescription` + XMP `dc:description`, viz kontrakt) |
| `tags[]` | štítky |
| `rating` | 0–5, `None` = nehodnoceno |
| `capture_datetime` | **EXIF přísně `YYYY:MM:DD HH:MM:SS`** — důvod, kvůli kterému to celé je |
| `gps_input` | surový vstup operátora („49.1124306N, 9.7371244E") pro přečtení zpět |
| `gps_lat` / `gps_lon` | desítkové stupně jako string (beztvarý round-trip, žádná ztráta přesnosti) |
| `gps_lat_ref` / `gps_lon_ref` | N/S, E/W |
| `gps_lat_exif_dms` / `gps_lon_exif_dms` | DMS helper stringy — připravené pro pozdější zápis GPS IFD |
| `rotation_degrees` | násobek 90°, uloženo %360; otočení **jen náhledu/exportu**, archiv hustot se netočí (stejná filozofie jako flipy filmu) |

Prázdný `capture_datetime` = neznámé datum. Neznámý den → zapisuje se
`1968:01:01 00:00:00` (rozkaz uživatele: i když nevím den, píšu 1.1. daného
roku, aby software fotky zařadil). Parser `annotator/store.py` umí české tvary
(d. m. r., tečka/i, rok sám o sobě, čas s tečkou i dvojtečkou, „49.11 N
14.2 E").

## Sekvenční minuty (řád v rámci dne)

Zadám-li datum **bez času** a dám „Použít na vybrané", snímky se nesmí v
prohlížeči míchat. `apply_common` proto číslovaný čas: první vybraný snímek
`00:01:00`, druhý `00:02:00`, … (po minutách, od půlnoci). Počítadlo žije v
rámci jednoho volání — **druhý „Použít na vybrané" s jiným datem začne znovu
od 00:01**, takže film se dvěma daty má dvě čistě oddělené série.

Explicitní čas (`20.7.2026 14:30`) zůstává přesný, nic se nečísluje. Příznak
`_sequential_time` je interní (z patch se před zápisem vybere), do sidecaru
se nedostane.

## Editovatelná akvizice + filmový panel

Pořadí panelů (povel 2026-09-22): **Snímek → Záběr (EXIF) → Digitalizace →
Film.**

- **Záběr** (`camera_make` + `camera_model`, `shooting_lens`, `film_iso` z
  filmového bloku) sedí nad akvizicí, protože k datu a geu snímku myslně
  patří: foťák co exponoval film — od rozkazu 2026-09-22 **dvě pole Výrobce
  / Model** („ERNST LEITZ WETZLAR GMBH" | „Leica R4s MOD.2"; vkládá se
  RUČNĚ, není to digitalizační kamera), objektiv a ISO filmu (textové pole
  `film_iso`, např. „100"). Ukládá se přes `save_film` do všech sidecarů;
  volné `camera` se synchronizuje na souhrn „make model". Popisky UI jsou
  česky, **data do EXIFu a metadata zůstávají anglická/machine-neutral**
  (rozhodnutí uživatele).
- **Digitalizace** (`ACQUISITION_FIELDS`: camera, camera_serial, exposure_time,
  gain, capture_date, copy_number) se v anotátoru předvyplní z sidecaru a jde
  **editovat i hromadně kopírovat**. „Čas pořízení (ISO)" byl přejmenován na
  **„Čas digitalizace"** — je to strojový čas rigu (ISO 8601 s pássem,
  `+02:00` = ČES), ne citlivost filmu. Retired
  pole (`lens`, `lens_serial`, `f_number`, WB, `mirrored`) se nevracejí
  (SEVERNÉ ZKAZY); filmová úroveň má proto nové jméno `shooting_lens`.
  Číselná pole: prázdné políčko u **povinného** (`pushed_stops`, `copy_number`)
  = zachovat uloženou hodnotu; prázdné u **volitelného** (`exposure_time`,
  `gain`) = záměrné vymazání na `None`. Text blbec → `ValueError` dřív, než se
  něco zapíše.
- **Film** (`FILM_FIELDS`, tj. filmový bloček kromě `film_id` a orientčních
  příznaků) se ukládá **do všech sidecarů + project.json** (rozhodnutí
  uživatele). Katalog se záměrně nemění — ten patří capture toku studia.
  Dvě fáze: nejdřív validace všech sidecarů, teprve pak zápis
  (atomicky přes tmp+replace).

## Automatické datum záběru z Film startu (2026-09-22)

Při otevření složky `auto_date_frames` změří `film.development_start`: je-li
to **čitelné datum** (den povinný, na začátku řetězce, i s dodatkem
„8.11.2026 Vacation 2026"), zapíše se jako `capture_datetime` do všech snímků,
které datum ještě **nemají** — sekvenčně po minutách (00:01, 00:02…), jako u
hromadného apply. Říkal uživatel: *„když film start je čitelné datum, aplikuj
na všechny fotky jako datum záběru, já si kdyžtak upravím ručně. pokud tam je
něco jako 'cca 2015' tak to nejde."* „cca 2/2017", „?2025", holý rok →
neprovést nic. Existující datum se nikdy nepřepíše.

## EXIF kontrakt (k implementaci v developeru při exportu JPEG)

Zásada ze zadání: *„já napíšu komentář, ty k němu přidej vše, co nemá vlastní
EXIF tag, oddělené středníky."* Tak to je navržené:

### Tagy (mají vlastní EXIF kolonku)

| EXIF tag | zdroj |
|---|---|
| `Make` / `Model` | `film.camera` (foťák, co exponoval film) |
| `LensModel` | `film.shooting_lens` |
| `ISOSpeedRatings` | `film.film_iso` (ISO filmu, rucni text — parsovat cislo) |
| `DateTimeOriginal` | `annotation.capture_datetime` (už EXIF-tvar) |
| `DateTimeDigitised` | `acquisition.capture_date` (čas digitalizace na riggu) |
| GPS IFD (`GPSLatitude`+Ref, `GPSLongitude`+Ref) | `annotation.gps_*` |
| `Rating` / `RatingPercent` | `annotation.rating` (0–5 → 0–100 %) |
| `ImageDescription` | `annotation.title` |
| `Orientation` | `annotation.rotation_degrees` (CW, %360) + filmové příznaky |

### JPEG comment (vše bez vlastního tagu, za popiskem, „; ")

```
<annotation.note> ; Digitising camera: <acquisition.camera> ;
Digitising lens: <film.digitising_lens> ; Exposure time: <acquisition.exposure_time>s ;
Gain: <acquisition.gain> ; Frames averaged: <acquisition.frames_averaged> ;
Conversion gain: <acquisition.conversion_gain> ; Development: <film.development> ;
Push: +<film.pushed_stops> EV ; Film: <film.film_name> (<film.format>) ;
Expiry: <film.expiry> ; Box: <film.box_number> ; Operator: <film.operator> ;
Digitised: <film.digitisation_date> ; Tags: <annotation.tags>
```

Pravidla:
- `annotation.note` je **vždy první** a nikdy se neopakuje/maže — je to slovo
  uživatele. Developer k němu jen **appenduje** „; " + kusy, které nemají
  vlastní EXIF tag.
- Prázdné/`None` kusy se přeskočí, žádná prázdná středníková políčka.
- Středník uvnitř hodnoty se má přepsat na „–", aby se comment dal
  zpětně rozdělit podle `; ` (ztráta informace nulová).
- `acquisition.capture_date` (čas expozice ve studiu) se do EXIFu **nedostane**
  jako DateTimeOriginal — to je čas digitalizace, nesmí zaměnit čas pořízení
  filmu. Má místo jen v commentu / jako DateTimeDigitised.
- `tags` jsou opakovatelné — až se bude exports rozšiřovat, cesta je XMP
  `dc:subject`; zatím textově v commentu.

Otevřená otázka pro implementaci: jestli vyvést i `rating` do XMP
(`xmp:Rating`) kromě EXIF `Rating` — některé prohlížeče čtou jen jedno.

### Finální kontrakt (po rozkazu 2026-09-22 odpoledne — TOHLE platí)

Návrh výše byl první řeka; provoz ukázal tři opravy, implementace je
následující. Odchylky od tabulky výše:

| místo | zdroj | pozn. |
|---|---|---|
| EXIF `ImageDescription` | **celý popis** (komentář + „ ; " kusy) | ne titulek; ASCII-fold (viz níže) |
| XMP `dc:title` | `annotation.title` | „Title" — první kolonka v Bridge |
| XMP `dc:description` | celý popis, plné UTF-8 | diakritika bez ztráty |
| EXIF `Make` / `Model` | `film.camera_make` / `film.camera_model` | přímá pole; fallback heuristika `split_camera` z volného `film.camera` |
| EXIF `Orientation` | **NEZAPISUJE SE** | rotace se zapéká do pixelů |
| (bývalý UserComment) | **vyřazen** | čtečky ho stejně nečetly |

- **Rotace**: `annotation.rotation_degrees` + filmové příznaky
  (`mirrored_*`, `rotated_180`) aplikuje developer do pixelů před exportem
  (`project.orientation_apply` + `project.rotate_frame`, np.rot90). EXIF
  Orientation tag by se s otočenými pixely u čtečky sečetl a otočil
  podruhé — proto se nepíše. Archivní density TIFF zůstává neotočené.
- **Make/Model systematicky**: anotátor má v Záběru dvě pole (Výrobce /
  Model), `save_film` píše `camera_make`+`camera_model` a drží volné
  `camera` jako lidský souhrn („Nikon FM2"). Staré JSONy s jen volným
  textem migrace `migrate_camera_split` (volá se při otevření složky)
  rozdělí jednou přes `split_camera` — úvodní run ≥2 all-caps tokenů je
  Make („ERNST LEITZ WETZLAR GMBH" | „Leica R4s MOD.2"), jinak první
  token/zbytek („Nikon" | „FM2"). Sidecar s už rozdělenými poli se
  nikdy nepřepíše (operátor mohl fixnout ručně). Developer čte přímá
  pole, heuristika je jen fallback pro data bez nich.
- **Diakritika v EXIF**: ASCII tagy Pillow sráží na „?" (změřeno) —
  do EXIFu jde `_ascii_fold` (NFKD + zahodit diakritiku, „–" → „-"),
  plné UTF-8 žije v XMP `dc:description`/`dc:title`.

### Jak je to implementováno (2026-09-22, `core/exportmeta.py`)

- `build_exif_bytes(record)` / `build_xmp_bytes(record)` berou celý sidecar
  dict (`FrameEntry.record`) a vrací hotové byty; `developer/gui.py` je najde
  podle stemu souboru a předá writeřům (`icc.write_*` nově `exif=`/`xmp=`).
  TIFF cesty se **nezměnily** — exiftool EXIF v TIFFu (tag 34665) nečitl
  (změřeno), takže TIFF zůstává u JSON popisku dle dok. 07.
- Otevřená otázka ratingu: **odevzdáno obojí** — EXIF `Rating`+`RatingPercent`
  i `xmp:Rating`. Štítky též dvakrát: XMP `dc:subject` (Lightroom) i textově
  v komentáři (čtečky bez XMP).
- Hodnoty bez času i holý rok se do datumů nezapíšou (prázdný řetězec);
  ISO se parsuje z textu (`"400/27°"` → 400, `"HP5 @ 1600"` → 1600, `"HP5"`
  → nic — číslo nalepené na písmeno není ISO).
- GPS: DMS rationals s vteřinami na desetinu (0,1" ≈ 3 m), ref z
  `gps_*_ref`, jinak suffix, jinak znaménko; carry 59,9″→60″→60′→1° ošetřen.
- `Software: Filmscan Studio` přibyl nad rámec tabulky: říká odkud data
  jsou. Orientation se na rozdíl od prvního návrhu **nepíše** — viz
  „Finální kontrakt“.
- UserComment (dřív `UNICODE\0` + UTF-16BE) byl prvním návrhem; po rozkazu
  „do ImageDescription dej to, cos dal do usercomment" je nahrazen
  ImageDescription s ASCII-fold popisem a XMP dc:description.
- Chyba při stavbě EXIF **nikdy nezahodí export** — `except` v
  `_export_metadata` logne a exportuje bez metadat. Prázdný record →
  `None` → bajtově shodný export jako dřív.
- Do komentáře přibily oproti návrhu políčka `Content` a `Film note`
  (uživatel 2026-09-22: „chci tam ta data všechna mít… koment k filmu").

## Co anotátor nesmí (ukotveno v kódu i testech)

1. **Additivita**: slovníkový round-trip (ne modelový), cizí klíče v sidecaru
   přežijí; validují se jen bloky, které anotátor píše. Atomy tmp+replace.
2. **Retired pole** se nevracejí: `shooting_lens` je záměrně jiné jméno než
   `lens` z akvizice (SEVERNÉ ZKAZY).
3. **Capture pipeline se nemění** — anotátor je samostatný entry point
   (`filmscan-annotate = filmscan_studio.annotator.gui:main`).
4. Developer (export) se zatím **nezasahuje** — EXIF zápis je samostatný úkol.
