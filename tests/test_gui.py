"""GUI tests against MockCamera, offscreen.

These are contract tests for the brief's hard rules, not pixel tests: the two
preview modes must differ in the image but *not* in the histogram, the zoom
selector must reconfigure the *stream* (binned overview vs hardware ROI), and
no camera call may run on the UI thread. (The old "Capture refuses a
non-archival gain" rule was removed 2026-09-19 on the operator's order —
`test_capture_allowed_at_any_gain` pins the new freedom.)
"""

from __future__ import annotations

import json

import re

import numpy as np
import pytest

pytest.importorskip("pytestqt")

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402

from filmscan_studio.capture.camera import LiveFrame  # noqa: E402
from filmscan_studio.capture.mock import MockCamera  # noqa: E402
from filmscan_studio.core.models import FilmMetadata  # noqa: E402
from filmscan_studio.core.zoom import FIT, NO_BINNING, OVERVIEW_BINNING  # noqa: E402
from filmscan_studio.gui.capture_window import CaptureWindow  # noqa: E402
from filmscan_studio.gui.imageutil import to_qimage  # noqa: E402
from filmscan_studio.gui.liveview import LiveViewWorker  # noqa: E402
from filmscan_studio.gui.widgets import ZOOM_LEVELS, HistogramWidget, ZoomView  # noqa: E402


@pytest.fixture
def camera() -> MockCamera:
    cam = MockCamera(scene_level=0.3)
    cam.connect()
    yield cam
    cam.disconnect()


@pytest.fixture
def window(qtbot, camera) -> CaptureWindow:
    w = CaptureWindow(camera=camera)
    qtbot.addWidget(w)
    # The window started its own Live View worker in the constructor; wait for
    # it to deliver a real frame rather than injecting one behind its back.
    # (qtbot.waitUntil pumps the event loop, which is what delivers queued
    # cross-thread signals here — QTest.qWait does not, learned the hard way.)
    qtbot.waitUntil(lambda: w._last_linear is not None, timeout=5000)
    return w


class TestModes:
    def test_modes_show_different_images(self, window, qtbot):
        raw_display = window._display_for(window._last_linear).copy()
        window.neg_toggle.setChecked(True)
        positive_display = window._display_for(window._last_linear)
        # The fast positive renders 3-channel (the mono frame repeats), the
        # raw view stays 2-D — and the picture itself differs too: a negative
        # with the filmic curve is not the gamma-only raw view.
        assert positive_display.ndim == 3 and raw_display.ndim == 2
        assert not np.allclose(raw_display, positive_display[..., 0])

    def test_histogram_stays_linear_across_modes(self, window):
        image = window._last_linear
        reading = window._last_reading
        window._set_mode(raw_view=True)
        window._update_histogram(image, reading)
        raw_hist = window.histogram._hist
        window._set_mode(raw_view=False)
        window._update_histogram(image, reading)
        positive_hist = window.histogram._hist
        # The brief: histogram is always linear sensor data. The frame
        # metered is the same frame, so the histogram must be identical.
        assert np.array_equal(raw_hist.counts, positive_hist.counts)

    def test_curve_overlay_only_in_positive(self, window):
        image, reading = window._last_linear, window._last_reading
        window._set_mode(raw_view=True)
        window._update_histogram(image, reading)
        assert window.histogram._curve is None
        assert "RAW View" in window.histogram_label.text()
        window._set_mode(raw_view=False)
        window._update_histogram(image, reading)
        assert window.histogram._curve is not None
        assert "křivka" in window.histogram_label.text()

    def test_mode_checkboxes_are_mutually_exclusive(self, window):
        # The reported bug (2026-09): clicking Negativ unticked RAW View but
        # also ticked the (now removed) Positive box — two modes at once. The
        # row is two boxes now and exactly one is ever ticked.
        assert not hasattr(window, "mode_positive")
        assert window.mode_raw.isChecked() and not window.neg_toggle.isChecked()
        window.neg_toggle.setChecked(True)
        assert window.neg_toggle.isChecked() and not window.mode_raw.isChecked()
        # Unchecking the active mode must not leave the app with no mode.
        window.neg_toggle.setChecked(False)
        assert window.mode_raw.isChecked()

    def test_invert_checkbox_follows_the_mode(self, window):
        # The reported contradiction (2026-09): a RAW View sat under a ticked
        # "Invertovat (negativ)". The invert checkbox is a *view* of the mode,
        # so it must agree with what the picture is actually doing.
        assert window.raw_view
        assert not window.filmic.invert.isChecked()
        window.neg_toggle.setChecked(True)
        assert window.filmic.invert.isChecked() and not window.raw_view
        window.neg_toggle.setChecked(False)
        assert not window.filmic.invert.isChecked() and window.raw_view
        # Driving it from the RAW View side agrees too.
        window.neg_toggle.setChecked(True)
        window.mode_raw.setChecked(True)
        assert not window.filmic.invert.isChecked() and window.raw_view

    def test_positive_param_invert_tracks_the_mode(self, window):
        # invert is baked into PositiveParams; a RAW View that ignores it must
        # not leave positive.invert lying about the displayed frame.
        window.neg_toggle.setChecked(True)
        assert window.positive.invert is True
        window.mode_raw.setChecked(True)
        assert window.positive.invert is False

    def test_filmic_panel_disabled_in_raw_view(self, window):
        assert not window.filmic.isEnabled()
        window.neg_toggle.setChecked(True)
        assert window.filmic.isEnabled()

    def test_meter_label_reports_utilisation(self, window):
        assert "plného rozsahu" in window.meter_label.text()

    def test_meter_label_flags_both_rails(self, window):
        """Red PŘEPAL on a blown reading, blue PODEXP on a crushed one."""
        from filmscan_studio.core.exposure import MeterReading

        blown = MeterReading(0.0, 1.0, 0.1, 0.5, 1.0, 1.0,
                             clipped_fraction=0.01, near_black_fraction=0.0)
        crushed = MeterReading(0.0, 1.0, 0.0, 0.05, 0.4, 0.6,
                               clipped_fraction=0.0, near_black_fraction=0.5,
                               black_fraction=0.5)
        window._update_meter_label(blown)
        assert "PŘEPAL" in window.meter_label.text()
        assert "PODEXP" not in window.meter_label.text()
        window._update_meter_label(crushed)
        assert "PODEXP" in window.meter_label.text()
        assert "PŘEPAL" not in window.meter_label.text()


