# Developer render: gray gamma 2.2, ICC a histogramy

Tento dokument je zadání pro další úpravu `filmscan-develop-gui`. Navazuje na
`02_rozbor_negadoctor.md` a `03_rendering_vrstva.md`.

## Závěr v jedné větě

Současný řetězec je monotónní a obraz není převrácený špatným směrem. Viditelný
prudký náběh u černé vzniká aplikací `out ** (1 / 2.2)`, tedy zobrazovacím
transferem, nikoli chybou měření hustoty. Pro přirozené prohlížení je tento
transfer žádoucí, ale nesmí se vydávat za fotografickou S-křivku.

## 1. Požadovaný význam jednotlivých prostorů

Rozlišovat tři různé prostory:

1. **Archivní hustota**
   - `D` v optických hustotách.
   - Vstup do renderu.
   - Zůstává fyzikální záloha a nesmí obsahovat display gamma, ICC ani S-křivku.

2. **Lineární pozitiv po tónové mapě**
   - Výsledek `render_density()`.
   - Obsahuje normalizaci `Dmin/Dmax`, expozici a filmovou/tiskovou křivku.
   - Je to prostor pro posuzování fotografické křivky, ne přímo obraz určený
     pro monitor.

3. **Gray gamma 2.2 / display-referred pozitiv**
   - Výsledek `apply_display()`.
   - Obsahuje transfer `x ** (1 / 2.2)` nebo ekvivalentní ICC gray gamma 2.2.
   - Tento výsledek má používat náhled, exportní JPEG a exportní pozitivní TIFF.
   - Není to lineární záloha. Lineární záloha je hustotní archiv, případně
     zvlášť exportovaný flat podle jasně definovaného kontraktu.

Pro tento monochromatický workflow je gray gamma 2.2 vhodnější a jednodušší
než sRGB. Neoznačovat výstup jako sRGB, pokud se nepoužije skutečný sRGB
transfer a sRGB ICC profil.

## 2. Jsou histogramy správně?

### Dolní histogram: ano, po opravě profilového značení

Dolní histogram má odpovídat obrazu, který uživatel skutečně vidí a který
otevře ve Photoshopu. Proto má být počítán z výsledku:

```text
D -> render_density -> apply_display -> histogram
```

Současný `OutputHistogramWidget` to dělá správně, protože GUI mu posílá
`display = render_for_display(...)`. Histogram tedy přirozeně obsahuje prudší
náběh způsobený gamma 2.2. Photoshop při otevření správně označeného gray
gamma 2.2 souboru zobrazí velmi podobný tónový rozklad.

### Horní histogram: histogram D ano, bílá křivka ne úplně

Šedá plocha horního histogramu je histogram hustot `D`, tedy správně.

Bílá křivka nad ním má ale být diagnostika fotografické tónové mapy. Proto
nemá obsahovat display transfer. Současný kód v `DensityHistogramWidget`
volá `render_for_display(xs, params)`, a tím do bílé křivky promítá i gamma
2.2. To je matoucí: uživatel pak vidí monitorovou charakteristiku jako část
úpravové S-křivky.

Požadovaný stav:

```python
curve = render_density(xs, params)
```

pro bílou křivku horního histogramu. Dolní histogram zůstává po
`render_for_display()`.

Tím se současně zachová správná interpretace:

```text
horní bílá křivka = co dělá filmová/tisková úprava v lineárním pozitivu
dolní histogram   = jak bude vypadat skutečný display-referred výstup
náhled            = skutečný display-referred výstup
export TIFF/JPEG  = skutečný display-referred výstup + odpovídající ICC
```

## 3. Je „hrb“ chyba?

Ne. Při `gamma_display = 2.2` je převod:

```text
display = linear_positive ** (1 / 2.2)
```

Takový převod zvedá malé nenulové hodnoty výrazně nad černou. Například
`0.01` se zobrazí přibližně jako `0.123`. Proto má křivka po display transferu
strmý začátek.

