"""Metadata → EXIF/XMP pro exportní JPEG a HEIC ( kontrakt: dok. 09 ).

Standardizovaná data jdou do standardních tagů — čtečka fotek je vidí bez
znalosti Filmscan Studio:

    Make / Model          ← film.camera (foťák na filmu, Make=první token)
    LensModel             ← film.shooting_lens
    ISOSpeedRatings       ← film.film_iso (text "400/27°" → 400)
    DateTimeOriginal      ← annotation.capture_datetime (datum záběru)
    DateTimeDigitized     ← acquisition.capture_date (čas na riggu)
    GPS IFD               ← annotation.gps_lat / gps_lon (+ refs)
    Rating/RatingPercent  ← annotation.rating (0–5 → 0–100 %)
    ImageDescription      ← annotation.title
    Orientation           ← annotation.rotation_degrees — developer rotaci
                            do pixelů NEzapéká (filmové příznaky ano), tag
                            ji naopak nařídí čtečce; výsledek je totéž co
                            otočený náhled anotátoru
    Software              ← Filmscan Studio

Zbytek (digitalizační kamera/objektiv/expozice/vývojka/push/…) putuje za
uživatelský komentář do UserComment jako " ; Key: value" — komentář je VŽDY
první, prázdné kusy se přeskočí, středník uvnitř hodnoty se nahradí "–", aby
szpětné dělení podle "; " zůstalo jednoznačné. Popisky kusů jsou anglické
(rozhodnutí uživatele: UI česky, metadata machine-neutral). Tagy jdou dvakrát:
XMP dc:subject (Lightroom) i textově v komentáři (čtečky bez XMP).

Vrací se hotové BYTY — writeři v icc.py je jen předají zapisovačům. TIFF cesta
se nemění (JSON v ImageDescription, dok. 07): exiftool TIFF EXIF pod tagem
34665 nečitl (změřeno 2026-09-22), takže TIFF zůstává u popisku.
"""
from __future__ import annotations

import re
from fractions import Fraction

from PIL.Image import Exif
from PIL.TiffImagePlugin import IFDRational

SOFTWARE_AGENT = "Filmscan Studio"

#: EXIF Orientation pro rotace náhledu (annotation.rotation_degrees, CW).
_ORIENTATION_FOR_ROTATION = {90: 6, 180: 3, 270: 8}

_SPLIT_CAMERA = re.compile(r"^(?P<make>\S+)\s+(?P<model>.+)$")


def _clean(value: object) -> str:
    """Text bez středníků — oddělovač kusů UserComment musí zůstat jediný."""
    text = str(value).strip() if value is not None else ""
    return text.replace(";", "–")


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
    """'Nikon FM2' → ('Nikon', 'FM2'); jeden token → Make ''.

    Stejná heuristika jako load-camera v anotátoru — EXIF chce Make a Model
    zvlášť a operátor zadává 'značka model'."""
    s = re.sub(r"\s+", " ", str(text).strip()) if text else ""
    m = _SPLIT_CAMERA.match(s)
    return ("", s) if not m else (m.group("make"), m.group("model").strip())


def _dms_triple(magnitude: float) -> tuple:
    """Desítkový stupeň → (deg, min, sec) IFDRational; sekundy na 9 DESetin."""
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
        "rotation_degrees": ann.get("rotation_degrees") or 0,
        # film — čím a na co exponováno + razítko dílny
        "camera": film.get("camera"),
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


def _comment_pieces(meta: dict) -> list:
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


def user_comment_text(record: dict) -> str:
    """UserComment: komentář uživatele FIRST, pak ' ; ' kusy (dok. 09)."""
    meta = _collect(record)
    pieces = ([_clean(meta["note"])] if meta["note"] else []) \
        + _comment_pieces(meta)
    return " ; ".join(p for p in pieces if p)


def _encode_comment(text: str) -> bytes:
    """UserComment musí být RAW bajty s prefixem znakové sady — Pillow
    (re)kóduje jen str, a ASCII/UNICODE prefix je povinnost Exifu. Čeština
    jde UTF-16BE (ověřeno exiftool 2026-09-22)."""
    try:
        text.encode("ascii")
        return b"ASCII\x00\x00\x00" + text.encode("ascii")
    except UnicodeEncodeError:
        return b"UNICODE\x00" + text.encode("utf-16-be")


def build_exif_bytes(record: dict) -> bytes | None:
    """EXIF APP1 byty z sidecaru záznamu; None když není co zapsat.

    `record` je celý sidecar dict (film/acquisition/annotation) — viz
    developer.project.FrameEntry.record."""
    meta = _collect(record)
    exif = Exif()
    exif_ifd = exif.get_ifd(0x8769)
    used = False

    if meta["title"]:
        exif[0x010E] = meta["title"]                       # ImageDescription
        used = True
    make, model = split_camera(meta["camera"])
    if model:
        exif[0x0110] = model                               # Model ← foťák
        used = True
    if make:
        exif[0x010F] = make                                # Make ← značka
        used = True
    iso = parse_iso(meta["film_iso"])
    if iso:
        exif_ifd[0x8827] = iso                             # ISOSpeedRatings
        used = True
    if meta["shooting_lens"]:
        exif_ifd[0xA434] = _clean(meta["shooting_lens"])   # LensModel
        used = True

    shot_dt = _exif_str(meta["capture_datetime"])
    if shot_dt:
        exif_ifd[0x9003] = shot_dt                         # DateTimeOriginal
        used = True
    dev_dt = _exif_str(meta["dev_date"])
    if dev_dt:
        exif_ifd[0x9004] = dev_dt        # DateTimeDigitized (0x900D je
        used = True                      # FormattingSensitivity — past hlídá)

    comment = user_comment_text(record)
    if comment:
        exif_ifd[0x9286] = _encode_comment(comment)        # UserComment
        used = True

    rating = meta["rating"]
    if isinstance(rating, int) and 0 <= rating <= 5:
        exif_ifd[0x4746] = rating                          # Rating 0–5
        exif_ifd[0x4747] = round(rating / 5 * 100)         # RatingPercent
        used = True

    orientation = _ORIENTATION_FOR_ROTATION.get(
        int(meta["rotation_degrees"] or 0) % 360)
    if orientation:
        exif[0x0112] = orientation
        used = True

    gps = {}
    lat = gps_to_dms(meta["gps_lat"], "lat", meta["gps_lat_ref"])
    lon = gps_to_dms(meta["gps_lon"], "lon", meta["gps_lon_ref"])
    if lat:
        gps[1], gps[2] = lat
    if lon:
        gps[3], gps[4] = lon
    if lat and lon:
        gps[0] = b"\x02\x03\x00\x00"                       # GPSVersionID 2.3
    if gps:
        exif.get_ifd(0x8825).update(gps)
        used = True

    if not used:
        return None
    exif[0x0131] = SOFTWARE_AGENT                          # Software
    return exif.tobytes()


def build_xmp_bytes(record: dict) -> bytes | None:
    """XMP packet s dc:subject (štítky) + xmp:Rating; None když nic z toho.

    Některé čtečky (macOS Foto) čtou hodnocení jen z jednoho z dvojic —
    proto EXIF Rating i xmp:Rating (otevřená otázka dok. 09: odpověď ano)."""
    meta = _collect(record)
    tags = [str(t).strip() for t in meta["tags"] if str(t).strip()]
    parts = []
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