class TestCaptureWorkflow:
    def test_buttons_disabled_without_film(self, window):
        assert not window.btn_capture.isEnabled()
        assert not window.btn_dark.isEnabled()

    def test_calibration_buttons_are_parented_into_the_box(self, window):
        """Regression: the 2026-09 calibration-box refactor silently dropped
        the Flat Field button — the widget was built, connected and styled,
        but never addWidget()'ed, so it vanished from the UI. A widget with
        no parent is no widget at all."""
        for button in (window.btn_dark, window.btn_flat,
                       window.btn_base_mode, window.btn_base_stream,
                       window.btn_base_frame):
            assert button.parent() is not None

    def test_session_start_enables_workflow(self, window, tmp_path, qtbot):
        from filmscan_studio.capture.session import CaptureSession, SessionPaths

        film = FilmMetadata(film_id="HP5_001", operator="JG")
        paths = SessionPaths.create(tmp_path, film.film_id)
        window.session = CaptureSession(camera=window.camera, film=film, paths=paths)
        window._refresh_buttons()
        assert window.btn_capture.isEnabled()

        window._capture(kind="dark")
        qtbot.waitUntil(lambda: window.session.state.dark_count == 1, timeout=60000)
        window._capture(kind="flat")
        # 2026-09-19: a flat is ONE averaged set now, not three files.
        qtbot.waitUntil(lambda: window.session.state.flat_count == 1, timeout=60000)
        window._capture(scan=True)
        qtbot.waitUntil(lambda: window.session.state.scan_count == 1, timeout=10000)

        sidecars = list(paths.frames.glob("*.json"))
        assert len(sidecars) == 3
        assert "3) Snímání" in window.stage_label.text()

    def test_average_spin_defaults_to_one(self, qtbot, camera):
        # Hermetic: a leaked setting from another test (or a real app run on
        # this machine) must not decide what "default" means here — hence a
        # window built fresh, after the key is wiped.
        from PySide6.QtCore import QSettings

        QSettings("filmscan-studio", "Capture").remove("capture/average_frames")
        fresh = CaptureWindow(camera=camera)
        qtbot.addWidget(fresh)
        assert fresh.average_spin.value() == 1
        assert fresh.average_spin.minimum() == 1

    def test_scan_passes_average_count_to_backend(self, window, tmp_path, qtbot):
        """The spinbox beside Capture is the exposure count the backend
        averages into the single archived TIFF (operator ruling 2026-09-19)."""
        from PySide6.QtCore import QSettings

        from filmscan_studio.capture.session import CaptureSession, SessionPaths

        settings = QSettings("filmscan-studio", "Capture")
        settings.remove("capture/average_frames")     # hermetic start
        film = FilmMetadata(film_id="HP5_AVG", operator="JG")
        paths = SessionPaths.create(tmp_path, film.film_id)
        window.session = CaptureSession(camera=window.camera, film=film, paths=paths)
        window.average_spin.setValue(3)
        window._capture(scan=True)
        qtbot.waitUntil(lambda: window.session.state.scan_count == 1, timeout=20000)
        assert window.camera.last_capture_frames == 3
        assert len(list(paths.frames.glob("frame*.tif"))) == 1
        settings.remove("capture/average_frames")     # don't leak to the app

    def test_scan_triggers_post_capture_audit(self, window, tmp_path, qtbot):
        """The linear stream makes the TIFF audit a confirmation, but it still
        runs and its verdict must reach the log."""
        from filmscan_studio.capture.session import CaptureSession, SessionPaths

        film = FilmMetadata(film_id="HP5_002", operator="JG")
        paths = SessionPaths.create(tmp_path, film.film_id)
        window.session = CaptureSession(camera=window.camera, film=film, paths=paths)
        window._capture(scan=True)
        qtbot.waitUntil(lambda: window._last_audit is not None, timeout=15000)
        # Verdict wording follows the mock's exposure state (the default
        # shutter underexposes scene 0.3); any verdict proves the audit ran.
        assert "EV" in window._last_audit
        assert "náhled" in window.log_view.text()   # preview JPEG was rendered

    def test_preview_renders_even_when_audit_fails(self, window, tmp_path, qtbot,
                                                   monkeypatch):
        """The old D750 pipeline bailed out of the whole post-capture job when
        the audit raised, so a corrupt/oversize TIFF left the operator without
        a JPEG of a frame they actually took. Audit and JPEG are independent
        try/excepts now — a dead audit must still paint the preview."""
        from filmscan_studio.capture.session import CaptureSession, SessionPaths
        from filmscan_studio.gui import capture_window as cw

        def exploding_audit(*_args, **_kwargs):
            raise RuntimeError("audit schytal výjimku")

        monkeypatch.setattr(cw, "audit_frame", exploding_audit)
        film = FilmMetadata(film_id="HP5_003", operator="JG")
        paths = SessionPaths.create(tmp_path, film.film_id)
        window.session = CaptureSession(camera=window.camera, film=film, paths=paths)
        window._capture(scan=True)
        # Audit raised -> _last_audit stays None, but the JPEG still renders.
        qtbot.waitUntil(
            lambda: "náhled" in window.log_view.text(), timeout=15000)
        assert window._last_audit is None           # audit genuinely failed
        assert any(p.suffix == ".jpg" for p in paths.frames.iterdir())

    def test_scan_stores_drawn_rect_as_crop_in_full_px(self, window, tmp_path,
                                                       qtbot):
        """The red frame doubles as the crop cue for the developer GUI: it is
        drawn on the 3×3-binned overview stream but must reach the sidecar in
        *full-size frame* px — storing stream px would crop a third of a third
        of the picture."""
        import json

        from filmscan_studio.capture.session import CaptureSession, SessionPaths

        film = FilmMetadata(film_id="HP5_004", operator="JG")
        paths = SessionPaths.create(tmp_path, film.film_id)
        window.session = CaptureSession(camera=window.camera, film=film, paths=paths)
        assert window._stream_binned          # overview: the stream is 3×3 binned
        window._on_ae_rect(QRect(100, 80, 1800, 1200))
        expected = window._ae_rect_in_sensor_px()
        assert expected is not None
        window._capture(scan=True)
        qtbot.waitUntil(lambda: window.session.state.scan_count == 1, timeout=10000)
        sidecar = next(p for p in paths.frames.glob("*.json"))
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        assert payload["crop_rect"] == list(expected)
        # Explicitly *not* the stream-px rect the operator dragged.
        assert payload["crop_rect"] != [100, 80, 1900, 1280]

    def test_capture_button_stays_disabled_while_busy(self, window, tmp_path, qtbot):
        from filmscan_studio.capture.session import CaptureSession, SessionPaths

        film = FilmMetadata(film_id="X_1")
        paths = SessionPaths.create(tmp_path, film.film_id)
        window.session = CaptureSession(
            camera=SlowCaptureCamera(), film=film, paths=paths
        )
        window._refresh_buttons()
        window._capture(scan=True)
        assert not window.btn_capture.isEnabled()
        # The catalog row lands from the camera thread; re-enabling follows via
        # the queued on_done relay a moment later — wait for the re-enable
        # itself, not for the count (which can be visible first; that raced).
        qtbot.waitUntil(lambda: window.btn_capture.isEnabled(), timeout=10000)
        assert window.session.state.scan_count == 1


