"""GUI tests of the developer window, offscreen against the synthetic session.

Contract tests, not pixel tests: opening a project fills the list and the
preview; slider moves re-render without crashing; Shift-drag sets a rect and
re-measures; exports write the right files with the right headers.
"""

from __future__ import annotations

import dataclasses
import json

import numpy as np
import pytest
import tifffile

pytest.importorskip("pytestqt")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QGroupBox  # noqa: E402

from filmscan_studio.developer.gui import (  # noqa: E402
    MainWindow, density_to_qimage)
from filmscan_studio.developer.project import DevelopProject  # noqa: E402

from tests.test_project import session  # noqa: E402 - reuse the fixture


@pytest.fixture
def window(qtbot, session) -> MainWindow:
    proj = DevelopProject.open(session)
    w = MainWindow(project=proj)
    qtbot.addWidget(w)
    qtbot.waitUntil(lambda: w._density is not None, timeout=5000)
    return w


class TestDensityImage:
    def test_nan_pixels_are_black_not_magenta(self) -> None:
        """Magenta mask is gone; NaN renders as black (exports fill black too)."""
        img = np.array([[0.5, np.nan]])
        q = density_to_qimage(img)
        assert q.pixelColor(1, 0).name() == "#000000"
        assert q.pixelColor(0, 0).name() in ("#808080", "#7f7f7f")

    def test_warning_masks_colorize(self) -> None:
        img = np.array([[0.5, 0.5, 0.5]])
        hi = np.array([[False, True, False]])
        lo = np.array([[True, False, False]])
        q = density_to_qimage(img, hi, lo)
        assert q.pixelColor(1, 0).name() == "#ff2828"   # světla červeně
        assert q.pixelColor(0, 0).name() == "#285aff"   # stíny modře
        assert q.pixelColor(2, 0).name() in ("#808080", "#7f7f7f")

    def test_rejects_3d(self) -> None:
        with pytest.raises(ValueError):
            density_to_qimage(np.zeros((4, 4, 3)))


