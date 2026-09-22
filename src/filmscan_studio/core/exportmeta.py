"""Metadata → EXIF/XMP pro exportní JPEG a HEIC ( kontrakt: dok. 09 ).

Standardizovaná data jdou do standardních tagů — čtečka fotek je vidí bez
znalosti Filmscan Studio:

    Make / Model          ← film.camera_make / film.camera_model (přímá
                            pole z anotátoru); fallback: heuristicý rozpad
                            volného textu film.camera (see split_camera)
    LensModel             ← film.shooting_lens
    ISOSpeedRatings       ← film.film_iso (text "400/27°" → 400)
    DateTimeOriginal      ← annotation.capture_datetime (datum záběru)
    DateTimeDigitized     ← acquisition.capture_date (čas na riggu)
    GPS IFD               ← annotation.gps_lat / gps_lon (+ refs)
    Rating/RatingPercent  ← annotation.rating (0–5 → 0–100 %)
    ImageDescription      ← celý popis (komentář + strojní kusy; viz níže)
    Software              ← Filmscan Studio

XMP nese co EXIF neumí (ASCII-only tagy by češtinu zmlaskly — změřeno):
dc:title (název — první kolonka Bridge), dc:description (popis v UTF-8) a
dc:subject (štítky); xmp:Rating k EXIF Ratingu (čtečky čtou jen jednu z dvojic).

POPIS (rozkaz 2026-09-22: „do ImageDescription dej to, cos dal do usercomment;
to, cos dal do ImageDescription, patří do Title"): komentář uživatele je VŽDY
první, pak „ ; Key: value" kusy toho, co nemá vlastní tag — prázdné kusy se
přeskočí, středník uvnitř hodnoty se nahradí „–", aby dělení podle „ ; "
zůstalo jednoznačné. Popisky kusů anglicky (UI česky, metadata strojově
neutralní). Do EXIF ImageDescription jde ASCII-fold verze (ě→e), plné UTF-8
texty žijí v XMP.

Rotation_degrees se zapéká do pixelů přímo vývojářem (rozkaz 2026-09-22),
EXIF Orientation se tedy NEZAPISUJE — tag by se s otočenými pixely sečetl
a čtečka by otočila podruhé.

Vrací se hotové BYTY — writeři v icc.py je jen předají zapisovačům. TIFF cesta
se nemění (JSON v ImageDescription, dok. 07): exiftool TIFF EXIF pod tagem
34665 nečitl (změřeno 2026-09-22), takže TIFF zůstává u popisku.
"""
from __future__ import annotations

import re
import unicodedata
from fractions import Fraction

from PIL.Image import Exif
from PIL.TiffImagePlugin import IFDRational

SOFTWARE_AGENT = "Filmscan Studio"


def _clean(value: object) -> str:
    """Text bez středníků — oddělovač kusů popisu musí zůstat jediný."""
    text = str(value).strip() if value is not None else ""
    return text.replace(";", "–")


def _ascii_fold(text: str) -> str:
    """Přes ASCII bez '?' — EXIF tagy jsou latentně ASCII a Pillow by
    ěščřžý srazil na otazníky (změřeno). NFKD rozklad + zahození diakritiky:
    'vyvolávka' → 'vyvolavka'. Pomlčka se musí vyměnit DŘÍV, než ji
    ascii/ignore zahodí — je to náhrada středníku v popisu, ztrácet ji
    nechceme. Plná verze zůstává v XMP."""
    decomposed = unicodedata.normalize("NFKD", text.replace("–", "-"))
    folded = "".join(c for c in decomposed
                     if not unicodedata.combining(c))
    return folded.encode("ascii", "ignore").decode("ascii")


def _exif_str(raw: object) -> str:
    """EXIF tvar 'YYYY:MM:DD HH:MM:SS'; ISO 'T' → mezera, sekundy doplnit.

    Snese i ISO 8601 s pásmem z acquisition.capture_date
    ('2026-09-20T16:53:17.956137+02:00'). Vrací "" pro nic, holý rok nebo
    nečitelné ('cca 2015') — ty do EXIFu nepatří."""
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return ""
    m = re.match(
        r"^(\d{4})[-:.](\d{1,2})[-:.](\d{1,2})(?:[T ](\d{1,2}):(\d{2})(?::(\d{2}))?)?",
        text,
    )
    if not m:
        return ""
    year, month, day, hour, minute, second = m.groups()
    date = f"{int(year):04d}:{int(month):02d}:{int(day):02d}"
    if hour is None:
        return f"{date} 00:00:00"
    return f"{date} {int(hour):02d}:{int(minute):02d}:{int(second or 0):02d}"