class TestConnect:
    """The connect button path — a single-argument callback into the window.

    The earlier bug lived exactly here: the worker calls back with one argument
    and the fixture injected the camera through the constructor instead, so the
    tested path and the clicked path were not the same path.
    """

    def test_connect_delivers_single_camera_argument(self, qtbot, camera):
        w = CaptureWindow()
        qtbot.addWidget(w)
        assert w.camera is None
        # Exactly the shape CameraWorker uses: on_done(result).
        w._on_connected(camera)
        assert w.camera is camera
        assert w.info == camera.info  # MockCamera.info is a property, not identity-stable
        assert w.act_new_film.isEnabled()  # the connected state is really applied

    def test_connect_failure_names_the_power_hint(self, qtbot, monkeypatch):
        w = CaptureWindow()
        qtbot.addWidget(w)
        w._connecting = True
        shown = {}
        monkeypatch.setattr(
            "filmscan_studio.gui.capture_window.QMessageBox.critical",
            staticmethod(lambda *a, **k: shown.setdefault("shown", a[2])),
        )
        w._on_connect_failed("Nenalezena žádná kamera")
        assert "Nenalezena" in shown["shown"]
        # The #1 cause on this camera is a missing power supply — say so.
        assert "11–14 V" in shown["shown"]
        assert not w._connecting


