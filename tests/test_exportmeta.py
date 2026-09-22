"""EXIF/XMP stavit exportů (dok. 09 kontrakt).

Round-tripy čestné — přes Pillow zápis/čtení, ne jen sestavený slovník:
ImageDescription s češtinou (ASCII-fold), GPS rationals, ISO parsing a
escape středníku se nesmí rozbít až v zapsaném souboru. exiftool potvrzen
2026-09-22 ručně. Rozkaz 22:30: popis → ImageDescription (ne UserComment),
název → XMP dc:title, Make/Model z přímých polů, Orientation SE NEPIŠE
(vývoják zapéká rotaci do pixelů).
"""
from __future__ import annotations

import numpy as np
import pytest

from filmscan_studio.core import exportmeta as em

Image = pytest.importorskip("PIL.Image")


def _record(**overrides) -> dict:
    """Typický anotovaný sidecar; overrides přepisují ploché klíče bloků."""
    record = {
        "film": {
            "film_id": "HP5_001", "film_name": "Ilford HP5 Plus",
            "camera": "Nikon FM2", "camera_make": "Nikon",
            "camera_model": "FM2", "shooting_lens": "Nikkor 50mm f/2",
            "film_iso": "400/27°", "format": "35mm",
            "development": "R 09 1:50", "pushed_stops": 1.0,
            "box_number": "BOX 7", "operator": "Operator",
            "digitising_lens": "Apo-Rodigon 75/4",
            "digitisation_date": "2026-09-20",
        },
        "acquisition": {
            "camera": "Touptek ATR2600M", "exposure_time": 0.1, "gain": 1.0,
            "capture_date": "2026-09-20T16:53:17.956137+02:00",
            "frames_averaged": 4, "conversion_gain": "HCG",
        },
        "annotation": {
            "title": "Vacation 2026", "note": "vacation 2026; kyvadlo",
            "tags": ["vacation", "trip"], "rating": 4,
            "capture_datetime": "2015:11:08 00:01:00",
            "gps_lat": "50.08747000", "gps_lon": "14.42756000",
            "gps_lat_ref": "N", "gps_lon_ref": "E",
            "rotation_degrees": 90,
        },
    }
    for spec, value in overrides.items():
        block, key = spec.split("__")
        record.setdefault(block, {})[key] = value
    return record


class TestHelpers:
    def test_exif_str_variants(self) -> None:
        assert em._exif_str("2015:11:08 00:01:00") == "2015:11:08 00:01:00"
        # ISO s pásmem z riggu → EXIF bez pásma (místní čas zůstal v textu)
        assert em._exif_str("2026-09-20T16:53:17.956137+02:00") \
            == "2026:09:20 16:53:17"
        # anotátor ukládá už EXIF tvar; tady stačí rok-den přepis + bez času
        assert em._exif_str("2015-11-08") == "2015:11:08 00:00:00"
        assert em._exif_str("cca 2015") == ""     # nečitelné → nic
        assert em._exif_str("1968") == ""         # holý rok → nic
        assert em._exif_str("") == ""

    def test_parse_iso(self) -> None:
        assert em.parse_iso("400/27°") == 400
        assert em.parse_iso("HP5 @ 1600") == 1600
        assert em.parse_iso(None) is None
        assert em.parse_iso("bez udani") is None

    def test_split_camera_fallback_heuristic(self) -> None:
        # vyhlášená past: velký výrobní název + model v jednom řetězci
        assert em.split_camera(
            "ERNST LEITZ WETZLAR GMBH Leica R4s MOD.2") \
            == ("ERNST LEITZ WETZLAR GMBH", "Leica R4s MOD.2")
        assert em.split_camera("Nikon FM2") == ("Nikon", "FM2")
        assert em.split_camera("OLYMPUS OM-1") == ("OLYMPUS", "OM-1")
        assert em.split_camera("Zenit") == ("", "Zenit")
        assert em.split_camera(None) == ("", "")

    def test_gps_to_dms(self) -> None:
        ref, dms = em.gps_to_dms("50.08747", "lat")
        assert ref == "N"
        assert (int(dms[0]), int(dms[1])) == (50, 5)
        ref, dms = em.gps_to_dms("-16.382", "lon")
        assert ref == "W"
        assert (int(dms[0]), int(dms[1])) == (16, 22)
        # suffix a ref hint porazí znaménko
        assert em.gps_to_dms("49.11S", "lat")[0] == "S"
        assert em.gps_to_dms("12.5", "lon", "W")[0] == "W"
        assert em.gps_to_dms("", "lat") is None
        assert em.gps_to_dms("kdeco", "lat") is None

    def test_dms_rounding_59_999_to_next_degree(self) -> None:
        # 49.9999999° → 50°00'00", nikdy 49°59'60"
        _, dms = em.gps_to_dms("49.999999999", "lat")
        assert (int(dms[0]), int(dms[1]), float(dms[2])) == (50, 0, 0.0)

    def test_ascii_fold_keeps_dash_folds_haces(self) -> None:
        assert em._ascii_fold("vyvolávka s ěščřžý") == "vyvolavka s escrzy"
        # náhrada středníku (pomlčka) musí přežít, ne zmizet
        assert em._ascii_fold("vacation 2026- kyvadlo") \
            == "vacation 2015- kyvadlo"