def parse_iso(text: object) -> int | None:
    """'400', '400/27°', 'HP5 @ 1600' → int; None když nic.

    Číslo nesmí být nalepené na písmeno (5 z 'HP5' není ISO). Norma
    ISO/DIN '400/27°' má citlivost první, push poznámka 'HP5 @ 1600'
    poslední — pair tvar se rozezná a vezme se z něj ISO, jinak poslední
    samostatné číslo."""
    s = str(text) if text is not None else ""
    pair = re.match(r"^\s*(\d{2,4})\s*/\s*\d{2}\s*°?", s)
    if pair:
        return int(pair.group(1))
    found = re.findall(r"(?<![A-Za-z\d])\d+", s)
    return int(found[-1]) if found else None


def split_camera(text: object) -> tuple[str, str]:
    """Rozpad volného textu foťáku na (Make, Model).

    Vyhlášená past (uživatel 2026-09-22): „ERNST LEITZ WETZLAR GMBH Leica R4s
    MOD.2" není dělitelný mezerou — make je celý uppercase-run, model zbytek.
    'Nikon FM2' zůstává první token / zbytek. Pravidla:

    * ≥2 úvodní tokeny OPS (každý obsahuje písmena a je celý VELKÝ) a něco
      za nimi → make = run, model = zbytek ('ERNST LEITZ WETZLAR GMBH' |
      'Leica R4s MOD.2'; 'OLYMPUS' | 'OM-1' je jeden token → pravidlo 2),
    * jinak make = první token, model = zbytek ('Nikon' | 'FM2',
      'OLYMPUS' | 'OM-1', 'PENTAX' | 'AUTO 1000'),
    * jeden token → make '' a model on sám ('Zenit').

    Anotátor i migrace sidecarů volají TOHLE — jeden zdroj pravdy."""
    s = re.sub(r"\s+", " ", str(text).strip()) if text else ""
    tokens = s.split(" ")
    if len(tokens) < 2:
        return "", s

    def is_upper_word(token: str) -> bool:
        return any(c.isalpha() for c in token) and token == token.upper()

    run = 0
    for token in tokens:
        if is_upper_word(token):
            run += 1
        else:
            break
    if run >= 2 and run < len(tokens):
        return " ".join(tokens[:run]), " ".join(tokens[run:])
    return tokens[0], " ".join(tokens[1:])


def _dms_triple(magnitude: float) -> tuple:
    """Desítkový stupeň → (deg, min, sec) IFDRational."""
    degrees = int(magnitude)
    rest = (magnitude - degrees) * 60
    minutes = int(rest)
    # sekundy na setinky vteřiny — dál je jen šum floatu (0,1" ≈ 3 m);
    # zaokrouhlení teprve teď může vyvolat carry 59,9→60 → 60' → 1°
    seconds = Fraction(round((rest - minutes) * 60 * 10), 10)
    if seconds >= 60:  # zaokrouhlení 59,9999… → 60 by neplatný EXIF
        minutes += 1
        seconds = Fraction(0)
    if minutes >= 60:  # carry až PO zaokrouhlení (49.9999999… → 50°00'00")
        degrees += 1
        minutes = 0
    return (IFDRational(degrees, 1), IFDRational(minutes, 1),
            IFDRational(seconds.numerator, seconds.denominator))


def gps_to_dms(value: object, axis: str,
               ref_hint: object = None) -> tuple[str, tuple] | None:
    """'49.11243060' / '49.11243N' / '-16.382' → (ref, DMS trojice).

    Ref přednostně z annotation.gps_*_ref (anotátor je ukládá), jinak podle
    suffixu, jinak podle znaménka. axis 'lat' → N/S, 'lon' → E/W. None když
    hodnota není číslo."""
    raw = str(value).strip() if value is not None else ""
    if not raw:
        return None
    suffix = raw[-1].upper() if raw and raw[-1].upper() in "NSEW" else ""
    body = raw[:-1] if suffix else raw
    try:
        number = float(body.replace(",", ".").strip())
    except ValueError:
        return None
    valid = ("N", "S") if axis == "lat" else ("E", "W")
    ref = str(ref_hint).strip().upper() if ref_hint else ""
    if ref not in valid:
        # suffix > znaménko; holé minus bez suffixu je jednoznačně S/W
        ref = suffix if suffix in valid else (valid[0] if number >= 0
                                              else valid[1])
    return ref, _dms_triple(abs(number))