class TestWindow:
    def test_project_populates_list_and_preview(self, window) -> None:
        assert window.frame_list.count() == 1
        assert window.frame_list.item(0).text() == "frame001.tif"
        assert window.view._image is not None
        # Dmin from the synthetic base (0.40) reached the spin box.
        assert abs(window.spin_dmin.value() - 0.40) < 0.02
        assert window.btn_export.isEnabled()

    def test_exposure_slider_rerenders(self, window, qtbot) -> None:
        before = np.array(window.view._image.constBits()[:64], copy=True)
        window.sl_ev.setValue(400)   # +4 EV -- picture must respond
        qtbot.wait(20)
        after = np.array(window.view._image.constBits()[:64], copy=True)
        assert not np.array_equal(before, after)
        assert window.current_params().exposure_ev == pytest.approx(4.0)

    def test_curve_sliders_feed_params(self, window) -> None:
        window.sl_toe.setValue(70)
        window.sl_gamma.setValue(150)
        p = window.current_params()
        assert p.profile.toe == pytest.approx(0.7)
        assert p.profile.gamma == pytest.approx(1.5)

    def test_manual_dmin_when_auto_off(self, window) -> None:
        window.chk_dmin_auto.setChecked(False)
        assert window.spin_dmin.isEnabled()
        window.chk_dmin_auto.setChecked(True)
        assert not window.spin_dmin.isEnabled()
        # Auto value from the measurement is restored into the spin.
        assert abs(window.spin_dmin.value() - 0.40) < 0.02

    def _drag_roi(self, window, qtbot) -> None:
        v = window.view
        v.set_image(np.full((200, 200), 0.5), (64, 48))
        # The synthetic map equals whole frame here (no crop for preview).
        c = QPoint(20, 20)
        qtbot.mousePress(v, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.ShiftModifier, c)
        qtbot.mouseMove(v, QPoint(180, 180))
        qtbot.mouseRelease(v, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.ShiftModifier,
                           QPoint(180, 180))

    def test_shift_drag_sets_rect(self, window, qtbot) -> None:
        self._drag_roi(window, qtbot)
        rect = window.view.roi
        assert rect is not None
        x0, y0, x1, y1 = rect
        assert x0 >= 0 and y0 >= 0 and x1 > x0 and y1 > y0
        assert window.project.rect_for(
            window.project.entry("frame001.tif")) == rect

    def test_small_drag_is_not_a_rect(self, window, qtbot) -> None:
        v = window.view
        v.set_image(np.full((200, 200), 0.5), (64, 48))
        c = QPoint(30, 30)
        qtbot.mousePress(v, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.ShiftModifier, c)
        qtbot.mouseRelease(v, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.ShiftModifier, QPoint(34, 34))
        # A <12 px gesture must not replace an existing/absent rect.
        assert v.roi is None

    def test_save_density_writes_float32_with_provenance(self, window,
                                                         tmp_path,
                                                         qtbot) -> None:
        window.project.root = tmp_path      # keep exports in pytest sandbox
        window.save_density()
        out = tmp_path / "derived" / "frame001.density.tif"
        assert out.exists()
        data = tifffile.imread(out)
        assert data.dtype == np.float32
        with tifffile.TiffFile(out) as tf:
            meta = json.loads(tf.pages[0].description)
        assert meta["magic"] == "filmscan-density"
        assert meta["source"] == "frame001.tif"

    def test_export_positive_and_flat(self, window, tmp_path) -> None:
        window.project.root = tmp_path
        window.save_render()
        window.save_flat()
        positive = tmp_path / "derived" / "frame001.positive.tif"
        flat = tmp_path / "derived" / "frame001.flat.tif"
        assert positive.exists() and flat.exists()
        data = tifffile.imread(positive)
        assert data.dtype == np.uint16
        with tifffile.TiffFile(positive) as tf:
            meta = json.loads(tf.pages[0].description)
        assert meta["kind"] == "positive"
        assert len(meta["fingerprint"]) == 16

    def test_positive_pixels_match_params(self, window, tmp_path) -> None:
        """The exported file is the render of the current params, byte-exact.

        Including the display transfer: the exported TIFF must be exactly
        what the preview showed (WYSIWYG), gamma and paper black included.
        """
        from filmscan_studio.core import density as dens
        from filmscan_studio.core import render as rnd

        window.project.root = tmp_path
        window.save_render()
        out = tmp_path / "derived" / "frame001.positive.tif"
        exported = tifffile.imread(out)
        d, _prov = window.project.build_density("frame001.tif", crop=True)
        expected = rnd.quantise16(rnd.render_for_display(d,
                                                         window.current_params()))
        assert np.array_equal(exported, expected)

    def test_jpeg_export_matches_display(self, window, tmp_path) -> None:
        """The JPEG is the display render quantised to 8 bit."""
        import cv2

        from filmscan_studio.core import render as rnd

        window.project.root = tmp_path
        window.save_jpeg()
        out = tmp_path / "derived" / "frame001.jpg"
        assert out.exists()
        loaded = cv2.imread(str(out), cv2.IMREAD_GRAYSCALE)
        d, _ = window.project.build_density("frame001.tif", crop=True)
        display = rnd.render_for_display(d, window.current_params())
        expected = np.rint(np.where(np.isfinite(display), display, 0.0)
                           * 255.0).astype(np.uint8)
        # JPEG is lossy: allow codec slack, demand agreement in tone.
        assert loaded.shape == expected.shape
        assert np.abs(loaded.astype(int)
                      - expected.astype(int)).mean() < 3

    def test_exports_carry_icc_profile_flat_does_not(self, window,
                                                     tmp_path) -> None:
        """Dokument 07 §4: display-referred výstupy nesou Gray Gamma 2.2.

        Flat profil NENESÉ — je lineární v hustotě a profil by o datech
        lhal; pozná se i podle encoding v metadatech.
        """
        from filmscan_studio.core import icc

        window.project.root = tmp_path
        window.save_render()
        window.save_flat()
        window.save_jpeg()
        positive = tmp_path / "derived" / "frame001.positive.tif"
        flat = tmp_path / "derived" / "frame001.flat.tif"
        jpeg = tmp_path / "derived" / "frame001.jpg"
        prof = icc.read_profile_from_tiff(positive)
        assert prof is not None
        assert prof == icc.build_gray_gamma_profile()
        assert icc.read_profile_from_jpeg(jpeg) == prof
        assert icc.read_profile_from_tiff(flat) is None
        with tifffile.TiffFile(positive) as tf:
            meta = json.loads(tf.pages[0].description)
            assert meta["encoding"] == "gray-gamma-2.2"
            assert meta["icc_profile"] == "Gray Gamma 2.2"
            assert len(meta["icc_profile_fingerprint"]) == 16
        with tifffile.TiffFile(flat) as tf:
            meta = json.loads(tf.pages[0].description)
            assert meta["encoding"] == "linear-density"
            assert "icc_profile" not in meta

    def test_jpeg_is_true_single_channel_gray(self, window, tmp_path) -> None:
        """Kdysi BGR se 3 identickými kanály; teď skutečný gray (režim L)."""
        Image = pytest.importorskip("PIL.Image")
        window.project.root = tmp_path
        window.save_jpeg()
        with Image.open(tmp_path / "derived" / "frame001.jpg") as im:
            assert im.mode == "L"

    def test_jpeg_srgb_is_rgb_carries_srgb_profile(self, window,
                                                   tmp_path) -> None:
        """Kompatibilitní cesta: R=G=B, sRGB profil, pixely NEMĚNĚNÉ gammou.

        Profil je obal — data v sobě mají gamma 2,2 z apply_display() právě
        jednou; sRGB profile se jen založí (žádný color convert, dok 07
        platí i pro RGB cesty).
        """
        from filmscan_studio.core import icc, render as rnd

        Image = pytest.importorskip("PIL.Image")
        window.project.root = tmp_path
        window.save_jpeg_srgb()
        out = tmp_path / "derived" / "frame001.srgb.jpg"
        assert out.exists()
        with Image.open(out) as im:
            assert im.mode == "RGB"
            prof = im.info.get("icc_profile")
            assert prof == icc.build_srgb_profile()
            arr = np.array(im)
        # kanály stejné — achromatický obsah, žádný barevný posun
        assert np.array_equal(arr[:, :, 0], arr[:, :, 1])
        assert np.array_equal(arr[:, :, 1], arr[:, :, 2])
        # pixely = totéž co gray cesta (gamma podruhé aplikovaná není)
        d, _ = window.project.build_density("frame001.tif", crop=True)
        display = rnd.render_for_display(d, window.current_params())
        expected = np.rint(np.where(np.isfinite(display), display, 0.0)
                           * 255.0).astype(np.uint8)
        assert np.abs(arr[:, :, 0].astype(int)
                      - expected.astype(int)).mean() < 3   # JPEG ztrátovost

    def test_heic_is_10bit_rgb_with_srgb_profile(self, window,
                                                 tmp_path) -> None:
        """10b HEIC: HEVC hloubka skutečně 10 bpc, RGB, sRGB profil uvnitř."""
        import pillow_heif

        from filmscan_studio.core import icc
        from filmscan_studio.core import render as rnd

        window.project.root = tmp_path
        window.save_heic()
        out = tmp_path / "derived" / "frame001.srgb10.heic"
        assert out.exists()
        buf = out.read_bytes()
        i = buf.find(b"hvcC")                      # konfigurace HEVC
        assert i >= 0
        assert (buf[i + 4 + 17] & 0x07) + 8 == 10  # bitDepthLumaMinus8
        heif = pillow_heif.open_heif(out)
        assert heif.mode == "RGB"
        im = heif.to_pillow()
        assert im.info.get("icc_profile") == icc.build_srgb_profile()
        # dekódovaný obraz sedí na render (HEVC je ztrátový, tolerance jako JPEG)
        arr = np.array(im)
        assert np.array_equal(arr[:, :, 0], arr[:, :, 1])
        # HODNOTY, ne jen kanály: chyba 2026-09-21 uložila ~1 % světla
        # (dvojité >>6), R=G=B přesto sedělo. Průměr musí sedět na render.
        d, _ = window.project.build_density("frame001.tif", crop=True)
        display = rnd.render_for_display(d, window.current_params())
        expected = np.rint(np.where(np.isfinite(display), display, 0.0)
                           * 255.0)
        assert abs(float(arr[:, :, 0].mean()) - float(expected.mean())) < 3.0

    def test_export_combo_dispatches_selected_format(self, window,
                                                     tmp_path) -> None:
        """Seznam + jedno tlačítko: each item routes to its method (08-style
        single-button UX nahrazuje čtyři tlačítka)."""
        labels = [window.cmb_export.itemText(i)
                  for i in range(window.cmb_export.count())]
        assert len(labels) == 7
        assert any("sRGB" in t and "JPEG" in t for t in labels)
        assert any("HEIC" in t and "10b" in t for t in labels)
        assert any("HEIF" in t and "mono" in t for t in labels)
        window.project.root = tmp_path
        window.cmb_export.setCurrentText(
            next(t for t in labels if "sRGB (Apple" in t))
        window.export_current()
        assert (tmp_path / "derived" / "frame001.srgb10.heic").exists()
        window.cmb_export.setCurrentText(
            next(t for t in labels if "mono" in t))
        window.export_current()
        assert (tmp_path / "derived" / "frame001.mono10.heic").exists()
        window.cmb_export.setCurrentText(
            next(t for t in labels if t.startswith("JPEG 8b sRGB")))
        window.export_current()
        assert (tmp_path / "derived" / "frame001.srgb.jpg").exists()

    def test_profile_insertion_does_not_regamma_pixels(self, window,
                                                       tmp_path) -> None:
        """Dokument 07 §4: vložit profil ≠ aplikovat gamma podruhé.

        Počítá se koloběhem export → Pillow: pixely se musí rovnat
        quantise16(render_for_display(...)) přesně na bajt; profil je jen
        metadata. Flat (lineární, bez profilu) totéž bez gammy.
        """
        from filmscan_studio.core import render as rnd

        window.project.root = tmp_path
        params = window.current_params()
        window.save_render()
        exported = tifffile.imread(
            tmp_path / "derived" / "frame001.positive.tif")
        d, _ = window.project.build_density("frame001.tif", crop=True)
        once = rnd.quantise16(rnd.render_for_display(d, params))
        assert np.array_equal(exported, once)
        # druha gamma navic (aplikovana na jiz gamma-enkodovana data) by
        # obraz ztmavila — export se ji musi lisit:
        twice = rnd.quantise16(rnd.apply_display(once / 65535.0, params))
        assert not np.array_equal(exported, twice)
        # a profil nese presne tu TRC, ktera v pixelich uz je
        from filmscan_studio.core import icc
        prof = icc.read_profile_from_tiff(
            tmp_path / "derived" / "frame001.positive.tif")
        assert icc.trc_gamma(prof) == pytest.approx(params.gamma_display,
                                                    abs=1e-3)

    def test_output_histogram_receives_display_referred_data(self,
                                                             window) -> None:
        """Dolní histogram = jak vypadá display výstup (dokument 07 §2).

        Musí dostávat data po render_for_display (vč. gammy), ne lineární
        pozitiv — to je kontrakt „co uvidím na monitoru / ve Photoshopu".
        """
        import inspect

        src = inspect.getsource(MainWindow.rerender)
        assert "render_for_display" in src
        assert "render_density" not in src
        # a skutečně: po gammě je střední D jinde než před ní
        from filmscan_studio.core import render as rnd
        params = window.current_params()
        d = np.array([(params.dmin + params.dmax) / 2.0])
        assert (float(rnd.render_for_display(d, params)[0])
                != pytest.approx(float(rnd.render_density(d, params)[0]),
                                 abs=1e-3))

    def test_display_sliders_feed_params_and_remember(self, window, tmp_path,
                                                      qtbot) -> None:
        window.project.root = tmp_path
        window.sl_sb.setValue(5)
        qtbot.wait(20)
        p = window.current_params()
        # gamma_display nema ovladac — je dana profilem (icc.GAMMA_DISPLAY)
        assert p.gamma_display == pytest.approx(2.2)
        assert p.shadow_band == pytest.approx(0.05)
        stored = window.project.frame_settings["frame001.tif"]
        assert stored["gamma_display"] == pytest.approx(2.2)
        assert stored["shadow_band"] == pytest.approx(0.05)

    def test_display_gamma_is_not_editable(self, window) -> None:
        """Uzivatel 2026-09-21: vystupni gamma se upravit neda — dava ji profil."""
        assert not hasattr(window, "sl_gd")
        assert not hasattr(window, "spin_gd")
        from filmscan_studio.core import icc
        assert window.current_params().gamma_display == icc.GAMMA_DISPLAY
        # i kdyby ulozene nastaveni neslo jine hodnotu, UI ji prepise na profil
        from filmscan_studio.core import render as rnd
        window._loading = True
        try:
            window._load_params(rnd.RenderParams(gamma_display=1.8))
        finally:
            window._loading = False
        assert window.current_params().gamma_display == icc.GAMMA_DISPLAY

    def test_spin_edits_drive_sliders(self, window) -> None:
        """Textove pole (vpravo od jezdce) jsou plnohodnotny vstup."""
        window.spin_toe.setValue(0.62)
        window.spin_gamma.setValue(1.35)
        window.spin_shoulder.setValue(0.28)
        window.spin_sb.setValue(0.08)
        p = window.current_params()
        assert p.profile.toe == pytest.approx(0.62)
        assert p.profile.gamma == pytest.approx(1.35)
        assert p.profile.shoulder == pytest.approx(0.28)
        assert p.shadow_band == pytest.approx(0.08)
        # a jezdec sedí na stejné hodnote (obousmerne spojeni)
        assert window.sl_toe.value() == 62

    def test_shadow_band_widens_floor(self, window, tmp_path) -> None:
        """Větší stínový pás zvedne nejtmavší pixel — pod-base graduje.

        Hodnota nad rozsah jezdce (0,08) jde polem — pole sahá do 0,30 a
        jezdec se jen zaparkuje na konci bez zpětného oříznutí."""
        window.project.root = tmp_path
        window.spin_sb.setValue(0)
        window.save_render()
        fused = tifffile.imread(tmp_path / "derived" / "frame001.positive.tif")
        window.spin_sb.setValue(0.20)
        assert window.spin_sb.value() == pytest.approx(0.20)
        assert window.sl_sb.value() == window.sl_sb.maximum()  # zaparkován
        window.save_render()
        graded = tifffile.imread(tmp_path / "derived" / "frame001.positive.tif")
        assert int(graded.min()) > int(fused.min())

    def test_exposure_warning_toggles_overlay(self, window) -> None:
        """Přepínač překreslí náhled bez nového renderu."""
        window.spin_ev.setValue(3.0)   # +3 EV: pravá část přepálí škálu
        before = np.array(window.view._image.constBits(), copy=True)
        window.chk_warn.setChecked(True)
        after = np.array(window.view._image.constBits(), copy=True)
        assert not np.array_equal(before, after)   # červené světla přibydou
        window.chk_warn.setChecked(False)
        back = np.array(window.view._image.constBits(), copy=True)
        assert np.array_equal(before, back)        # vypnuto = původní obraz

    def test_output_histogram_fills(self, window) -> None:
        assert window.out_histogram._hist is not None

    def test_output_histogram_no_false_saturation(self, window) -> None:
        """Signál s maximem 0,98 není saturace — kolejnice musí mlčet.

        Dřív padla hodnota 1,0 (aniž by ji křivka ořízla) do posledního binu
        s uzavřenou prava hranou a histogram namaloval hřeben u zdi + text
        „světla" hlásil procenta, která v obraze nejsou.
        """
        near_white = np.linspace(0.02, 0.98, 64 * 64).reshape(64, 64)
        x = near_white * 0.9           # nikde >= 1.0 -> žádný ořez křivkou
        window.out_histogram.set_output(near_white, x=x)
        assert window.out_histogram._hi_pct == pytest.approx(0.0)
        assert window.out_histogram._lo_pct == pytest.approx(0.0)

    def test_output_histogram_reports_true_clip(self, window) -> None:
        """Skutečný ořez (x >= 1) se hlásí v procentech, ne v posledním binu."""
        out = np.full((8, 8), 1.0)
        x = np.full((8, 8), 1.2)       # křivka přestřelila škálu
        window.out_histogram.set_output(out, x=x)
        assert window.out_histogram._hi_pct == pytest.approx(100.0)
        # žádná data v binech — nasycené pixily patří jen do textu
        assert not window.out_histogram._hist.any()

    def test_output_histogram_cursor_line_follows_hover(self, window) -> None:
        """Oranžová jezdící čára i ve spodním histogramu (povel večer
        2026-09-21 — ruší ranní „žádné překryvy"; kolejnice pořád ne)."""
        window._pixel_hovered((1.2, 0.75))
        assert window.histogram._cursor_d == pytest.approx(1.2)     # horní
        assert window.out_histogram._cursor_out == pytest.approx(0.75)
        window._pixel_hovered(None)
        assert window.histogram._cursor_d is None
        assert window.out_histogram._cursor_out is None

    def test_output_histogram_cursor_silent_on_no_light(self, window) -> None:
        """NaN/inf pixel nemá na ose 0..1 pozici — čára zmizí, neskočí na kraj."""
        window._pixel_hovered((1.2, float("nan")))
        assert window.out_histogram._cursor_out is None

    def test_output_histogram_has_no_rails(self, window) -> None:
        """Kolejnice (±inf plné pruhy) tu dál nesmí být — povolený je jen kurzor."""
        assert not hasattr(window.out_histogram, "_clip_hi")
        assert not hasattr(window.out_histogram, "_neg_inf_fraction")

    def test_output_histogram_ignores_knob_clipping(self, window) -> None:
        """Jas/kontrast uříznou display na 1,0 i když křivka sahá max k 0,96 —
        saturaci (ať už hlásenou textem, nebo v posledním binu) měří osa x,
        nikdy hodnota po gammě. Reprodukce: histogram křičel přepálenou bílou,
        overlay exposure warningu neměl jedinou červenou."""
        from filmscan_studio.core import render as rnd
        from filmscan_studio.core.filmic import FilmicProfile
        params = rnd.RenderParams(
            dmin=0.6, dmax=3.0,
            profile=FilmicProfile(name="generic", toe=1.0, gamma=2.28,
                                  shoulder=0.45),
            brightness=0.15, contrast=2.0)
        d = np.linspace(0.6, 2.9, 64 * 64).reshape(64, 64)   # D těsně pod dmax
        x = rnd.positive_x(d, params)
        out = rnd.render_for_display(d, params)
        assert (out >= 1.0).mean() > 0.2          # páčky uřízly přes čtvrtinu
        assert x.max() < 1.0                       # křivka bílé nedosáhla
        window.out_histogram.set_output(out, x=x)
        assert window.out_histogram._hi_pct == pytest.approx(0.0)


