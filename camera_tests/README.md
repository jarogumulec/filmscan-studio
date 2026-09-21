# Karakterizace kamery ATR2600M (TS2600MP, IMX571)

Standalone měření na reálném hardwaru, **bokem od aplikace** — skripty tu
nic v `capture/` nemění (jen čtou přes `TouptekCamera`; tam, kde aplikace
clampuje agresivněji než kamera, jdou primou cestou přes SDK).

Měřeno 2026-09-19, ATR2600M, bílé homogenní pozadí rigu, USB3, napájení
12 V, gain 1,00× všude. Data: `data/` (gitignored), grafy: `*.png` vedle.

```
.venv/bin/python camera_tests/probe_min_exposure.py    # (a) min expozice, ~3 min
.venv/bin/python camera_tests/averaging_snr.py         # (b) averaging, ~2 min
.venv/bin/python camera_tests/temperature_snr.py       # (c) teplota, ~10 min
.venv/bin/python camera_tests/gain_snr.py              # (d) gain × SNR, ~8 min
.venv/bin/python camera_tests/probe_modes.py           # (e) LCG/HCG + low noise: co kamera umí, ~1 min
.venv/bin/python camera_tests/lcg_hcg_snr.py           # (f) LCG/HCG × LN šum/DR, ~20 min, ŽÁDÁ obsluhu: tma→světlo
```

---

## (a) Nejkratší expozice — SW vs. HW limit