class TestDescription:
    def test_note_first_then_pieces(self) -> None:
        text = em.description_text(_record())
        pieces = text.split(" ; ")
        # komentář uživatele je VŽDY první (dok 09)
        assert pieces[0].startswith("vacation 2026")
        assert "Film: Ilford HP5 Plus (35mm)" in pieces
        assert "Digitising camera: Touptek ATR2600M" in pieces
        assert "Exposure time: 0.1s" in pieces
        assert "Push: +1 EV" in pieces

    def test_semicolon_inside_value_escaped(self) -> None:
        """Středník v hodnotě → '–', dělení podle ' ; ' zůstává jednoznačné."""
        text = em.description_text(_record())
        for piece in text.split(" ; "):
            assert ";" not in piece

    def test_empty_pieces_skipped(self) -> None:
        record = _record()
        record["annotation"]["note"] = ""
        record["film"]["development"] = None
        record["acquisition"]["conversion_gain"] = None
        pieces = em.description_text(record).split(" ; ")
        assert not [p for p in pieces if p.strip() == ""]
        assert not [p for p in pieces if p.endswith(": ")]
        assert not any(p.startswith("Development") for p in pieces)

    def test_rating_zero_written(self) -> None:
        """Nula je zámerné ' škaredé' hodnocení, ne 'nehodnoceno'."""
        exif = em.build_exif_bytes(_record(annotation__rating=0))
        ifd = Image.Exif(); ifd.load(exif)
        assert ifd.get_ifd(0x8769)[0x4746] == 0
        assert ifd.get_ifd(0x8769)[0x4747] == 0


class TestExifBuilder:
    def test_empty_record_is_none(self) -> None:
        assert em.build_exif_bytes({}) is None
        assert em.build_xmp_bytes({}) is None

    def test_standard_tags_land_where_contract_says(self) -> None:
        exif = em.build_exif_bytes(_record())
        assert exif and exif.startswith(b"Exif\x00\x00")
        top = Image.Exif(); top.load(exif)
        # ImageDescription ← celý popis (komentář první), ASCII-fold
        assert top[0x010E].startswith("vacation 2015- kyvadlo")
        assert "Film: Ilford HP5 Plus (35mm)" in top[0x010E]
        assert top[0x010F] == "Nikon"               # Make ← camera_make
        assert top[0x0110] == "FM2"                 # Model ← camera_model
        assert top[0x0131] == em.SOFTWARE_AGENT     # Software
        # Orientation se nepíše — vývoják rotaci zapéká do pixelů
        assert 0x0112 not in top
        ifd = top.get_ifd(0x8769)
        assert 0x9286 not in ifd                    # UserComment pryč
        assert ifd[0x8827] == 400                   # ISO ← film_iso "400/27°"
        assert ifd[0xA434] == "Nikkor 50mm f/2"     # LensModel
        assert ifd[0x9003] == "2015:11:08 00:01:00" # DateTimeOriginal ← záběr
        assert ifd[0x9004] == "2026:09:20 16:53:17" # DateTimeDigitized ← rigg
        assert ifd[0x4746] == 4 and ifd[0x4747] == 80

    def test_direct_make_model_beat_free_text(self) -> None:
        """Přímá pole anotátoru mají přednost před heuristikou z `camera`."""
        record = _record(film__camera="ERNST LEITZ WETZLAR GMBH Leica R4s MOD.2",
                         film__camera_make="Leica", film__camera_model="R4s")
        top = Image.Exif(); top.load(em.build_exif_bytes(record))
        assert top[0x010F] == "Leica"
        assert top[0x0110] == "R4s"

    def test_free_text_fallback_when_no_direct_fields(self) -> None:
        record = _record()
        del record["film"]["camera_make"]
        del record["film"]["camera_model"]
        record["film"]["camera"] = "ERNST LEITZ WETZLAR GMBH Leica R4s MOD.2"
        top = Image.Exif(); top.load(em.build_exif_bytes(record))
        assert top[0x010F] == "ERNST LEITZ WETZLAR GMBH"
        assert top[0x0110] == "Leica R4s MOD.2"

    def test_description_ascii_folded_for_exif(self) -> None:
        record = _record()
        record["annotation"]["note"] = "Karlův most"
        top = Image.Exif(); top.load(em.build_exif_bytes(record))
        assert top[0x010E].startswith("Karluv most")
        assert top[0x010E].isascii()

    def test_gps_ifd_rationals(self) -> None:
        top = Image.Exif()
        top.load(em.build_exif_bytes(_record()))
        gps = top.get_ifd(0x8825)
        assert gps[1] == "N" and gps[3] == "E"
        lat = tuple(float(v) for v in gps[2])
        assert lat[0] == 50 and lat[1] == 5
        assert abs(lat[2] - 14.89) < 0.01           # 50.08747° → 5'14.89"
        assert gps[0] == b"\x02\x03\x00\x00"        # version 2.3

    def test_gps_only_lat_still_written_without_version(self) -> None:
        top = Image.Exif()
        top.load(em.build_exif_bytes(
            _record(annotation__gps_lon="")))
        gps = top.get_ifd(0x8825)
        assert gps[1] == "N" and 0 not in gps