class TestZoomView:
    """Pure widget geometry — the stream wiring lives in TestStreamModeWiring."""

    def test_zoom_ladder_is_sensor_pixels(self):
        # 100% = one screen pixel per *sensor* pixel (of the 6224×4168 TIFF),
        # not per stream pixel: the sub-1 ladders that scaled the old 640px
        # JPEG preview are intentionally gone with the D750.
        assert ZOOM_LEVELS == (FIT, 1.0, 2.0, 4.0)

    def test_set_zoom_rejects_off_ladder_and_emits(self, qtbot):
        view = ZoomView()
        qtbot.addWidget(view)
        view.resize(200, 200)
        view.show()
        view.set_image(np.zeros((100, 100)))
        with qtbot.waitSignal(view.zoomChanged):
            view.set_zoom(2.0)
        assert view.zoom() == 2.0
        with pytest.raises(ValueError):
            view.set_zoom(3.0)

    def test_center_uv_reports_stream_pixels(self, qtbot):
        """The centre lives in sensor-scaled coords internally; the window
        needs the stream-pixel grid to aim a hardware ROI."""
        view = ZoomView()
        qtbot.addWidget(view)
        view.resize(200, 200)
        view.set_source_scale(3.0)
        view.set_image(np.zeros((100, 100)))   # centre = 150,150 scaled px
        cx, cy = view.center_uv()
        assert (cx, cy) == pytest.approx((50.0, 50.0))

    def test_click_recenters_on_release(self, qtbot):
        view = ZoomView()
        qtbot.addWidget(view)
        view.resize(200, 200)
        view.show()
        view.set_image(np.zeros((400, 400)))
        view.set_zoom(1.0)
        before = QPoint(view._center)
        view.mousePressEvent(_mouse_event(10, 10))
        view.mouseReleaseEvent(_mouse_event(10, 10))
        assert view._center != before  # moved toward click

    SHIFT = Qt.KeyboardModifier.ShiftModifier

    def test_shift_drag_draws_ae_rect_not_recenter(self, qtbot):
        view = ZoomView()
        qtbot.addWidget(view)
        view.resize(200, 200)
        view.show()
        view.set_image(np.zeros((400, 400)))
        view.set_zoom(1.0)
        before = QPoint(view._center)
        with qtbot.waitSignal(view.aeRectChanged):
            view.mousePressEvent(_mouse_event(20, 20, modifiers=self.SHIFT))
            view.mouseMoveEvent(_mouse_event(90, 70, modifiers=self.SHIFT))
            view.mouseReleaseEvent(_mouse_event(90, 70, modifiers=self.SHIFT))
        rect = view.ae_rect()
        assert rect is not None and rect.width() > 0
        assert view._center == before  # an AE drag must not pan

    def test_plain_drag_pans_when_zoomed(self, qtbot):
        """Zoomed in, drag = move around the picture (focus the point of
        interest), not an AE rectangle. Shift is the AE modifier."""
        view = ZoomView()
        qtbot.addWidget(view)
        view.resize(200, 200)
        view.show()
        view.set_image(np.zeros((400, 400)))
        view.set_zoom(1.0)
        before = QPoint(view._center)
        view.mousePressEvent(_mouse_event(150, 150))
        view.mouseMoveEvent(_mouse_event(50, 50))
        view.mouseReleaseEvent(_mouse_event(50, 50))
        assert view.ae_rect() is None            # plain drag never draws AE
        moved = QPoint(view._center)
        assert moved != before                    # it panned
        # Dragged image up-left → the sensor point moved the other way.
        assert moved.x() > before.x() and moved.y() > before.y()

    def test_plain_drag_at_fit_still_draws_ae_rect(self, qtbot):
        view = ZoomView()
        qtbot.addWidget(view)
        view.resize(200, 200)
        view.show()
        view.set_image(np.zeros((400, 400)))      # zoom == FIT
        with qtbot.waitSignal(view.aeRectChanged):
            view.mousePressEvent(_mouse_event(20, 20))
            view.mouseMoveEvent(_mouse_event(90, 70))
            view.mouseReleaseEvent(_mouse_event(90, 70))
        assert view.ae_rect() is not None

    def test_zoomed_view_fills_the_widget(self, qtbot):
        """Zoomed frames must fill the view, and at a whole multiple only: a
        requested 0.5× of a 1:1 stream clamps UP to ×1 and the view pans, it
        never shrinks the stream below its native size."""
        view = ZoomView()
        qtbot.addWidget(view)
        view.resize(200, 200)
        view.show()
        view.set_image(np.zeros((400, 400)))
        view.set_source_scale(1.0)
        view.set_zoom(1.0)
        assert view.source_zoom() == 1.0
        crop, dest = view._crop_rect()
        assert dest.width() >= view.width() - 1
        assert dest.height() >= view.height() - 1
        assert crop.width() <= 400 and crop.height() <= 400

    def test_zoom_snaps_to_whole_multiples_of_the_stream(self, qtbot):
        """'ať není 3.425×' — display is whole screen px per stream px."""
        view = ZoomView()
        qtbot.addWidget(view)
        view.resize(600, 400)
        view.show()
        view.set_image(np.zeros((416, 622)))
        view.set_source_scale(3.0)          # a binned overview stream
        view.set_zoom(1.0)                  # 1.0 * 3 = 3.0 screen px per stream px
        assert view.source_zoom() == 3.0
        view.set_zoom(2.0)                  # 2.0 * 3 = 6.0
        assert view.source_zoom() == 6.0
        view.set_zoom(4.0)                  # 12.0 — the ROI level shown over
        assert view.source_zoom() == 12.0   # the overview interpolates ×4

    def test_right_click_clears_ae_rect(self, qtbot):
        view = ZoomView()
        qtbot.addWidget(view)
        view.resize(200, 200)
        view.show()
        view.set_image(np.zeros((400, 400)))
        view.set_zoom(1.0)
        view.mousePressEvent(_mouse_event(20, 20, modifiers=self.SHIFT))
        view.mouseReleaseEvent(_mouse_event(90, 70, modifiers=self.SHIFT))
        assert view.ae_rect() is not None
        view.mousePressEvent(_mouse_event(5, 5, Qt.MouseButton.RightButton))
        assert view.ae_rect() is None

    def test_detail_note_set(self, qtbot):
        view = ZoomView()
        qtbot.addWidget(view)
        view.set_detail_note("stream 2074×1389")
        assert view._detail_note == "stream 2074×1389"


class TestStreamModeWiring:
    """Selecting a zoom level must reconfigure the *stream* (binning / ROI)."""

    def test_low_zooms_keep_the_binned_overview(self, window):
        # Below the honesty limit (one overview px = 3×3 sensor px) the
        # overview already shows every real detail; no stream request is made.
        for level in (1.0, 2.0, FIT):
            window.zoom_select.setCurrentIndex(window.zoom_select.findData(level))
        assert window.camera.stream_history == []
        assert window._roi_applied is None
        assert "overview" in window.zoom_note.text()

    def test_zoom_4_switches_to_a_hardware_roi(self, window):
        window.zoom_select.setCurrentIndex(window.zoom_select.findData(4.0))
        assert window._roi_applied is not None
        assert window._stream_binned is False
        binning, roi = window.camera.stream_history[-1]
        assert binning == NO_BINNING
        assert roi is not None and roi.width == 1200
        x0, y0, w, h = window._roi_applied
        assert (w, h) == (1200, 1200)
        assert (x0, y0) == (roi.x, roi.y)
        assert "ROI" in window.zoom_note.text() and "1:1" in window.zoom_note.text()

    def test_roi_frames_arrive_at_full_scale(self, window, qtbot):
        window.zoom_select.setCurrentIndex(window.zoom_select.findData(4.0))
        qtbot.waitUntil(lambda: window._stream_size == (1200, 1200), timeout=5000)
        assert window.view.source_scale() == 1.0
        # The note is recomputed per frame: ×4 of a 1:1 ROI is ×4 display,
        # not the stale ×12 of the overview it replaced.
        qtbot.waitUntil(lambda: "zobrazení ×4" in window.zoom_note.text(), timeout=3000)
        assert "interpolace" in window.zoom_note.text()

    def test_fit_returns_to_the_overview(self, window, qtbot):
        window.zoom_select.setCurrentIndex(window.zoom_select.findData(4.0))
        qtbot.waitUntil(lambda: window._stream_size == (1200, 1200), timeout=5000)
        window.zoom_select.setCurrentIndex(window.zoom_select.findData(FIT))
        assert window.camera.stream_history[-1] == (OVERVIEW_BINNING, None)
        assert window._roi_applied is None and window._stream_binned
        qtbot.waitUntil(lambda: window._stream_size[0] > 1200, timeout=5000)

    def test_stream_switch_clears_the_ae_rect(self, window):
        # The red rect is in *stream* px; the new stream delivers different
        # pixels, so keeping it would meter the wrong area. Drawn as a real
        # Shift-drag on the live frame: the clear must propagate back to the
        # window through the view's own signal, not just reset one side.
        shift = Qt.KeyboardModifier.ShiftModifier
        window.view.mousePressEvent(_mouse_event(20, 20, modifiers=shift))
        window.view.mouseMoveEvent(_mouse_event(90, 70, modifiers=shift))
        window.view.mouseReleaseEvent(_mouse_event(90, 70, modifiers=shift))
        assert window._ae_rect is not None
        window.zoom_select.setCurrentIndex(window.zoom_select.findData(4.0))
        assert window.view.ae_rect() is None
        assert window._ae_rect is None

    def test_zoom_note_discloses_interpolation(self, window):
        window.zoom_select.setCurrentIndex(window.zoom_select.findData(4.0))
        assert "interpolace" in window.zoom_note.text()


