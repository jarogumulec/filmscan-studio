# CHANGELOG

Vše, co se od posledního stavu změnilo, a hlavně: **co nešlo bez fotoaparátu
ověřit** a jak to poznat při prvním zapnutí s tělem.

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
   pixel je z dokumentace Nikon LV zoomu, **ne z měření**. Probe teď uloží
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

## TODO (vědomě neděláno)

- **Zoom 1:1 z plného rozlišení**: capture + výřez z NEFu (ostření z reálného
  snímku, ne živé). Sloužilo by tlačítko „Ostřit na NEF“ — vyžádá si
  rawpy demosaic a focus panel.
- **Aplikovat `mirrored`**: převrátit export ve vývojářském pipeline (a
  případně náhled) — zatím jen metadata, dle dohody.
- **Auto maximální `LiveViewImageSize`** po změření bodu 3.
- Překlopit `choose_body_rate` na naměřené tabulky po bodu 1.
- Podpora `LiveViewPosition` pro přesné centrovaní při body-zoomu (bod 2).
