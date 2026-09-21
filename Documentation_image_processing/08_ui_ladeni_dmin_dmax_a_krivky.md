# UI ladeni pozitivu: doporucene rozsahy a postup

Tento dokument je implementacni zadani pro dalsi upravu
`filmscan-develop-gui`. Cilem je, aby uzivatel pri ladeni rozlisil:

- meritko skutecneho filmu (`Dmin`, `Dmax`),
- kontrast fotografickeho renderu (`toe`, `gamma`, `shoulder`),
- technicky zobrazovaci transfer (`gamma_display`).

## 1. Zakladni rozhodnuti

`Dmin` a `Dmax` nejsou bezne kontrastove ovladace. Urcuji, jaka cast hustotni
osy filmu se mapuje do vystupu:

```text
Dmin = cira podlozka / nejcernejsi pozitiv
Dmax = nejhustsi pouzitelne stribro / nejsvetlejsi pozitiv
```

`gamma` ma menit kontrast stredu. `toe` a `shoulder` maji komprimovat konce,
ne nahrazovat spatne nastavene body rozsahu. `gamma_display` se pri ladeni
fotografickeho kontrastu nemení.

## 2. Doporucene rozsahy UI

### Meritko filmu

- `Dmin`: zachovat fyzicky rozsah `0.0-2.0 D`, s krokem `0.001-0.01 D`.
  Pri automatickem rezimu je hodnota zamcena a pochazi z film base.
- `Dmax`: zachovat technicky rozsah `0.2-5.0 D`, s jemnym krokem
  `0.01-0.05 D`. Auto navrh zustava `p99.9 + margin`, ale UI musi jasne
  ukazat, ze jde o navrh pracovniho maxima, ne o pevnou vlastnost emulze.
- `shadow_band`: zmenit popisek na `Tolerance pod Dmin [D]` nebo
  `Stinova tolerance [D]`. Rozsah pro bezne ladeni `0.00-0.08 D`, vyjimecne
  lze povolit az `0.15 D`. Soucasnych `0.30 D` je pro jeden slider prilis
  siroky rozsah a snadno vytvori vyprane cerne.
- Nepridavat zatim `highlight_band`. Pro horni konec je prvni spravny ovladac
  `Dmax`; analogicky slider by menil meritko a maskoval, ze Dmax je spatne.

### Fotograficka krivka

Doporucene rozsahy pro ovladani:

- `toe`: `0.00-0.80`, vychozi `0.20`, krok `0.01`.
- `gamma`: `0.80-2.50`, vychozi `1.35`, krok `0.01`.
- `shoulder`: `0.00-0.80`, vychozi `0.20`, krok `0.01`.

Technicky muze model ponechat maximalni hodnotu `1.666`, ale bezne UI ji nema
nabizet jako hlavni rozsah. Hodnoty nad `0.8` jsou specialni komprese koncu,
ne normalni ladeni fotografie.

Prakticky vyznam gamma:

```text
1.00       neutralni / temer linearni
1.15-1.35  jemny prirozeny kontrast
1.35-1.70  bezny kontrast vetsiny snimku
1.70-2.20  vyrazny kontrast
nad 2.20   specialni pripad, casto prilis tvrdy
```

`gamma_display` ponechat jako technicky parametr oddelene od kreativni
krivky. Vychozi hodnota `2.2` zustava, ale pri posuzovani Dmin/Dmax/toe/gamma/
shoulder se nema menit.

## 3. Doporucene poradi ladeni

UI, napoveda i dokumentace maji uzivatele vest timto postupem:

### Krok 1: Over Dmin

Pouzit film base. Dmin musi odpovidat ciste, neosvetlene casti filmu, nikoli
nejtmavsimu obrazovemu motivu. Pokud neni zmerena film base, rucni Dmin je
jen relativni odhad a nelze ocekavat fyzicky stabilni vysledek mezi snimky.

### Krok 2: Nastav Dmax podle praveho konce hustot

Sledovat horni histogram hustoty. Dmax posunout tak, aby obsahl nejhustsi
pouzitelne obrazove hodnoty, ale nebyl zbytecne daleko za nimi.

- Dmax prilis vysoko: svetla se smrsti do male casti vystupu a obraz je
  plochy.
- Dmax prilis nizko: svetla se roztahnou, ale cast se oreze na bile.

Automaticke `p99.9 + 0.05 D` je konzervativni navrh. Rucne snizeni Dmax je
opravnene, pokud pravy okraj hustot obsahuje skutecne obrazova data a ne jen
saturaci, skrabanec nebo sum.

### Krok 3: Nastav gamma stredu