class TestAeRectMetering:
    def test_ae_rect_reaches_window_state(self, window):
        window._on_ae_rect(QRect(10, 20, 100, 50))
        assert window._ae_rect == (10, 20, 110, 70)
        window._on_ae_rect(None)
        assert window._ae_rect is None

    def test_rect_to_sensor_px_overview_scales_by_three(self, window,
                                                        monkeypatch):
        """Overview stream px → sensor px is the exact 3×3 bin scale; the
        audit is handed sensor px so its 1:1 map cannot lie (2026-09: it used
        to meter the whole frame in ROI mode instead of guessing)."""
        from filmscan_studio.core.zoom import SensorSize
        monkeypatch.setattr(window, "_sensor_size",
                            lambda: SensorSize(6000, 3000))
        window._stream_binned = True
        window._stream_size = (2000, 1000)
        window._ae_rect = (10, 20, 110, 120)
        assert window._ae_rect_in_sensor_px() == (30, 60, 330, 360)

    def test_rect_to_sensor_px_in_roi_adds_the_origin(self, window,
                                                      monkeypatch):
        """Over a hardware ROI the stream is the ROI window 1:1 — the rect's
        sensor position is stream px + ROI origin, not a scale."""
        from filmscan_studio.core.zoom import SensorSize
        monkeypatch.setattr(window, "_sensor_size",
                            lambda: SensorSize(6000, 3000))
        window._stream_binned = False
        window._roi_applied = (500, 600, 1200, 1200)
        window._ae_rect = (10, 20, 110, 120)
        assert window._ae_rect_in_sensor_px() == (510, 620, 610, 720)

    def test_rect_to_sensor_px_without_rect_is_none(self, window):
        window._ae_rect = None
        assert window._ae_rect_in_sensor_px() is None


class TestExposureControls:
    def test_gain_spin_applies_to_camera(self, window):
        window.gain_spin.setValue(4.0)
        window.gain_spin.editingFinished.emit()
        assert window.camera.get_settings().gain == pytest.approx(4.0)

    def test_gain_button_is_gone_and_gain_lock_is_gone(self, window):
        # 2026-09: the "Gain 1.00× (archiv)" and "Auto Exposure" buttons are
        # out of the UI (manual-only rig). 2026-09-19: the archival *rule*
        # itself was removed on the operator's order — gain is a free control.
        assert not hasattr(window, "btn_gain_base")
        assert not hasattr(window, "btn_autoexposure")
        window.camera.set_gain(4.0)
        window._refresh_settings()   # syncs the gain spin to 4.00
        assert window.gain_spin.value() == pytest.approx(4.0)

    def test_capture_allowed_at_any_gain(self, window, tmp_path, qtbot):
        """The gain lock is gone (operator order 2026-09-19): Capture works
        at 4× just like at 1×, and the frame records the gain it got."""
        from filmscan_studio.capture.session import CaptureSession, SessionPaths

        window.camera.set_gain(4.0)
        window._refresh_settings()
        window._refresh_buttons()
        assert window._capture_block_reason() is None

        film = FilmMetadata(film_id="HP5_G4", operator="JG")
        paths = SessionPaths.create(tmp_path, film.film_id)
        window.session = CaptureSession(camera=window.camera, film=film, paths=paths)
        window._refresh_buttons()
        assert window.btn_capture.isEnabled()
        window._capture(scan=True)
        qtbot.waitUntil(lambda: window.session.state.scan_count == 1, timeout=20000)
        payload = json.loads(
            (paths.frames / "frame001.tif.json").read_text(encoding="utf-8"))
        assert payload["acquisition"]["gain"] == pytest.approx(4.0)

    def test_shutter_text_edit_applies(self, window):
        window.shutter_edit.setEditText("1/125")
        window.shutter_edit.lineEdit().editingFinished.emit()
        assert window.camera.get_settings().shutter == pytest.approx(1 / 125)

    def test_short_shutter_presets_exist(self, window):
        # 2026-09: "ať pokračují i kratší než 1/10" — the preset ladder now
        # runs to 1/200 (the sensor delivers down to 300 µs).
        from filmscan_studio.gui.capture_window import SHUTTER_PRESETS

        assert min(SHUTTER_PRESETS) <= 1 / 200
        labels = [window.shutter_edit.itemText(i)
                  for i in range(window.shutter_edit.count())]
        assert "1/200" in labels and "1/50" in labels and "1/25" in labels
        # And a preset actually applies:
        window.shutter_edit.setEditText("1/50")
        window.shutter_edit.lineEdit().editingFinished.emit()
        assert window.camera.get_settings().shutter == pytest.approx(1 / 50)

    def test_unparseable_shutter_text_is_rejected(self, window):
        # The mock accepts any shutter (the real backend clamps to its range);
        # what must hold everywhere is that garbage never reaches the camera.
        before = window.camera.get_settings().shutter
        window.shutter_edit.setEditText("pátý přes devátou")
        window.shutter_edit.lineEdit().editingFinished.emit()
        assert window.camera.get_settings().shutter == pytest.approx(before)
        assert "nejde pochopit" in window.statusBar().currentMessage()

    def test_audit_clamped_suggestion_is_announced(self, window, monkeypatch):
        """2026-09: the audit's EV verdict wanted e.g. 52 s+ or a shutter the
        camera cannot deliver; the silent clamp read as "applied". The status
        line must name both numbers (52 s → 50 s confusion)."""
        from filmscan_studio.capture.quality import AuditResult
        from filmscan_studio.core.exposure import ExposureSettings

        class ClampingCamera:
            """The real Touptek clamps µs shutters to EXPO_TIME_RANGE_US."""
            MAX_S = 50.0

            def __init__(self):
                self.settings = ExposureSettings(10.0, iso=None, gain=1.0)

            def get_settings(self):
                return self.settings

            def set_shutter(self, seconds):
                self.settings = self.settings.with_shutter(
                    min(seconds, self.MAX_S))
                return self.settings.shutter

            def disconnect(self):  # window teardown calls it
                pass

        window.camera = ClampingCamera()
        monkeypatch.setattr(window, "_refresh_settings", lambda: None)
        # +2.5 EV from 10 s would be 56.6 s — beyond the camera's 50 s end.
        audit = AuditResult(verdict="under", message="PODEXPOZICOVÁNO",
                            ev_change=2.5, suggested_shutter=None,
                            clipped_fraction=0.0)
        window._on_audit_done((audit, None))
        msg = window.statusBar().currentMessage()
        assert "56.5" in msg                      # what the scene asked for
        assert "max 50" in msg                    # what the camera allowed
        assert window.camera.get_settings().shutter == pytest.approx(50.0)

    def test_refresh_does_not_reapply(self, window):
        window._refresh_settings()
        before_shutter = list(window.camera.shutter_history)
        before_gain = list(window.camera.gain_history)
        window._refresh_settings()
        assert window.camera.shutter_history == before_shutter
        assert window.camera.gain_history == before_gain


