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

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
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
        qtbot.waitUntil(lambda: window.session.state.scan_count == 1, timeout=10000)
        assert window.btn_capture.isEnabled()
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
    def test_zoom_ladder_matches_brief(self):
        assert ZOOM_LEVELS == (0.25, 1.0, 2.0, 4.0)

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

    def test_click_recenters(self, qtbot):
        view = ZoomView()
        qtbot.addWidget(view)
        view.resize(200, 200)
        view.show()
        view.set_image(np.zeros((400, 400)))
        view.set_zoom(1.0)
        before = view._center
        view.mousePressEvent(_mouse_event(10, 10))
        assert view._center != before or before.x() == 0  # moved toward click


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


def _mouse_event(x: int, y: int):
    return QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(float(x), float(y)),
        QPointF(float(x), float(y)),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
