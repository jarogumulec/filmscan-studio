# CHANGELOG

Vše, co se od posledního stavu změnilo, a hlavně: **co nešlo bez fotoaparátu
ověřit** a jak to poznat při prvním zapnutí s tělem.

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
