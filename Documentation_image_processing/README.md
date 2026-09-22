# Dokumentace obrazového zpracování — filmscan-studio

Návrhová dokumentace (žádný kód) pro **monochrom digitizér „vyvolávač"**: jak z kvantitativních
skenů (16bit raw, dark, flat, film base) vytvořit fyzikálně co nejpřesnější, reprodukovatelný
digitální pozitiv.

## Dokumenty

| Dokument | Obsah |
|---|---|
| [01_fyzikalni_model.md](01_fyzikalni_model.md) | Měřicí vrstva: T = (raw−dark)/flat, D = −log₁₀T, korekce Dmin, archiv hustotního TIFFu, chybová analýza na reálných datech B1 |
| [02_rozbor_negadoctor.md](02_rozbor_negadoctor.md) | Rozbor matematiky darktable negadoctoru (zdroják `src/iop/negadoctor.c`): co je použitelné pro mono transmisi, co je pouze barevné, co nahradit naším dark/flat měřením |
| [03_rendering_vrstva.md](03_rendering_vrstva.md) | Návrh rendering vrstvy: hustotní doména, parametry (exposure/contrast/black point/white point/toe/shoulder/gamma), režimy A (univerzální) / B (emulze) / C (vlastní H-D) — rozhraní |
| [04_plan_implementace.md](04_plan_implementace.md) | Plan pro developer: fáze, moduly, formáty, sidecary, testy, otevřené otázky |
| [05_darktable_source_kopy.md](05_darktable_source_kopy.md) | Kopie klíčových zdrojových souborů darktable + rozbor (fixace zdroje pravdy) |
| [06_roi_metadata.md](06_roi_metadata.md) | **Požadavek na akvizici:** `image_rect` v metadatech; záchrana Shift+kreslením v GUI |
| [07_rendering_profile_a_histogramy.md](07_rendering_profile_a_histogramy.md) | Rendering profil a histogramy |
| [08_ui_ladeni_dmin_dmax_a_krivky.md](08_ui_ladeni_dmin_dmax_a_krivky.md) | UI ladění Dmin/Dmax a křivek |
| [09_anotace_a_exif.md](09_anotace_a_exif.md) | **Anotátor `filmscan-annotate`:** blok `annotation` v sidecaru, sekvenční minuty, editovatelná akvizice + film, EXIF kontrakt pro export v developeru |

## Implementace (stav 2026-09-18)

První řez vyvolávače je v kódu — akviziční pipeline se nedotkla:

- `core/density.py` — měřicí vrstva (T per-pixel flat, D float32 s NaN, Dmin z měření, archiv TIFF)
- `core/render.py` — rendering z hustoty (parametry v [D], fingerprint)
- `developer/project.py` — otevření složky projektu, automatická detekce dark/flat/base, ořez na ROI
- `developer/gui.py` — `uv run filmscan-develop-gui [složka]`: náhled, slidery, **Shift+kreslení ROI**, exporty
- `calibration.stack_darks` — sjednocení časů darků před mediánem (nález z B1)
| [05_darktable_source_kopy.md](05_darktable_source_kopy.md) | Kopie zdrojových souborů darktable (negadoctor.c/.cl, spektrafilm.c) + mapa parametrů na naše veličiny, ověření absence film-specific křivek, pozice spektrafilmu, licence |

Zdrojové podklady:

- `darktable_source/` — fixované kopie relevantních souborů (upstream `nightly-4-g2c42f30809`, GPL v3)
- `darktable user manual - negadoctor.html` — manuál modulu (oficiální docs)
- upstream [darktable-org/darktable](https://github.com/darktable-org/darktable) — kompletní zdrojáky darktable (lokální checkout)
- lokální archív skenů `B1` — reálná ukázková data (6 scanů, 5 dark, 9 flat, 2 base, 2026-09-18; nejsou v repu)
- Uživatelský nástin: `2026 Monochrom digitizér software part.md` (poznámkový blok)

## Rozhodnutí (2026-09-18)

1. **Vlastní Python developer** — rendering běží v filmscan-studio, ne v darktable-cli;
   matematika negadoctoru slouží jako inspirace/ověřený protějšek.
2. **Archiv = optická hustota D, float32 TIFF** — měřicí vrstva je archivní artefakt;
   pozitiv je odvozený rendering (16bit) s fingerprintem parametrů.
3. **Režim A teď, B/C jen rozhraní** — registraturu H-D křivek navrhneme, obsah (emulze
   Fomapan/HP5+/Tri-X, vlastní měření) se plní až po naměření dat.
4. **Akvizice je vyřešená** — dodá raw, dark, flat (light), měření filmu base s metadaty;
   ROI s obrazovými daty je odlišena od tmavého okraje. Na straně akvizice nic neměníme.
5. Dokumentace česky, v této složce; UI texty repa zůstávají česky (viz pravidla projektu).