class TestExposureSpin:
    """Jemný krok expozice: spin pojme i hodnoty mimo krok jezdce."""

    def test_spin_accepts_off_step_value(self, window) -> None:
        window.spin_ev.setValue(0.15)
        assert window.current_params().exposure_ev == pytest.approx(0.15)
        # jezdec se prisune na nejbлиžnich 5 ticku (0,05)
        assert window.sl_ev.value() == 15

    def test_slider_drives_spin(self, window) -> None:
        window.sl_ev.setValue(-75)                    # -0,75 EV
        assert window.spin_ev.value() == pytest.approx(-0.75)
        assert window.current_params().exposure_ev == pytest.approx(-0.75)

    def test_no_oscillation_slider_spin(self, window) -> None:
        # retezec obojstrannych spojeni se musi zastavit na jedne hodnote
        window.spin_ev.setValue(2.37)
        assert window.sl_ev.value() == 237
        assert window.spin_ev.value() == pytest.approx(2.37)


class TestPixelProbe:
    """Hover nad nahledem: oranzova cara v obou histogramech.

    Status radek praveho panelu byl odstranen (uzivatel 2026-09-21 vecer:
    „cele to dej pryc") — hover kresli jen cary, zadny text."""

    def test_hover_moves_cursor_lines_and_writes_no_text(self, window,
                                                         qtbot) -> None:
        from PySide6.QtCore import QPointF, QEvent
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QApplication
        assert not hasattr(window, "lbl_status")
        img = window.view._image
        assert img is not None
        # stred obrazu v widget souradnicich: map_px * zoom + origin
        z = window.view._zoom
        cx = img.width() * z / 2 + window.view._origin.x()
        cy = img.height() * z / 2 + window.view._origin.y()
        # qtbot.mouseMove v offscreen neposila hover bez tlacitka --
        # sinteticky QMouseEvent je to, co Qt posila pri skutecnem mysi-pohybu
        QApplication.sendEvent(
            window.view,
            QMouseEvent(QEvent.Type.MouseMove, QPointF(cx, cy),
                        QPointF(cx, cy), Qt.MouseButton.NoButton,
                        Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier))
        qtbot.wait(20)
        assert window.histogram._cursor_d is not None
        assert window.out_histogram._cursor_out is not None
        window.view.leaveEvent(None)
        qtbot.wait(20)
        assert window.histogram._cursor_d is None
        assert window.out_histogram._cursor_out is None