To není důvod měnit fyzikální hustoty, Dmin, Dmax ani směr pozitivu. Je to
vlastnost enkódování pro sledování. Hrb však nemá být kreslen jako součást
horní „korekční“ křivky. Má být vidět pouze nepřímo v dolním histogramu a v
obrazu.

## 4. ICC profil pro všechny výstupy

Implementovat explicitní profilový kontrakt pro `filmscan-develop-gui`:

### Profil

- Přidat projektový nebo dodaný ICC profil `Gray Gamma 2.2`.
- Profil musí být skutečně monochromatický gray profil, ne RGB s třemi
  identickými kanály.
- Profil musí popisovat stejný transfer, jaký používá `apply_display()`.
- Pokud se ponechá přesná mocnina `1/2.2`, ICC profil musí mít odpovídající
  gamma TRC. Pokud se přejde na standardní ICC gray gamma 2.2 TRC, musí se
  stejná TRC používat v náhledu i při exportu.

### Náhled

- Náhled má dál zobrazovat display-referred hodnoty po gamma 2.2.
- ICC profil není důvod aplikovat podruhé do pixelů náhledu.
- GUI widget dostává už display-referred hodnoty; Qt pouze převádí 0..1 na
  8bit šedou.
- Profil slouží k označení/interpretaci exportu a k tomu, aby náhledový
  kontrakt byl stejný jako exportní kontrakt.

### Export

Pro všechny výstupy, které obsahují display-referred obraz:

- pozitivní 16bit TIFF,
- JPEG pozitiv,
- případný další gray JPEG,

zapsat ICC profil do souboru.

Nejprve ověřit knihovnu použitou pro zápis. `cv2.imwrite()` ani současné
`tifffile.imwrite()` samy o sobě v tomto kódu nezaručují vložení gray ICC
profilu. Praktická implementace může použít Pillow nebo jiný ICC-capable
writer, ale musí zachovat:

- 16bit grayscale TIFF jako skutečný single-channel gray obraz,
- 8bit grayscale JPEG jako skutečný single-channel gray obraz, pokud to
  podporovaný writer umožňuje,
- bezztrátový/nezměněný pixelový kontrakt mimo samotné vložení profilu,
- metadata s fingerprintem a názvem profilu.

Testy musí ověřit, že export obsahuje ICC bytes a že načtení přes podporovanou
knihovnu vrátí očekávaný profil `Gray Gamma 2.2`.

Pozor: vložení ICC profilu nesmí znamenat další aplikaci gamma na již
gamma-enkódovaná data. Gamma se aplikuje právě jednou v `apply_display()`;
ICC profil pouze říká ostatním aplikacím, jak tato data interpretovat.

## 5. Photoshop a 16bit TIFF

Photoshop nepotřebuje, aby každý 16bit TIFF byl lineární. Pokud TIFF obsahuje
display-referred data a správný gray ICC profil, Photoshop ho může přímo
zobrazit jako normální koukatelnou fotografii. Photoshop případně převádí
mezi pracovním prostorem a profilem monitoru, ale to není druhá fotografická
tónová křivka.

Rozlišovat tedy:

```text
16bit neznamená automaticky lineární
ICC profil neznamená další gamma aplikovanou do pixelů
```

Současný pozitivní TIFF je určený jako hotový display-referred render. Hustotní
TIFF je archivní fyzikální mezivýsledek. Flat export je třetí větev a musí mít
samostatně zdokumentováno, zda je lineární v hustotě, v transmitanci, nebo
display-referred.

## 6. Znaménko expozice

Tohle není současně prokázaná chyba v implementaci, ale dokumentace je
nekonzistentní.

Kód v `positive_x()` používá:

```python
D_eff = D - dmin + D_PER_STOP * exposure_ev + shadow_band
```