class TestHistogramFollowsAeRect:
    def test_histogram_uses_only_the_rect(self, window):
        """Histogram must describe the same area AE meters — bright film
        borders may not fake a blown/clipped report."""
        from filmscan_studio.core.exposure import measure

        img = np.full((200, 200), 0.05)          # dark film base
        img[10:14, 10:14] = 2.5                  # blown patch, 0.08% of frame
        reading = measure(img, 0.0, 1.0)

        window._ae_rect = None
        window._update_histogram(img, reading)
        hist_whole = window.histogram._hist

        window._ae_rect = (60, 60, 160, 160)     # away from the blown corner
        returned = window._update_histogram(img, reading)
        hist_rect = window.histogram._hist

        assert hist_whole.clipped_high > 0
        assert hist_rect.clipped_high == 0
        assert hist_rect.total < hist_whole.total
        # The returned reading follows the rect too (p99.9 inside is 0.05),
        # and says so — the operator must know which area the numbers lie about.
        assert returned.signal_p999 < 0.2
        assert "měřicí výřez" in window.histogram_label.text()

    def test_stale_rect_outside_frame_falls_back_to_whole(self, window):
        from filmscan_studio.core.exposure import measure

        img = np.full((100, 100), 0.4)
        reading = measure(img, 0.0, 1.0)
        window._ae_rect = (500, 500, 700, 700)
        window._update_histogram(img, reading)
        assert window.histogram._hist.total == 100 * 100


class TestCoolingPanel:
    def test_panel_visible_and_settled_for_cooled_camera(self, window):
        assert window.cool_box.isVisibleTo(window)
        assert window._temp_timer.isActive()
        window._poll_temperature()
        assert "°C" in window.temp_label.text()
        # Mock starts settled at the setpoint: the semaphore is green.
        assert "u cíle" in window.cool_note.text()

    def test_warm_camera_reports_off_target(self, qtbot):
        cam = MockCamera(scene_level=0.3, temperature_c=20.0)   # target −5
        cam.connect()
        w = CaptureWindow(camera=cam)
        qtbot.addWidget(w)
        w._poll_temperature()
        # The mock's temperature drifts with wall-clock time, so by the poll
        # the gap is 25.0 or a hair less — match the magnitude, not the digit.
        assert re.search(r"mimo cíl o 2[45]\.\d °C", w.cool_note.text())

    def test_target_spin_reaches_the_camera(self, window):
        window.target_spin.setValue(-12.0)
        window.target_spin.editingFinished.emit()
        assert window.camera.target_history[-1] == pytest.approx(-12.0)

    def test_tec_toggle_reaches_the_camera(self, window):
        # The checkbox starts unchecked (the backend protocol has no TEC-state
        # readback) — first toggle on, then off, each must reach the camera.
        window.tec_check.setChecked(True)
        assert window.camera.tec_enabled is True
        window.tec_check.setChecked(False)
        assert window.camera.tec_enabled is False

    def test_uncooled_camera_hides_the_panel(self, qtbot):
        cam = MockCamera(scene_level=0.3, cooling=False)
        cam.connect()
        w = CaptureWindow(camera=cam)
        qtbot.addWidget(w)
        assert not w.cool_box.isVisibleTo(w)
        assert not w._temp_timer.isActive()


class TestNegativeQuickToggle:
    """The 'negative with curve' preview switch must exist and work alone."""

    def test_toggle_switches_to_working_positive_with_invert(self, window):
        assert window.raw_view
        window.neg_toggle.setChecked(True)
        assert not window.raw_view
        assert window.positive.invert

    def test_toggle_back_returns_to_raw_view(self, window):
        window.neg_toggle.setChecked(True)
        window.neg_toggle.setChecked(False)
        assert window.raw_view

    def test_mode_checkboxes_sync_each_other(self, window):
        # The removed Positive box used to be synced here; its bug (staying
        # ticked next to Negativ) is what shrank the row to two boxes.
        window.neg_toggle.setChecked(True)
        assert window.neg_toggle.isChecked() and not window.mode_raw.isChecked()
        window.mode_raw.setChecked(True)
        assert window.mode_raw.isChecked() and not window.neg_toggle.isChecked()