class TestHistogramAxis:
    def test_axis_extends_below_zero(self, window) -> None:
        d = np.array([[-0.3, -0.1, 0.0, 0.5, 1.2]] * 4, dtype=np.float32)
        window.histogram.set_density(d)
        assert window.histogram._xmin < -0.25   # data vlevo se nesmi oříznout

    def test_axis_never_thinner_than_min_span(self, window) -> None:
        d = np.full((8, 8), 0.05, dtype=np.float32)
        window.histogram.set_density(d)
        assert (window.histogram._xmax - window.histogram._xmin
                >= 0.5 - 1e-9)


class TestFlatlessProject:
    """Bez flatů musí náhled vyjít a status musí radit, ne tvrdit, že
    nemám otevřenou složku."""

    def test_preview_shows_and_status_flags_fallback(self, qtbot,
                                                     session, tmp_path) -> None:
        for p in (session / "frames").glob("flat_*.tif*"):
            p.unlink()
        proj = DevelopProject.open(session)
        w = MainWindow(project=proj)
        qtbot.addWidget(w)
        qtbot.waitUntil(lambda: w._density is not None, timeout=5000)
        assert w.view._image is not None            # náhled je
        # varování jde do spodní lišty — pravý panel se hoverem nepřeskupuje
        assert "bez flat" in w.statusBar().currentMessage()
        assert w.chk_dmin_auto.isChecked() is False  # Dmin ručně
        # Tlačítka exportu se mají odblokovat i bez flatu.
        assert w.btn_export.isEnabled()

    def test_dmax_auto_stays_above_dmin(self, qtbot, session,
                                        monkeypatch) -> None:
        # Relativní D bez flatu může mít dmax < Dmin (reálné test4: 0,15 vs
        # výchozích 0,2): auto dmax musí Dmin přece jen přesáhnout, jinak
        # RenderParams spadne.
        for p in (session / "frames").glob("flat_*.tif*"):
            p.unlink()
        proj = DevelopProject.open(session)
        monkeypatch.setattr(proj, "suggested_dmax", lambda _n: 0.1)
        w = MainWindow(project=proj)
        qtbot.addWidget(w)
        qtbot.waitUntil(lambda: w._density is not None, timeout=5000)
        # Proposal musel spodní bod stáhnout, aby dmax (0,1) prošlo.
        assert w.spin_dmin.value() < 0.1
        w.chk_dmax_auto.setChecked(False)
        w.chk_dmax_auto.setChecked(True)              # znova p99,9 + okraj
        qtbot.wait(20)
        assert w.spin_dmax.value() > w.spin_dmin.value()


