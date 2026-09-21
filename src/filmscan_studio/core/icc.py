"""ICC profil gray gamma pro display-referred exporty (dokument 07).

Render-chain končí ``apply_display()``: pixely jsou gray, enkóvané mocninou
``x ** (1/gamma_display)``. Dokument 07 §4 žádá, aby každý display-referred
export (pozitivní 16b TIFF, 8b JPEG) nesl ICC profil, který říká přesně
tohle -- žádná další gamma do pixelů, jen interpretace pro Photoshop a
spol. Profil se proto *zakládá*, ne *převádí*: pixely zůstávají bajty,
jak je quantisoval render.

Proč generovaný a ne stažený soubor:

* je deterministický (stejná gamma => stejné bajty => fingerprint sedí),
* je skutečně monochromatický ``GRAY`` s jedním ``kTRC`` — ne RGB se třemi
  shodnými kanály, jak dokument 07 výslovně požaduje,
* TRC je přesná mocnina (parametrický typ ``para``, gamma v
  zařízením→linkárním směru), tedy přesně transfer ``apply_display()`` —
  ne aproximace tabulkou 1024 hodnot jako u generických profilů,
* defaultní profil ``Gray Gamma 2.2`` sedí na defaultní ``gamma_display``.
  Uživatel 2026-09-21: výstupní gamma už se v UI nedá měnit — je definována
  profilem; ``profile_for()`` to drží i pro případné budoucí změny.

Formát: ICC v2.10, třída ``mntr`` (zobrazovací zařízení), barevný prostor
``GRAY``, PCS ``XYZ``, bílý bod D65. Tagy ``desc``, ``cprt``, ``wtpt``,
``kTRC`` — totéž minimum, co nesou komerční "Gray Gamma 2.2" profily;
lcms2 (Pillow ImageCms) profil čte a Photoshop ho pozná.
"""

from __future__ import annotations

import hashlib
import io
import struct
from pathlib import Path

#: Display gamma, kterou definuje profil (uživatel 2026-09-21: možnost
#: upravovat výstupní gammu zmizela z UI — je definována profilem).
GAMMA_DISPLAY = 2.2

#: Popis kódování dat v metadatech exportů (dokument 07 §8 bod 7).
ENCODING_GRAY_GAMMA_22 = "gray-gamma-2.2"
#: Flat není display-referred: lineární v čisté hustotě, bez gammy, bez profilu.
ENCODING_LINEAR_DENSITY = "linear-density"

#: TIFF tag InterColorProfile, kudy putuje ICC bytes do TIFFu.
TIPTAG_ICCPROFILE = 34675