Větší `exposure_ev` tedy posune pixel k vyšší hustotě a pozitiv zesvětlí,
protože v tomto modelu vyšší D znamená světlejší scénu na negativu a světlejší
pozitiv.

To je konzistentní s uživatelským významem „+EV = světlejší pozitiv“. Není to
stejné znaménko jako „scan exposure bias“ v `negadoctoru`; jde o jiný parametr
v jiném místě řetězce. Opravit dokumentaci tak, aby popisovala skutečný kód,
a přidat test, že +1 EV odpovídá posunu o `+log10(2)` D.

Neměnit znaménko pouze kvůli shodě názvu s `negadoctorem`. Nejdřív zachovat
uživatelský kontrakt a vysvětlit, že implicitní inverze negativu je v hustotní
definici: vyšší D se mapuje na vyšší jas pozitivu.

## 7. Co znamená slabá nezávislost profilové gammy

V `FilmicProfile` existují dvě různé věci:

- `gamma_display`: technický transfer pro monitor/soubor,
- `profile.gamma`: parametr tvaru filmové S-křivky.

`profile.gamma` je implementována jako další piecewise power transformace
výstupu spline. Dokumentace tvrdí, že její sklon v pivotu je přesně `gamma`.
To platí pro samotný piecewise power krok, ale při kombinaci s toe/shoulder
není výsledný sklon celé křivky přesně `gamma`, protože spline před ním už má
jiný sklon.

Praktický důsledek pro uživatele:

- není to příčina hrbu u černé;
- monotónnost a pořadí tónů tím nejsou porušeny;
- „Gamma (prostřed)“ je vhodné chápat jako středový kontrast/tvarování, ne jako
  laboratorně přesnou absolutní směrnici celé křivky;
- UI a dokumentace mají přestat slibovat přesný sklon celé kombinované křivky,
  nebo se má později přepracovat parametrizace tak, aby se spline a gamma
  normalizovaly společně.

Pro nynější opravu stačí přejmenování a dokumentační přesnost. Není potřeba
měnit matematiku jen kvůli tomuto bodu.

## 8. Přesný seznam úprav

1. V `DensityHistogramWidget.paintEvent()` kreslit bílou křivku pomocí
   `render_density(xs, params)`, ne `render_for_display(xs, params)`.
2. Dolní `OutputHistogramWidget` ponechat na display-referred hodnotách po
   `render_for_display()`.
3. Zachovat display gamma v náhledu, pozitivním TIFFu a JPEG exportu.
4. Zvolit a dodat skutečný gray gamma 2.2 ICC profil.
5. Přidat ICC profil do všech display-referred TIFF/JPEG výstupů.
6. Zajistit, že ICC embedding profil pouze zapíše, ale znovu nepřevádí pixely.
7. Do metadat exportu přidat např. `encoding: "gray-gamma-2.2"` a identitu
   profilu.
8. Opravit znaménko expozice v `03_rendering_vrstva.md` a přidat vysvětlení
   rozdílu proti `negadoctor`.
9. Upravit texty `FilmicProfile.gamma` na „středový kontrast“ místo tvrzení,
   že jde o přesný sklon celé výsledné křivky.
10. Přidat testy:
    - horní křivka nepoužívá display gamma;
    - dolní histogram dostává display-referred data;
    - exporty obsahují ICC profil;
    - gamma se neaplikuje podruhé při zápisu;
    - archivní hustota se profilem ani display gamma nemění.

## 9. Co se nemá dělat

- Neodstraňovat gamma 2.2 z náhledu jen proto, že horní křivka má hrb.
- Neaplikovat ICC profil jako další mocninu do pixelů.
- Nepřidávat sRGB jen proto, že ho používá většina barevných obrazů.
- Nekreslit horní diagnostickou křivku z hotového display obrazu.
- Neměnit hustotní archiv na gamma-enkódovaný obraz.
- Nezaměňovat `profile.gamma` za `gamma_display` ani za paper gamma
  `negadoctoru`.