class TestPerFrameSettings:
    def test_slider_move_is_remembered_per_frame(self, window, tmp_path,
                                                 qtbot) -> None:
        window.project.root = tmp_path
        window.sl_ev.setValue(500)                     # +5 EV
        qtbot.wait(20)
        stored = window.project.frame_settings["frame001.tif"]
        assert stored["exposure_ev"] == pytest.approx(5.0)
        assert (tmp_path / "develop_settings.json").exists()

    def test_switching_frames_restores_settings(self, window, tmp_path,
                                                qtbot) -> None:
        import numpy as np
        from filmscan_studio.core.rawio import open_frame, write_frame
        from tests.test_project import _write, SHUTTER_SCAN
        # Add a second frame to the session list at runtime.
        fr = open_frame(window.project.entry("frame001.tif").frame.path)
        p2 = window.project.entry("frame001.tif").frame.path.parent \
            / "frame002.tif"
        _write(p2, fr.data, "scan", SHUTTER_SCAN)
        from filmscan_studio.developer.project import DevelopProject
        proj2 = DevelopProject.open(window.project.root)
        window.set_project(proj2)
        qtbot.wait(20)
        assert window.frame_list.count() == 2
        window.sl_ev.setValue(-300)                    # -3 EV on frame001
        qtbot.wait(20)
        window.frame_list.setCurrentRow(1)             # frame002: fresh
        qtbot.wait(50)
        assert window.current_params().exposure_ev == pytest.approx(0.0)
        window.frame_list.setCurrentRow(0)             # back: restored
        qtbot.wait(50)
        assert window.current_params().exposure_ev == pytest.approx(-3.0)

    def test_settings_reload_on_reopen(self, window, tmp_path, qtbot) -> None:
        window.project.root = tmp_path
        window.sl_ev.setValue(200)                     # +2 EV
        qtbot.wait(20)
        from filmscan_studio.developer.project import DevelopProject
        again = DevelopProject.open(tmp_path)
        assert again.frame_settings["frame001.tif"]["exposure_ev"] \
            == pytest.approx(2.0)