**Proč se měřilo:** aplikace „nepustila" kratší než ~1/3330 s. Je to její
vlastní clamp `EXPO_TIME_RANGE_US = (300, 1_800_000_000)`
(`src/filmscan_studio/capture/touptek.py:84`) — **300 µs = 1/3333**.
Konstanta byla doposud jen odhad („until a narrower range is probed").

| žádáno | firmware |
|---|---|
| 1–50 µs | **ODMÍTNO** (`E_INVALIDARG 0x80070057`) — tvrdé hw dno |
| 100–250 µs | přijato, readback přesný — **aplikace sem nedovolí sáhnout** |
| ≥300 µs | v pořádku (odtud to 1/3330) |

- **HW dno kamery: 100 µs (1/10 000).** Pod ním `put_ExpoTime` hází
  E_INVALIDARG; ve streamu i bez něj, se stejným chováním.
- **SW dno aplikace: 300 µs.** To je to, co vidí operátor. Kdyby chtěla
  aplikace dolů, je to jednočíslová změna na 100 — neděláno, do pipeline
  se nesahá; na zvážení.
  **⇒ Vyřešeno 2026-09-19 (přímý povel uživatele):** `EXPO_TIME_RANGE_US`
  srovnano na hw dno 100 µs; pod ~400 µs operátor uvidí přes readback
  kvantovanou schody (viz níže).
- **Kvantizace spodku (překvapení):** expozice pod ~400 µs není spojitá.
  Medián DN roste po skocích ~12,1–12,7 k DN, tj. po krocích ~75 µs
  (globální závěrka IMX571):

  | žádaný čas | medián DN |
  |---|---|
  | 100 µs | 15,0 k |
  | 125–150 | 27,1 k |
  | 175–225 | 39,3 k |
  | 250–300 | 51,5 k |
  | 325–375 | 63,6 k |
  | ≥400 | 65 535 (saturace) |

  Pod ~400 µs tedy nelze plynule dávkovat expozici „po světelných krocích" —
  kamera exponuje buď 100, nebo 175, nebo 250 µs… a mezi tím nic.
- Praktické dno pro tento rig je stejně **~350–400 µs**: bílé pozadí dává
  ~170 DN/µs a při 400 µs je plná lázeň.
- Kadence v ROI 1200×1200: ~11,7 fps konstantně od 100 do 20 000 µs —
  u krátkých časů limituje readout, ne expozice. (`FRAMEINTERVAL_*` a
  `*_PRECISE_FRAMERATE` kamera neimplementuje.)

Grafy: `min_exposure_applied.png` (žádáno vs. skutečnost),
`min_exposure_fine.png` (schody kvantizace).

## (b) Averaging — SNR vs. počet snímků

Podmínky dle zadání: **gain 1,00×, TEC cíl +10 °C** (držel +8,2…+9,7 °C),
**30 stillů** 6224×4168, bílé pozadí, expozice 115 µs (nejblíž 26 k DN, co
kamera na tomto pozadí umí; signál 26 976 DN), měřeno ve **středové
čtvrtině plochy** (3112×2084 px).

| K | 1 | 2 | 4 | 8 | 15 | 30 |
|---|---|---|---|---|---|---|
| SNR = mean/σ | 20,2 | 24,2 | 27,2 | 29,3 | 30,4 | **31,1** |
| zisk nad K=1 [dB] | — | +1,6 | +2,6 | +3,2 | +3,6 | **+3,7** |

Ideál √K by slíbil +14,8 dB; **křivka se láme už kolem K≈4 a saturuje
na ~31** (`averaging_snr.png`). Rozklad šumu jednoho snímku (σ = 1336 DN):

- **fixní nehomogenita scény σ ≈ 859 DN (3,2 %)** — nepravidelnost
  podsvícení/difuze; průměrováním se neodstraní (každý snímek ji má
  stejnou). Strop SNR = 26 976/859 ≈ 31,4 — přesně tam křivka dosedla.
- **mezisnímková nestabilita osvětlení σ ≈ 1024 DN (3,8 %)** — svítivo
  mezi expozicemi kolísá; to vysvětluje i nízké SNR při K=1.
- Skutečný šum senzoru je maskován — při 27 k DN je pouhý shot noise
  √27 000 ≈ 165 DN (senzor-only SNR ~160, tj. ~6× líp než naměřeno).

**Praktický závěr:** averaging má smysl jen dokud dotírá na nestabilitu
osvětlení, ne na šum senzoru: **K = 4–8 (+2,6…+3,2 dB)**, dál už jen
průměruje blikání světla. Chce-li provoz víc, má stabilizovat zdroj
světla (DC, teplota LED), ne fotit víc snímků. Pro archiv (film v cestě,
sekundové osvitové řády) je poměr sil jiný — tam dominují fotony a fixní
vzor se řeší flaty; tento závěr platí pro bílé pozadí při 115 µs.

## (c) Teplota × SNR (1 snímek)

Body: TEC off (pokojová) → +20, +15, +10, +5, 0, −5 °C; stabilizace
čekána (pokojová trvala 276 s, chladnutí 20–90 s na 5 °C); 2 snímky/bod,
stejný osvit 115 µs, gain 1×.

| nastavení | senzor [°C] | signál [DN] | σ celk. [DN] | σ temporální [DN] | SNR |
|---|---|---|---|---|---|
| off | +30,9 | 27 299 | 1344 | 1038 | 20,3 |
| +20 | +19,7 | 27 053 | 1336 | 1034 | 20,3 |
| +15 | +15,1 | 27 001 | 1334 | 1033 | 20,2 |
| +10 | +10,1 | 26 913 | 1332 | 1032 | 20,2 |
| +5 | +5,2 | 26 825 | 1329 | 1030 | 20,2 |
| 0 | −0,1 | 26 712 | 1325 | 1028 | 20,2 |
| −5 | −5,1 | 26 576 | 1321 | 1026 | 20,1 |

**Verdikt: v rozsahu +31…−5 °C je SNR jednoho krátkého snímku neměnný
v měřítku měření (0,09 dB).** Temporalní σ klesá s chladnutím jen slabě
(1038→1026 DN, −1,2 %) — při 115 µs není co chladit: dark current za tu
dobu je zanedbatelný a šumu vládnou fotony + nestabilita světla (viz (b)).
Mírný pokles signálu s chladnutím (−2,6 %) je termika LED svítiva, ne
senzoru.

**Co z toho plyne:** TEC se na tomto rigu nevyplácí pro krátké snímky —
vyplácí se pro **provozní sekundové osvitové řády**, kde dark current a
jeho shot noise rostou s časem i teplotou. Měřený rozsah byl vynucený
pozadím (115 µs, nad ~400 µs saturace) — to je limit měření, zdokumentován.
Doporučený follow-up: dark-field charakteristika (zatemněno, 30–300 s,
po stupních) — `temperature_snr.py` je strukturně připravený, stačí
vyměnit zdroj světla za tmu.

## (d) Gain × SNR (expozice kompenzována na stejný signál)

**Otázka:** je gain 0,1× jen matematická operace (digitální škálování po
ADC), nebo reálná analogová předzesílená? A je při expoziční kompenzaci
zisk vidět na SNR mezi 0,1× a 1,0×?

**Metoda:** pro každý gain se hledá expozice, která drží bílé pole na ~26 k DN
(`expose_roi`, iterativně, sat. limit 58 k), 3 stily/bod, gain 0,1…10.
Podlaha (tmavé snímky) se měřila jen když dvojexpozice 100/300 µs prokázala
tmu — v tomto prostředí ale světlo zůstávalo rozsvícené, takže tmavé sloupce
nejsou platné (viz poznámka níže).

| gain [×] | expozice [µs] | signál [DN] | SNR(1) | σ temp. [DN] | σ·gain |
|---|---|---|---|---|---|
| 0,1 | 1439 | 26 241 | **29,8** | 321 | 32,1 k |
| 0,2 | 705 | 25 052 | 27,8 | 443 | 31,9 k |
| 0,5 | 291 | 25 698 | 24,0 | 710 | 32,2 k |
| 1,0 | 105 | 27 011 | 20,2 | 1033 | 32,9 k |
| 2,0 | 100 (dno) | 29 727 | 16,5 | 1540 | 32,7 k |
| 3,0 | 100 (dno) | 44 523 | 16,4 | 2319 | — (nekompenzováno) |
| 5,0 / 10,0 | — | — | — | — | **skip:** ani na 100 µs nelze pod saturaci |

**Verdikt 1 — gain je ANALOGOVÝ (před ADC), ne digitální násobek:**
při 0,1× signál s delší expozicí lineárně doroste až na plných 65 535 DN
(~4 ms); digitální ×0,1 by ořízl nejvýš na ~6 550 DN. Krok 0,2× dává přesně
2× DN/µs oproti 0,1× (lineární žebříček 0,2/0,5/1/2/3).

**Verdikt 2 — zisk JE na SNR vidět, ale ne „jako zisk":** při stejné
expozici by SNR na gainu nezávisel (signál i šum se škálují stejně). Tady
se ale kompenzovala expozice: nižší gain → delší expozice → víc fotonů →
SNR roste, jak gain klesá (0,1×: 29,8 vs. 1,0×: 20,2, tj. **+1,44 dB**).
Je to zisk fotonů, ne elektroniky.

**Verdikt 3 — σ·gain je konstantní (~32–33 k DN·×):** temporální šum
přepočtený na elektrony je na gainu nezávislý a jeho relativní velikost
(1033/27 011 = 3,8 % při gainu 1) přesně sedí na mezisnímkovou nestabilitu
světla z (b). Naměřené chování je tedy konzistentní s kolísáním osvětlení
(vč. možného blikání LED při ~100 µs expozicích), ne se šumem elektroniky
senzoru. Rozhodnout mezi blikáním a pravým shot/flicker šumem by šlo až
dark-field měřením s dlouhými expozicemi.

**Kaveats:**
- Zisk ≥ 2–3 neudrží 26 k DN cíl nad hw dnem 100 µs (viz kvantizace spodku
  v (a)) — srovnání tam už není fér, řádky 3,0+ jsou jen informativní.
- Čas 1439 µs při 0,1× je nad kvantizovaným pásmem, OK; časy ~100–105 µs
  u gainů 1–3 sedí na schodech po ~75 µs.
- **Datová vadba:** v `data/gain_snr.csv` má řádek gain 3,0 neplatné
  `dark_*` hodnoty (44 510 / 2319) — test „je tma?" klamal saturující
  referenční 300µs snímek. Chyba byla v `gain_snr.py` dodatečně opravena
  (přidaný absolutní limit), platná tmavá data ale vzniknou až při
  skutečně zatemněném rigu.

Graf: `gain_snr.png` (vlevo SNR vs. gain log-x s vertikálami saturace,
vpravo temporální σ [DN] vs. gain + modelová přímka σ ∝ 1/gain).

---

## (e) LCG / HCG × Low Noise — co kamera umí (probe, 2026-09-20)

`probe_modes.py`, rychlý capability probe bez tmy.

| vlastnost | zjištění |
|---|---|
| vlajky | `FLAG_CG` ANO, `FLAG_LOW_NOISE` ANO, `FLAG_CGHDR` ne |
| `TOUPCAM_OPTION_CG` | 0=LCG, 1=HCG přijímány + readback sedí; **2=HDR odmítnuto** (E_INVALIDARG) |
| `TOUPCAM_OPTION_LOW_NOISE` (0x38) | 0/1 přijímány + readback sedí |
| **výchozí stav kamery** | **CG=1 (HCG)!** kamera přetrvává v astro módu — aplikace si o LCG musí říct sama (od 2026-09-20 to při connectu dělá) |
| fps full-res 16bit | LN=0: **6,99 fps**, LN=1: **3,57 fps** (manuál: 6,8/3,4 — sedí) |
| LN + 3×3 binning | funguje, **3,65 fps** (binned overview 2074×1388); manuálové „LN jen All Pixel" se na tomto kuse nejeví jako hard limit |
| still s LN=1 | produkční `capture()` projde v pořádku (51,9 MB TIFF) |
| zápis CG/LN za proudu | **přijato** (na rozdíl od BINNING/ROI) — přepínání živé, bez Stop/Start |
| **funkční poměr HCG/LCG** | při stejném osvitu DN poměr **2,81** (manuál slibuje gain ratio 3,01) |
| **DN scale LN** | v **živém proudu** LN=1 čte **~0,83×** DN stejné expozice (2374→1965 DN); na **stillu je LN DN-neutrální** (řízený test 2×: poměr 0,996) a nemění ani σ — posun platí jen pro live view, tedy pro metering náhledu |
| `get_ExpoAGainRange` | **(100, 10000, 100) = procenta**, ne permile → viz oprava GAIN_UNIT níže |

**Důsledek pro aplikaci (implementováno 2026-09-20):** při connectu se
automaticky aplikuje **LCG + Low noise** (přesně přepnutelné za chodu
zaškrtáváním v GUI, implicitně zapnuto; obojí se zapisuje do metadat
snímků). Dřívější chování = TICHOULÉ HCG = třetinový full well oproti tomu,
co jsme si mysleli.

**Oprava jednotek gainu (zároveň):** `GAIN_UNIT` byl 1000 (permile odhad),
skutečnost jsou **procenta (100 = 1×)**. Co aplikace nazývala „archivní
gain 1,00×", bylo fyzikálně **GV 1000 = 10×** ⇒ reálně jsme archivovali
při ~1/10 full wellu LCG. Po opravě: „1,00×" = 1,00× = GV 100 = 51 ke−
(LCG). Archivy pořízené před opravou mají v metadatech `gain: 1.0`,
ve skutečnosti ale bylo 10× — DN hodnoty jsou platné, jen popisek lhal.

## (f) LCG/HCG × Low Noise — šum a DR (lcg_hcg_snr.py)

**FÁZE SVĚTLO hotova 2026-09-20** (8 řádků v `data/lcg_hcg_snr.csv`,
graf `lcg_hcg_snr.png`). **Fáze TMA čeká** na zatemnění rigu —
`.venv/bin/python camera_tests/lcg_hcg_snr.py dark` (grafy se doplní,
`… plot` je překreslí bez měření).

Postup: Gain Value 100 (= 1,00×), pro každý mód najít expozici na cílový
medián ROI (20 k / 60 k DN), 5 stillů produkční `capture()`, per-pixel
temporální σ přes ROI zásobník.

| cíl DN | mód | expozice | med DN | σ [DN] | σ/mean |
|---|---|---|---|---|---|
| 20 k | HCG | 534 µs | 20 202 | 264,0 | 1,31 % |
| 20 k | HCG+LN | 534 µs | 20 160 | 264,1 | 1,31 % |
| 20 k | **LCG** | **1 635 µs** | 19 724 | **149,9** | **0,76 %** |
| 20 k | LCG+LN | 1 635 µs | 19 607 | 149,6 | 0,76 % |
| 58 k | HCG | 1 635 µs | 58 618 | 446,9 | 0,76 % — **max 65 049, dosahuje rail** |
| 58 k | LCG | 5 002 µs | 57 859 | 255,8 | 0,44 % |

**Co měření říká:**

1. **LCG při stejném DN má σ ~1,76× nižší** (264→150; 447→256 — oba cíle
   shodně). Je to dané e/ADU: oba módy jsou při ms expozicích
   **shot-noise-limitované** (σ/mean odpovídá √(DN·e/ADU)/DN — LCG 0,76 %
   sedí na 0,77 e/ADU), takže nižší e/ADU = větší náboj na stejném DN =
   nižší relativní šum. Žádný read-noise split se při tomto světle neuplatní.
2. **Expoziční headroom LCG je 3,06×** (1 635/534 µs na stejný signál) —
   přesně odpovídá manuálovému gain ratio 3,01. HCG při 58 k DN už naráží
   na rail (65 049): malý full well 16,5 ke− se nevyplatí.
3. **Low noise na stillu nic nedělá:** σ 264,0 vs 264,1 a 149,9 vs 149,6;
   DN medián poměr 0,996. Volí se jen pro live view (a tam jen škodí —
   poloviční fps; viz (e)). Manuálové „DN scale ~0,83×" platí pro proud,
   ne pro snímek.
4. Stejné závody jako u teploty v (c): při ms expozicích valcuje výsledek
   statistika fotonů, ne elektronika. Split módů je vidět jen v tmě
   (read noise) a u DR (full well) — obojí ve prospěch LCG.

**Verdik (pro (c) / filmscan-studio):** výchozí **LCG + Low noise zapnuto**
je správně — LCG je pro osvícený rig jednoznačně lepší (3× full well i 3×
expoziční čas, nižší relativní šum), LN je na stillu neškodný (nepozřen
žádný trest kromě fps náhledu, které při snímání stillu stejně nepotřebujeme
— při preview ale LN vypínejte, pokud chcete plynulý náhled). **Nejnižší gain
(GV 100 = 1,00×) + ETTR** potvrzeno: při 20 k DN už jsme hluboko pod
rail-em, čili gain zvedat nemusíme. **Averaging** zůstává přínosný jen do
K≈4–8 (viz (b) — nad to limituje nestabilita světla, ne šum).

---

## Poznámky k měření

- Live rámce tohoto modelu hlásí `expotime = 0` ve frame info — stampy
  nejsou použitelné; skutečná expozice se prokazuje DN linearitou.
- `expose_to_target` v `_common.py` respektuje kvantizovaný spodek (viz (a))
  a nastavuje čas primou cestou přes SDK (app-cesta by pod 300 µs clampla).
- Still capture běží produkční cestou `TouptekCamera.capture()` včetně
  RAW kontraktu a readback poznámek.
- Syrová data: `data/min_exposure_*.csv`, `data/averaging_snr.csv`
  (+ zásobník 30 ROI polí v `data/averaging_frames/`),
  `data/temperature_snr.csv`.
