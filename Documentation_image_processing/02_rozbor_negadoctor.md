# 02 — Rozbor matematiky darktable negadoctoru

Zdroj: `local darktable checkout (github.com/darktable-org/darktable)/src/iop/negadoctor.c` (1078 ř., verze master
z lokálního checkoutu) + oficiální manuál (`darktable user manual - negadoctor.html`).
Zajímá nás matematika a tok dat, ne GUI.

## 1. Celkový tok modulu

Vstup: **lineární RGB, display-referred** (po input color profilu, po demosaicu).
Jádro je per-pixel, bez histogramu, bez splineů — vše analytické (kvůli OpenCL SIMD):

```
pix_in (lineární „transmittance" skenu, 0..1)
 1. density  = Dmin / max(pix_in, THRESHOLD)          # THRESHOLD = 2.33e-10 (−32 EV)
 2. log_den  = −log10(density)                        # = −log10(Dmin/T), přes log2
 3. corrected= wb_high·log_den + offset               #wb_high = WB koef / D_max
 4. ten_x    = 10^corrected
 5. print    = max(exposure·ten_x − exposure·(1+black), 0)   # „tisk na papír", invertováno
 6. printed  = print^gamma                            # paper grade
 7. softclip: nad soft_clip exponenciální komprese    # paper gloss
```

Jádro `_process_pixel` (negadoctor.c:268-322), konsolidovaný vzor (složkově, pro mono c:vše stejné):

```
out = { p^γ                                       pro p ≤ s
      { s + (1 − e^−(p−s)/(1−s))·(1−s)            pro p > s
kde p = [ max( E·10^(W·(−log10(Dmin/T)) + O) − E·(1+B), 0 ) ]
```

Parametry (struktura `dt_iop_negadoctor_params_t`, ř.70-91):
`Dmin[4]` (barva podložky v „transmittanci", 0..1.5), `wb_high[4]`, `wb_low[4]`,
`D_max` (1.6 color / 2.2 BW preset, ř.398/413), `offset` (= scan exposure bias, default −0,05),
`black` (paper black, 0,0755), `gamma` (paper grade, 4 color / 5 BW),
`soft_clip` (paper gloss, 0,75), `exposure` (0,9245).

### Klíčové identitní překvapení: fází 3–6 je **Cineon/ACES tónová křivka**

Dosadíme-li mono a `W = 1/D_max`, `O = 0`, `E = 1`, `B = −1`:

```
p = 10^(−D/D_max)          D = −log10 T' (D' = T/Dmin, tj. hustota vztažená k base)
```

a volba `γ`, `B`, `E,` pak tvaruje „odtiisk". Popis v manuálu
`RGB_out = (RGB_in·exposure + black)^grade` je jen zjednodušení fází 5–6 bez expozice.
Tvrzení v hlavičce modulu (ř.42-47): implementace **Cineon densitometry** — odkazy na Kodak
Sensitometry Workbook, Cineon spec (10-bit log, 0.45–2.19 D) — a soft-clip z openexr-devel
mailing listu. Tónová mapa negadoctoru **není spline ani filmic**; je to exponenciála
v hustotní doméně. Toe neexistuje samostatně — toe/shoulder chování je důsledek
`γ` a `soft_clip`.

### Auto-pickery (GUI logika, ale definují význam parametrů)

- `apply_auto_Dmin` (ř.635): Dmin = průměr výběru přes neosvětlený okraj — **v prostoru
  transmittance, ne v D.**
- `apply_auto_Dmax` (ř.655): `D_max = log10(Dmin / min(pick))` — dynamický rozsah filmu
  z minima výběru (nejhustší misto).
- `apply_auto_offset` (ř.679): offset tak, aby rozsah mapoval na [0;1].
- `apply_auto_WB_low/_high` (ř.702/729): korekce nádechů **v log-densitometrickém prostoru**
  — multiplikativní na `log_density` (wb_high) a additivní offset (wb_low). Pro mono
  bezbarvé negativy identita.
- `apply_auto_black/_exposure` (ř.756/782): dopočet paper black/exposure z pickerů.

## 2. Co je pro náš mono transmisi přímo použitelné

1. **Základní identity** `D' = −log10(T/T_base)` a tisková mapa v log-densitometrickém
   prostoru — přesně dvourozlové rozdělení měření/render, které chceme. Negadoctor je
   důkazem, že tohle je prakticky použitelná matematika (Cineon průmyslový standard).
2. **Soft-clip exponenciála** (ř.317-321, zdroj openexr-devel): hladká, C¹ komprese
   nad prahem `s`, výstup zůstává pod 1. Jednoduchá, levná, bez iterací. Dobrý stavební
   kámen pro „shoulder/paper gloss" nášho renderu. (My už máme monotone spline —
   soft-clip je dobrý doplněk pro *highlight-only* kompresi.)
