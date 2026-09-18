# 06 — Požadavek: `image_rect` v metadatech akvizice (ROI)

Stav: **DODÁNO 2026-09-18** (projekt test2) — pole se ve skutečnosti jmenuje
**`crop_rect`** (top-level sidecaru, jen u `kind: scan`); developer čte
`crop_rect` i `image_rect`, obojí na obou místech. Staré projekty (B1) pole
nemají → fallback: Shift+kreslení v GUI + NaN maska.
Původní zjištění: chybělo v sidecaru, v embedded acquisition JSON, v katalogu
`captures` i v `project_export.json` (B1).

Ověřeno na test2: `crop_rect [597,216,6164,3889]` → archiv ořezán 3673×5567,
platných pixelů **99,7 %** (B1 bez rectu 86 % — ROI triage přesně dělá co má).

## Proč na tom trvat

Při snímání operátor kreslí obdélník s obrazovými daty; mimo něj je tmavý okraj
= **rám držáku**. Ten nemá smysl hustotově zpracovávat:

1. Není to film — T je tam dáno tmou, ne emulzí; hustota je nedefinovaná.
2. Znečišťuje statistiky (percentily Dmax, histogram, auto-black).
3. Vize uživatele: **archiv 32bit TIFF se ukládá jen v ořezu**, rendery z něj.

## Co má akvizice dodávat

Do sidecaru snímku (`frameNNN.tif.json`) i do embedded JSON v TIFF headeru:

```json
"image_rect": [x0, y0, x1, y1]
```

- **souřadnice plného snímku** (6224×4168), ne streamu — stejný slib jako u
  `FilmBaseSample.rect` (modul `filmbase` nic nepřepočítává, GUI vlastní
  bookkeeping binned streamu; při zápisu full-frame frameu GUI přepočítá),
- x0,y0 inclusive, x1,y1 exclusive,
- **povinné pro `kind: scan`** (a `kind: base`, je-li base měřena na full-frame),
- additivní pole: **`SCHEMA_VERSION` se nezvyšuje**, staré archiv čitelné
  (stejný vzor jako `_strip_retired` — kdo pole nezná, ignoruje ho).

## Co s chybějícím rectem (záchrana, protože na kreslení se dá zapomenout)

Developer GUI (přidáno 2026-09-18): **Shift+tažení myší v náhledu** nakreslí ROI
manuálně; `DevelopProject.set_rect()` ji drží pro aktuální session. Chybí-li i ta,
`valid_mask` aspoň označí neosvětlené kraje jako NaN — okraj pak nezkreslí D,
jen plýtvá pixely archivu.

Priorita ručního kreslení: **metadlo z akvizice je autorita** (operátor kreslil
při exponování, kdy viděl film v držáku naostro); ruční ROI je záchrana, ne
cesta. GUI zobrazuje, ze kterého zdroje rect je (sidecar / ruční / žádný).

## Kam sahne, až se dodá (strana developera — hotovo)

`developer/project.py::_rect_from_record()` už pole čte (i z `acquisition.image_rect`)
a automaticky jím ořezává hustotní archiv i rendery — po dodání akvizicí se
chování změní samo, bez zásahu do developeru.

## Pozn. k testům

`tests/test_project.py::test_image_rect_read_from_record` simuluje dodání pole
vpisem do sidecaru a ověřuje, že developer ho použil — tento test je smlouva,
kterou akvizice musí naplnit stejnym jménem a tvarem.