def _bcd_date(year: int, month: int, day: int,
              hours: int, minutes: int, seconds: int) -> bytes:
    """display date (offset 24, 12 B) podle ICC.1: sedm BCD číslic.

    Nulové pole i binárně zapsané hodnoty ColorSync (Preview, Quick Look,
    Finder, `sips`) tichý znetvoří — nevyhodí chybu, jen špatně aplikuje
    TRC. V ICC v4 je datum binární, v našem v2 profilu musí být BCD.
    Konstanta, ne now(): exporty musí zůstat deterministické.
    """
    def bcd(v: int) -> int:
        return ((v // 10) << 4) | (v % 10)
    year_bcd = (bcd(year // 100) << 8) | bcd(year % 100)
    return (struct.pack(">H", year_bcd)
            + bytes(map(bcd, (month, day, hours, minutes, seconds)))
            + bytes(5))


_PROFILE_DATE = _bcd_date(2026, 9, 21, 12, 0, 0)

# ICC header: 0 size, 4 cmm, 8 version, 12 class, 16 colorspace, 20 pcs,
# 24 display date (12 B), 36 'acsp', 40 flags, 44 manufacturer, 64 intent.
#
# CHYBA 2026-09-21 (uživatel: „mono jpeg šel otevřít jen v ImageJ, jinak
# solarizace; srgb jpeg dovně barevný; heic zrovnatak“): v hlavičce byl
# manufacturer 'FLMR'. macOS ColorSync na to NEREAGUJE chybou — profil
# tiše znetvoří: gray TRC aplikuje poškozeně (rampa 0→17→181→238→96,
# „solarizace“), RGB profil zkolabuje na škálu 0..3 s R−B posunem 253
# („dovně barevný“). lcms2 (ImageJ, Photoshop, Pillow) 'FLMR' v pohodě
# snáší — proto profil prošel všemi testy i ImageJ a prasklo to až v
# Náhledu/Finderi. Řešení: manufacturer i model nechávat nulové/'none'
# (přesně to dělají Apple i lcms2 profily; decisive test: stejný profil
# s 'FLMR' rozbitý, s nulou OK, na gray i RGB, nezávisle na datu).

#: Název profilu, jak ho uvidí Photoshop/ColorSync.
PROFILE_NAME = "Gray Gamma 2.2"


def _s15f16(value: float) -> int:
    """s15Fixed16Number jako int32 zápis (může být záporný — chad matice)."""
    v = int(round(value * 65536.0))
    return v - 0x100000000 if v >= 0x80000000 else v


def _pad(data: bytes) -> bytes:
    """Zarovnění na 4 bajty, jak ICC vyžaduje."""
    return data + b"\x00" * ((4 - len(data) % 4) % 4)


def _text_value(sig: bytes, text: str) -> bytes:
    """textDescriptionType (v2): signature, reserved, count, ASCII + NUL."""
    data = text.encode("ascii", errors="replace") + b"\x00"
    return _pad(sig + b"\x00\x00\x00\x00" + struct.pack(">I", len(data))
                + data)


def build_gray_gamma_profile(gamma: float = GAMMA_DISPLAY,
                             name: str = PROFILE_NAME) -> bytes:
    """Sestrojit monochromatický gray profil s TRC = mocnina 1/gamma.

    Každá hodnota tagu začíná signaturou svého datového typu (``desc``,
    ``XYZ ``, ``para``) — ne názvem tagu; to je nejčastější chyba ručního
    zápisu a lcms2 takový profil zamítne.

    Bílý bod D65: display gray profil uvádí bílou zařízení přímo, adaption
    na D50 se čeká u tiskových profilů. Profil mapuje zařízením->PCS, tedy
    E' -> E = E'^gamma; čtenář (Photoshop) dělá opak, E' = E^(1/gamma) —
    přesně co ``apply_display()`` udělalo s pixely.
    """
    if gamma <= 0:
        raise ValueError(f"gamma must be positive, got {gamma}")
    tags: list[tuple[bytes, bytes]] = [
        (b"desc", _text_value(b"desc", name)),
        (b"cprt", _text_value(b"cprt", "Filmscan Studio - volne pouzitelny")),
        # wtpt: XYZ_Type, bílý bod D65
        (b"wtpt", _pad(b"XYZ " + b"\x00\x00\x00\x00"
                       + struct.pack(">iii", _s15f16(0.9505), _s15f16(1.0),
                                     _s15f16(1.0890)))),
        # kTRC: para_Type funkce 0 -> Y = X^gamma (lineární lomená prázdná);
        # profil je zařízením->PCS, takže čtenář derivací dostane X = Y^(1/g)
        (b"kTRC", _pad(b"para" + b"\x00\x00\x00\x00"
                       + struct.pack(">H", 0) + b"\x00\x00"
                       + struct.pack(">i", _s15f16(gamma))
                       + struct.pack(">iiii", 0, 0, 0, 0))),
    ]
    directory = bytearray()
    body = bytearray()
    off0 = 128 + 4 + 12 * len(tags)
    for sig, value in tags:
        directory += sig + struct.pack(">II", off0 + len(body), len(value))
        body += value
    header = bytearray(128)
    struct.pack_into(">I", header, 0, off0 + len(body))
    header[4:8] = b"none"                    # žádný preferovaný CMM
    header[8:12] = bytes([2, 0x10, 0, 0])    # ICC v2.10 — max. kompatibilita
    header[12:16] = b"mntr"                  # zobrazovací zařízení
    header[16:20] = b"GRAY"                  # skutecne monochromaticky
    header[20:24] = b"XYZ "                  # PCS
    header[24:36] = _PROFILE_DATE            # nesmí být nula — viz komentář
    header[36:40] = b"acsp"
    struct.pack_into(">I", header, 40, 0)    # flags
    # manufacturer (44) zůstává 0, model (48) 'none' — Apple i lcms2 profily
    # to tak mají; vlastní IČO manufacturer ColorSync tiše znetvoří TRC
    header[48:52] = b"none"
    struct.pack_into(">I", header, 64, 0)    # rendering intent: perceptive
    # za hlavičkou MUSÍ předcházet adresáři ještě 4B počet tagů (off0 to
    # počítá; vynechat ji = lcms2 profil zamítne "cannot open profile")
    return (bytes(header) + struct.pack(">I", len(tags))
            + bytes(directory) + bytes(body))


#: Název sRGB profilu pro RGB exporty (kompatibilita — JPEG/HEIC čtečky
#: bez ICC tagu hádají sRGB tak jako tak, tag to jen stvrzuje).
SRGB_PROFILE_NAME = "sRGB IEC61966-2.1"
#: RGB exporty (kompatibilní cesta) — pixely R=G=B, enkóvané jako sRGB.
ENCODING_SRGB = "srgb"
#: 10b HEIC nese totéž co sRGB JPEG, jen hlubší bitovou hloubku.
ENCODING_SRGB_10B = "srgb-10b-hevc"


def build_srgb_profile(name: str = SRGB_PROFILE_NAME) -> bytes:
    """Kompletní RGB profil sRGB IEC61966-2.1 (pro kompatibilní exporty).

    Hodnoty jsou kanonické z profilu IEC 61966-2-1: primáry v D50 PCS
    (matice REC709 adaptovaná Bradfordem), ``wtpt`` D50, ``chad`` D65→D50
    a piecewise TRC (para type 3: g 2,4, a 1/1,055, b 0,055/1,055,
    c 1/12,92 — sklon lineární části, d 0,04045 — mez; Y = (aX+b)^g nad ní,
    cX pod ní). TRC je *skutečná* sRGB křivka, ne aproximace gamvou 2,2;
    profil koloběhuje lcms2 identicky s vestavěným ``sRGB`` (test).

    Pro export ale platí totéž co u gray profilu: profil se zakládá, ne
    převádí — pixely v sobě mají gamma 2,2 z ``apply_display()`` a sRGB je
    kompatibilní obal, jehož TRC se od 2,2 liší jen v lineární zóně
    (uživatel 2026-09-21: „jpeg v sRGB (kompatibilita oproti černobílému)“).
    Deterministické konstanty => deterministické bajty profilu => fingerprint
    exportu sedí.
    """
    def xyz(x: float, y: float, z: float) -> bytes:
        return _pad(b"XYZ " + b"\x00\x00\x00\x00"
                    + struct.pack(">iii", _s15f16(x), _s15f16(y), _s15f16(z)))

    # chromatic adapted response: Bradford D65 -> D50 (kanonická matice ICC)
    chad_rows = ((0.9555766, -0.0230393, 0.0631636),
                 (-0.0282895, 1.0099416, 0.0210077),
                 (0.0122982, -0.0204830, 1.3299098))
    chad = (b"sf32" + b"\x00\x00\x00\x00"
            + b"".join(struct.pack(">iii", *(_s15f16(v) for v in row))
                       for row in chad_rows))
    trc = (b"para" + b"\x00\x00\x00\x00"
           + struct.pack(">H", 3) + b"\x00\x00"
           + struct.pack(">i", _s15f16(2.4))            # g
           + struct.pack(">i", _s15f16(1.0 / 1.055))    # a
           + struct.pack(">i", _s15f16(0.055 / 1.055))  # b
           + struct.pack(">i", _s15f16(1.0 / 12.92))    # c (sklon lineární části)
           + struct.pack(">i", _s15f16(0.04045)))       # d (mez, pod ní lineární)
    tags: list[tuple[bytes, bytes]] = [
        (b"desc", _text_value(b"desc", name)),
        (b"cprt", _text_value(b"cprt", "Filmscan Studio - volne pouzitelny")),
        (b"wtpt", xyz(0.96422, 1.0, 0.82489)),          # D50 bílá
        (b"chad", _pad(chad)),
        # D50-adaptované primáry sRGB (REC709), sloupce matice RGB->XYZ
        (b"rXYZ", xyz(0.43615, 0.22250, 0.01394)),
        (b"gXYZ", xyz(0.38507, 0.71688, 0.09710)),
        (b"bXYZ", xyz(0.14308, 0.06061, 0.71414)),
        *[(sig, _pad(trc)) for sig in (b"rTRC", b"gTRC", b"bTRC")],
    ]
    directory = bytearray()
    body = bytearray()
    off0 = 128 + 4 + 12 * len(tags)
    for sig, value in tags:
        directory += sig + struct.pack(">II", off0 + len(body), len(value))
        body += value
    header = bytearray(128)
    struct.pack_into(">I", header, 0, off0 + len(body))
    header[4:8] = b"none"
    header[8:12] = bytes([2, 0x10, 0, 0])
    header[12:16] = b"mntr"
    header[16:20] = b"RGB "                    # skutečně trikanálový
    header[20:24] = b"XYZ "
    header[24:36] = _PROFILE_DATE              # nesmí být nula — viz komentář
    header[36:40] = b"acsp"
    struct.pack_into(">I", header, 40, 0)
    header[48:52] = b"none"                  # manufacturer 0 — viz komentář nahoře
    struct.pack_into(">I", header, 64, 0)      # perceptual
    return (bytes(header) + struct.pack(">I", len(tags))
            + bytes(directory) + bytes(body))


def profile_for(gamma: float = GAMMA_DISPLAY) -> bytes:
    """Profil ladící s display gammou; default = Gray Gamma 2.2.

    Volá se při exportu z RenderParams.gamma_display, aby vložený profil
    vždy odpovídal pixelům. Cesta, kterou reálně vidí uživatel, je pevná
    2,2 — páčka v UI je pryč.
    """
    if abs(gamma - GAMMA_DISPLAY) < 1e-9:
        return build_gray_gamma_profile()
    return build_gray_gamma_profile(gamma, name=f"Gray Gamma {gamma:g}")


def profile_fingerprint(profile: bytes) -> str:
    """Krátký otisk profilu pro metadata (16 hex, shodně s render params)."""
    return hashlib.sha256(profile).hexdigest()[:16]


def read_profile_from_tiff(path) -> bytes | None:
    """ICC bytes z TIFF tagu InterColorProfile (34675), nebo None."""
    import tifffile

    with tifffile.TiffFile(path) as tf:
        tag = tf.pages[0].tags.get(TIPTAG_ICCPROFILE)
        if tag is None:
            return None
        value = tag.value
        return bytes(value) if value is not None else None


def read_profile_from_jpeg(path) -> bytes | None:
    """ICC bytes z JPEG APP2, nebo None."""
    from PIL import Image

    with Image.open(path) as im:
        profile = im.info.get("icc_profile")
        return bytes(profile) if profile else None


def write_gray_tiff(path, data, icc_profile: bytes | None,
                    description: str | None = None) -> Path:
    """16b single-channel gray TIFF s volitelným ICC profilem.

    Pillow zapíše profil do tagu 34675 verbatim — pixely se nepřevádějí
    (dokument 07: vložit profil, NE podruhé aplikovat gammu). Description
    (tag 270) nese náš JSON s parametry a fingerprintem. Na rozdíl od
    cv2/imwrite tudy projde i 16bit gray — cv2 umí 16b TIFF jen BGR.
    """
    from PIL import Image

    if data.dtype != "uint16" or data.ndim != 2:
        raise ValueError(f"expected 2-D uint16, got {data.ndim}-D {data.dtype}")
    im = Image.fromarray(data)      # uint16 2-D -> režim I;16 (skutečný gray)
    if im.mode != "I;16":
        raise ValueError(f"unexpected Pillow mode {im.mode!r} for uint16 gray")
    kwargs: dict[str, object] = {}
    if icc_profile:
        kwargs["icc_profile"] = icc_profile
    if description is not None:
        from PIL.TiffImagePlugin import ImageFileDirectory_v2

        info = ImageFileDirectory_v2()
        info[270] = description                       # ImageDescription
        info.tagtype[270] = 2                         # ASCII
        kwargs["tiffinfo"] = info
    im.save(path, format="TIFF", **kwargs)
    return Path(path)


def write_gray_jpeg(path, data, icc_profile: bytes | None,
                    quality: int = 95) -> Path:
    """8b single-channel gray JPEG s ICC profilem.

    Dřívější zápis přes cv2 psal BGR se třemi identickými kanály — tady
    jde opravdový gray (režim L) a k němu APP2 profil. Pixely se nemění.
    """
    import numpy as np
    from PIL import Image

    arr = np.asarray(data)
    if arr.dtype != "uint8" or arr.ndim != 2:
        raise ValueError(f"expected 2-D uint8, got {arr.ndim}-D {arr.dtype}")
    im = Image.fromarray(arr, mode="L")
    kwargs: dict[str, object] = {"quality": quality}
    if icc_profile:
        kwargs["icc_profile"] = icc_profile
    im.save(path, format="JPEG", **kwargs)
    return Path(path)


def write_srgb_jpeg(path, rgb_data, icc_profile: bytes,
                    quality: int = 95) -> Path:
    """8b RGB JPEG (kompatibilní cesta) — gray pixely jako R=G=B + sRGB.

    Pixely se nemění ani nepřevádějí: gamma 2,2 z apply_display() v nich už
    je, sRGB profil je kompatibilní obal (uživatel 2026-09-21). Na rozdíl
    od gray cesty tudy jdou tři stejné kanály záměrně — kde black-and-white
    JPEG může vyžadovat explicitní gray konverzi, RGB JPEG umí každá čtečka.
    """
    import numpy as np
    from PIL import Image

    arr = np.asarray(rgb_data)
    if arr.dtype != "uint8" or arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError(f"expected HxWx3 uint8, got {arr.shape} {arr.dtype}")
    Image.fromarray(arr, mode="RGB").save(
        path, format="JPEG", quality=quality, icc_profile=icc_profile)
    return Path(path)


#: HEIC default quality — ne 95! Změřeno 2026-09-21 na reálném 20 MP
#: snímku: q95 = 9,1 MB (z blízka bezeztrátové), ale už q75 drží 99,6 %
#: filmového zrna a PSNR 54 dB (max chyba ~2 % z 1023 úrovní) při 5,7 MB.
#: 72 ≈ 5 MB — kompromis velikosti a zrna, který chce uživatel.
HEIC_QUALITY = 72


def write_srgb_heic(path, rgb16, icc_profile: bytes,
                    quality: int = HEIC_QUALITY, bit_depth: int = 10) -> Path:
    """10b RGB HEIC (HEVC) — hlubší bitová hloubka pro Apple/ProApps čtečky.

    16b vstup (``>u2`` nebo ``<u2``, hodnoty 0..65535 = plná škála) se koduje
    HEVCem na ``bit_depth`` bitů — enkoder sám škáluje `>>6`; ``>>6`` předat
    ruce by znamenalo dvojité zmenšení na ~1 % světla (chyba 2026-09-21).
    Bajty se enkodéru podávají little-endian: ``RGB;16`` je u pillow-heif
    nativní orden a na arm64 je nativní LE — big-endian vstup by se čtenářsky
    znehodnotil záměnou bajtů. (Symetrická testovací data 0x0101… záměnu
    skrývají, náhodná ne — test to hlídá.) Apple monochrom HEIC zvládá taky,
    ale RGB cesta je jistota — R=G=B, 10 bitů bez pásovaní.
    """
    import numpy as np
    import pillow_heif

    arr = np.asarray(rgb16)
    if arr.dtype not in (np.dtype(">u2"), np.dtype("<u2"), np.dtype("uint16")) \
            or arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError(f"expected HxWx3 uint16, got {arr.shape} {arr.dtype}")
    h, w, _ = arr.shape
    pillow_heif.encode("RGB;16", (w, h), arr.astype("<u2").tobytes(), str(path),
                       quality=quality, bit_depth=bit_depth,
                       icc_profile=icc_profile)
    return Path(path)


def write_mono_heic(path, gray16, icc_profile: bytes,
                    quality: int = HEIC_QUALITY, bit_depth: int = 10) -> Path:
    """10b monochromatické HEIF (HEVC) — skutečný single-channel mono.

    Uživatel 2026-09-21: „přidej 10bit heif mono“ — Apple monochrom zvládá
    (ověřeno: pixi 1×10 bpc, ColorSync čte s Gray Gamma 2.2 profilem).
    Pro konvence vstupu platí totéž co u write_srgb_heic: plná 16b škála
    0..65535 (enkoder škáluje sám!) a bajty little-endian.
    """
    import numpy as np
    import pillow_heif

    arr = np.asarray(gray16)
    if arr.dtype not in (np.dtype(">u2"), np.dtype("<u2"), np.dtype("uint16")) \
            or arr.ndim != 2:
        raise ValueError(f"expected HxW uint16, got {arr.shape} {arr.dtype}")
    h, w = arr.shape
    pillow_heif.encode("L;16", (w, h), arr.astype("<u2").tobytes(), str(path),
                       quality=quality, bit_depth=bit_depth,
                       icc_profile=icc_profile)
    return Path(path)


def profile_name(profile: bytes) -> str:
    """Popis profilu tak, jak ho čte lcms2 (Photoshop)."""
    from PIL import ImageCms

    prof = ImageCms.getOpenProfile(io.BytesIO(profile))
    return ImageCms.getProfileName(prof).strip()


def trc_gamma(profile: bytes) -> float:
    """Gamma z kTRC profilu (parametrický typ, ne tabulka)."""
    n = struct.unpack_from(">I", profile, 128)[0]
    for i in range(n):
        entry = 132 + i * 12          # directory: 4B sig + 4B offset + 4B size
        sig = profile[entry: entry + 4]
        off = struct.unpack_from(">I", profile, entry + 4)[0]
        if sig != b"kTRC":
            continue
        if profile[off:off + 4] != b"para":
            raise ValueError("kTRC is not a parametric curve")
        fn_type = struct.unpack_from(">H", profile, off + 8)[0]
        if fn_type != 0:
            raise ValueError(f"unsupported para type {fn_type}")
        # para_Type: 0 signature, 4 reserved, 8 fn type (u16), 10 reserved,
        # 12 G (s15f16), dalek a..f
        raw = struct.unpack_from(">i", profile, off + 12)[0]
        return raw / 65536.0
    raise ValueError("profile has no kTRC")