class TestPositiveRender:
    def test_preview_is_positive_of_density(self, window) -> None:
        # The synthetic ramp rises in density left->right; the positive must
        # brighten left->right (dense = bright scene = bright print).
        assert window.view._image is not None
        px = [window.view._image.pixelColor(x, 5).red() for x in (2, 60)]
        assert px[1] > px[0]

    def test_export_applies_orientation(self, window, tmp_path) -> None:
        import numpy as np
        import tifffile
        from filmscan_studio.developer.project import DevelopProject
        window.project.root = tmp_path
        payload = {"film": {"film_id": "x", "mirrored_horizontal": True}}
        (window.project.root / "project.json").write_text(json.dumps(payload))
        window.project.mirrored_horizontal = True
        window.save_render()
        exported = tifffile.imread(
            tmp_path / "derived" / "frame001.positive.tif")
        d, _ = window.project.build_density("frame001.tif", crop=True)
        from filmscan_studio.core import render as rnd
        unflipped = rnd.quantise16(rnd.render_for_display(
            d, window.current_params()))
        assert not np.array_equal(exported, unflipped)
        flipped = rnd.quantise16(rnd.render_for_display(
            d[:, ::-1], window.current_params()))
        assert np.array_equal(exported, flipped)


class TestHistogram:
    def test_density_histogram_counts_finite_only(self) -> None:
        from filmscan_studio.developer.gui import density_histogram
        d = np.array([[0.1, 0.2, np.nan, np.inf, -np.inf, 0.2]],
                     dtype=np.float32)
        h = density_histogram(d, xmax=3.2, bins=64)
        assert h.max() == pytest.approx(1.0)      # normalizováno na peak
        assert h.shape == (64,)
        # inf/NaN nesmily vytvořit sloupec na koncích.
        assert h[0] == 0.0 and h[-1] == 0.0

    def test_widget_receives_density_and_params(self, window) -> None:
        assert window.histogram._hist is not None
        assert window.histogram._params is not None
        assert window.histogram._params.dmax == pytest.approx(
            window.spin_dmax.value(), abs=1e-3)

    def test_widget_rerenders_on_slider(self, window, qtbot) -> None:
        before = window.histogram._params.fingerprint()
        window.sl_ev.setValue(300)
        qtbot.wait(20)
        assert window.histogram._params.exposure_ev == pytest.approx(3.0)
        assert window.histogram._params.fingerprint() != before

    def test_widget_paints_without_crash(self, window, qtbot) -> None:
        # render() spouští paintEvent -- ten musí přežít prázdný i naplněný
        # stav (kdysi spadlý paintEvent kreslil dokola v nekonečné smyčce).
        from PySide6.QtGui import QImage
        img = QImage(window.histogram.size(), QImage.Format.Format_ARGB32)
        window.histogram.render(img)
        window.histogram.set_density(None)
        window.histogram.render(img)

    def test_shadow_band_moves_input_curve(self, window) -> None:
        """Stínový pás musí být vidět i v křivce vstupního histogramu.

        Křivka se počítá z render_density (positive_x vč. pásu), ne ze
        zjednodušeného vzorce bez pásu -- jinak šoupátko hýbe výstupem, ale
        v ladícím histogramu se nic neděje a operátor nevidí, co dělá.
        """
        from filmscan_studio.core import render as rnd
        params = window.current_params()
        xs = np.array([params.dmin])          # film base: bez pásu bod 0
        no_band = rnd.render_density(
            xs, dataclasses.replace(params, shadow_band=0.0))
        with_band = rnd.render_density(
            xs, dataclasses.replace(params, shadow_band=0.10))
        assert float(no_band[0]) == pytest.approx(0.0, abs=1e-6)
        assert float(with_band[0]) > float(no_band[0])

    def test_input_curve_excludes_display_gamma(self, window) -> None:
        """Bílá křivka horního histogramu = diagnostika tónové mapy.

        Dokument 07: musí být z render_density (lineární pozitiv), ne z
        render_for_display -- display gamma v ní dělá hrb u černé, který
        operátor mylně čte jako součást S-křivky.
        """
        import inspect

        from filmscan_studio.core import render as rnd
        src = inspect.getsource(
            type(window.histogram).paintEvent)
        assert "render_density" in src
        assert "render_for_display" not in src
        # matematicky: při gamma_display != 2 musí křivka (render_density)
        # zůstat na tom místě, kde ji mění jen tónová mapa
        params = window.current_params()
        d = np.array([(params.dmin + params.dmax) / 2.0])
        linear = rnd.render_density(
            d, dataclasses.replace(params, gamma_display=1.0))
        with_gamma = rnd.render_density(
            d, dataclasses.replace(params, gamma_display=3.0))
        assert np.allclose(linear, with_gamma)   # gamma do ní nevstupuje