Gamma je hlavni ovladac oddeleni stinu a svetel v beznem obraze. Zacit kolem
`1.35`. Zvysovat po malych krocich, dokud stredni tonovy kontrast nevypada
spravne. Pokud zacnou svetla tvrde utekat k bile nebo stiny mizet do cerne,
vratit gamma a zkontrolovat Dmax/Dmin.

### Krok 4: Pouzij toe pouze pro spodek

Vyssi toe stlaci spodni cast pozitivu. Neni to recovery ztracenych pixelu.
Pokud jsou stiny prilis slite, toe snizit. Pokud je v nich neprijemny sedy
mlak, toe mirne zvysit.

### Krok 5: Pouzij shoulder pouze pro svetla

Vyssi shoulder stlaci horni cast pozitivu. Pokud jsou svetla prilis slita,
shoulder snizit. Pokud je potreba zachranit jemne gradace pred bilym orezem,
shoulder mirne zvysit, ale nejprve se ujistit, ze Dmax neni nastaveny spatne.

### Krok 6: shadow_band jen jako tolerance mereni

`shadow_band` neni druhe Dmin a nevraci detail, ktery byl fyzicky orezan.
Posouva vsechny hodnoty o maly kus nad nulu v hustotnim vstupu, aby mirne
podbase hodnoty nesplynuly okamzite do cerne. Bezny rozsah je `0.00-0.03 D`.
Vyssi hodnota muze zvednout a vyprat cernou.

## 4. Ma se pridat slider pro svetelnou rezervu?

V prvni implementaci ne. Svetelny analog shadow bandu by umoznil schovat
spatne Dmax a ztizil by interpretaci histogramu. Dmax je spravne misto pro
rozhodnuti, kde konci pouzitelny filmovy rozsah.

O `highlight_band` uvazovat az po datech, pokud se opakovane potvrdi, ze:

1. automaticky Dmax je statisticky stabilni,
2. hodnoty nad Dmax jsou opakovane platne obrazove hodnoty,
3. nechceme je zahrnout primo snizenim nebo rucnim nastavenim Dmax.

Pokud se nekdy prida, musi se jmenovat jako tolerance nad Dmax a byt vizualne
oddeleny od Dmax, aby nebyl zamenen za filmove maximum.

## 5. Navrh UI zmen

Rozdelit panel na tri jasne skupiny:

```text
Meritko filmu
  Dmin
  Dmax
  Tolerance pod Dmin

Tonalni krivka
  Patka (toe)
  Kontrast stredu (gamma)
  Rameno (shoulder)

Zobrazeni
  Display gamma
  Jas (display)
  Kontrast (display)
```

Zmeny textu:

- `Gamma (prostred)` -> `Kontrast stredu (gamma)`.
- `Stinovy pas [D]` -> `Tolerance pod Dmin [D]`.
- U Dmax zobrazit stav `auto: p99,9 + 0,05 D` a moznost rucniho prevzeti.
- U `Display gamma` uvést `technicke zobrazeni, nemenit pri tonovani`.
- U slideru toe/shoulder pridat kratkou tooltip napovedu o kompresi konce,
  ne o recovery.

Pri zapnutem auto Dmax muze UI zobrazit doporucenou hodnotu, ale rucni zmena
ma byt snadno dostupna. Uzivatel tak muze nejprve respektovat fyzicky navrh a
pak vedome upravit pracovni rozsah podle obrazovych dat.

## 6. Akceptacni testy

Pridat testy na model a GUI:

- Dmax nizsi nez puvodni hodnota roztahne stredni a svetelne hodnoty, ale
  spravne oznaci nebo omeri hodnoty nad novym rozsahem.
- Vyssi toe snizi lokalni rozsah ve stinech.
- Vyssi shoulder snizi lokalni rozsah ve svetlech.
- Vyssi gamma zvysi kontrast kolem stredu a zachova monotonnost.
- `shadow_band` ovlivni pouze oblast kolem a pod Dmin, ne posun celeho
  meritka stejne jako rucni zmena Dmin.
- Zmena `gamma_display` nezmeni vysledek `render_density`, pouze
  `render_for_display` a dolni histogram.
- Automaticky navrzeny Dmax zustane reprodukovatelny a rucni Dmax se ulozi do
  `develop_settings.json` s `dmax_source="manual"`.

## 7. Co ma zustat mimo tuto zmenu

- Neměnit smer pozitivu ani hustotní archiv.
- Nezaměňovat Dmax za paper gamma z `negadoctoru`.
- Nepřidávat druhou gamma do exportu.
- Neřešit zatím plný H-D model konkrétní emulze; to je samostatný budoucí
  režim, ne oprava ovládacích rozsahů.