"""ICC gray gamma profil: generování, TRC, zápis do TIFF/JPEG (dokument 07).

Dokument 07 §4/§8: exporty nesou skutečný monochromatický gray profil se
stejnou TRC jako ``apply_display()``; vloží se, ale pixely se nepřevádějí.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import numpy as np
import pytest
import tifffile

from filmscan_studio.core import icc
from filmscan_studio.core import render as rnd


class TestProfileGeneration:
    def test_default_profile_is_gray_gamma_22(self) -> None:
        prof = icc.build_gray_gamma_profile()
        assert prof[36:40] == b"acsp"
        assert prof[16:20] == b"GRAY"      # skutecne monochromaticky, ne RGB
        assert prof[12:16] == b"mntr"
        assert abs(icc.trc_gamma(prof) - 2.2) < 1e-4

    def test_profile_is_deterministic(self) -> None:
        """Stejna gamma => stejne bajty => fingerprint metadat sedí."""
        assert icc.build_gray_gamma_profile() == icc.build_gray_gamma_profile()
        assert (icc.profile_fingerprint(icc.build_gray_gamma_profile())
                == icc.profile_fingerprint(icc.build_gray_gamma_profile()))

    def test_lcms2_reads_profile_name(self) -> None:
        ImageCms = pytest.importorskip("PIL.ImageCms")
        import io

        prof = icc.build_gray_gamma_profile()
        p = ImageCms.getOpenProfile(io.BytesIO(prof))
        assert ImageCms.getProfileName(p).strip() == "Gray Gamma 2.2"

    def test_profile_for_matches_render_gamma(self) -> None:
        prof = icc.profile_for(2.2)
        assert abs(icc.trc_gamma(prof) - icc.GAMMA_DISPLAY) < 1e-4
        other = icc.profile_for(1.8)
        assert abs(icc.trc_gamma(other) - 1.8) < 1e-3
        assert other != prof

    def test_trc_encodes_apply_display_transfer(self) -> None:
        """TRC (device->linkární) je mocnina gamma; čtenář derivuje 1/gamma,
        což je přesně x ** (1/g) z apply_display()."""
        g = icc.trc_gamma(icc.build_gray_gamma_profile())
        display = rnd.apply_display(np.array([0.25]),
                                    rnd.RenderParams(gamma_display=g))
        # dekodovani profilem: linearni = display^g -> zpet na 0.25
        assert float(display[0]) ** g == pytest.approx(0.25, abs=1e-6)


class TestWriters:
    def test_gray_tiff_carries_profile_pixels_unchanged(self, tmp_path) -> None:
        prof = icc.build_gray_gamma_profile()
        data = np.linspace(0, 65535, 64 * 64).reshape(64, 64).astype(np.uint16)
        out = tmp_path / "a.tif"
        icc.write_gray_tiff(out, data, prof, description='{"magic": "t"}')
        # single-channel gray, 16 bit: 2-D data, minisblack, BPS 16
        with tifffile.TiffFile(out) as tf:
            page = tf.pages[0]
            assert page.photometric.name == "MINISBLACK"
            assert page.tags[258].value == 16      # BitsPerSample
            assert page.shape == data.shape         # 2-D, ne (H, W, 3)
            assert page.description == '{"magic": "t"}'
        # pixely beze zmeny — profil se jen zapsal, zadna druha gamma
        assert np.array_equal(tifffile.imread(out), data)
        assert icc.read_profile_from_tiff(out) == prof

    def test_gray_tiff_without_profile(self, tmp_path) -> None:
        data = np.zeros((8, 8), dtype=np.uint16)
        out = tmp_path / "b.tif"
        icc.write_gray_tiff(out, data, None)
        assert icc.read_profile_from_tiff(out) is None

    def test_gray_jpeg_single_channel_with_profile(self, tmp_path) -> None:
        Image = pytest.importorskip("PIL.Image")
        prof = icc.build_gray_gamma_profile()
        data = np.linspace(0, 255, 64).astype(np.uint8).reshape(8, 8)
        out = tmp_path / "a.jpg"
        icc.write_gray_jpeg(out, data, prof, quality=100)
        with Image.open(out) as im:
            assert im.mode == "L"          # skutecny gray, ne RGB/BGR
        assert icc.read_profile_from_jpeg(out) == prof

    def test_writers_reject_wrong_dtype(self, tmp_path) -> None:
        with pytest.raises(ValueError):
            icc.write_gray_tiff(tmp_path / "x.tif",
                                np.zeros((4, 4), np.float32), None)
        with pytest.raises(ValueError):
            icc.write_gray_jpeg(tmp_path / "x.jpg",
                                np.zeros((4, 4), np.uint16), None)


class TestArchiveUnaffected:
    """Dokument 07 §9: hustotní archiv se profilem ani display gammou nemění."""

    def test_density_archive_has_no_icc_and_stays_density(self, tmp_path) -> None:
        from filmscan_studio.core import density as dens

        d = np.linspace(0.0, 2.5, 16 * 16, dtype=np.float32).reshape(16, 16)
        prov = dens.DensityProvenance(source="t.tif", shutter=1.0, gain=1.0,
                                      black_level=0.0, white_level=65535.0)
        out = dens.write_density_tiff(tmp_path / "d.density.tif", d, prov)
        assert icc.read_profile_from_tiff(out) is None
        back = tifffile.imread(out)
        assert back.dtype == np.float32
        np.testing.assert_allclose(back, d, equal_nan=True)


class TestSrgbProfile:
    """Kompatibilitní RGB cesty (JPEG sRGB, 10b HEIC) — uživatelský povel
    2026-09-21 večer. Profil se zakládá, pixely se nepřevádějí."""

    def test_srgb_profile_parses_and_is_deterministic(self) -> None:
        prof = icc.build_srgb_profile()
        assert prof[36:40] == b"acsp"
        assert prof[16:20] == b"RGB "           # skutečně RGB profil
        assert icc.build_srgb_profile() == prof  # fingerprint sedí
        from PIL import ImageCms
        opened = ImageCms.getOpenProfile(io.BytesIO(prof))
        assert "sRGB" in ImageCms.getProfileName(opened).strip()

    def test_srgb_profile_round_trips_as_identity(self) -> None:
        """lcms2 musí náš profil brát jako ekvivalent vestavěného sRGB:
        neutral R=G=B projde transformací beze změny (kontrola D50 matice
        i para type 3 — prohozené c/d daly 200→50)."""
        from PIL import Image, ImageCms

        mine = ImageCms.ImageCmsProfile(io.BytesIO(icc.build_srgb_profile()))
        builtin = ImageCms.createProfile("sRGB")
        t = ImageCms.buildTransform(mine, builtin, "RGB", "RGB")
        for v in (0, 64, 128, 200, 255):
            out = ImageCms.applyTransform(
                Image.new("RGB", (1, 1), (v, v, v)), t)
            assert out.getpixel((0, 0)) == (v, v, v)

    def test_srgb_jpeg_roundtrip_pixels_unchanged(self, tmp_path) -> None:
        """Zápis → čtení: pixely přesně shrnují, profil je v APP2, režim RGB."""
        from PIL import Image

        prof = icc.build_srgb_profile()
        g = np.linspace(0, 255, 64).astype(np.uint8).reshape(8, 8)
        rgb = np.dstack([g, g, g])
        out = icc.write_srgb_jpeg(tmp_path / "s.jpg", rgb, prof, quality=100)
        with Image.open(out) as im:
            assert im.mode == "RGB"
            assert im.info.get("icc_profile") == prof
            back = np.array(im)
        # JPEG je ztrátový i na quality 100 (DCT+subsampling) — tolerantně:
        # žádáme, že se zápis rovná zápisu beze změny gammy, ne po bitech
        assert np.abs(back[:, :, 0].astype(int)
                      - g.astype(int)).max() <= 2
        assert np.abs(back[:, :, 0].astype(int)
                      - back[:, :, 2].astype(int)).max() <= 2

    def test_heic_writer_rejects_wrong_shape(self, tmp_path) -> None:
        with pytest.raises(ValueError):
            icc.write_srgb_heic(tmp_path / "x.heic",
                                np.zeros((4, 4), np.uint16), b"")

    def test_heic_10bit_values_round_trip(self, tmp_path) -> None:
        """16b vstup se škáluje na celou hloubku, ne dvakrát `>>6`, a endian
        se musí shodovat s tím, co enkoder čeká.

        Chyba 2026-09-21: GUI předávalo hodnoty 0..1023 (už `>>6`) a
        pillow_heif je podruhé zmenšilo na ~1 % světla — černobílý rozpad.
        Druhá past: big-endian data na LE enkoderu prohodí bajty; rampa
        `i*257` (0x0101, 0x0202…) to skrývá, náhodná ne. Testuje oba
        endian vstupy i nesymetrická data.
        """
        import pillow_heif

        rng = np.random.default_rng(1)
        vals = rng.integers(0, 65536, (4, 64)).astype(np.uint16)
        for endian in (">u2", "<u2"):
            rgb = np.dstack([vals] * 3).astype(endian)
            out = icc.write_srgb_heic(tmp_path / f"rt_{endian}.heic", rgb,
                                      icc.build_srgb_profile(), quality=100)
            heif = pillow_heif.open_heif(out)
            arr = np.array(heif.to_pillow().convert("RGB"))
            err = np.abs(arr[:, :, 0].astype(int)
                         - (vals >> 8).astype(int)).max()
            assert err <= 3, f"{endian}: HEIC hodnoty mimo: max err {err}"


    def test_mono_heic_is_single_channel_10bit(self, tmp_path) -> None:
        """10b mono HEIF: HEVC 10 bpc, ONE kanál (pixi 01 0a), gray profil,
        hodnoty sedí na 16b vstup (enkoder škáluje sám, endian sedí)."""
        import pillow_heif

        rng = np.random.default_rng(3)
        vals = rng.integers(0, 65536, (8, 64)).astype(np.uint16)
        out = icc.write_mono_heic(tmp_path / "m.heic", vals,
                                  icc.build_gray_gamma_profile(), quality=100)
        buf = out.read_bytes()
        i = buf.find(b"hvcC")
        assert (buf[i + 4 + 17] & 0x07) + 8 == 10
        ip = buf.find(b"pixi")
        # pixi: signature, 4B version/flags, 1B počet kanálů, 1B bpc/kanál
        assert buf[ip + 8] == 1 and buf[ip + 9] == 10   # 1 kanál @ 10 bpc
        heif = pillow_heif.open_heif(out)
        assert heif.info["bit_depth"] == 10
        assert heif.to_pillow().info.get("icc_profile") == \
            icc.build_gray_gamma_profile()
        dec = np.array(heif.to_pillow())               # 16b škála
        assert dec.shape == vals.shape
        err8 = np.abs((dec >> 8).astype(int)
                      - (vals >> 8).astype(int)).max()
        assert err8 <= 3, f"mono HEIC hodnoty mimo: max err {err8}"

    def test_mono_heic_rejects_3d(self, tmp_path) -> None:
        with pytest.raises(ValueError):
            icc.write_mono_heic(tmp_path / "x.heic",
                                np.zeros((4, 4, 3), np.uint16), b"")


class TestColorSyncCompat:
    """ColorSync (Preview/Quick Look/sips) umí s profilem jiného čtenáře.

    Historická chyba 2026-09-21: nulové display date v headeru → ColorSync
    profil neodmítl, ale tiše znetvořil: mono rampa 0→17→181→238→96
    („solarizace“), RGB profil dal R−B posun 253. lcms2 (ImageJ, Photoshop,
    Pillow) čte i nulové datum korektně — proto všechny lcms2 testy prošly
    a uživatel to viděl až v Náhledu.
    """

    def test_display_date_nonzero(self) -> None:
        for prof in (icc.build_gray_gamma_profile(), icc.build_srgb_profile()):
            assert prof[24:36] != bytes(12), "ColorSync znetvoří profil"
        assert (icc.build_gray_gamma_profile()[24:36]
                == icc.build_srgb_profile()[24:36])   # determinismus

    @staticmethod
    def _sips_managed(prof: bytes, mode: str, tmp_path) -> np.ndarray:
        """Rampa s profilem → ColorSync transformace na display sRGB."""
        import shutil
        import subprocess

        srgb_icc = "/System/Library/ColorSync/Profiles/sRGB Profile.icc"
        if shutil.which("sips") is None or not Path(srgb_icc).exists():
            pytest.skip("ColorSync k dispozici jen na macOS")
        from PIL import Image

        ramp = np.tile(np.arange(256, dtype=np.uint8), (4, 1))
        im = (Image.fromarray(ramp, mode="L") if mode == "L"
              else Image.fromarray(np.dstack([ramp] * 3), mode="RGB"))
        src, dst = str(tmp_path / "ramp.jpg"), str(tmp_path / "managed.png")
        im.save(src, icc_profile=prof,
                **({} if mode == "L" else {"subsampling": 0}))
        subprocess.run(["sips", "-s", "format", "png", "-m", srgb_icc,
                        "--out", dst, src], check=True, capture_output=True)
        return np.array(Image.open(dst).convert("RGB"))[2]

    @pytest.mark.skipif(sys.platform != "darwin", reason="ColorSync jen na macOS")
    def test_gray_profile_survives_colorsync(self, tmp_path) -> None:
        arr = self._sips_managed(icc.build_gray_gamma_profile(), "L", tmp_path)
        y = arr[:, 1].astype(int)
        assert np.all(np.diff(y) >= -2), f"negrafická TRC: {list(y[::32])}"
        assert abs(y[64] - 64) <= 3 and abs(y[128] - 129) <= 3

    @pytest.mark.skipif(sys.platform != "darwin", reason="ColorSync jen na macOS")
    def test_srgb_profile_survives_colorsync(self, tmp_path) -> None:
        arr = self._sips_managed(icc.build_srgb_profile(), "RGB", tmp_path)
        y = arr[:, 1].astype(int)
        assert np.all(np.diff(y) >= -2), f"zkolabovaná škála: {list(y[::32])}"
        assert abs(y[64] - 64) <= 3 and abs(y[128] - 129) <= 3
        tint = np.abs(arr[:, 0].astype(int) - arr[:, 2].astype(int)).max()
        assert tint <= 2, f"barevný posun R−B {tint}"