class TestDoc08Layout:
    """Reorganizace panelu dle dok 08: skupiny, nazvy, vychozi rozsahy."""

    def test_groups_split_scale_curve_display(self, window) -> None:
        """Tři skupiny dle 08 §5: tolerance pod Dmin patří DO meritka filmu
        (společně s Dmin/Dmax), ne do zobrazení; jas/kontrast do zobrazení."""
        def box_of(w):
            q = w.parent()
            while q is not None and not isinstance(q, QGroupBox):
                q = q.parent()
            return q
        assert box_of(window.spin_dmin) is box_of(window.spin_dmax)
        assert box_of(window.sl_sb) is box_of(window.spin_dmin)
        assert box_of(window.sl_br) is box_of(window.sl_ct)
        assert box_of(window.sl_br) is not box_of(window.spin_dmin)
        assert box_of(window.sl_toe) is box_of(window.spin_gamma)
        titles = {box_of(w).title() for w in
                  (window.spin_dmin, window.sl_toe, window.sl_br)}
        assert titles == {"Meritko filmu", "Tónová křivka", "Zobrazení"}

    def test_curve_defaults_are_doc08_working_start(self, window) -> None:
        p = window.current_params()
        # okno nactene bez ulozenych nastaveni = Proposal = vychozi S-ko
        # toe 0,20 / gamma 1,35 / shoulder 0,20 (08 §2)
        assert p.profile.toe == pytest.approx(0.20)
        assert p.profile.gamma == pytest.approx(1.35)
        assert p.profile.shoulder == pytest.approx(0.20)

    def test_curve_sliders_cover_common_range_only(self, window) -> None:
        """Jezdec = bezne ladeni (08 §2); pole nad nej = specialni komprese."""
        assert (window.sl_toe.minimum(), window.sl_toe.maximum()) == (0, 80)
        assert (window.sl_shoulder.minimum(),
                window.sl_shoulder.maximum()) == (0, 80)
        assert (window.sl_gamma.minimum(), window.sl_gamma.maximum()) \
            == (80, 250)                       # 0,80 … 2,50
        assert window.spin_toe.maximum() == pytest.approx(1.66)
        assert window.spin_shoulder.maximum() == pytest.approx(1.66)

    def test_shadow_band_slider_common_range(self, window) -> None:
        assert (window.sl_sb.minimum(), window.sl_sb.maximum()) == (0, 8)

    def test_out_of_range_stored_value_survives_load(self, window) -> None:
        """Načtené starší nastavení mimo jezdec se NESMÍ tiše oříznout
        řetězcem jezdec→pole (skutečná data: frame001 toe 1,0; 028 gamma 3,0;
        033 shadow_band 0,3)."""
        from filmscan_studio.core import render as rnd
        from filmscan_studio.core.filmic import FilmicProfile
        window._loading = True
        try:
            window._load_params(rnd.RenderParams(
                dmin=0.622, dmax=3.0,
                profile=FilmicProfile(toe=1.0, gamma=3.0, shoulder=0.45),
                shadow_band=0.30))
        finally:
            window._loading = False
        p = window.current_params()
        assert p.profile.toe == pytest.approx(1.0)
        assert p.profile.gamma == pytest.approx(3.0)
        assert p.shadow_band == pytest.approx(0.30)
        # jezdce jsou zaparkované na konci, data zachována v polích
        assert window.sl_toe.value() == window.sl_toe.maximum()
        assert window.sl_gamma.value() == window.sl_gamma.maximum()
        assert window.sl_sb.value() == window.sl_sb.maximum()