class TestXmp:
    def test_title_and_description_and_tags(self) -> None:
        xmp = em.build_xmp_bytes(_record()).decode("utf-8")
        # název je Title (první kolonka Bridge), plná diakritika
        assert '<dc:title><rdf:Alt><rdf:li xml:lang="x-default">' \
            "Vacation 2026</rdf:li></rdf:Alt></dc:title>" in xmp
        # popis v UTF-8 (ďábelská verze, ne ASCII-fold)
        assert "vacation 2026" in xmp
        assert "<dc:description>" in xmp
        assert "<li>" in xmp and "trip" in xmp
        assert "<xmp:Rating>4</xmp:Rating>" in xmp
        assert xmp.startswith("<?xpacket begin=")

    def test_rating_none_and_zero_omitted(self) -> None:
        # nic k zapsání (nula ani None do XMP nepatří, popis prázdný)
        assert em.build_xmp_bytes({"annotation": {"rating": None}}) is None
        assert b"xmp:Rating" not in em.build_xmp_bytes(
            _record(annotation__tags=["a"], annotation__rating=0))

    def test_xml_escaping(self) -> None:
        xmp = em.build_xmp_bytes(
            _record(annotation__tags=["A&B <x>"])).decode("utf-8")
        assert "A&amp;B &lt;x&gt;" in xmp


class TestRoundTripThroughFiles:
    """Úplná cesta write→read: to podstatné musí přežít reálný zápis."""

    @pytest.fixture
    def both(self) -> tuple:
        record = _record()
        return (record,
                em.build_exif_bytes(record), em.build_xmp_bytes(record))

    def test_gray_jpeg_carries_all(self, both, tmp_path) -> None:
        from filmscan_studio.core import icc
        record, exif, xmp = both
        data = (np.random.rand(16, 16) * 255).astype(np.uint8)
        out = tmp_path / "x.jpg"
        icc.write_gray_jpeg(out, data, icc.build_gray_gamma_profile(),
                            exif=exif, xmp=xmp)
        with Image.open(out) as im:
            top = im.getexif()
            assert top[0x010E].startswith("vacation 2015-")
            assert top[0x010F] == "Nikon"
            ifd = top.get_ifd(0x8769)
            assert ifd[0x8827] == 400
            assert ifd[0x9003] == "2015:11:08 00:01:00"
            assert float(top.get_ifd(0x8825)[2][0]) == 50.0
            xmp_bytes = im.info["xmp"]
            assert "trip".encode() in xmp_bytes
            assert "Vacation 2026".encode() in xmp_bytes

    def test_heic_mono_carries_all(self, both, tmp_path) -> None:
        import pillow_heif

        from filmscan_studio.core import icc
        record, exif, xmp = both
        q16 = (np.random.rand(16, 16) * 65535).astype(np.uint16)
        out = tmp_path / "x.heic"
        icc.write_mono_heic(out, q16, icc.build_gray_gamma_profile(),
                            exif=exif, xmp=xmp)
        heif = pillow_heif.read_heif(out)
        top = Image.Exif(); top.load(heif.info["exif"])
        assert top[0x0110] == "FM2"
        assert top.get_ifd(0x8769)[0xA434] == "Nikkor 50mm f/2"
        assert b"<xmp:Rating>4" in heif.info["xmp"]

    def test_srgb_jpeg_and_srgb_heic(self, both, tmp_path) -> None:
        import pillow_heif

        from filmscan_studio.core import icc
        record, exif, xmp = both
        data = (np.random.rand(16, 16) * 255).astype(np.uint8)
        rgb = np.dstack([data] * 3)
        out = tmp_path / "x.jpg"
        icc.write_srgb_jpeg(out, rgb, icc.build_srgb_profile(),
                            exif=exif, xmp=xmp)
        with Image.open(out) as im:
            assert im.getexif()[0x010F] == "Nikon"
        q16 = (np.random.rand(16, 16) * 65535).astype(np.uint16)
        outrgb = tmp_path / "y.heic"
        icc.write_srgb_heic(outrgb, np.dstack([q16] * 3),
                            icc.build_srgb_profile(), exif=exif, xmp=xmp)
        assert b"Exif" in pillow_heif.read_heif(outrgb).info["exif"]

    def test_no_metadata_no_change_to_bytes(self, tmp_path) -> None:
        """exif=None xmp=None = přesně staré chování (žádné APP1/APP2-xmp)."""
        from filmscan_studio.core import icc
        data = (np.random.rand(16, 16) * 255).astype(np.uint8)
        a, b = tmp_path / "a.jpg", tmp_path / "b.jpg"
        icc.write_gray_jpeg(a, data, icc.build_gray_gamma_profile())
        icc.write_gray_jpeg(b, data, icc.build_gray_gamma_profile(),
                            exif=None, xmp=None)
        assert a.read_bytes() == b.read_bytes()