class TestHistogramRect:
    def test_histogram_meters_only_the_rect(self, window):
        # A frame that is dark everywhere except the rect: whole-frame and
        # rect histograming must disagree, proving the crop is applied.
        # (The red-rect metering outlived the Auto Exposure button — the
        # histogram and the post-capture audit still honour the rect.)
        data = np.zeros((200, 200), dtype=np.uint16)
        # Bright patch must be < 0.1% of the frame or p99.9 of the whole
        # frame would hit it too and the test would prove nothing.
        data[100:105, 100:105] = 65000
        frame = LiveFrame(data=data, width=200, height=200,
                          black_level=0.0, white_level=65535.0)
        image, reading = window._meter.normalized_frame(frame)

        window._ae_rect = (100, 100, 105, 105)
        window._update_histogram(image, reading)
        rect_hist = window.histogram._hist
        assert "měřicí výřez" in window.histogram_label.text()

        window._ae_rect = None
        window._update_histogram(image, reading)
        whole_hist = window.histogram._hist
        assert rect_hist.counts.max() > 0
        assert not np.array_equal(rect_hist.counts, whole_hist.counts)
        assert "celý snímek" in window.histogram_label.text()


class TestExportWarning:
    def test_temperature_unmatched_scans_are_listed(self, window, tmp_path,
                                                    monkeypatch):
        """INSTRUCTIONS §7: the export still happens, but scans whose darks
        sit at another temperature must be listed by frame number."""
        from pathlib import Path

        from filmscan_studio.capture.session import CaptureSession, SessionPaths

        film = FilmMetadata(film_id="T_1")
        paths = SessionPaths.create(tmp_path, film.film_id)
        window.session = CaptureSession(camera=window.camera, film=film, paths=paths)
        window.session.export_project = lambda: (Path("t.zip"), [3, 7])
        shown = {}
        monkeypatch.setattr(
            "filmscan_studio.gui.capture_window.QMessageBox.warning",
            staticmethod(lambda *a, **k: shown.setdefault("text", a[2])),
        )
        window._export_project()
        assert "3, 7" in shown["text"]


class TestFilmDialogOrientation:
    """2026-09: atributy orientace filmu v dialogu Nový film."""

    def _dialog(self, qtbot):
        from filmscan_studio.gui.filmdialog import FilmDialog

        d = FilmDialog()
        qtbot.addWidget(d)
        d.film_id.setText("O1")
        return d

    def test_flags_round_trip_through_metadata(self, qtbot):
        d = self._dialog(qtbot)
        d.mirrored_horizontal.setChecked(True)
        d.rotated_180.setChecked(True)
        film = d.metadata()
        assert film.mirrored_horizontal and film.rotated_180
        assert not film.mirrored_vertical

    def test_defaults_off_and_not_inherited_from_rig(self, qtbot):
        from filmscan_studio.core.models import FilmMetadata

        prev = FilmMetadata(film_id="PREV", mirrored_vertical=True)
        # rig_defaults (film N+1) must NOT carry the previous strip's
        # orientation — only the edited film's own record does.
        from filmscan_studio.gui.filmdialog import FilmDialog

        d = FilmDialog(rig_defaults=prev)
        qtbot.addWidget(d)
        assert not d.mirrored_vertical.isChecked()
        d2 = FilmDialog(defaults=prev)
        qtbot.addWidget(d2)
        assert d2.mirrored_vertical.isChecked()

    def test_all_three_refuses_accept(self, qtbot, monkeypatch):
        # H+V is the 180° rotation — all three is identity and the model
        # refuses; the dialog must show a warning instead of closing.
        shown = {}
        monkeypatch.setattr(
            "filmscan_studio.gui.filmdialog.QMessageBox.warning",
            staticmethod(lambda *a, **k: shown.setdefault("text", a[2])),
        )
        d = self._dialog(qtbot)
        d.mirrored_horizontal.setChecked(True)
        d.mirrored_vertical.setChecked(True)
        d.rotated_180.setChecked(True)
        d._accept()
        assert "text" in shown
        # accept() never ran: the dialog's result is still the rejected 0.
        assert d.result() == 0


class TestHistogramWidget:
    def test_paints_without_crashing(self, qtbot):
        from filmscan_studio.core.histogram import compute

        widget = HistogramWidget()
        qtbot.addWidget(widget)
        widget.resize(300, 100)
        widget.show()
        widget.set_histogram(compute(np.random.rand(32, 32), 0.0, 1.0))
        widget.set_curve(np.linspace(0, 1, 64))
        widget.grab()  # forces a paint


class TestImageUtil:
    def test_greyscale_and_rgb_round_sizes(self):
        grey = to_qimage(np.random.rand(8, 12))
        assert (grey.width(), grey.height()) == (12, 8)
        rgb = to_qimage(np.random.rand(8, 12, 3))
        assert (rgb.width(), rgb.height()) == (12, 8)
        with pytest.raises(ValueError):
            to_qimage(np.zeros((4, 4, 2)))


class TestLiveWorker:
    def test_delivers_linear_frames_off_ui_thread(self, qtbot, camera):
        worker = LiveViewWorker(camera, max_fps=30.0)
        with qtbot.waitSignal(worker.frameReady, timeout=5000) as blocker:
            worker.start()
        worker.stop()
        image, reading = blocker.args
        # The TS2600MP-G2 stream is linear mono — 2-D, normalised 0..1, with
        # its meter reading always emitted together (one honest path).
        assert image.ndim == 2
        assert image.dtype == np.float64
        assert 0.0 <= float(image.min()) and float(image.max()) <= 1.0
        assert reading.signal_p999 > 0.0

    def test_leave_live_view_keeps_the_stream_up(self, qtbot, camera):
        worker = LiveViewWorker(camera, max_fps=30.0)
        worker.leave_live_view = True
        with qtbot.waitSignal(worker.frameReady, timeout=5000):
            worker.start()
        worker.stop()
        assert camera._live_view          # AE can keep polling after teardown

    def test_normal_stop_closes_the_stream(self, qtbot, camera):
        worker = LiveViewWorker(camera, max_fps=30.0)
        with qtbot.waitSignal(worker.frameReady, timeout=5000):
            worker.start()
        worker.stop()
        assert not camera._live_view