class TestExportMetadata:
    """Dok. 09: export JPEG/HEIC nese EXIF+XMP ze sidecaru (anotace + film)."""

    ANNOTATION = {
        "title": "Vacation 2026", "note": "uvodni komentar",
        "tags": ["vacation"], "rating": 3,
        "capture_datetime": "2026:11:08 00:01:00",
        "gps_lat": "50.08747000", "gps_lon": "14.42756000",
        "gps_lat_ref": "N", "gps_lon_ref": "E",
        "rotation_degrees": 90,
    }
    FILM = {
        "film_id": "HP5_001", "film_name": "Ilford HP5 Plus",
        "camera": "Nikon FM2", "shooting_lens": "Nikkor 50/2",
        "film_iso": "400", "format": "35mm",
    }

    def _annotate(self, session) -> None:
        """Anotace do sidecaru jako anotátor — merge, ne přepis."""
        sidecar = session / "frames" / "frame001.tif.json"
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        data["annotation"] = dict(self.ANNOTATION)
        data["film"] = dict(self.FILM)
        sidecar.write_text(json.dumps(data), encoding="utf-8")

    def test_jpeg_export_embeds_exif_and_xmp(self, window, session,
                                             tmp_path) -> None:
        Image = pytest.importorskip("PIL.Image")
        self._annotate(session)
        window.project = DevelopProject.open(session)
        window.project.root = tmp_path
        window.save_jpeg()
        with Image.open(tmp_path / "derived" / "HP5_frame001.jpg") as im:
            top = im.getexif()
            # popis (komentář první) → ImageDescription, název → dc:title
            assert top[0x010E].startswith("uvodni komentar")
            assert top[0x0110] == "FM2"            # fallback split z "Nikon FM2"
            assert top[0x010F] == "Nikon"
            # rotace se zapéká do pixelů, tag Orientation se nepíše
            assert 0x0112 not in top
            ifd = top.get_ifd(0x8769)
            assert ifd[0x8827] == 400
            assert ifd[0x9003] == "2026:11:08 00:01:00"
            assert 0x9286 not in ifd               # UserComment už se nepíše
            assert float(top.get_ifd(0x8825)[2][0]) == 50.0
            assert b"<dc:title>" in im.info["xmp"]
            assert "Vacation 2026".encode() in im.info["xmp"]
            assert "vacation".encode() in im.info["xmp"]
            assert b"<xmp:Rating>3" in im.info["xmp"]

    def test_export_names_carry_film_prefix(self, window, session,
                                            tmp_path) -> None:
        """Rozkaz 2026-09-22: „z 'K16O04_2026-07-22' udělej název
        'K16O04_frame001.mono10.heic'". Prefix = film_id před první
        podtržítkem; bez film_id zůstává staré jméno."""
        self._annotate(session)                    # FILM má film_id HP5_001
        window.project = DevelopProject.open(session)
        assert window.project.film_id == "HP5_001"
        assert window.project.export_prefix == "HP5"
        window.project.root = tmp_path
        window.save_jpeg()
        window.save_density()
        assert (tmp_path / "derived" / "HP5_frame001.jpg").exists()
        assert (tmp_path / "derived" / "HP5_frame001.density.tif").exists()
        # bez filmu (film_id prázdné) → bez prefixu
        window.project.film_id = ""
        window.save_jpeg()
        assert (tmp_path / "derived" / "frame001.jpg").exists()

    def test_rotation_baked_into_pixels(self, window, session,
                                        tmp_path) -> None:
        """Rozkaz 2026-09-22: „orientation je ok, jen implementuj a ť exportovaný
        soubor je takto otočen developerem" — rotation_degrees se zapéká do
        pixelů (48×64 → 64×48), Archivní density TIFF se neotáčí."""
        Image = pytest.importorskip("PIL.Image")
        self._annotate(session)                    # ANNOTATION má rotaci 90
        window.project = DevelopProject.open(session)
        window.project.root = tmp_path
        window.save_jpeg()
        with Image.open(tmp_path / "derived" / "HP5_frame001.jpg") as im:
            assert im.size == (48, 64)             # JPEG: výška×šířka prohozeně
            assert 0x0112 not in im.getexif()      # žádný tag, jen pixely
        window.save_density()                      # archiv zůstává neotočený
        import tifffile
        with tifffile.TiffFile(tmp_path / "derived"
                               / "HP5_frame001.density.tif") as tf:
            assert tf.pages[0].shape == (48, 64)

    def test_record_without_data_has_no_exif(self, window, tmp_path) -> None:
        """Prázdný record = žádné APP1/XMP — export byte shodný s minulostí."""
        Image = pytest.importorskip("PIL.Image")
        window.project.root = tmp_path
        # i bez anotací nese sidecar akvizici (DateTimeDigitized) — proto
        # explicitně vypnout všechno: tohle je faktický stav starého archivu
        window.project.frames[0].record = {}
        window.save_jpeg()
        with Image.open(tmp_path / "derived" / "frame001.jpg") as im:
            assert dict(im.getexif()) == {}
            assert "xmp" not in im.info

    def test_heic_export_embeds_exif(self, window, session, tmp_path) -> None:
        Image = pytest.importorskip("PIL.Image")
        pillow_heif = pytest.importorskip("pillow_heif")
        self._annotate(session)
        window.project = DevelopProject.open(session)
        window.project.root = tmp_path
        window.save_heic_mono()
        heif = pillow_heif.read_heif(tmp_path / "derived"
                                     / "HP5_frame001.mono10.heic")
        top = Image.Exif()
        top.load(heif.info["exif"])
        assert top[0x010F] == "Nikon"
        assert top.get_ifd(0x8769)[0x4746] == 3

    def test_broken_sidecar_never_kills_export(self, window, session,
                                               tmp_path) -> None:
        """Vlastní chyba EXIFu musí přežet export — metadata jsou bonus."""
        window.project.root = tmp_path
        window.project.frames[0].record = {"annotation": {"rating": "blbost"}}
        window.save_jpeg()                        # nesmí vyhodit
        assert (tmp_path / "derived" / "frame001.jpg").exists()