def _collect(record: dict) -> dict:
    """Splácne film+acquisition+annotation sidecaru do jedné perspektivy."""
    film = record.get("film") or {}
    acq = record.get("acquisition") or {}
    ann = record.get("annotation") or {}
    return {
        # annotation — co fotka je
        "title": ann.get("title") or "",
        "note": ann.get("note") or "",
        "rating": ann.get("rating"),
        "tags": ann.get("tags") or [],
        "capture_datetime": ann.get("capture_datetime") or "",
        "gps_lat": ann.get("gps_lat"),
        "gps_lon": ann.get("gps_lon"),
        "gps_lat_ref": ann.get("gps_lat_ref"),
        "gps_lon_ref": ann.get("gps_lon_ref"),
        # film — čím a na co exponováno + razítko dílny
        "camera": film.get("camera"),
        "camera_make": film.get("camera_make"),
        "camera_model": film.get("camera_model"),
        "shooting_lens": film.get("shooting_lens"),
        "film_iso": film.get("film_iso"),
        "film_name": film.get("film_name"),
        "format": film.get("format"),
        "development": film.get("development"),
        "content": film.get("content"),
        "film_notes": film.get("notes"),
        "pushed_stops": film.get("pushed_stops") or 0,
        "expiry": film.get("expiry"),
        "box": film.get("box_number"),
        "operator": film.get("operator"),
        "digitising_lens": film.get("digitising_lens"),
        "digitisation_date": film.get("digitisation_date"),
        # acquisition — strojové razítko rigu
        "dev_camera": acq.get("camera"),
        "dev_date": acq.get("capture_date") or "",
        "exposure": acq.get("exposure_time"),
        "gain": acq.get("gain"),
        "frames_avg": acq.get("frames_averaged"),
        "conversion_gain": acq.get("conversion_gain"),
    }


