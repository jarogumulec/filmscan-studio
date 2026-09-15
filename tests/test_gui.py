"""GUI tests against MockCamera, offscreen.

These are contract tests for the brief's hard rules, not pixel tests: the two
preview modes must differ in the image but *not* in the histogram, captures must
leave raw bytes to the session (never the GUI writing files), and no camera call
may run on the UI thread.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("pytestqt")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402

from filmscan_studio.capture.mock import MockCamera  # noqa: E402
from filmscan_studio.core.models import FilmMetadata  # noqa: E402
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
    qtbot.waitUntil(lambda: w._last_linear is not None, timeout=5000)
    return w


class TestModes:
    def test_modes_show_different_images(self, window, qtbot):
        raw_display = window._display_for(window._last_linear).copy()
        window.mode_positive.setChecked(True)
        qtbot.wait(10)
        positive_display = window._display_for(window._last_linear)
        # A negative inverted is not the same picture as the raw mosaic view.
        assert not np.allclose(raw_display, positive_display)

    def test_histogram_stays_linear_in_working_positive(self, window, qtbot):
        window.mode_raw.setChecked(True)
        qtbot.wait(10)
        raw_hist = window.histogram._hist
        window.mode_positive.setChecked(True)
        qtbot.wait(10)
        positive_hist = window.histogram._hist
        # The brief: histogram is always linear sensor data. The frame metered
        # is the same frame, so the histogram must be identical.
        assert np.array_equal(raw_hist.counts, positive_hist.counts)

    def test_curve_overlay_only_in_positive(self, window, qtbot):
        assert window.histogram._curve is None
        window.mode_positive.setChecked(True)
        qtbot.wait(10)
        assert window.histogram._curve is not None
        window.mode_raw.setChecked(True)
        qtbot.wait(10)
        assert window.histogram._curve is None

    def test_mode_checkboxes_are_mutually_exclusive(self, window, qtbot):
        window.mode_positive.setChecked(True)
        qtbot.wait(10)
        assert not window.mode_raw.isChecked()
        # Unchecking the active mode must not leave the app with no mode.
        window.mode_positive.setChecked(False)
        qtbot.wait(10)
        assert window.mode_raw.isChecked()

    def test_filmic_panel_disabled_in_raw_view(self, window, qtbot):
        assert not window.filmic.isEnabled()
        window.mode_positive.setChecked(True)
        qtbot.wait(10)
        assert window.filmic.isEnabled()


class TestCaptureWorkflow:
    def test_buttons_disabled_without_film(self, window):
        assert not window.btn_capture.isEnabled()
        assert not window.btn_dark.isEnabled()

    def test_session_startenables_workflow(self, window, tmp_path, qtbot):
        from filmscan_studio.capture.session import CaptureSession, SessionPaths

        film = FilmMetadata(film_id="HP5_001", operator="JG")
        paths = SessionPaths.create(tmp_path, film.film_id)
        window.session = CaptureSession(
            camera=window.camera, film=film, paths=paths, frame_reader=_npy_reader
        )
        window._refresh_buttons()
        assert window.btn_capture.isEnabled()

        window._capture(kind="dark")
        qtbot.waitUntil(lambda: window.session.state.dark_count == 1, timeout=10000)
        window._capture(kind="flat")
        qtbot.waitUntil(lambda: window.session.state.flat_count == 3, timeout=20000)
        window._capture(scan=True)
        qtbot.waitUntil(lambda: window.session.state.scan_count == 1, timeout=10000)

        sidecars = list(paths.frames.glob("*.json"))
        assert len(sidecars) == 5
        assert "3) Snímání" in window.stage_label.text()

    def test_capture_button_stays_disabled_while_busy(self, window, tmp_path, qtbot):
        from filmscan_studio.capture.session import CaptureSession, SessionPaths

        film = FilmMetadata(film_id="X_1")
        paths = SessionPaths.create(tmp_path, film.film_id)
        window.session = CaptureSession(
            camera=SlowCaptureCamera(), film=film, paths=paths, frame_reader=_npy_reader
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
        assert w.btn_autoexposure.isEnabled()

    def test_connect_failure_notifies(self, qtbot, monkeypatch):
        w = CaptureWindow()
        qtbot.addWidget(w)
        w._connecting = True
        shown = {}
        monkeypatch.setattr(
            "filmscan_studio.gui.capture_window.QMessageBox.critical",
            staticmethod(lambda *a, **k: shown.setdefault("shown", a[2])),
        )
        w._on_connect_failed("Cannot allocate USB device")
        assert "USB" in shown["shown"]
        assert not w._connecting


class TestZoom:
    def test_zoom_ladder_is_sensor_pixels(self):
        # 100% = one screen pixel per *sensor* pixel (of the 6016x4016 NEF),
        # not per preview-window pixel: the brief's old 0.25/1/2/4 ladder
        # scaled the 640px stream and is intentionally gone.
        from filmscan_studio.core.zoom import FIT

        assert ZOOM_LEVELS == (FIT, 0.125, 0.25, 0.5, 1.0, 2.0)

    def test_set_zoom_clamps_and_emits(self, qtbot):
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

    def test_drag_draws_ae_rect_not_recenter(self, qtbot):
        view = ZoomView()
        qtbot.addWidget(view)
        view.resize(200, 200)
        view.show()
        view.set_image(np.zeros((400, 400)))
        view.set_zoom(1.0)
        before = QPoint(view._center)
        with qtbot.waitSignal(view.aeRectChanged):
            view.mousePressEvent(_mouse_event(20, 20))
            view.mouseMoveEvent(_mouse_event(90, 70))
            view.mouseReleaseEvent(_mouse_event(90, 70))
        rect = view.ae_rect()
        assert rect is not None and rect.width() > 0
        assert view._center == before  # a drag must not pan

    def test_right_click_clears_ae_rect(self, qtbot):
        view = ZoomView()
        qtbot.addWidget(view)
        view.resize(200, 200)
        view.show()
        view.set_image(np.zeros((400, 400)))
        view.set_zoom(1.0)
        view.mousePressEvent(_mouse_event(20, 20))
        view.mouseReleaseEvent(_mouse_event(90, 70))
        assert view.ae_rect() is not None
        view.mousePressEvent(_mouse_event(5, 5, Qt.MouseButton.RightButton))
        assert view.ae_rect() is None

    def test_source_scale_shows_in_detail_note(self, qtbot):
        view = ZoomView()
        qtbot.addWidget(view)
        view.set_detail_note("stream 640×424")
        assert view._detail_note == "stream 640×424"



class TestBodyZoomWiring:
    """Selecting a zoom level must ask the *body* for a zoomed stream."""

    def test_zoom_selection_requests_body_crop(self, window, qtbot):
        window.zoom_select.setCurrentIndex(window.zoom_select.findData(1.0))
        qtbot.waitUntil(lambda: window.camera.live_view_zoom_rate != 0, timeout=2000)
        # 100% sensor from a 640px whole-frame stream: a crop is the only way
        # to any real detail; Whole is not acceptable any more.
        assert window.camera.live_view_zoom_rate != 0
        assert window.camera.live_view_zoom_history  # it actually reached the "camera"

    def test_fit_returns_to_whole_frame(self, window, qtbot):
        window.zoom_select.setCurrentIndex(window.zoom_select.findData(1.0))
        qtbot.waitUntil(lambda: window.camera.live_view_zoom_rate != 0, timeout=2000)
        window.zoom_select.setCurrentIndex(window.zoom_select.findData(0.0))
        qtbot.waitUntil(lambda: window.camera.live_view_zoom_rate == 0, timeout=2000)

    def test_zoom_note_discloses_interpolation(self, window, qtbot):
        window.zoom_select.setCurrentIndex(window.zoom_select.findData(1.0))
        qtbot.wait(50)
        assert "interpolace" in window.zoom_note.text()

    def test_no_body_zoom_without_capability(self, qtbot, camera):
        from dataclasses import replace

        base_caps = camera.capabilities()
        camera.capabilities = lambda: replace(base_caps, live_view_zoom=False)
        w = CaptureWindow(camera=camera)
        qtbot.addWidget(w)
        # A backend without the capability (gphoto2) must never be sent
        # set_live_view_zoom, and must be called out in the log.
        w.zoom_select.setCurrentIndex(w.zoom_select.findData(1.0))
        qtbot.wait(50)
        assert camera.live_view_zoom_history == []
        assert "interpolovaný" in w.log_view.text()


class TestAeRectMetering:
    def test_ae_rect_reaches_window_state(self, window):
        from PySide6.QtCore import QRect

        window._on_ae_rect(QRect(10, 20, 100, 50))
        assert window._ae_rect == (10, 20, 110, 70)
        window._on_ae_rect(None)
        assert window._ae_rect is None

    def test_meter_source_meters_only_the_rect(self, window):
        # A frame that is dark everywhere except the rect: whole-frame and
        # rect metering must disagree, proving the crop is applied.
        import cv2

        # Bright patch must be < 0.1% of the frame or p99.9 of the whole
        # frame would hit it too and the test would prove nothing.
        img = np.zeros((200, 200, 3), dtype=np.uint8)
        img[100:105, 100:105] = 250
        ok, buf = cv2.imencode(".jpg", img)
        assert ok
        from filmscan_studio.capture.camera import LiveFrame

        class RectCamera:
            def next_live_frame(self):
                return LiveFrame(jpeg=buf.tobytes())

            def disconnect(self):  # window teardown calls it
                pass

        window.camera = RectCamera()
        window._ae_rect = (100, 100, 105, 105)
        reading = window._meter_source()()
        assert reading.signal_p999 > 0.9

        window._ae_rect = None
        whole = window._meter_source()()
        assert whole.signal_p999 < reading.signal_p999 / 3


class TestExposureControls:
    def test_iso_select_applies_to_camera(self, window, qtbot):
        index = window.iso_select.findData(400)
        assert index >= 0
        window.iso_select.setCurrentIndex(index)
        qtbot.waitUntil(lambda: window.camera.get_settings().iso == 400, timeout=2000)

    def test_iso_base_button_uses_lowest_choice(self, window, qtbot):
        window.iso_select.setCurrentIndex(window.iso_select.findData(1600))
        qtbot.wait(10)
        window.btn_iso_low.click()
        qtbot.waitUntil(lambda: window.camera.get_settings().iso == 50, timeout=2000)

    def test_shutter_text_edit_snaps_to_ladder(self, window, qtbot):
        window.shutter_edit.setEditText("1/125")
        window.shutter_edit.lineEdit().editingFinished.emit()
        qtbot.waitUntil(lambda: window.camera.get_settings().shutter == 1 / 125,
                        timeout=2000)

    def test_refresh_does_not_reapply(self, window, qtbot):
        window._refresh_settings()
        before = list(window.camera.iso_history)
        window._refresh_settings()
        assert window.camera.iso_history == before

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
    def test_delivers_frames_off_ui_thread(self, qtbot, camera):
        worker = LiveViewWorker(camera, max_fps=30.0)
        worker.setObjectName("worker-under-test")  # noqa: keep a strong ref below
        with qtbot.waitSignal(worker.frameReady, timeout=5000) as blocker:
            worker.start()
        worker.stop()
        del worker
        image, reading = blocker.args
        assert image.ndim == 3 and image.shape[2] == 3
        assert 0.0 <= float(image.min()) and float(image.max()) <= 1.0
        assert reading.signal_p999 > 0.0


def _npy_reader(path):
    """Reader for MockCamera's .npy output (same injectable as session tests)."""
    from filmscan_studio.core.rawio import RawFrame
    from filmscan_studio.core.models import AcquisitionMetadata

    data = np.load(path)
    return RawFrame(
        path=path,
        data=data,
        black_level=600.0,
        white_level=16383.0,
        color_desc="BGGR",
        width=data.shape[1],
        height=data.shape[0],
        acquisition=AcquisitionMetadata(exposure_time=1.0, iso=100),
    )


class SlowCaptureCamera(MockCamera):
    """Adds a delay so the busy-state assertion has time to observe it."""

    def __init__(self) -> None:
        super().__init__(capture_seconds=0.4)
        self.connect()


def _mouse_event(x: int, y: int, button: Qt.MouseButton = Qt.MouseButton.LeftButton,
                 kind: QEvent.Type = QEvent.Type.MouseButtonPress):
    return QMouseEvent(
        kind,
        QPointF(float(x), float(y)),
        QPointF(float(x), float(y)),
        button,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
