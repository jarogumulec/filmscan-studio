"""The Capture window.

Layout: the Live View fills most of the window; every control (histogram,
settings, actions) lives in a narrow column on the right, because the one thing
that must be big while digitising film is the picture. The zoom ladder is
labelled in sensor pixels — 100% is one screen pixel per pixel of the 6016x4016
NEF — and asks the body for a zoomed Live View crop when more real detail is
wanted (see :mod:`filmscan_studio.core.zoom`).

Everything that talks to the camera runs in
:class:`~filmscan_studio.gui.liveview.LiveViewWorker` or a one-shot worker
object, never on the UI thread. Two UI controls break that rule deliberately:
the shutter/ISO editors and the body EV-compensation spin call the camera
directly, because they are one small enum/range write per interaction and a
queue would fight rapid stepping. Against a blocking backend they would freeze
the UI — acceptable for the SDK helper's enum writes (milliseconds); if a slow
path ever appears, route them through ``_camera_queue``.

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

import json
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
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from filmscan_studio.capture.autoexposure import (
    AutoExposureController,
    LiveMeter,
)
from filmscan_studio.capture.camera import CameraBackend, CameraError, CameraInfo
from filmscan_studio.capture.gphoto2 import GPhoto2Backend, parse_shutter
from filmscan_studio.capture.mock import MockCamera
from filmscan_studio.capture.nikon_backend import NikonSdkBackend
from filmscan_studio.capture.quality import audit_nef, render_preview_jpeg
from filmscan_studio.capture.session import CaptureSession, SessionPaths
from filmscan_studio.core.exposure import (
    ARCHIVE_ISO,
    DEFAULT_HEADROOM_EV,
    ExposureSettings,
    MeterReading,
)
from filmscan_studio.core.filmic import FilmicProfile
from filmscan_studio.core.histogram import compute
from filmscan_studio.core.models import FilmMetadata
from filmscan_studio.core.positive import (
    FastPositivePreview,
    PositiveParams,
    estimate_wb_gains,
)
from filmscan_studio.core.zoom import (
    FIT,
    ZOOM_ALL,
    ZOOM_LABELS,
    ZOOM_LEVELS,
    SensorSize,
    StreamDetail,
    choose_body_rate,
    detail_for,
    display_scale,
)
from filmscan_studio.gui.filmdialog import FilmDialog
from filmscan_studio.gui.imageutil import preview
from filmscan_studio.gui.liveview import LiveViewWorker, luminance
from filmscan_studio.gui.widgets import (
    CollapsibleBox,
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
        #: The reading computed from the last frame *before* prediction —
        #: what the histogram repaints from when the toggle flips.
        self._last_frame_reading: MeterReading | None = None
        self._last_linear: np.ndarray | None = None
        #: Body exposure-meter EV (log2(applied/correct)) from the poller, and
        #: whether the histogram should be *predicted* to the NEF with it.
        #: The D750 Live View JPEG is auto-brightness (measured 2026-09-15:
        #: identical median at 1/2 s and 1/500 s), so its own histogram says
        #: nothing about the capture; the body meter does. See _update_histogram.
        self._body_ev: float | None = None
        #: When a body meter exists, the histogram shows the *predicted NEF*
        #: (frame x 2^ev) instead of the auto-brightnessed preview. Switchable:
        #: preview metering is still what you want while composing.
        self._hist_follows_body = True
        #: Body-side Live View zoom rate currently applied (core.zoom ZOOM_*).
        self._body_zoom = ZOOM_ALL
        #: Honest account of the delivered stream, refreshed per frame.
        self._stream_detail: StreamDetail | None = None
        #: AE metering rectangle in source pixels, from the view's red drag.
        self._ae_rect: tuple[int, int, int, int] | None = None  # x0, y0, x1, y1
        #: Echo suppression: refresh writes must not re-trigger apply handlers.
        self._echo = False
        #: Set when the in-flight capture is using the mirror-up path (Live
        #: View left running, poller paused) rather than stopping Live View.
        self._capture_kept_lv = False
        #: Post-capture NEF exposure audit verdict, for the status label.
        self._last_audit: str | None = None
        self._wb_gains: tuple[float, float, float] | None = None
        self._fast_preview = FastPositivePreview(
            PositiveParams(base_percentile=_LIVE_BASE_PERCENTILE))
        #: Last film record — prefills the rig group of the next dialog.
        self._last_film: FilmMetadata | None = None

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

        # The picture owns the window: the Live View is the central widget and
        # everything else — histogram, metering, camera controls, actions —
        # lives in one narrow column to its right. Focusing and checking
        # exposure both want the largest possible view of real pixels.
        self.view = ZoomView(sensor=self._sensor_size())
        self.view.centerChanged.connect(
            lambda x, y: self.statusBar().showMessage(
                f"střed {x}×{y} px senzoru", 2000
            )
        )
        self.view.aeRectChanged.connect(self._on_ae_rect)

        right = QWidget()
        right_layout = QVBoxLayout(right)

        self.histogram = HistogramWidget()
        right_layout.addWidget(self.histogram)
        self.histogram_label = QLabel("histogram: lineární data senzoru")
        self.histogram_label.setWordWrap(True)
        right_layout.addWidget(self.histogram_label)
        self.clip_label = QLabel("clip: —")
        self.clip_label.setStyleSheet("font-family: Menlo, monospace;")
        self.clip_label.setWordWrap(True)
        right_layout.addWidget(self.clip_label)

        self.hist_predict = QCheckBox("Histogram = predikce NEFu (tělní meter)")
        self.hist_predict.setStyleSheet("font-size: 11px;")
        self.hist_predict.setChecked(True)
        self.hist_predict.setToolTip(
            "Náhledový JPEG těla je automaticky podsvícený — jeho histogram "
            "o expozici NEFu nevypovídá nic (změřeno 2026-09). Zapnuto "
            "histogram zobrazuje, jak snímek dopadne: náhled posunutý o "
            "měřič těla. Vypni pro histogram samotného náhledu (skládání)."
        )
        self.hist_predict.toggled.connect(self._on_hist_predict_toggled)
        right_layout.addWidget(self.hist_predict)

        # ------------------------------------------------ camera exposure layer
        # Vertical economy (2026-09: "pravý panel se nevhází — zkomprimuj"):
        # one QFormLayout whose rows are two-control HBoxLayouts — label text
        # lives in tooltips, which are shorter than the widgets they name.
        exposure_box = CollapsibleBox("Expozice fotoaparátu (ovlivňuje focení)",
                                      expanded=True)
        exposure_form = QFormLayout()
        exposure_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        exposure_box.body_layout().addLayout(exposure_form)

        self.shutter_edit = QComboBox()
        self.shutter_edit.setEditable(True)
        self.shutter_edit.setToolTip(
            "Čas — zadej 1/60, 2.5…; tělo snapne na nejbližší dostupný. "
            "Vyžaduje režim M/S na těle."
        )
        self.shutter_edit.activated.connect(self._apply_shutter_edit)
        # editingFinished lives on the editable combo's line edit, not the combo.
        self.shutter_edit.lineEdit().editingFinished.connect(self._apply_shutter_edit)
        self.iso_select = QComboBox()
        self.iso_select.setToolTip(
            "ISO nabízené tělem. Pro digitalizaci platí: nejnižší = nejlepší "
            "odstup signálu od šumu."
        )
        self.iso_select.currentIndexChanged.connect(self._apply_iso_select)
        row = QHBoxLayout()
        row.addWidget(self.shutter_edit)
        row.addWidget(self.iso_select)
        # No field label: in a 360 px column the label would steal width from
        # the two widgets it names, and both say what they are once opened.
        exposure_form.addRow(row)

        self.ev_spin = QDoubleSpinBox()
        self.ev_spin.setRange(-5.0, 5.0)
        self.ev_spin.setSingleStep(1 / 3)
        self.ev_spin.setDecimals(2)
        self.ev_spin.setSuffix(" EV")
        self.ev_spin.setToolTip(
            "Expoziční korekce těla (ExposureComp). Pozor: náhledová expozice "
            "v Working Positive je jiná vrstva — viz Náhled výše."
        )
        self.ev_spin.editingFinished.connect(self._apply_ev_comp)
        self.btn_iso_low = QPushButton("ISO 100")
        self.btn_iso_low.setToolTip(
            "Archivní sken se exponuje při ISO 100 (native base D750) — "
            "odstup signálu od šumu; expozice se pak řeší jen časem."
        )
        self.btn_iso_low.clicked.connect(self._iso_to_base)
        row = QHBoxLayout()
        row.addWidget(self.ev_spin)
        row.addWidget(self.btn_iso_low)
        exposure_form.addRow("Korekce", row)

        self.btn_autoexposure = QPushButton("Auto Exposure")
        self.btn_autoexposure.clicked.connect(self._auto_exposure)
        self.btn_autoexposure.setToolTip(
            "Řeší expozici najednou na tělním měřiči — stiskni jednou a hotovo. "
            "Změníš-li pak ručně čas/ISO nebo osvětlení, stiskni znovu (histogram "
            "= predikce NEFu hnutí hned ukáže)."
        )
        self.ae_hint = QLabel(
            "Shift+tažení: červený AE rámeček (histogram měří uvnitř; pravé "
            "tlačítko zruší). Tažení bez Shiftu při zoomu posouvá, klik = "
            "vycentrovat."
        )
        self.ae_hint.setWordWrap(True)
        self.ae_hint.setStyleSheet("color: #9a9; font-size: 11px;")
        exposure_box.body_layout().addWidget(self.btn_autoexposure)
        exposure_box.body_layout().addWidget(self.ae_hint)
        right_layout.addWidget(exposure_box)

        self.settings_label = QLabel("ISO —   čas —   clona —")
        self.settings_label.setStyleSheet("font-family: Menlo, monospace; font-size: 13px;")
        self.meter_label = QLabel("—")
        self.meter_label.setStyleSheet("font-family: Menlo, monospace;")
        self.meter_label.setWordWrap(True)
        right_layout.addWidget(self.settings_label)
        right_layout.addWidget(self.meter_label)

        # ------------------------------------------------------- preview layer
        # The negative toggle used to need three clicks inside this box
        # (expand → Working Positive → Invertovat) and effectively vanished;
        # the brief asks for the toggle alone, so it sits in the panel header
        # row next to the mode checkboxes — same layer, same one line.
        self.preview_box = CollapsibleBox(
            "Náhled — expozice & křivka (NEOVlivňuje focení)", expanded=False
        )
        mode_row = QHBoxLayout()
        self.neg_toggle = QCheckBox("Negativ")
        self.neg_toggle.setToolTip(
            "Invertuje a aplikuje křivku na náhled. Uložený RAW se nemění."
        )
        self.neg_toggle.toggled.connect(self._toggle_negative_preview)
        mode_row.addWidget(self.neg_toggle)
        self.mode_raw = QCheckBox("RAW View")
        self.mode_raw.setChecked(True)
        self.mode_raw.toggled.connect(lambda on: self._set_mode(raw_view=on))
        mode_row.addWidget(self.mode_raw)
        self.mode_positive = QCheckBox("Positive")
        self.mode_positive.toggled.connect(lambda on: self._set_mode(raw_view=not on))
        mode_row.addWidget(self.mode_positive)
        mode_row.addStretch()
        # Camera WB is not an option here: the body would bake a per-frame
        # guess into the display while the orange mask of a colour negative
        # needs ONE neutral for the whole film. So the app solves it once —
        # on film base inside the AE rect if set — and holds it.
        self.btn_wb = QPushButton("Auto WB")
        self.btn_wb.setToolTip(
            "Vyváží bílou z nejjasnějšího pole snímku (filmový podklad), "
            "případně z červeného AE rámečku. Zisky se uzamknou pro celý "
            "film — per-frame by oranžová maska barevného negativu "
            "vyvažování roztančila. Po vyvážení se zapne Negative náhled, "
            "ať je výsledek vidět."
        )
        self.btn_wb.clicked.connect(self._auto_white_balance)
        mode_row.addWidget(self.btn_wb)
        # Outside the collapsed box on purpose: the toggle is the point of the
        # box's whole layer, hidden-behind-expand is what made it disappear.
        right_layout.addLayout(mode_row)
        self.filmic = FilmicPanel()
        self.filmic.paramsChanged.connect(self._on_filmic_changed)
        self.filmic.setEnabled(False)
        self.preview_box.body_layout().addWidget(self.filmic)
        right_layout.addWidget(self.preview_box)

        # ---------------------------------------------------------------- zoom
        zoom_row = QHBoxLayout()
        zoom_row.addWidget(QLabel("Zoom"))
        self.zoom_select = QComboBox()
        for level in ZOOM_LEVELS:
            self.zoom_select.addItem(ZOOM_LABELS[level], level)
        self.zoom_select.currentIndexChanged.connect(self._on_zoom_selected)
        self.view.zoomChanged.connect(
            lambda z: self.zoom_select.setCurrentIndex(
                self.zoom_select.findData(z)
            )
        )
        zoom_row.addWidget(self.zoom_select)
        zoom_row.addStretch()
        right_layout.addLayout(zoom_row)
        self.zoom_note = QLabel()
        self.zoom_note.setWordWrap(True)
        self.zoom_note.setStyleSheet("color: #fc9; font-size: 11px;")
        right_layout.addWidget(self.zoom_note)

        # ------------------------------------------------------------- actions
        self.btn_capture = QPushButton("Capture")
        self.btn_capture.setMinimumHeight(48)
        self.btn_capture.clicked.connect(lambda: self._capture(scan=True))
        right_layout.addWidget(self.btn_capture)

        # Media-format guard. The D750 answers a still capture with whatever
        # Compression Level/Image Size the body dial says — measured 2026-09:
        # JPEG Basic at S size, a ~1 MB 'NEF'. The button only appears when
        # the body is actually misconfigured (or a JPEG capture proved it).
        self.btn_raw_media = QPushButton("⚠ Tělo posílá JPEG — Nastavit RAW + L")
        self.btn_raw_media.setStyleSheet(
            "QPushButton { color: #211; background: #fc9; font-weight: bold; }"
        )
        self.btn_raw_media.clicked.connect(self._fix_raw_media)
        self.btn_raw_media.setVisible(False)
        right_layout.addWidget(self.btn_raw_media)

        # Calibration and numbering happen once per session each, yet used to
        # claim three permanent rows (2026-09: "nevleze se tam vše").
        calib_box = CollapsibleBox("Kalibrace & číslo snímku", expanded=False)
        calib_row = QHBoxLayout()
        self.btn_dark = QPushButton("Dark Frame")
        self.btn_dark.clicked.connect(lambda: self._capture(kind="dark"))
        calib_row.addWidget(self.btn_dark)
        self.btn_flat = QPushButton("Flat Field")
        self.btn_flat.clicked.connect(lambda: self._capture(kind="flat"))
        calib_row.addWidget(self.btn_flat)
        calib_box.body_layout().addLayout(calib_row)
        frame_row = QHBoxLayout()
        frame_row.addWidget(QLabel("Číslo snímku"))
        self.frame_number = QSpinBox()
        self.frame_number.setMinimum(1)
        self.frame_number.setSpecialValueText("auto")
        frame_row.addWidget(self.frame_number)
        frame_row.addStretch()
        calib_box.body_layout().addLayout(frame_row)
        right_layout.addWidget(calib_box)

        self.stage_label = QLabel("Fáze: —")
        self.stage_label.setWordWrap(True)
        self.stage_label.setStyleSheet("font-size: 11px;")
        right_layout.addWidget(self.stage_label)

        self.log_view = QLabel("")
        self.log_view.setWordWrap(True)
        self.log_view.setMaximumHeight(64)
        self.log_view.setStyleSheet(
            "color: #aaa; font-family: Menlo, monospace; font-size: 10px;"
        )
        right_layout.addWidget(self.log_view)
        right_layout.addStretch()

        # Belt and braces for the vertical economy: even with every row
        # merged, a short window must not cut controls off — it scrolls.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(right)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        scroll.setFrameShape(scroll.Shape.NoFrame)

        splitter = QSplitter()
        splitter.addWidget(self.view)
        right.setFixedWidth(360)
        splitter.addWidget(scroll)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setChildrenCollapsible(False)
        self.setCentralWidget(splitter)
        self.view.set_zoom(ZOOM_LEVELS[0])
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
        # Earlier text printed len(iso_choices)/len(shutter_choices) here and
        # read as "ISO 22 · časů 52" — counts of *offerings*, not settings.
        iso_note = ""
        if info.iso_choices:
            iso_note = f" · ISO {min(info.iso_choices)}–{max(info.iso_choices)}"
        shutter_note = ""
        if info.shutter_choices:
            shutter_note = (
                f" · časy 1/{round(1 / min(info.shutter_choices))}–"
                f"{max(info.shutter_choices):g}s"
            )
        self._log(f"{info.manufacturer or ''} {info.model}{iso_note}{shutter_note}")
        caps = camera.capabilities()
        if not caps.aperture:
            self._log("Clona není přes USB ovladatelná (manuální objektiv) — nastavuje se na objektivu.")
        if not caps.live_view_zoom:
            self._log(
                "Tento backend neumí zoom proudu na straně těla — detail při "
                "zoomu zůstane interpolovaný (gphoto2 posílá jen whole-frame "
                "640 px). Přepni na Nikon SDK."
            )
        self.view.sensor = self._sensor_size()
        self._populate_exposure_editors()
        self.start_live_view()
        self._refresh_settings()
        self._refresh_buttons()
        self._check_media_settings()

    def _sensor_size(self) -> SensorSize:
        if self.info and self.info.sensor_width and self.info.sensor_height:
            return SensorSize(self.info.sensor_width, self.info.sensor_height)
        return SensorSize()  # D750 NEF default, documented fallback

    # ------------------------------------------------------ camera exposure UI

    def _populate_exposure_editors(self) -> None:
        """Fill the shutter/ISO editors from what the body offers.

        No signal suppression helper is needed for the shutter combo: its
        handlers run on ``activated``/``editingFinished`` (user actions), not on
        ``currentIndexChanged``; the ISO combo's index signal is guarded by
        ``_echo`` because a refresh must not re-apply (and re-snap) settings the
        body already has.
        """
        assert self.camera is not None and self.info is not None
        self._echo = True
        try:
            self.shutter_edit.clear()
            for seconds in self.info.shutter_choices:
                text = ExposureSettings(shutter=seconds).shutter_string()
                self.shutter_edit.addItem(text)
            self.iso_select.clear()
            for iso in self.info.iso_choices:
                self.iso_select.addItem(str(iso), iso)
        finally:
            self._echo = False

    def _apply_shutter_edit(self) -> None:
        if self.camera is None:
            return
        text = self.shutter_edit.currentText().strip()
        seconds = parse_shutter(text)
        if seconds is None or seconds <= 0:
            self.statusBar().showMessage(f"Čas '{text}' nejde pochopit (zkus 1/60 nebo 2.5)", 4000)
            self._refresh_settings()
            return
        try:
            applied = self.camera.set_shutter(seconds)
        except CameraError as exc:
            QMessageBox.warning(self, "Čas", str(exc))
            self._refresh_settings()
            return
        if abs(applied - seconds) / seconds > 0.02:
            self.statusBar().showMessage(
                f"Čas {ExposureSettings(shutter=seconds).shutter_string()} "
                f"snapnut na {ExposureSettings(shutter=applied).shutter_string()}", 4000
            )
        self._refresh_settings()

    def _apply_iso_select(self) -> None:
        if self.camera is None or self._echo:
            return
        iso = self.iso_select.currentData()
        if iso is None:
            return
        try:
            applied = self.camera.set_iso(int(iso))
        except CameraError as exc:
            QMessageBox.warning(self, "ISO", str(exc))
            self._refresh_settings()
            return
        if applied != iso:
            self.statusBar().showMessage(f"ISO {iso} snapnuto na {applied}", 4000)
        self._refresh_settings()

    def _iso_to_base(self) -> None:
        """Archival ISO: the scan is a transmission measurement at the noise
        floor — ISO 100 on the D750, its native base, exposure lives in time."""
        self._set_iso_pinned(ARCHIVE_ISO)

    def _set_iso_pinned(self, wanted: int) -> None:
        if self.camera is None or self.info is None or not self.info.iso_choices:
            return
        try:
            applied = self.camera.set_iso(wanted)
        except CameraError as exc:
            QMessageBox.warning(self, "ISO", str(exc))
            return
        note = "" if applied == wanted else f" — tělo snaplo z {wanted}"
        self.statusBar().showMessage(f"ISO {applied}{note}", 4000)
        self._refresh_settings()
        self._refresh_buttons()

    def _capture_block_reason(self) -> str | None:
        """Why Capture must refuse right now, or None when it may run.

        The archival rule (2026-09 brief): scan at ISO 100 and vary only the
        shutter. A frame exposed at any other ISO is a different measurement
        with more noise, so the button refuses rather than let it slip through;
        the operator presses the ISO 100 button or fixes it on the body.
        """
        if self.camera is None:
            return "Fotoaparát není připojen."
        try:
            iso = self.camera.get_settings().iso
        except Exception:  # noqa: BLE001 - settings read flaky over USB
            return None       # never block a shot on a meter read that hiccuped
        if iso != ARCHIVE_ISO:
            return (f"Archivní sken vyžaduje ISO {ARCHIVE_ISO} (teď {iso}) — "
                    "stiskni 'ISO 100' a exponuj jen časem.")
        return None

    def _apply_ev_comp(self) -> None:
        if self.camera is None:
            return
        try:
            applied = self.camera.set_exposure_ev(self.ev_spin.value())
        except NotImplementedError:
            self.statusBar().showMessage("Expoziční korekce přes tento backend nejde nastavit.", 4000)
        except CameraError as exc:
            QMessageBox.warning(self, "Korekce expozice", str(exc))
            return
        else:
            self._echo = True
            self.ev_spin.setValue(applied)
            self._echo = False

    # -------------------------------------------------------------- zoom wiring

    def _on_zoom_selected(self) -> None:
        self.view.set_zoom(self.zoom_select.currentData())
        self._apply_body_zoom()

    def _apply_body_zoom(self) -> None:
        """Ask the body for the crop that serves the selected display scale.

        Runs on the UI thread; the SDK helper's enum write is milliseconds and
        the Live View worker retries the transient DeviceBusy the body answers
        while a frame is in flight. The alternative (queue it, apply it seconds
        later after the poller stopped) would make zoom feel lagged.
        """
        if self.camera is None:
            return
        caps = self.camera.capabilities()
        if not caps.live_view_zoom:
            self._update_zoom_note()
            return
        frame_size = self._last_source_size()
        rate = choose_body_rate(
            self.view.zoom(), self.view.width(), self.view.height(),
            frame_size[0], frame_size[1], self._sensor_size(),
        )
        if rate == self._body_zoom:
            self._update_zoom_note()
            return
        try:
            self.camera.set_live_view_zoom(rate)
        except CameraError as exc:
            self.statusBar().showMessage(f"Zoom těla se nepovedl: {exc}", 4000)
            self._update_zoom_note()
            return
        self._body_zoom = rate
        # The red rect is in *stream* pixels; the new rate delivers different
        # pixels, so a kept rect would silently meter the wrong film area.
        self.view.clear_ae_rect()
        self._update_zoom_note()

    def _last_source_size(self) -> tuple[int, int]:
        if self._last_linear is not None:
            h, w = self._last_linear.shape[:2]
            return w, h
        return 640, 424  # D750 whole-frame LV stream, measured

    def _update_zoom_note(self) -> None:
        w, h = self._last_source_size()
        detail = detail_for(self._body_zoom, w, h, self._sensor_size())
        zoom = self.view.zoom()
        # Disclose what is *drawn*, not what was requested: the view snaps to
        # whole multiples of the stream (×1 ×2 ×3) so focusing never fights a
        # resampled 3.425× grain. One delivered pixel lands on `k` screen
        # pixels; the stream is worth `sensor_px_per_lv_px` sensor pixels per
        # delivered pixel, so inventing detail starts where k exceeds it.
        k = self.view.source_zoom()
        interp = detail.interpolation_at(k / detail.sensor_px_per_lv_px)
        note = detail.summary() + f" · zobrazení ×{k:g} (interpolace ×{interp:.1f})"
        if interp > 2.0:
            note += " — proud neposkytuje tolik detailu"
        self.zoom_note.setText(note)
        # On the picture itself only when zoomed: Fit is an overview, the
        # overlay would just clutter it.
        self.view.set_detail_note("" if zoom == FIT else note)

    # ----------------------------------------------------------------- AE rect

    def _on_ae_rect(self, rect) -> None:
        if rect is None:
            self._ae_rect = None
            self.statusBar().showMessage("AE výřez zrušen", 2500)
            return
        self._ae_rect = (rect.x(), rect.y(),
                         rect.x() + rect.width(), rect.y() + rect.height())
        self.statusBar().showMessage(
            f"AE výřez {rect.width()}×{rect.height()} px proudu "
            "(pravé tlačítko zruší)", 4000
        )

    def _meter_source(self):
        """Metering callable for Auto Exposure: whole frame or the red rect."""
        rect = self._ae_rect

        def read():
            frame = self.camera.next_live_frame()
            if frame is None:
                raise RuntimeError("Live View neběží – nelze měřit")
            encoded = self._meter.decode_live_frame(frame)
            if rect is not None:
                x0, y0, x1, y1 = rect
                h, w = encoded.shape[:2]
                x0, y0 = max(x0, 0), max(y0, 0)
                x1, y1 = min(x1, w), min(y1, h)
                if x1 <= x0 or y1 <= y0:
                    raise RuntimeError("AE výřez leží mimo snímek")
                encoded = encoded[y0:y1, x0:x1]
            linear = self._meter.jpeg_to_linear(encoded)
            lum = linear @ np.array([0.2126, 0.7152, 0.0722])
            return self._meter.meter_raw_signal(
                lum, self._meter.black_level, self._meter.white_level
            )

        return read

    def _pause_live_view(self) -> None:
        """Stop the poller so a blocking camera call owns the session."""
        if self._worker is not None:
            self._worker.stop()
            self._worker = None

    def _resume_live_view(self) -> None:
        # A worker only *paused* for a mirror-up capture resumes polling; the
        # body never left Live View, so there is nothing to re-arm.
        if self._worker is not None and self._worker.running:
            self._worker.resume_polling()
            return
        if self.camera is not None:
            self.start_live_view()

    def start_live_view(self) -> None:
        if self.camera is None or (self._worker is not None and self._worker.running):
            return
        worker = LiveViewWorker(
            self.camera, self._meter,
            # None where the backend has no body meter (mock, gphoto2): the
            # worker then emits None with every frame and the histogram stays
            # a plain preview histogram.
            body_ev_fn=getattr(self.camera, "exposure_ev", None),
        )
        worker.frameReady.connect(self._on_live_frame)
        worker.error.connect(self._on_live_error)
        worker.start()
        self._worker = worker

    # ------------------------------------------------------------------ frames

    def _on_live_frame(
        self, image: np.ndarray, reading: MeterReading, body_ev=None
    ) -> None:
        h, w = image.shape[:2]
        self._stream_detail = detail_for(self._body_zoom, w, h, self._sensor_size())
        self.view.set_source_scale(self._stream_detail.sensor_px_per_lv_px)
        if body_ev is not None:
            self._body_ev = float(body_ev)
        self._last_frame_reading = reading
        # The reading the histogram describes (rect-trimmed, NEF-predicted when
        # the body meter is on) is also what the label shows — one number, one
        # truth.
        self._last_reading = self._update_histogram(reading, image)
        self._update_meter_label(self._last_reading)
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
        # The exact chain costs a spline evaluation per pixel per frame — the
        # 2026-09 fps complaint. WB + base subtract + exposure + filmic are all
        # per-channel scalar maps once the base is fixed, so the fast preview
        # precomputes them into LUTs and rebuilds only when a parameter moved
        # or its base refresh came due. Same curve, table-lookup price.
        self._fast_preview.set_params(positive)
        return self._fast_preview.render(image)

    def _on_live_error(self, message: str) -> None:
        # Live View dying (cable pull, ptpcamerad stealing the device) must be
        # visible but must not close the window with captured frames on disk.
        self.statusBar().showMessage(f"Live View: {message}")
        self._log(f"Live View chyba: {message}")
        if self._worker is not None:
            self._worker.stop()
            self._worker = None

    def _hist_prediction(self) -> float | None:
        """EV factor the preview must be scaled by to predict the NEF.

        The auto-brightnessed LV display shows the scene as *correct* (mid at
        18%) whatever the capture settings are; the body meter reports
        ``ev = log2(applied/correct)``. So the NEF's mid lands at
        ``0.18 * 2**ev`` and every preview luminance scales by ``2**ev``.
        None = no body meter (mock/gphoto2) or the operator wants the raw
        preview histogram (compose-time mode).
        """
        if not self._hist_follows_body or self._body_ev is None:
            return None
        return self._body_ev

    def _on_hist_predict_toggled(self, on: bool) -> None:
        self._hist_follows_body = on
        # Repaint from the last *frame* (not from `_last_reading`, which on
        # the prediction path is a synthetic predicted reading): waiting for
        # the next frame would make the checkbox feel dead when LV is paused.
        if (self._last_linear is not None
                and self._last_frame_reading is not None):
            self._last_reading = self._update_histogram(
                self._last_frame_reading, self._last_linear
            )
            self._update_meter_label(self._last_reading)

    def _update_histogram(self, reading: MeterReading, image: np.ndarray) -> MeterReading:
        # Linear domain always -- the brief is explicit and this is not a toggle.
        # Same source as Auto Exposure: when the red rect is set, the histogram
        # describes exactly the area AE meters, or the two tools disagree and
        # the operator starts trusting the wrong one.
        region = image
        if self._ae_rect is not None:
            x0, y0, x1, y1 = self._ae_rect
            h, w = image.shape[:2]
            x0, y0 = max(int(x0), 0), max(int(y0), 0)
            x1, y1 = min(int(x1), w), min(int(y1), h)
            if x1 > x0 and y1 > y0:      # stale rect after a zoom-stream change
                region = image[y0:y1, x0:x1]
        lum = luminance(region)
        predicted = self._hist_prediction()
        scale = 2.0**predicted if predicted is not None else 1.0
        display_lum = np.clip(lum * scale, 0.0, 1.0)
        hist = compute(display_lum, black_level=0.0, white_level=1.0)
        if self._ae_rect is not None or predicted is not None:
            reading = self._meter.meter_raw_signal(
                display_lum, self._meter.black_level, self._meter.white_level
            )
        self.histogram.set_histogram(hist)
        total = max(hist.total, 1)
        # The clip report the brief asks for: both rails, as a share of pixels.
        # Red bar (blown) and blue bar (crushed) are drawn on the histogram
        # itself; this is their numeric counterpart.
        self.clip_label.setText(
            f"clip: bílá {hist.clipped_high / total:.3%} (červeně) · "
            f"černá {hist.clipped_low / total:.3%} (modře)"
        )
        scope = "AE výřez" if self._ae_rect is not None else "celý snímek"
        if predicted is not None:
            source = f"predikce NEFu ({scope}, tělní meter {predicted:+.2f} EV)"
        else:
            source = f"lineární data náhledu · {scope}"
        if self.raw_view:
            self.histogram.set_curve(None)
            self.histogram_label.setText(f"histogram: {source} (RAW View)")
        else:
            self.histogram.set_curve(self.positive.profile.curve_table(256))
            self.histogram_label.setText(
                f"histogram: {source} — křivka je jen přiložený model"
            )
        return reading

    def _toggle_negative_preview(self, on: bool) -> None:
        """One-switch negative → picture preview; the filmic settings stay in
        the collapsed panel. RAW capture is untouched — this only repaints."""
        self._set_mode(raw_view=not on)
        if on and not self.filmic.invert.isChecked():
            self.filmic.invert.setChecked(True)   # fires _on_filmic_changed

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
        # Keep the one-switch negative toggle honest whichever way we got here.
        self.neg_toggle.blockSignals(True)
        self.neg_toggle.setChecked(not raw_view)
        self.neg_toggle.blockSignals(False)
        self.filmic.setEnabled(not raw_view)
        # Repaint the last frame now, so switching modes is instant even with
        # Live View stopped — and repaint the histogram with it: its curve
        # overlay belongs to the mode, and waiting up to a frame period for
        # the next one made the toggle look lagged.
        if self._last_linear is not None:
            self.view.set_image(self._display_for(self._last_linear))
            if self._last_frame_reading is not None:
                self._last_reading = self._update_histogram(
                    self._last_frame_reading, self._last_linear
                )
        self._refresh_buttons()

    def _auto_white_balance(self) -> None:
        """Solve WB once (film base / AE rect) and hold it for the whole film.

        Camera WB cannot do this job: the D750 would re-guess per frame, and
        the orange mask of a colour negative makes every frame's guess slightly
        different — a cast that breathes. Metering the *brightest* area of a
        negative is metering clear film base, which is the one thing on the
        frame that is truly neutral. The AE rect lets the operator point the
        meter at a base strip deliberately.
        """
        if self._last_linear is None:
            self.statusBar().showMessage("Auto WB: zatím není co vyvážit (Liv View?)", 4000)
            return
        img = self._last_linear
        if self._ae_rect is not None:
            x0, y0, x1, y1 = self._ae_rect
            h, w = img.shape[:2]
            x0, y0 = max(int(x0), 0), max(int(y0), 0)
            x1, y1 = min(int(x1), w), min(int(y1), h)
            if x1 > x0 and y1 > y0:
                img = img[y0:y1, x0:x1]
        try:
            gains = estimate_wb_gains(img)
        except ValueError as exc:
            self.statusBar().showMessage(f"Auto WB: {exc}", 4000)
            return
        self._wb_gains = gains
        # PositiveParams is frozen and rebuilt by the filmic panel — carry the
        # locked gains through so no slider move silently drops the balance.
        self.positive = replace(self.positive, wb_gains=gains)
        # "ukazovat náhled po vyvážení" (2026-09): the correction is only
        # judgeable through the inverted, curved preview.
        if self.raw_view:
            self.neg_toggle.setChecked(True)   # routes through _set_mode
        if self._last_linear is not None:
            self.view.set_image(self._display_for(self._last_linear))
        self.statusBar().showMessage(
            f"Auto WB uzamčen pro celý film: R{gains[0]:.2f} "
            f"G{gains[1]:.2f} B{gains[2]:.2f}"
            + (" (měřeno v AE rámečku)" if self._ae_rect is not None else ""),
            6000,
        )
        self._log(f"Auto WB {gains[0]:.2f}/{gains[1]:.2f}/{gains[2]:.2f} uzamčeno")

    def _on_filmic_changed(self) -> None:
        self.positive = PositiveParams(
            exposure_ev=self.filmic.exposure.value(),
            profile=FilmicProfile(
                toe=self.filmic.toe.value(),
                gamma=self.filmic.gamma.value(),
                shoulder=self.filmic.shoulder.value(),
            ),
            invert=self.filmic.invert.isChecked(),
            # The locked WB survives every curve edit (see _auto_white_balance).
            wb_gains=self._wb_gains,
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
        reason = self._capture_block_reason()
        if reason is not None:
            self.statusBar().showMessage(reason, 6000)
            return
        if scan:
            number = self.frame_number.value() if self.frame_number.value() > 0 else None
            fn, args = self.session.capture_scan, (number,)
            label = "Snímek"
        else:
            fn = self.session.capture_dark if kind == "dark" else self.session.capture_flat
            args = ()
            label = "Dark" if kind == "dark" else "Flat"
        self._set_actions_busy(True)
        # Mirror-up path: a backend that releases during Live View keeps the
        # mirror raised — poller paused (free pipe), Live View *left on*.
        # Stopping the worker instead would drop the mirror, and the re-raise
        # before the next frame is the shake that blurred captures (2026-09).
        self._capture_kept_lv = False
        if (self._worker is not None and self._worker.running
                and self.session.keep_live_view
                and self.camera is not None
                and self.camera.capabilities().capture_in_live_view):
            self._worker.pause_polling()
            self._capture_kept_lv = True
        else:
            self._pause_live_view()
        self.statusBar().showMessage(f"{label}: expozice…")
        self._start_worker(fn, lambda res: self._on_captured(label, res), *args)

    def _check_media_settings(self) -> None:
        """Ask the body what format/size its stills will be, warn if wrong.

        Runs on the UI thread because both calls are one CapGet each (the same
        small-write exception as the shutter/ISO editors); a backend without
        the caps answers None/raises and is simply skipped.
        """
        fn = getattr(self.camera, "media_settings", None)
        if fn is None:
            return
        try:
            media = fn()
        except Exception as exc:  # noqa: BLE001 - a diagnostic must not block
            self._log(f"Kontrola formátu snímání selhala: {exc}")
            return
        if self._media_is_raw_full_frame(media):
            self.btn_raw_media.setVisible(False)
        else:
            self.btn_raw_media.setVisible(True)
            self._log("Tělo má Compression Level/Size nastaveno tak, že "
                      "posílá JPEG místo RAW — Archiv chce 6016×4016 NEF. "
                      "Klikni 'Nastavit RAW + L'.")

    @staticmethod
    def _media_is_raw_full_frame(media: dict) -> bool:
        """NEF-only depends on Compression Level alone.

        Measured 2026-09-15 on the D750: with Compression Level = RAW the body
        drops OP_SET from Image Size — size then configures only the JPEG
        companion of a RAW+JPEG combo, never the NEF, which is always
        L(6016×4016). Checking size here would nag forever in RAW mode.
        """
        comp = media.get("compression", {})
        cur, strings = comp.get("current"), comp.get("strings") or []
        if cur is None or not strings or cur >= len(strings):
            return True      # unreadable cap: do not nag on an unknown
        return strings[cur].upper().startswith("RAW")

    def _fix_raw_media(self) -> None:
        if self.camera is None:
            return
        fn = getattr(self.camera, "set_media", None)
        if fn is None:
            QMessageBox.information(self, "RAW", "Tento backend neumí nastavit formát.")
            return
        try:
            wanted = self._raw_media_indices()
            readback = fn(**wanted) if wanted else self.camera.media_settings()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "RAW", f"Tělo nastavení nepřijalo: {exc}")
            return
        if self._media_is_raw_full_frame(readback):
            self.btn_raw_media.setVisible(False)
            self.statusBar().showMessage(
                "Formát nastaven: RAW (NEF 6016×4016) — ověřeno zpětným čtením", 6000
            )
            self._log("Compression Level → RAW, ověřeno CapGet (Image Size se "
                      "na NEF nevztahuje — měřeno 2026-09)")
        else:
            QMessageBox.warning(
                self, "RAW",
                "Tělo sice odpovědělo, ale zpětné čtení hlásí jiný stav:\n"
                + json.dumps(readback, ensure_ascii=False)
                + "\nZkontroluj, není volič režimů na M/P/S/A (režim scény "
                "umí JPEG vynutit).",
            )

    def _raw_media_indices(self) -> dict:
        """Index of a RAW-bearing element in the body's own enum strings."""
        comp = self.camera.media_settings().get("compression", {})
        if not comp.get("settable", False):
            return {}        # body refuses writes right now (mode dial)
        for i, s in enumerate(comp.get("strings") or []):
            if s.upper().startswith("RAW"):
                return {"compression": i}
        return {}

    def _on_captured(self, label: str, results) -> None:
        self._set_actions_busy(False)
        self._resume_live_view()
        first = results[0] if isinstance(results, list) else results
        self.statusBar().showMessage(
            f"{label} uložen: {first.path.name} ({first.size_bytes / 1e6:.1f} MB, {first.elapsed:.1f} s)"
        )
        self._log(f"{label}: {first.path.name} · {first.settings.shutter_string()} · ISO {first.settings.iso}")
        if getattr(first, "lv_cycled", False) and self.session is not None:
            # The body refused the mirror-up release this time. One note and
            # stop asking: repeated cycling is exactly the shake the operator
            # is trying to eliminate, and now it would be a known cost.
            self.session.keep_live_view = False
            self._log("tělo při Capture vypnulo Live View (zrcadlo sjelo) — "
                      "zaznamenáno, příště se LV před expozicí vypne rovnou.")
        if getattr(first, "file_format", "nef") == "jpeg":
            # A capture is harder evidence than any CapGet read: the body
            # answered a still with JPEG. Surface the fix, not just a log line.
            self.btn_raw_media.setVisible(True)
            QMessageBox.warning(
                self, label,
                f"{first.path.name} je ve skutečnosti JPEG "
                f"({first.size_bytes / 1e6:.1f} MB) — tělo má Compression "
                "Level jinou než RAW. Na archivní digitalizaci je "
                "nepoužitelné. Klikni na '⚠ Nastavit RAW + L' a snímek "
                "zopakuj.",
            )
        self._refresh_settings()
        self._refresh_stage()
        self.act_export.setEnabled(True)
        # Post-capture exposure audit + positive preview JPEG, off the UI
        # thread and off the camera: both read the NEF that just landed.
        if label == "Snímek" and getattr(first, "file_format", "nef") == "nef":
            self._post_capture_check(first)

    # --------------------------------------------------- post-capture pipeline

    def _post_capture_check(self, result) -> None:
        """Audit the fresh NEF and render its positive JPEG, off-thread.

        The NEF is the only honest metering source (the Live View JPEG is
        auto-brightness and the body meter is global), so "right-max" exposure
        is decided *after* the capture: the audit says under/over, the shutter
        is corrected for the next frame, and the operator is told to repeat
        this one — an exposed frame cannot be rescued. File IO only; the
        camera is never touched, so the queue slot frees for the next shot.
        """
        if self.session is None:
            return
        if result.path.suffix.lower() not in (".nef", ".nrw", ".dng"):
            # The mock body writes .npy mosaics; auditing one through rawpy
            # would only produce a scary traceback over a passing test.
            return
        # Rect→sensor mapping is linear only for the whole-frame stream. At a
        # body zoom the crop's position on the sensor is unreported by the
        # D750, so the rect would meter an unknown area — meter the whole
        # frame instead of guessing (audit_nef documents the same refusal).
        rect = self._ae_rect if self._body_zoom == ZOOM_ALL else None
        lv_size = self._last_source_size()
        settings = result.settings
        ladder = list(self.info.shutter_choices) if self.info else None
        # Snapshot for the JPEG: the same look the live preview is showing.
        # self.positive is frozen and already carries the NEF-side base
        # percentile (99) + locked WB gains; only the live path overrides it.
        params = self.positive
        jpg = result.path.with_suffix(".jpg")

        def job():
            audit = None
            try:
                audit = audit_nef(result.path, rect, lv_size, settings,
                                  shutter_ladder=ladder)
            except Exception:  # noqa: BLE001 - audit must never eat the capture
                log.exception("audit NEFu %s selhal", result.path.name)
            written: Path | None = None
            try:
                written = render_preview_jpeg(result.path, jpg, params)
            except Exception:  # noqa: BLE001
                log.exception("náhledový JPEG %s selhal", jpg.name)
            return audit, written

        self._start_worker(job, self._on_audit_done)

    def _on_audit_done(self, payload) -> None:
        audit, jpg = payload if payload else (None, None)
        if jpg is not None:
            self._log(f"náhled {Path(jpg).name}")
        if audit is None:
            return
        self._last_audit = audit.message
        self._log(audit.message)
        if audit.verdict == "ok":
            self.statusBar().showMessage(audit.message, 5000)
            return
        # Fix the *next* frame with the shutter — never ISO (archival rule) —
        # then tell the operator this frame must be repeated.
        applied = ""
        if audit.suggested_shutter is not None and self.camera is not None:
            try:
                # set_shutter returns what the body actually took (ladder snap);
                # the message reports that, not what was requested.
                got = self.camera.set_shutter(audit.suggested_shutter)
                self._refresh_settings()
                applied = (" — čas pro další snímek nastaven na "
                           f"{ExposureSettings(shutter=got).shutter_string()}")
            except Exception as exc:  # noqa: BLE001 - the verdict still stands
                applied = f" (čas se nepodařilo nastavit: {exc})"
        self.statusBar().showMessage(
            audit.message + applied + " — tento snímek zopakuj", 10000
        )

    def _auto_exposure(self) -> None:
        if self.camera is None or self._worker is None:
            QMessageBox.information(self, "Auto Exposure", "Musí běžet Live View.")
            return
        controller = AutoExposureController(
            self.camera, self._meter, headroom_ev=DEFAULT_HEADROOM_EV,
            # Archival rule: AE solves with the shutter alone, ISO 100 stays.
            iso_lock=True,
        )
        self._set_actions_busy(True)
        if self._worker is not None:
            # AE keeps grabbing frames after the worker stops — the body must
            # stay in Live View (the SDK answers GetLiveViewImage with -127
            # once the worker's teardown has switched it off).
            self._worker.leave_live_view = True
        self._pause_live_view()
        # On a body with an exposure meter the controller drives that; the red
        # AE rect still matters — _roi_bias below measures how much brighter or
        # darker the framed area is than the whole frame and aims the *body
        # meter* at the ROI's correct exposure, not the frame's (2026-09
        # complaint: the SDK path ignored the rect and metered everything).
        bias = self._roi_bias() if self._has_body_meter() else 0.0
        controller.target_ev = bias
        self.statusBar().showMessage(
            "Auto Exposure: řeším na tělní meter…"
            + (f", AE výřez {bias:+.2f} EV" if bias else "")
        )
        self._start_worker(
            controller.run,
            self._on_auto_exposure_done,
            None if self._has_body_meter() else self._meter_source(),
        )

    def _roi_bias(self) -> float:
        """EV correction the red AE rect implies for the body meter.

        The D750's body meter is global — it cannot be aimed at a rectangle.
        The LV JPEG is auto-brightness (its absolute levels mean nothing), but
        the *ratio* of the framed area's luminance to the whole frame's is
        untouched by the body's uniform display gain, and that ratio is exactly
        how far the framed area sits from what the global meter assumes. A
        rect on dark film inside a frame with bright borders therefore reads a
        positive bias: expose so the *rect* is correct, not the borders.
        Medians (rank statistics survive the JPEG's compression), clamped to
        ±3 EV so a pathological rect cannot command a runaway exposure.
        """
        if self._last_linear is None or self._ae_rect is None:
            return 0.0
        img = self._last_linear
        x0, y0, x1, y1 = self._ae_rect
        h, w = img.shape[:2]
        x0, y0 = max(int(x0), 0), max(int(y0), 0)
        x1, y1 = min(int(x1), w), min(int(y1), h)
        if x1 <= x0 or y1 <= y0:
            return 0.0
        lum = luminance(img)
        whole = float(np.median(lum))
        roi = float(np.median(lum[y0:y1, x0:x1]))
        if whole <= 1e-6 or roi <= 1e-6:
            return 0.0
        return float(np.clip(np.log2(whole / roi), -3.0, 3.0))

    def _has_body_meter(self) -> bool:
        return self.camera is not None and hasattr(self.camera, "exposure_ev")

    def _on_auto_exposure_done(self, result) -> None:
        self._set_actions_busy(False)
        self._resume_live_view()
        self._refresh_settings()
        # The controller converged on the body meter: one press solves the
        # whole move. If it stopped short, it names the concrete ladder rungs
        # and the residual EV — show exactly that, never a canned sentence.
        if not result.converged:
            QMessageBox.warning(
                self,
                "Auto Exposure",
                result.limit_note
                or "Vyčerpaná žebříková nastavení — scéna mimo rozsah fotoaparátu.",
            )
        else:
            self.statusBar().showMessage(
                f"Auto Exposure: {result.settings.shutter_string()} · ISO "
                f"{result.settings.iso} — vyřešeno v {result.iterations} krocích"
                + (f" ({result.limit_note})" if result.limit_note else
                   ", tělní meter na nule")
            )
        self._log(
            f"AE: {result.settings.shutter_string()} ISO {result.settings.iso} "
            f"converged={result.converged}"
            + (f" · {result.limit_note}" if result.limit_note else "")
        )

    # ------------------------------------------------------------------ film

    def _new_film(self) -> None:
        # The rig (body, lens, light, holder, mirroring) carries over from the
        # last film — light settings etc. are deliberately reused; the film's
        # own identity and development log never do.
        dialog = FilmDialog(self, rig_defaults=self._last_film)
        if not dialog.exec():
            return
        film: FilmMetadata = dialog.metadata()
        directory = self._choose_directory()
        if directory is None:
            return
        paths = SessionPaths.create(directory, film.film_id)
        assert self.camera is not None
        self.session = CaptureSession(camera=self.camera, film=film, paths=paths)
        self._last_film = film
        # A new film is a new light + a new emulsion: neither the auto-detected
        # base nor the locked WB gains of the previous one may leak into it.
        self._wb_gains = None
        self.positive = replace(self.positive, base_level=None, wb_gains=None)
        self._fast_preview.reset()
        self.setWindowTitle(f"FilmScan Studio — Capture — {film.label()}")
        self.act_export.setEnabled(True)
        self.statusBar().showMessage(f"Film {film.film_id}: {paths.root}")
        self._log(f"nový film {film.label()} ({film.film_type_class.value})")
        if film.mirrored:
            self._log("snímky označeny jako zrcadlově — zatím pouze v metadatech, "
                      "převracení obrazů zatím neběží (CHANGELOG TODO).")
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
        buttons = (self.btn_capture, self.btn_autoexposure,
                   self.btn_dark, self.btn_flat)
        if busy:
            for button in buttons:
                button.setEnabled(False)
        else:
            # _refresh_buttons is the single source of truth for who may be
            # enabled — Auto Exposure needs only the camera, the captures also
            # need a film. Disabling with a blanket session check here is what
            # left Auto Exposure greyed out after its first (failed) run.
            self._refresh_buttons()

    # -------------------------------------------------------------- indicators

    def _refresh_settings(self) -> None:
        if self.camera is None:
            return
        settings: ExposureSettings = self.camera.get_settings()
        aperture = f"f/{settings.aperture:g}" if settings.aperture else "clona — (na objektivu)"
        self.settings_label.setText(
            f"ISO {settings.iso}   čas {settings.shutter_string()}   {aperture}"
        )
        # Echo-guarded so syncing the widgets never re-applies to the camera.
        self._echo = True
        try:
            index = self.iso_select.findData(settings.iso)
            if index >= 0:
                self.iso_select.setCurrentIndex(index)
            self.shutter_edit.setCurrentText(settings.shutter_string())
        finally:
            self._echo = False

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
        # The archival ISO rule (2026-09): no scan at any sensitivity but the
        # base one. The tooltip states why, so the greyed button is an
        # explanation and not a mystery.
        scans_allowed = has_camera and has_session and (
            self.camera is None or self._capture_block_reason() is None)
        self.btn_capture.setEnabled(scans_allowed)
        self.btn_capture.setToolTip(
            "" if scans_allowed else
            "Archivní sken se exponuje při ISO 100 — stiskni 'ISO 100' "
            "a nastavuj jen čas."
        )
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