def _fmt_number(value: object) -> str:
    """Float bez kožichu: 0.100000001490116 → '0.1', 2.0 → '2'."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return _clean(value)
    return f"{f:g}"


def _description_pieces(meta: dict) -> list:
    """Kusy bez vlastního EXIF tagu, anglické popisky (dok. 09 + strojová
    neutralita metadat). Hodnoty bez středníků, prázdné vynechat."""
    film_label = _clean(meta["film_name"])
    if meta["format"]:
        film_label = f"{film_label} ({_clean(meta['format'])})" if film_label \
            else _clean(meta["format"])
    push = meta["pushed_stops"]
    try:
        push = float(push)
    except (TypeError, ValueError):
        push = 0.0
    tags = ", ".join(str(t).strip() for t in meta["tags"] if str(t).strip())
    pairs = (
        ("Film", film_label),
        ("Content", _clean(meta["content"])),
        ("Film note", _clean(meta["film_notes"])),
        ("Development", _clean(meta["development"])),
        ("Push", f"+{push:g} EV" if push else ""),
        ("Digitising camera", _clean(meta["dev_camera"])),
        ("Digitising lens", _clean(meta["digitising_lens"])),
        ("Exposure time", f"{_fmt_number(meta['exposure'])}s"
                          if meta["exposure"] else ""),
        ("Gain", _fmt_number(meta["gain"]) if meta["gain"] is not None else ""),
        ("Frames averaged", str(meta["frames_avg"])
                            if meta["frames_avg"] not in (None, 0, 1) else ""),
        ("Conversion gain", _clean(meta["conversion_gain"])),
        ("Operator", _clean(meta["operator"])),
        ("Expiry", _clean(meta["expiry"])),
        ("Box", _clean(meta["box"])),
        ("Digitised", _exif_str(meta["digitisation_date"])),
        ("Tags", tags),
    )
    return [f"{key}: {value}" for key, value in pairs if key and value]


def description_text(record: dict) -> str:
    """Popis snímku: komentář uživatele FIRST, pak ' ; ' kusy (dok. 09).

    Jde do EXIF ImageDescription (ASCII-fold) a do XMP dc:description (plné
    UTF-8). Dřív UserComment — ten čtečky stejně netušily (rozkaz 2026-09-22)."""
    meta = _collect(record)
    pieces = ([_clean(meta["note"])] if meta["note"] else []) \
        + _description_pieces(meta)
    return " ; ".join(p for p in pieces if p)


def _camera_parts(meta: dict) -> tuple[str, str]:
    """(Make, Model): přímá pole anotátoru mají přednost, fallback je
    heuristický rozpad volného `camera` (split_camera)."""
    make = _clean(meta["camera_make"])
    model = _clean(meta["camera_model"])
    if make or model:
        return make, model
    return split_camera(meta["camera"])


def build_exif_bytes(record: dict) -> bytes | None:
    """EXIF APP1 byty z sidecaru záznamu; None když není co zapsat.

    `record` je celý sidecar dict (film/acquisition/annotation) — viz
    developer.project.FrameEntry.record."""
    meta = _collect(record)
    exif = Exif()
    exif_ifd = exif.get_ifd(0x8769)
    used = False

    description = _ascii_fold(description_text(record))
    if description:
        exif[0x010E] = description          # ImageDescription ← celý popis
        used = True
    make, model = _camera_parts(meta)
    if model:
        exif[0x0110] = _ascii_fold(model)                # Model
        used = True
    if make:
        exif[0x010F] = _ascii_fold(make)                  # Make
        used = True


    iso = parse_iso(meta["film_iso"])
    if iso:
        exif_ifd[0x8827] = iso                           # ISOSpeedRatings
        used = True
    if meta["shooting_lens"]:
        exif_ifd[0xA434] = _ascii_fold(
            _clean(meta["shooting_lens"]))               # LensModel
        used = True

    shot_dt = _exif_str(meta["capture_datetime"])
    if shot_dt:
        exif_ifd[0x9003] = shot_dt                       # DateTimeOriginal
        used = True
    dev_dt = _exif_str(meta["dev_date"])
    if dev_dt:
        exif_ifd[0x9004] = dev_dt      # DateTimeDigitized (0x900D je
        used = True                    # FormattingSensitivity — past hlídá)

    rating = meta["rating"]
    if isinstance(rating, int) and 0 <= rating <= 5:
        exif_ifd[0x4746] = rating                        # Rating 0–5
        exif_ifd[0x4747] = round(rating / 5 * 100)       # RatingPercent
        used = True

    # Orientation se NEPIŠE: vývoják rotaci zapéká do pixelů (dok. 09),
    # tag by u čtečky způsobil druhé otočení.

    gps = {}
    lat = gps_to_dms(meta["gps_lat"], "lat", meta["gps_lat_ref"])
    lon = gps_to_dms(meta["gps_lon"], "lon", meta["gps_lon_ref"])
    if lat:
        gps[1], gps[2] = lat
    if lon:
        gps[3], gps[4] = lon
    if lat and lon:
        gps[0] = b"\x02\x03\x00\x00"                     # GPSVersionID 2.3
    if gps:
        exif.get_ifd(0x8825).update(gps)
        used = True

    if not used:
        return None
    exif[0x0131] = SOFTWARE_AGENT                        # Software
    return exif.tobytes()


def build_xmp_bytes(record: dict) -> bytes | None:
    """XMP packet: dc:title (název — první kolonka Bridge), dc:description
    (popis v UTF-8, verze bez ztráty diakritiky), dc:subject (štítky) a
    xmp:Rating; None když je vše prázdné.

    Některé čtečky (macOS Foto) čtou hodnocení jen z jednoho z dvojic —
    proto EXIF Rating i xmp:Rating (otevřená otázka dok. 09: odpověď ano)."""
    meta = _collect(record)
    tags = [str(t).strip() for t in meta["tags"] if str(t).strip()]
    parts = []
    if meta["title"]:
        parts.append(
            f'<dc:title><rdf:Alt><rdf:li xml:lang="x-default">'
            f'{_xml_escape(meta["title"])}</rdf:li></rdf:Alt></dc:title>')
    description = description_text(record)
    if description:
        parts.append(f"<dc:description><rdf:Alt>"
                     f'<rdf:li xml:lang="x-default">'
                     f"{_xml_escape(description)}</rdf:li>"
                     f"</rdf:Alt></dc:description>")
    if tags:
        items = "".join(f"<li>{_xml_escape(t)}</li>" for t in tags)
        parts.append(f"<dc:subject><rdf:Seq>{items}</rdf:Seq></dc:subject>")
    rating = meta["rating"]
    if isinstance(rating, int) and 1 <= rating <= 5:
        parts.append(f"<xmp:Rating>{rating}</xmp:Rating>")
    if not parts:
        return None
    body = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:xmp="http://ns.adobe.com/xap/1.0/">'
        + "".join(parts)
        + "</rdf:Description></rdf:RDF></x:xmpmeta>"
    )
    return (
        "<?xpacket begin='﻿' id='W5M0MpCehiHzreSzNTczkc9d'?>"
        + body
        + "<?xpacket end='w'?>"
    ).encode("utf-8")


def _xml_escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))
