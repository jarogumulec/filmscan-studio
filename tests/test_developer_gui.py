"""GUI tests of the developer window, offscreen against the synthetic session.

Contract tests, not pixel tests: opening a project fills the list and the
preview; slider moves re-render without crashing; Shift-drag sets a rect and
re-measures; exports write the right files with the right headers.
"""

from __future__ import annotations

import json

import numpy as np
import pytest
import tifffile

pytest.importorskip("pytestqt")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402

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
    def test_nan_pixels_get_the_mask_color(self) -> None:
        img = np.array([[0.5, np.nan]])
        q = density_to_qimage(img)
        assert q.pixelColor(1, 0).name() == "#ff00ff"
        assert q.pixelColor(0, 0).name() in ("#808080", "#7f7f7f")

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
        assert window.btn_save_density.isEnabled()

    def test_exposure_slider_rerenders(self, window, qtbot) -> None:
        before = np.array(window.view._image.constBits()[:64], copy=True)
        window.sl_ev.setValue(30)   # +10 EV -- picture must respond
        qtbot.wait(20)
        after = np.array(window.view._image.constBits()[:64], copy=True)
        assert not np.array_equal(before, after)
        assert window.current_params().exposure_ev == pytest.approx(10.0)

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
        """The exported file is the render of the current params, byte-exact."""
        from filmscan_studio.core import density as dens
        from filmscan_studio.core import render as rnd

        window.project.root = tmp_path
        window.save_render()
        out = tmp_path / "derived" / "frame001.positive.tif"
        exported = tifffile.imread(out)
        d, _prov = window.project.build_density("frame001.tif", crop=True)
        expected = rnd.quantise16(rnd.render_density(d,
                                                     window.current_params()))
        assert np.array_equal(exported, expected)


class TestPerFrameSettings:
    def test_slider_move_is_remembered_per_frame(self, window, tmp_path,
                                                 qtbot) -> None:
        window.project.root = tmp_path
        window.sl_ev.setValue(15)                      # +5 EV
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
        window.sl_ev.setValue(-9)                      # -3 EV on frame001
        qtbot.wait(20)
        window.frame_list.setCurrentRow(1)             # frame002: fresh
        qtbot.wait(50)
        assert window.current_params().exposure_ev == pytest.approx(0.0)
        window.frame_list.setCurrentRow(0)             # back: restored
        qtbot.wait(50)
        assert window.current_params().exposure_ev == pytest.approx(-3.0)

    def test_settings_reload_on_reopen(self, window, tmp_path, qtbot) -> None:
        window.project.root = tmp_path
        window.sl_ev.setValue(6)                       # +2 EV
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
        unflipped = rnd.quantise16(rnd.render_density(
            d, window.current_params()))
        assert not np.array_equal(exported, unflipped)
        flipped = rnd.quantise16(rnd.render_density(
            d[:, ::-1], window.current_params()))
        assert np.array_equal(exported, flipped)