class TestFilmBaseMinPoint:
    """The blue min-point rect: its own drag role, measurement and storage."""

    def test_base_mode_drag_draws_base_not_ae_rect(self, qtbot):
        view = ZoomView()
        qtbot.addWidget(view)
        view.resize(200, 200)
        view.show()
        view.set_image(np.zeros((400, 400)))
        view.set_zoom(1.0)
        view.set_rect_mode("base")
        SHIFT = Qt.KeyboardModifier.ShiftModifier
        with qtbot.waitSignal(view.baseRectChanged):
            view.mousePressEvent(_mouse_event(20, 20, modifiers=SHIFT))
            view.mouseMoveEvent(_mouse_event(90, 70, modifiers=SHIFT))
            view.mouseReleaseEvent(_mouse_event(90, 70, modifiers=SHIFT))
        assert view.base_rect() is not None
        assert view.ae_rect() is None          # the AE rect stayed untouched

    def test_right_click_clears_only_the_active_role(self, qtbot):
        view = ZoomView()
        qtbot.addWidget(view)
        view.resize(200, 200)
        view.show()
        view.set_image(np.zeros((400, 400)))
        view._ae_rect = QRect(10, 10, 50, 50)
        view._base_rect = QRect(60, 60, 40, 40)
        view.set_rect_mode("base")
        view.mousePressEvent(_mouse_event(5, 5, button=Qt.MouseButton.RightButton))
        assert view.base_rect() is None
        assert view.ae_rect() is not None      # AE metering survives
        view.set_rect_mode("ae")
        view.mousePressEvent(_mouse_event(5, 5, button=Qt.MouseButton.RightButton))
        assert view.ae_rect() is None

    def test_mode_toggle_switches_the_view(self, window):
        window.btn_base_mode.setChecked(True)
        assert window.view.rect_mode() == "base"
        window.btn_base_mode.setChecked(False)
        assert window.view.rect_mode() == "ae"

    def test_stream_measurement_stores_sample_with_exposure(self, window,
                                                            tmp_path, qtbot):
        """Mean DN of the blue rect from the live stream, stored with the
        exposure it was read at — the pair the scaling rule needs."""
        from filmscan_studio.capture.session import CaptureSession, SessionPaths

        film = FilmMetadata(film_id="FB_1", operator="JG")
        paths = SessionPaths.create(tmp_path, film.film_id)
        window.session = CaptureSession(camera=window.camera, film=film, paths=paths)
        window._refresh_buttons()
        window._on_base_rect(QRect(100, 100, 200, 150))
        window._measure_base_from_stream()
        # waitUntil accepts None/bool only — a still-empty list must be
        # False, not the [] the accessor naturally returns.
        qtbot.waitUntil(lambda: bool(window.session.base_samples()),
                        timeout=10000)
        sample = window.session.base_samples()[0]
        assert sample.kind == "stream"
        assert sample.mean_dn > 0
        # The exposure travelled with the reading: the mock is at 1.0 s gain 1.0.
        assert sample.shutter > 0
        assert sample.gain == pytest.approx(1.0)
        assert sample.rect is not None          # converted to sensor px
        assert (paths.root / "film_base.json").exists()
        assert "film base" in window.log_view.text().lower()

    def test_stream_measurement_refuses_without_rect(self, window, tmp_path):
        from filmscan_studio.capture.session import CaptureSession, SessionPaths

        film = FilmMetadata(film_id="FB_2", operator="JG")
        paths = SessionPaths.create(tmp_path, film.film_id)
        window.session = CaptureSession(camera=window.camera, film=film, paths=paths)
        window._measure_base_from_stream()
        assert window.session.base_samples() == []
        assert "min point" in window.statusBar().currentMessage().lower()

    def test_base_frame_capture_archives_and_measures(self, window, tmp_path,
                                                      qtbot):
        from filmscan_studio.capture.session import CaptureSession, SessionPaths

        film = FilmMetadata(film_id="FB_3", operator="JG")
        paths = SessionPaths.create(tmp_path, film.film_id)
        window.session = CaptureSession(camera=window.camera, film=film, paths=paths)
        window._refresh_buttons()
        window._on_base_rect(QRect(10, 20, 110, 120))
        window._capture_base_frame()
        # The frame now averages CALIBRATION_AVERAGE exposures (mock: ~8 s).
        qtbot.waitUntil(lambda: window.session.state.base_count == 1,
                        timeout=60000)
        qtbot.waitUntil(lambda: bool(window.session.base_samples()),
                        timeout=5000)
        sample = window.session.base_samples()[0]
        assert sample.kind == "frame"
        assert sample.source.endswith(".tif")
        # QRect(10,20,110,120) == stream px (x0,y0,x1,y1)=(10,20,120,140);
        # overview 3x3 binning -> sensor px x3.
        assert sample.rect == (30, 60, 360, 420)


class SlowCaptureCamera(MockCamera):
    """Adds a delay so the busy-state assertion has time to observe it."""

    def __init__(self) -> None:
        super().__init__(capture_seconds=0.4)
        self.connect()


def _mouse_event(x: int, y: int, button: Qt.MouseButton = Qt.MouseButton.LeftButton,
                 kind: QEvent.Type = QEvent.Type.MouseButtonPress,
                 modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier):
    return QMouseEvent(
        kind,
        QPointF(float(x), float(y)),
        QPointF(float(x), float(y)),
        button,
        Qt.MouseButton.NoButton,
        modifiers,
    )
