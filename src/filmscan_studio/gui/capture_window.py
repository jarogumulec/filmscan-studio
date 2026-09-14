"""The Capture window.

Layout is the brief's: Live View with zoom 100/200/400%, a linear histogram,
ISO/shutter/aperture readouts, and the four action buttons. Everything that
talks to the camera runs in :class:`~filmscan_studio.gui.liveview.LiveViewWorker`
or a one-shot worker object, never on the UI thread.

The two preview modes are the heart of the design and are enforced here rather
than left to the operator:

* **RAW View** shows the frame with display gamma only, and the histogram is
  computed from linear sensor signal. Exposure judgements happen here.
* **Working Positive** shows inversion + base subtraction + preview exposure +
  filmic, for judging the *picture*. The histogram does not change: it keeps
  describing linear data even in this mode, because a histogram of an inverted,
  tone-curled preview is decorative.

Nothing in this window can write to a stored RAW file: captures go through
:class:`CaptureSession`, which copies camera files and writes sidecars.

One rule governs the threads: **only one thread may touch the camera at a
time**. libgphoto2's session and config tree are not thread-safe, and a D750
physically delivers no Live View frames during an exposure anyway. So a still
capture or an Auto Exposure run stops the Live View worker first and restarts
it afterwards.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import replace
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from filmscan_studio.capture.autoexposure import (
    AutoExposureController,
    LiveMeter,
)
from filmscan_studio.capture.camera import CameraBackend, CameraError, CameraInfo
from filmscan_studio.capture.gphoto2 import GPhoto2Backend
from filmscan_studio.capture.mock import MockCamera
from filmscan_studio.capture.nikon_backend import NikonSdkBackend
from filmscan_studio.capture.session import CaptureSession, SessionPaths
from filmscan_studio.core.exposure import (
    DEFAULT_HEADROOM_EV,
    ExposureSettings,
    MeterReading,
)
from filmscan_studio.core.filmic import FilmicProfile
from filmscan_studio.core.histogram import Histogram, compute
from filmscan_studio.core.models import FilmMetadata
from filmscan_studio.core.positive import PositiveParams
from filmscan_studio.gui.filmdialog import FilmDialog
from filmscan_studio.gui.imageutil import preview, to_qimage
from filmscan_studio.gui.liveview import LiveViewWorker, luminance
from filmscan_studio.gui.widgets import (
    ZOOM_LABELS,
    ZOOM_LEVELS,
    FilmicPanel,
    HistogramWidget,
    ZoomView,
)

log = logging.getLogger(__name__)


class ResultRelay(QObject):
    """Lands worker results on the UI thread.

    A QObject created on the UI thread and *never moved*: emitting its signals
    from the worker thread makes Qt queue the delivery to the UI thread. Calling
    the callbacks directly instead -- which an earlier version did -- runs Qt
    calls like setWindowTitle on a worker thread, and macOS AppKit answers with
    an abort, not a warning. The offscreen test platform tolerates it; cocoa
    does not, which is exactly why this relay exists.
    """

    delivered = Signal(object, object, object)  # callback, arg, unused

    def __init__(self) -> None:
        super().__init__()
        self.delivered.connect(self._invoke)

    def send(self, callback, arg) -> None:
        self.delivered.emit(callback, arg, None)

    def _invoke(self, callback, arg, _unused) -> None:
        callback(arg)


class CameraWorker(QObject):
    """A queue of camera calls, run one at a time on one dedicated thread.

    Every blocking camera operation -- capture, Auto Exposure, connect -- is
    submitted here. Serialising here is what keeps libgphoto2 single-threaded
    in practice rather than by convention. Results reach callbacks only through
    :class:`ResultRelay`, never as direct calls from this thread.
    """

    _callSubmitted = Signal()

    def __init__(self, relay: ResultRelay) -> None:
        super().__init__()
        self._queue: deque[tuple] = deque()
        self._lock = threading.Lock()
        self._relay = relay
        self._callSubmitted.connect(self._run_next, Qt.ConnectionType.QueuedConnection)

    def start(self) -> None:
        self._thread = QThread()
        self.moveToThread(self._thread)
        self._thread.start()

    def submit(self, fn, on_done, on_failed, *args) -> None:
        with self._lock:
            idle = not self._queue
            self._queue.append((fn, on_done, on_failed, args))
        if idle:
            self._callSubmitted.emit()

    def _run_next(self) -> None:
        with self._lock:
            if not self._queue:
                return
            fn, on_done, on_failed, args = self._queue[0]
        try:
            result = fn(*args)
        except Exception as exc:  # noqa: BLE001 - surfaced to the status bar
            log.exception("camera operation failed")
            self._relay.send(on_failed, str(exc))
        else:
            self._relay.send(on_done, result)
        with self._lock:
            self._queue.popleft()
            more = bool(self._queue)
        if more:
            self._callSubmitted.emit()

    def stop(self, wait_ms: int = 5000) -> None:
        thread = getattr(self, "_thread", None)
        if thread is not None and thread.isRunning():
            thread.quit()
            thread.wait(wait_ms)

    @property
    def busy(self) -> bool:
        with self._lock:
            return bool(self._queue)


class CaptureWindow(QMainWindow):
    """Main window of the Capture module."""

    def __init__(self, camera: CameraBackend | None = None) -> None:
        super().__init__()
        self.setWindowTitle("FilmScan Studio — Capture")
        self.resize(1400, 900)

        self.camera: CameraBackend | None = camera
        self.info: CameraInfo | None = None
        self.session: CaptureSession | None = None
        self.raw_view = True
        self.positive = PositiveParams()
        self._meter = LiveMeter()
        self._worker: LiveViewWorker | None = None
        self._connecting = False
        # The relay is created here, on the UI thread, and never moved: that is
        # what makes its queued delivery land back on this thread.
        self._relay = ResultRelay()
        self._camera_queue = CameraWorker(self._relay)
        self._camera_queue.start()
        self._last_reading: MeterReading | None = None
        self._last_linear: np.ndarray | None = None

        self._build_ui()
        self._set_mode(raw_view=True)
        if camera is not None:
            self._on_camera_connected(camera)

    # ----------------------------------------------------------------------- UI

    def _build_ui(self) -> None:
        self.setStatusBar(QStatusBar())

        top = QToolBar("Camera")
        top.setMovable(False)
        self.addToolBar(top)
        self.act_connect = top.addAction("Připojit fotoaparát", self._connect_prompt)
        self.act_new_film = top.addAction("Nový film", self._new_film)
        self.act_new_film.setEnabled(False)
        self.act_export = top.addAction("Exportovat projekt", self._export_project)
        self.act_export.setEnabled(False)

        central = QWidget()
        root = QHBoxLayout(central)

        left = QVBoxLayout()
        root.addLayout(left, stretch=3)

        self.view = ZoomView()
        self.view.centerChanged.connect(
            lambda x, y: self.statusBar().showMessage(f"střed {x},{y}", 2000)
        )
        left.addWidget(self.view, stretch=3)
        self.view.set_zoom(ZOOM_LEVELS[0])

        bottom_row = QHBoxLayout()
        left.addLayout(bottom_row, stretch=2)

        hist_box = QWidget()
        hist_layout = QVBoxLayout(hist_box)
        hist_layout.setContentsMargins(0, 0, 0, 0)
        self.histogram = HistogramWidget()
        hist_layout.addWidget(self.histogram)
        self.histogram_label = QLabel("histogram: lineární data senzoru")
        hist_layout.addWidget(self.histogram_label)
        bottom_row.addWidget(hist_box, stretch=2)

        controls = QWidget()
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)

        zoom_row = QHBoxLayout()
        zoom_row.addWidget(QLabel("Zoom"))
        self.zoom_select = QComboBox()
        for level in ZOOM_LEVELS:
            self.zoom_select.addItem(ZOOM_LABELS[str(level)], level)
        self.zoom_select.currentIndexChanged.connect(
            lambda _: self.view.set_zoom(self.zoom_select.currentData())
        )
        self.view.zoomChanged.connect(
            lambda z: self.zoom_select.setCurrentIndex(
                self.zoom_select.findData(z)
            )
        )
        zoom_row.addWidget(self.zoom_select)
        zoom_row.addStretch()
        controls_layout.addLayout(zoom_row)

        mode_row = QHBoxLayout()
        self.mode_raw = QCheckBox("RAW View")
        self.mode_raw.setChecked(True)
        self.mode_raw.toggled.connect(lambda on: self._set_mode(raw_view=on))
        mode_row.addWidget(self.mode_raw)
        self.mode_positive = QCheckBox("Working Positive")
        self.mode_positive.toggled.connect(lambda on: self._set_mode(raw_view=not on))
        mode_row.addWidget(self.mode_positive)
        mode_row.addStretch()
        controls_layout.addLayout(mode_row)

        self.settings_label = QLabel("ISO —   čas —   clona —")
        self.settings_label.setStyleSheet("font-family: Menlo, monospace; font-size: 13px;")
        controls_layout.addWidget(self.settings_label)

        self.meter_label = QLabel("—")
        self.meter_label.setStyleSheet("font-family: Menlo, monospace;")
        controls_layout.addWidget(self.meter_label)

        self.stage_label = QLabel("Fáze: —")
        controls_layout.addWidget(self.stage_label)

        controls_layout.addStretch()
        bottom_row.addWidget(controls, stretch=1)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.actions = QWidget()
        actions_layout = QVBoxLayout(self.actions)
        actions_layout.setContentsMargins(0, 0, 0, 0)

        self.btn_capture = QPushButton("Capture")
        self.btn_capture.setMinimumHeight(48)
        self.btn_capture.clicked.connect(lambda: self._capture(scan=True))
        actions_layout.addWidget(self.btn_capture)

        self.btn_autoexposure = QPushButton("Auto Exposure")
        self.btn_autoexposure.clicked.connect(self._auto_exposure)
        actions_layout.addWidget(self.btn_autoexposure)

        self.btn_dark = QPushButton("Dark Frame")
        self.btn_dark.clicked.connect(lambda: self._capture(kind="dark"))
        actions_layout.addWidget(self.btn_dark)

        self.btn_flat = QPushButton("Flat Field")
        self.btn_flat.clicked.connect(lambda: self._capture(kind="flat"))
        actions_layout.addWidget(self.btn_flat)

        frame_row = QHBoxLayout()
        frame_row.addWidget(QLabel("Číslo snímku"))
        self.frame_number = QSpinBox()
        self.frame_number.setMinimum(1)
        self.frame_number.setSpecialValueText("auto")
        frame_row.addWidget(self.frame_number)
        frame_row.addStretch()
        actions_layout.addLayout(frame_row)

        right_layout.addWidget(self.actions)

        self.filmic = FilmicPanel()
        self.filmic.paramsChanged.connect(self._on_filmic_changed)
        self.filmic.setEnabled(False)
        filmic_scroll = QScrollArea()
        filmic_scroll.setWidget(self.filmic)
        filmic_scroll.setWidgetResizable(True)
        filmic_scroll.setMaximumHeight(260)
        right_layout.addWidget(filmic_scroll)

        self.log_view = QLabel("")
        self.log_view.setWordWrap(True)
        self.log_view.setStyleSheet(
            "color: #aaa; font-family: Menlo, monospace; font-size: 11px;"
        )
        right_layout.addWidget(self.log_view)
        right_layout.addStretch()

        splitter = QSplitter()
        splitter.addWidget(central)
        right.setMaximumWidth(360)
        splitter.addWidget(right)
        self.setCentralWidget(splitter)
        self._refresh_buttons()

    # ------------------------------------------------------------- camera setup

    def _connect_prompt(self) -> None:
        if self.camera is not None or self._connecting:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Připojit fotoaparát")
        box.setText("Vyber zdroj:")
        box.setInformativeText(
            "Nikon SDK je preferovaný kanál (Live View zoom na straně těla); "
            "vyžaduje scripts/install_helper.sh + install_sdk.sh. gphoto2 "
            "zůstává záloha. Mock simuluje D750 bez fotoaparátu."
        )
        sdk_btn = box.addButton("Nikon D750 (Nikon SDK)", QMessageBox.ButtonRole.AcceptRole)
        sdk_btn.setDefault(True)
        mock = box.addButton("Mock kamera", QMessageBox.ButtonRole.AcceptRole)
        real = box.addButton("Nikon D750 (gphoto2)", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        chosen = box.clickedButton()
        if chosen is sdk_btn:
            self._connecting = True
            self.act_connect.setEnabled(False)
            self.statusBar().showMessage("Připojuji přes Nikon SDK (spouštím x86_64 helper)…")
            self._start_worker(_connect_sdk, self._on_connected, on_failed=self._on_connect_failed)
        elif chosen is real:
            self._connecting = True
            self.act_connect.setEnabled(False)
            # Persistent status (no timeout): connecting retries against
            # ptpcamerad for several seconds, and clearing this early is what
            # made a click look like it had done nothing at all.
            self.statusBar().showMessage("Připojuji k D750 (opakuje kvůli ptpcamerad)…")
            self._start_worker(_connect_gphoto2, self._on_connected, on_failed=self._on_connect_failed)
        elif chosen is mock:
            camera = MockCamera()
            camera.connect()
            self._on_camera_connected(camera)

    def _on_connected(self, camera: GPhoto2Backend) -> None:
        self._on_camera_connected(camera)

    def _on_connect_failed(self, message: str) -> None:
        self._connecting = False
        self.act_connect.setEnabled(True)
        self.statusBar().clearMessage()
        # A failed connect must be visible in the UI: before this, the only
        # trace was a Python traceback on stderr, which is nowhere when the app
        # is launched from Finder.
        QMessageBox.critical(
            self,
            "Nelze se připojit",
            f"{message}\n\nZavřete Foto / Image Capture a zkuste to znovu.",
        )

    def _on_camera_connected(self, camera: CameraBackend) -> None:
        self._connecting = False
        self.statusBar().clearMessage()
        self.camera = camera
        info = camera.info
        self.info = info
        self.act_connect.setEnabled(False)
        self.setWindowTitle(f"FilmScan Studio — Capture — {info.model}")
        self.act_new_film.setEnabled(True)
        self._log(f"{info.manufacturer or ''} {info.model} · ISO {len(info.iso_choices)} · časů {len(info.shutter_choices)}")
        caps = camera.capabilities()
        if not caps.aperture:
            self._log("Clona není přes USB ovladatelná (manuální objektiv) — nastavuje se na objektivu.")
        self.start_live_view()
        self._refresh_settings()
        self._refresh_buttons()

    def _pause_live_view(self) -> None:
        """Stop the poller so a blocking camera call owns the session."""
        if self._worker is not None:
            self._worker.stop()
            self._worker = None

    def _resume_live_view(self) -> None:
        if self.camera is not None:
            self.start_live_view()

    def start_live_view(self) -> None:
        if self.camera is None or (self._worker is not None and self._worker.running):
            return
        worker = LiveViewWorker(self.camera, self._meter)
        worker.frameReady.connect(self._on_live_frame)
        worker.error.connect(self._on_live_error)
        worker.start()
        self._worker = worker

    # ------------------------------------------------------------------ frames

    def _on_live_frame(self, image: np.ndarray, reading: MeterReading) -> None:
        self._last_reading = reading
        self._update_histogram(reading, image)
        self._update_meter_label(reading)
        self._last_linear = image
        self.view.set_image(self._display_for(image))

    def _display_for(self, image: np.ndarray) -> np.ndarray:
        """Preview transform for one Live View frame.

        The Live View JPEG is already gamma-encoded; ``jpeg_to_linear`` linearised
        it in the worker, so the preview chain here receives true linear data --
        the same domain the developer works in. Black/white are 0/1 because the
        JPEG has no sensor pedestal.
        """
        if self.raw_view:
            # to_raw_view applies 1/2.2 display gamma to linear input.
            return preview(image, self.positive, raw_view=True, black_level=0.0, white_level=1.0)
        # A Live View JPEG's tone curve puts "base" somewhere a raw's does not,
        # so the base percentile is measured, not inherited from the raw default.
        positive = replace(self.positive, base_percentile=_LIVE_BASE_PERCENTILE)
        return preview(image, positive, raw_view=False, black_level=0.0, white_level=1.0)

    def _on_live_error(self, message: str) -> None:
        # Live View dying (cable pull, ptpcamerad stealing the device) must be
        # visible but must not close the window with captured frames on disk.
        self.statusBar().showMessage(f"Live View: {message}")
        self._log(f"Live View chyba: {message}")
        if self._worker is not None:
            self._worker.stop()
            self._worker = None

    def _update_histogram(self, reading: MeterReading, image: np.ndarray) -> None:
        # Linear domain always -- the brief is explicit and this is not a toggle.
        hist = compute(luminance(image), black_level=0.0, white_level=1.0)
        self.histogram.set_histogram(hist)
        if self.raw_view:
            self.histogram.set_curve(None)
            self.histogram_label.setText("histogram: lineární data senzoru (RAW View)")
        else:
            self.histogram.set_curve(self.positive.profile.curve_table(256))
            self.histogram_label.setText(
                "histogram: stále lineární data — křivka je jen přiložený model"
            )

    def _update_meter_label(self, reading: MeterReading) -> None:
        util = reading.highlight_utilisation
        self.meter_label.setText(
            f"p99.9 {util:6.1%} plného rozsahu"
            + ("  · PŘEPAL" if reading.clipped else "")
        )

    # ------------------------------------------------------------------ modes

    def _set_mode(self, raw_view: bool) -> None:
        self.raw_view = raw_view
        sender = self.sender()
        # The two checkboxes are mutually exclusive; block the echo toggle.
        if sender is not self.mode_raw:
            self.mode_raw.setChecked(raw_view)
        if sender is not self.mode_positive:
            self.mode_positive.setChecked(not raw_view)
        self.filmic.setEnabled(not raw_view)
        # Repaint the last frame now, so switching modes is instant even with
        # Live View stopped.
        if self._last_linear is not None:
            self.view.set_image(self._display_for(self._last_linear))
        self._refresh_buttons()

    def _on_filmic_changed(self) -> None:
        self.positive = PositiveParams(
            exposure_ev=self.filmic.exposure.value(),
            profile=FilmicProfile(
                toe=self.filmic.toe.value(),
                gamma=self.filmic.gamma.value(),
                shoulder=self.filmic.shoulder.value(),
            ),
            invert=self.filmic.invert.isChecked(),
        )
        self.histogram.set_curve(self.positive.profile.curve_table(256))
        if self._last_linear is not None and not self.raw_view:
            self.view.set_image(self._display_for(self._last_linear))

    # ---------------------------------------------------------------- actions

    def _capture(self, kind: str = "scan", scan: bool = False) -> None:
        if self.session is None:
            QMessageBox.information(self, "Nový film", "Nejdřív vytvoř film (Nástroje → Nový film).")
            return
        if self._camera_queue.busy:
            return  # one exposure at a time - the camera can only do that anyway
        if scan:
            number = self.frame_number.value() if self.frame_number.value() > 0 else None
            fn, args = self.session.capture_scan, (number,)
            label = "Snímek"
        else:
            fn = self.session.capture_dark if kind == "dark" else self.session.capture_flat
            args = ()
            label = "Dark" if kind == "dark" else "Flat"
        self._set_actions_busy(True)
        self._pause_live_view()
        self.statusBar().showMessage(f"{label}: expozice…")
        self._start_worker(fn, lambda res: self._on_captured(label, res), *args)

    def _on_captured(self, label: str, results) -> None:
        self._set_actions_busy(False)
        self._resume_live_view()
        first = results[0] if isinstance(results, list) else results
        self.statusBar().showMessage(
            f"{label} uložen: {first.path.name} ({first.size_bytes / 1e6:.1f} MB, {first.elapsed:.1f} s)"
        )
        self._log(f"{label}: {first.path.name} · {first.settings.shutter_string()} · ISO {first.settings.iso}")
        self._refresh_settings()
        self._refresh_stage()
        self.act_export.setEnabled(True)

    def _auto_exposure(self) -> None:
        if self.camera is None or self._worker is None:
            QMessageBox.information(self, "Auto Exposure", "Musí běžet Live View.")
            return
        controller = AutoExposureController(
            self.camera, self._meter, headroom_ev=DEFAULT_HEADROOM_EV
        )
        self._set_actions_busy(True)
        self._pause_live_view()
        self.statusBar().showMessage("Auto Exposure: měřím lineární data…")
        self._start_worker(
            controller.run,
            self._on_auto_exposure_done,
        )

    def _on_auto_exposure_done(self, result) -> None:
        self._set_actions_busy(False)
        self._resume_live_view()
        self._refresh_settings()
        if result.limited_by_lens:
            QMessageBox.warning(
                self,
                "Auto Exposure",
                "Fotoaparát je na nejdelším dostupném čase a scéna je stále "
                "tmavá. Jde o limit osvětlení nebo objektivu, ne softwaru.",
            )
        else:
            self.statusBar().showMessage(
                f"Auto Exposure: {result.settings.shutter_string()} "
                f"(konvergovalo po {result.iterations} krocích)"
            )
        self._log(f"AE: {result.settings.shutter_string()} converged={result.converged}")

    # ------------------------------------------------------------------ film

    def _new_film(self) -> None:
        dialog = FilmDialog(self, defaults=self.session.film if self.session else None)
        if not dialog.exec():
            return
        film: FilmMetadata = dialog.metadata()
        directory = self._choose_directory()
        if directory is None:
            return
        paths = SessionPaths.create(directory, film.film_id)
        assert self.camera is not None
        self.session = CaptureSession(camera=self.camera, film=film, paths=paths)
        self.positive = self.positive.with_base(None)
        self.setWindowTitle(f"FilmScan Studio — Capture — {film.label()}")
        self.act_export.setEnabled(True)
        self.statusBar().showMessage(f"Film {film.film_id}: {paths.root}")
        self._log(f"nový film {film.label()} ({film.film_type_class.value})")
        self._refresh_stage()
        self._refresh_buttons()

    def _choose_directory(self) -> Path | None:
        from PySide6.QtWidgets import QFileDialog

        home = Path.home()
        return Path(
            QFileDialog.getExistingDirectory(self, "Složka projektu", str(home))
        ) or None

    def _export_project(self) -> None:
        if self.session is None:
            return
        path = self.session.export_project()
        self.statusBar().showMessage(f"Projekt exportován: {path}")
        self._log(f"export {path.name}")

    # ------------------------------------------------------------- one-shot IO

    def _start_worker(self, fn, on_done, *args, on_failed=None) -> None:
        """Queue ``fn(*args)`` on the camera thread; ``on_done(result)`` on the UI thread.

        One persistent worker thread, not a thread per action: libgphoto2's
        session is not thread-safe, so every camera call must happen on the same
        thread it connected on, and a per-call QThread had to be kept alive
        against the garbage collector between ``start()`` and its first signal
        (which it was not, and the failure was a silently dropped capture).
        """
        self._camera_queue.submit(
            fn, on_done, on_failed or self._on_worker_failed, *args
        )

    def _on_worker_failed(self, message: str) -> None:
        self._set_actions_busy(False)
        self._resume_live_view()
        self.statusBar().showMessage(f"Chyba: {message}")
        self._log(f"CHYBA: {message}")

    def _set_actions_busy(self, busy: bool) -> None:
        for button in (
            self.btn_capture,
            self.btn_autoexposure,
            self.btn_dark,
            self.btn_flat,
        ):
            button.setEnabled(not busy and self.session is not None)

    # -------------------------------------------------------------- indicators

    def _refresh_settings(self) -> None:
        if self.camera is None:
            return
        settings: ExposureSettings = self.camera.get_settings()
        aperture = f"f/{settings.aperture:g}" if settings.aperture else "clona — (na objektivu)"
        self.settings_label.setText(
            f"ISO {settings.iso}   čas {settings.shutter_string()}   {aperture}"
        )

    def _refresh_stage(self) -> None:
        if self.session is None:
            self.stage_label.setText("Fáze: —")
            return
        state = self.session.state
        stage = {"dark": "1) Dark frame", "flat": "2) Flat field", "frames": "3) Snímání filmů"}[state.stage]
        self.stage_label.setText(
            f"Fáze: {stage} · dark {state.dark_count} · flat {state.flat_count} · "
            f"snímků {state.scan_count} · další #{state.next_frame_number}"
        )
        self.frame_number.setValue(0)
        self.frame_number.setMinimum(0)

    def _refresh_buttons(self) -> None:
        has_camera = self.camera is not None
        has_session = self.session is not None
        for button in (self.btn_dark, self.btn_flat):
            button.setEnabled(has_camera and has_session)
        self.btn_capture.setEnabled(has_camera and has_session)
        self.btn_autoexposure.setEnabled(has_camera)

    def _log(self, line: str) -> None:
        current = self.log_view.text()
        self.log_view.setText(f"{line}\n{current}".strip()[:2000])

    # ------------------------------------------------------------------ teardown

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._worker is not None:
            self._worker.stop()
        self._camera_queue.stop()
        if self.camera is not None:
            try:
                self.camera.disconnect()
            except Exception:  # noqa: BLE001
                log.exception("camera disconnect failed")
        super().closeEvent(event)


def _connect_gphoto2() -> GPhoto2Backend:
    """Connect on a worker thread; the retry loop can take seconds."""
    backend = GPhoto2Backend()
    backend.connect()
    return backend


def _connect_sdk() -> NikonSdkBackend:
    """Prefer the Nikon SDK, fall back to gphoto2 when its helper is missing.

    The SDK path needs a one-time x86_64 venv plus the sudo install of the
    camera module; on a fresh clone neither exists, so silently degrading to
    the backend that works keeps the app usable.
    """
    try:
        backend = NikonSdkBackend()
        backend.connect()
        return backend
    except CameraError as exc:
        log.warning("Nikon SDK unavailable (%s); falling back to gphoto2", exc)
        return _connect_gphoto2()


#: Base level for Live View previews: the JPEG is already tone-curve-compressed,
#: so the percentile that behaves like "film base" on a full raw does not
#: transfer. Measured on the live 640x424 frame instead of assumed.
_LIVE_BASE_PERCENTILE = 97.0