3. **Práce v hustotách + exponent na konci** („print grade"): `out = p^γ` při
   `p = 10^(−D/Dmax)` je mocnina v hustotě = **konstantní strmost H-D křivky** (gamma
   papíru je přesně gradient lineární části H-D). To je přesně režim B z našeho nástinu.
4. **Ořezání v transmittanci, ne v D:** `max(pix_in, THRESHOLD)` = −32 EV strop na D
   (≈9,6 D). Naše verze: spíš NaN maska, ale princip „odřezávej nízce, nepouštěj log(0)"
   přejímáme.
5. **Separace technických a kreativních parametrů** (film properties vs print properties)
   — struktura UI i API: nejdřív kalibrační/měřicí konstanty, pak vzhled.

## 3. Co je pouze barevné → zahodit

- `Dmin[4]` RGB (u BW filmu se přenásobí na monochromální, ř.250-254 — důkaz, že pro mono
  je to zbytečná generalizace), `wb_low/wb_high` RGB, barevné matice a picking po složkách,
  oranžová maska (ta se implicitně „spolyká" Dminem po kanálech — pro BW negativ
  nemaskovaný neexistuje).
- Barevné korekce stínů/světel na záložce *corrections* — pro IMX571 mono bezvadně
  mimo obor (viz i pravidlo projektu: **žádný WB**).

## 4. Co je vhodné NAHRADIT naším měřením (hlavní diference)

| negadoctor předpokládá | náš systém má | důsledek |
|---|---|---|
| Vstup je raw sken bez kalibrace; dark, flat, citlivost pixelů, vinětace zůstávají v datech | dark + flat per akvizice | **Nemusíme nic odhadovat z histogramu.** `Dmin` picker (98 % bounding box!) je u nás nahrazen přímým měřením `FilmBaseSample` s exponováním a teplotou; vestavěné chyby pickeru řádově 1 % mizí. |
| „White" reference = Dmin odhadnutá z okraje skenu nebo z celého snímku | T referenční = flat per-pixel (světlo bez filmu) | Naše T je absolutní veličina (0..1), negadoctorova je vztažená k odhadu. Jeho `Dmin/T` je our `(T_meas/T_base)` po dosazení — ale jen při správném pickeru. |
| Chybí flat ⇒ vinětace/dust se promítá do D a kompenzuje se `wb_low/high` a offsetem | flat koriguje multipikativní nehomogenitu fyzikálně | Barevné i jasové korekce, které negadoctor nabízí jako záplaty, máme vyřešené před vstupem do hustoty. |
| Teplotní/dark drift ošetřit nelze (modul neví o darku) | dark metadata + teplota hlídána akvizicí | Naše provenance allow recalc; negadoctor má jen „scan exposure bias" (`offset`) jako jedinou páčku na chyby expozice. |
| D_max z pickeru (nebo ručně) | per-frame p99.9 D + měřené Dmin | Režim A auto; D_max se stává odvozená metadata archivu, ne parametr uživatele. |
| Vstup display-referred lineární, výstup display | My držíme dva oddělené obory (D archiv, render z D) | Archiv podstatně hodnotnější než negadoctor history stack. |

## 5. Slabá místa negadoctoru, kterým se vyhýbáme (nebo je zdokumentujeme)

1. **Tónová mapa bez toe** — exponenciála má všude kladnou strmost; komprese spodku
   se dosahuje jen `black` offsetem (ořez). Náš monotone spline (filmic.py) umí toe
   kompresi bez ořezu; negadoctorův BW preset to řeší agresivním `gamma=5`.
2. **`γ` škáluje s `D_max`** — manuál doporučuje „4 − D_max ≈ 2..3"; u vysoce hustých
   filmů (BW Dmax 3+) preset selhává. Náš render definuje kontrast přímo v D
   (strmlost na středové hladině), ne přes volbu jednotek.
3. **Žádné H-D emulze** — negadoctor nezná konkrétní filmy; Cineon konstanty jsou univerzální
   (a odpovídají Motion Picture negativu, ne HP5). Náš režim B/C je doplní.
4. **THRESHOLD = −32 EV** je libovůle; my dáváme smysluplný strop z poměru signál/šum.

## 6. Co převzít do našeho renderu (konkrétní návrh — detail v 03)

- Zobrazení „tisku": `out = softclip( (E·10^(−(D−Dmin_eff)/D_max_eff·k) … )^γ )` — tj.
  exponenciální mapa v čistých hustotách + paper grade + soft-clip gloss, s parametry
  pojmenovanými podle tiskové analogy (uživatelsky přívětivé) **ale** definované tak,
  aby `γ` byla přímo strmost H-D (směrnice v D→log expozice).
- Pořadí parametrů a jejich „technické vs kreativní" skupiny.
