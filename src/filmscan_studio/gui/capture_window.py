"""The Capture window.

Layout: the Live View fills most of the window; every control (histogram,
settings, actions) lives in a narrow column on the right, because the one thing
that must be big while digitising film is the picture. The zoom steps are
labelled in sensor pixels — 100 % is one screen pixel per sensor pixel,
whether the binned overview or a ROI window carries it — and past the point
where the overview stops being honest the
sensor itself reframes: a hardware ROI window of real 1:1 pixels (see
:mod:`filmscan_studio.core.zoom`).

Everything that talks to the camera runs in
:class:`~filmscan_studio.gui.liveview.LiveViewWorker` or a one-shot worker
object, never on the UI thread. Two UI controls break that rule deliberately:
the shutter/gain editors and the cooling spin call the camera directly,
because they are one small write per interaction and a queue would fight
rapid stepping (the Touptek writes are sub-millisecond local SDK calls, not
the USB-round-trip gamble they were on the D750).

The two preview modes are the heart of the design and are enforced here rather
than left to the operator:

* **RAW View** shows the frame with display gamma only, and the histogram is
  computed from linear sensor signal. Exposure judgements happen here.
* **Negativ** (Working Positive) shows inversion + base subtraction + preview
  exposure + filmic, for judging the *picture*. The histogram does not change:
  it keeps describing linear data even in this mode, because a histogram of an
  inverted, tone-curled preview is decorative.

(2026-09: the third „Positive" checkbox is gone — it set invert=False while
still entering this same Working Positive branch, so it rendered a differently
treated picture than the Negativ switch implied and let two mode boxes be
ticked at once. Two mutually exclusive states replaced three tangled ones.)

The D750-era "histogram = NEF prediction via body meter" is gone by design:
the Touptek stream is itself linear, un-tone-curled sensor data, so what the
histogram shows *is* what the capture gets. One honest path replaced two
disagreeing ones.

Nothing in this window can write to a stored archive file: captures go through
:class:`CaptureSession`, which writes the TIFF and sidecars.

One rule governs the threads: **only one thread may touch the camera at a
time**. The worker queue serialises every call, and the Touptek SDK forbids
BINNING/ROI writes from its own callback context — so Live View is paused (or
its stream simply reconfigured through the queue) before a still capture or a
stream frame grab runs.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import replace
from functools import partial
from datetime import datetime
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QSettings, Qt, QTimer, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
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

# LiveMeter / fresh_live_frame meter the Live View stream (film-base sampling
# uses them); the AutoExposureController that also lived here is unhooked from
# the UI (manual-only rig, 2026-09) but stays in the module with its tests.
from filmscan_studio.capture.autoexposure import LiveMeter, fresh_live_frame
from filmscan_studio.capture.camera import CameraBackend, CameraError, CameraInfo
from filmscan_studio.capture.mock import MockCamera
from filmscan_studio.capture.quality import audit_frame, render_preview_jpeg
from filmscan_studio.capture.session import CaptureSession, SessionPaths
from filmscan_studio.capture.touptek import TouptekCamera
from filmscan_studio.core.exposure import (
    ExposureSettings,
    MeterReading,
    parse_shutter,
)
from filmscan_studio.core.filmbase import FilmBaseSample, region_mean
from filmscan_studio.core.filmic import FilmicProfile
from filmscan_studio.core.histogram import compute
from filmscan_studio.core.models import FilmMetadata
from filmscan_studio.core.positive import FastPositivePreview, PositiveParams
from filmscan_studio.core.zoom import (
    FIT,
    OVERVIEW_SCALE,
    ZOOM_LABELS,
    ZOOM_LEVELS,
    SensorSize,
    stream_plan,
)
from filmscan_studio.gui.filmdialog import FilmDialog
from filmscan_studio.gui.imageutil import preview
from filmscan_studio.gui.liveview import LiveViewWorker
from filmscan_studio.gui.widgets import (
    CollapsibleBox,
    FilmicPanel,
    HistogramWidget,
    ZoomView,
)

log = logging.getLogger(__name__)

#: Semaphore half-width, degC: the TEC oscillates around its setpoint; inside
#: this band the darks captured now are valid for scans captured now. The
#: export-time dark matching uses the tighter DARK_TEMPERATURE_TOLERANCE_C —
#: this light is the coarse "is cooling settled at all" signal (INSTRUCTIONS
#: §7: warning, never blocking).
COOLING_SEMAPHORE_TOLERANCE_C = 2.0

#: Shutter presets for the editable combo of a continuous (microsecond)
#: shutter — film digitising lives in whole seconds, unlike the D750 ladder.
#: 2026-09: the short end was extended below 1/10 (bright light / thin
#: negatives want 1/50–1/200 too); the sensor goes to 300 µs, so these are all
#: deliverable. Anything finer stays typeable — the combo is editable.
SHUTTER_PRESETS: tuple[float, ...] = (
    1 / 200, 1 / 100, 1 / 50, 1 / 25, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0,
    15.0, 30.0, 60.0, 120.0, 300.0,
)


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
    submitted here. Serialising here is what keeps the SDK's single-device
    handle single-threaded in practice rather than by convention, and what
    keeps BINNING/ROI reconfigurations out of the SDK's own callback thread.
    Results reach callbacks only through :class:`ResultRelay`, never as direct
    calls from this thread.
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
        #: Stream geometry bookkeeping: what the sensor is currently sending
        #: (binned overview or ROI window) and where the view is centred, in
        #: stream pixels — the raw material for the next ROI request.
        self._stream_binned = True
        self._stream_size = (0, 0)
        self._roi_applied: tuple[int, int, int, int] | None = None
        #: AE metering rectangle in source pixels, from the view's red drag.
        self._ae_rect: tuple[int, int, int, int] | None = None  # x0, y0, x1, y1
        #: Film base / min point rectangle, same source-pixel space, from the
        #: view's blue drag (rect mode "base").
        self._base_rect: tuple[int, int, int, int] | None = None
        #: Echo suppression: refresh writes must not re-trigger apply handlers.
        self._echo = False
        #: Post-capture exposure audit verdict, for the status label.
        self._last_audit: str | None = None
        self._fast_preview = FastPositivePreview(PositiveParams())
        #: Last film record — prefills the rig group of the next dialog.
        self._last_film: FilmMetadata | None = None
        #: Sensor-temperature poll (cooling semaphore).
        self._temp_timer = QTimer(self)
        self._temp_timer.setInterval(2000)
        self._temp_timer.timeout.connect(self._poll_temperature)
        #: Readout modes the operator wants (QSettings-persistent; the
        #: scanner optimum LCG + low noise is the default — see camera_tests
        #: lcg_hcg_snr). Applied at connect and switchable live.
        mode_settings = QSettings("filmscan-studio", "Capture")
        self._want_lcg = mode_settings.value("capture/mode_lcg", True, type=bool)
        self._want_low_noise = mode_settings.value(
            "capture/mode_low_noise", True, type=bool)

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
        # Standalone stream restart: the wedge this escapes used to require
        # killing the app. It sits next to Connect so "proud se zasekl →
        # restartuj tady" needs no discover guesswork.
        self.act_restart = top.addAction("Restartovat proud", self._restart_stream)
        self.act_restart.setEnabled(False)
        self.act_restart.setToolTip(
            "Zavře a znovu otevře kanál kamery, když zasekne proud nebo "
            "zmraze náhled. Film i snímky na disku zůstanou."
        )
        self.act_new_film = top.addAction("Nový film", self._new_film)
        self.act_new_film.setEnabled(False)
        self.act_export = top.addAction("Exportovat projekt", self._export_project)
        self.act_export.setEnabled(False)

        # The picture owns the window: the Live View is the central widget and
        # everything else — histogram, metering, camera controls, actions —
        # lives in one narrow column to its right. Focusing and checking
        # exposure both want the largest possible view of real pixels.
        self.view = ZoomView(sensor=self._sensor_size())
        self.view.aeRectChanged.connect(self._on_ae_rect)
        self.view.baseRectChanged.connect(self._on_base_rect)

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

        # ------------------------------------------------ camera exposure layer
        # Vertical economy: one QFormLayout whose rows are two-control
        # HBoxLayouts — label text lives in tooltips, which are shorter than
        # the widgets they name.
        exposure_box = CollapsibleBox("Expozice fotoaparátu (ovlivňuje focení)",
                                      expanded=True)
        exposure_form = QFormLayout()
        exposure_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        exposure_box.body_layout().addLayout(exposure_form)

        self.shutter_edit = QComboBox()
        self.shutter_edit.setEditable(True)
        self.shutter_edit.setToolTip(
            "Čas — zadej 1/60, 2.5, 30…; kamera má spojitý čas (mikrosekundy), "
            "vezme přesně co napíšeš."
        )
        self.shutter_edit.activated.connect(self._apply_shutter_edit)
        # editingFinished lives on the editable combo's line edit, not the combo.
        self.shutter_edit.lineEdit().editingFinished.connect(self._apply_shutter_edit)
        self.gain_spin = QDoubleSpinBox()
        self.gain_spin.setRange(1.0, 1.0)
        self.gain_spin.setSingleStep(0.1)
        self.gain_spin.setDecimals(2)
        self.gain_spin.setSuffix("×")
        self.gain_spin.setToolTip(
            "Analogový gain (násobič signálu, ne ISO). 1.00× = plný full well "
            "a max. DR — pro archiv platí: expozice patří do času, gain je "
            "identita měření. (Pozor: camera_tests Gain Value je totéž; "
            "kamera ho hlásí v procentech, 100 = 1,00×.)"
        )
        self.gain_spin.editingFinished.connect(self._apply_gain_spin)
        row = QHBoxLayout()
        row.addWidget(self.shutter_edit)
        row.addWidget(self.gain_spin)
        # No field label: in a 360 px column the label would steal width from
        # the two widgets it names, and both say what they are once touched.
        exposure_form.addRow(row)

        # ------------------------------------------------- readout mode switches
        # LCG (max full well/DR — the scanner domain) and low-noise readout
        # are the operator's live switches (user order 2026-09-20: „udělej
        # možnost ty režimy zaškrtnout do sw, implicitně ať jsou on ty
        # optimální"). Both are recorded into every frame's metadata.
        mode_row = QHBoxLayout()
        self.mode_lcg = QCheckBox("LCG")
        self.mode_lcg.setToolTip(
            "Low Conversion Gain: max. full well (51 ke−) a dynamic range "
            "(~14,4 stopu) — pro filmový skener se silným světlem. Vypnuto = "
            "HCG: třetinový full well, minimální read noise (na astro).\n"
            "Přepnutí mění DN stupnici (~2,8×) — po přepnutí přeřeš expozici."
        )
        self.mode_lcg.setChecked(self._want_lcg)
        self.mode_lcg.toggled.connect(self._apply_mode_lcg)
        mode_row.addWidget(self.mode_lcg)
        self.mode_low_noise = QCheckBox("Low noise")
        self.mode_low_noise.setToolTip(
            "Low-noise readout: nižší čtecí šum, ale poloviční kadence proudu "
            "(6,8→3,4 fps). Still snímek se nemění (měřeno 0,996×); jen živý "
            "náhled čte nižší DN (~0,83×). Přepínat lze i za běhu; po "
            "přepnutí přeřeš expozici (histogram náhledu sedí jinak)."
        )
        self.mode_low_noise.setChecked(self._want_low_noise)
        self.mode_low_noise.toggled.connect(self._apply_mode_low_noise)
        mode_row.addWidget(self.mode_low_noise)
        mode_row.addStretch()
        exposure_box.body_layout().addLayout(mode_row)

        # The „Gain 1.00× (archiv)" button and the „Auto Exposure" button are
        # gone (2026-09): the rig runs manual-only now — exposure is solved by
        # the operator from the honest linear histogram, not by a closed loop.
        # The archival gain RULE guarding Capture was removed too (operator
        # order 2026-09-19): the gain spin is a free exposure control now.
        self.ae_hint = QLabel(
            "Měřicí rámeček: drž SHIFT a přetáhni myší — histogram i audit "
            "pak měří jen uvnitř (mimo něj nic nevidí). Při snímku se uloží "
            "do sidecaru jako ořez (přepočten na plné rozlišení). Pravé "
            "tlačítko ho zruší. Vystříhněný rámeček zmizí z pohledu, ale měří "
            "dál — velikost ukáže stavový řádek. Tažení bez Shiftu při zoomu "
            "posouvá, klik = vycentrovat."
        )
        self.ae_hint.setWordWrap(True)
        self.ae_hint.setStyleSheet("color: #9a9; font-size: 11px;")
        exposure_box.body_layout().addWidget(self.ae_hint)
        right_layout.addWidget(exposure_box)

        self.settings_label = QLabel("gain —   čas —")
        self.settings_label.setStyleSheet("font-family: Menlo, monospace; font-size: 13px;")
        self.meter_label = QLabel("—")
        self.meter_label.setStyleSheet("font-family: Menlo, monospace;")
        self.meter_label.setWordWrap(True)
        right_layout.addWidget(self.settings_label)
        right_layout.addWidget(self.meter_label)

        # ------------------------------------------------------------- cooling
        self.cool_box = CollapsibleBox("Chlazení senzoru (TEC)", expanded=False)
        cool_form = QFormLayout()
        self.cool_box.body_layout().addLayout(cool_form)
        self.temp_label = QLabel("— / — °C")
        self.temp_label.setStyleSheet("font-family: Menlo, monospace;")
        cool_form.addRow("Teplota", self.temp_label)
        self.target_spin = QDoubleSpinBox()
        self.target_spin.setRange(-35.0, 20.0)
        self.target_spin.setSingleStep(0.5)
        self.target_spin.setDecimals(1)
        self.target_spin.setSuffix(" °C")
        self.target_spin.setToolTip(
            "Cílová teplota senzoru (TEC, do −35 °C nebo dle rozsahu kamery). "
            "Kontrolka: zelená = u cíle (darky platí), červená = mimo."
        )
        self.target_spin.editingFinished.connect(self._apply_target_temperature)
        cool_form.addRow("Cíl", self.target_spin)
        self.tec_check = QCheckBox("TEC zapnut")
        self.tec_check.setToolTip("Vypnutí nechá senzor ohřát se na okolní teplotu.")
        self.tec_check.toggled.connect(self._apply_tec_enabled)
        cool_form.addRow(self.tec_check)
        self.cool_semaphore = QLabel("●")
        self.cool_semaphore.setStyleSheet("color: #777; font-size: 18px;")
        self.cool_note = QLabel("chlazení nedostupné")
        sem_row = QHBoxLayout()
        sem_row.addWidget(self.cool_semaphore)
        sem_row.addWidget(self.cool_note)
        sem_row.addStretch()
        self.cool_box.body_layout().addLayout(sem_row)
        right_layout.addWidget(self.cool_box)
        self.cool_box.setVisible(False)

        # ------------------------------------------------------- preview layer
        # Two mutually exclusive preview states, one row. The old third box
        # „Positive" is gone (2026-09): it set invert=False yet still rendered
        # the Working Positive branch, so clicking „Negativ" left *both* it and
        # „Positive" ticked — a picture that contradicted its own checkboxes.
        self.preview_box = CollapsibleBox(
            "Náhled — expozice & křivka (NEOVlivňuje focení)", expanded=False
        )
        mode_row = QHBoxLayout()
        self.mode_raw = QCheckBox("RAW View")
        self.mode_raw.setChecked(True)
        self.mode_raw.toggled.connect(lambda on: self._set_mode(raw_view=on))
        mode_row.addWidget(self.mode_raw)
        self.neg_toggle = QCheckBox("Negativ")
        self.neg_toggle.setToolTip(
            "Invertuje a aplikuje křivku na náhled. Uložený snímek se nemění."
        )
        self.neg_toggle.toggled.connect(lambda on: self._set_mode(raw_view=not on))
        mode_row.addWidget(self.neg_toggle)
        mode_row.addStretch()
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
        # Capture + its averaging count share one row (2026-09-19: the
        # operator asked for "vedle capture box, z kolika se averageuje").
        capture_row = QHBoxLayout()
        self.btn_capture = QPushButton("Capture")
        self.btn_capture.setMinimumHeight(48)
        self.btn_capture.clicked.connect(lambda: self._capture(scan=True))
        capture_row.addWidget(self.btn_capture, stretch=1)
        self.average_spin = QSpinBox()
        self.average_spin.setRange(1, 16)
        self.average_spin.setValue(1)
        self.average_spin.setPrefix("×")
        self.average_spin.setToolTip(
            "Průměr z N po sobě jdoucích expozic — šum klesá ~√N, "
            "při K≈8 už ale dotírá na kolísání světla (měřeno, camera_tests). "
            "Ukládá se jediný TIFF = průměr; dílčí snímky se neukládají."
        )
        # Load first, connect second: otherwise constructing the window
        # re-writes the setting on every launch.
        self.average_spin.setValue(QSettings("filmscan-studio", "Capture").value(
            "capture/average_frames", 1, type=int))
        self.average_spin.valueChanged.connect(self._store_average_frames)
        average_col = QVBoxLayout()
        average_col.addWidget(QLabel("Average"))
        average_col.addWidget(self.average_spin)
        capture_row.addLayout(average_col)
        right_layout.addLayout(capture_row)

        # Calibration and numbering happen once per session each, yet used to
        # claim three permanent rows (2026-09: "nevleze se tam vše").
        calib_box = CollapsibleBox("Kalibrace & číslo snímku", expanded=False)
        calib_row = QHBoxLayout()
        self.btn_dark = QPushButton("Dark Frame")
        self.btn_dark.clicked.connect(lambda: self._capture(kind="dark"))
        self.btn_dark.setToolTip(
            "Tma: průměr z 10 expozic za tmy, uložen jako jeden TIFF. "
            "Spinbox Average se ho nedotýká — kalibrace averageuje vždy."
        )
        calib_row.addWidget(self.btn_dark)
        self.btn_flat = QPushButton("Flat Field")
        self.btn_flat.clicked.connect(lambda: self._capture(kind="flat"))
        self.btn_flat.setToolTip(
            "Vyrovnané pole: průměr z 10 expozic, uložen jako jeden TIFF. "
            "Spinbox Average se ho nedotýká — kalibrace averageuje vždy."
        )
        calib_row.addWidget(self.btn_flat)
        calib_box.body_layout().addLayout(calib_row)
        # Film base / min point — not a flat: a flat is shot WITHOUT film and
        # divides out vignetting/dust; this measures the held film's clear
        # base (subtraction floor for the H-D characterisation), usually only
        # a patch of the frame — hence its own blue rect, not the red AE one.
        base_row = QHBoxLayout()
        self.btn_base_mode = QPushButton("Režim min point")
        self.btn_base_mode.setCheckable(True)
        self.btn_base_mode.setToolTip(
            "Přepne tažení SHIFT+mýší na modrý obdélník film base / min point "
            "(červený měřicí rámeček zůstává a dál měří expozici). Modrý rámeček "
            "vezmi na čirou základnu filmu (okraj bez obrazu)."
        )
        self.btn_base_mode.toggled.connect(self._on_base_mode_toggled)
        base_row.addWidget(self.btn_base_mode)
        self.btn_base_stream = QPushButton("Měřit z proudu")
        self.btn_base_stream.setToolTip(
            "Změří průměr DN modrého obdélníku z aktuálního Live View proudu "
            "(žádná nová expozice) a uloží ho i s expozicí proudu (čas, gain, "
            "teplota) do film_base.json — škáluje se na jinak exponované "
            "snímky poměrem."
        )
        self.btn_base_stream.clicked.connect(self._measure_base_from_stream)
        base_row.addWidget(self.btn_base_stream)
        self.btn_base_frame = QPushButton("Snímek base")
        self.btn_base_frame.setToolTip(
            "Vyfotí a archivuje full-size snímek (jako dark/flat, s sidecarem) "
            "a změří na něm modrý obdélník. Pro měření na konkrétní expozici."
        )
        self.btn_base_frame.clicked.connect(self._capture_base_frame)
        base_row.addWidget(self.btn_base_frame)
        calib_box.body_layout().addLayout(base_row)
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
            "Touptek TS2600MP-G2 vyžaduje připojené napájení 11–14 V — bez něj "
            "se přes USB nemusí vůbec objevit. Mock simuluje totéž bez fotoaparátu."
        )
        real = box.addButton("Touptek TS2600MP-G2", QMessageBox.ButtonRole.AcceptRole)
        real.setDefault(True)
        mock = box.addButton("Mock kamera", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        chosen = box.clickedButton()
        if chosen is real:
            self._connecting = True
            self.act_connect.setEnabled(False)
            self.statusBar().showMessage("Hledám Touptek…")
            lcg, ln = self._want_lcg, self._want_low_noise
            self._start_worker(
                lambda: _connect_touptek(default_lcg=lcg,
                                         default_low_noise=ln),
                self._on_connected, on_failed=self._on_connect_failed)
        elif chosen is mock:
            camera = MockCamera()
            camera.connect()
            self._on_camera_connected(camera)

    def _on_connected(self, camera: CameraBackend) -> None:
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
            f"{message}\n\nZkontroluj napájení 11–14 V a USB kabel.",
        )

    def _on_camera_connected(self, camera: CameraBackend) -> None:
        self._connecting = False
        self.statusBar().clearMessage()
        self.camera = camera
        info = camera.info
        self.info = info
        self.act_connect.setEnabled(False)
        self.act_restart.setEnabled(True)
        self.setWindowTitle(f"FilmScan Studio — Capture — {info.model}")
        self.act_new_film.setEnabled(True)
        gain_note = ""
        if info.gain_range:
            gain_note = f" · gain {info.gain_range[0]:g}–{info.gain_range[1]:g}×"
        self._log(f"{info.manufacturer or ''} {info.model}{gain_note} · "
                  f"senzor {info.sensor_width}×{info.sensor_height}")
        caps = camera.capabilities()
        self.cool_box.setVisible(caps.cooling)
        if caps.cooling:
            target = camera.get_target_temperature_c()
            if target is not None:
                self._echo = True
                self.target_spin.setValue(target)
                self._echo = False
            self._temp_timer.start()
            self._poll_temperature()
        self.view.sensor = self._sensor_size()
        self._populate_exposure_editors()
        self._sync_mode_checkboxes()
        self.start_live_view()
        self._refresh_settings()
        self._refresh_buttons()

    def _sensor_size(self) -> SensorSize:
        if self.info and self.info.sensor_width and self.info.sensor_height:
            return SensorSize(self.info.sensor_width, self.info.sensor_height)
        return SensorSize()  # IMX571 default, documented fallback

    # ------------------------------------------------------ camera exposure UI

    def _populate_exposure_editors(self) -> None:
        """Fill the shutter presets and gain range from the camera.

        No signal suppression helper is needed for the shutter combo: its
        handlers run on ``activated``/``editingFinished`` (user actions). The
        gain spin's editingFinished is guarded by ``_echo`` because a refresh
        must not re-apply a value the camera already has.
        """
        assert self.camera is not None and self.info is not None
        self.shutter_edit.clear()
        for seconds in SHUTTER_PRESETS:
            self.shutter_edit.addItem(ExposureSettings(shutter=seconds).shutter_string())
        if self.info.gain_range:
            lo, hi = self.info.gain_range
            self.gain_spin.setRange(lo, hi)
            self.gain_spin.setEnabled(hi > lo)
        else:
            self.gain_spin.setEnabled(False)

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
                f"omezen na rozsah kamery: {ExposureSettings(shutter=applied).shutter_string()}",
                4000,
            )
        self._refresh_settings()

    def _apply_gain_spin(self) -> None:
        if self.camera is None or self._echo:
            return
        try:
            applied = self.camera.set_gain(self.gain_spin.value())
        except CameraError as exc:
            QMessageBox.warning(self, "Gain", str(exc))
            self._refresh_settings()
            return
        if abs(applied - self.gain_spin.value()) > 0.011:
            self.statusBar().showMessage(f"Gain snapnut na {applied:.2f}×", 4000)
        self._refresh_settings()
        self._refresh_buttons()

    # ------------------------------------------------------------ readout modes

    def _sync_mode_checkboxes(self) -> None:
        """Mirror what the camera *reports* (not what we asked) into the UI."""
        if self.camera is None:
            return
        caps = self.camera.capabilities()
        modes = self.camera.get_modes()
        self._echo = True
        try:
            self.mode_lcg.setEnabled(caps.conversion_gain)
            self.mode_low_noise.setEnabled(caps.low_noise)
            if modes.hcg is not None:
                self.mode_lcg.setChecked(not modes.hcg)
            if modes.low_noise is not None:
                self.mode_low_noise.setChecked(modes.low_noise)
        finally:
            self._echo = False

    def _store_mode(self, key: str, value: bool) -> None:
        QSettings("filmscan-studio", "Capture").setValue(key, value)

    def _apply_mode_lcg(self, lcg: bool) -> None:
        self._want_lcg = lcg
        self._store_mode("capture/mode_lcg", lcg)
        if self.camera is None or self._echo:
            return
        try:
            modes = self.camera.set_conversion_gain(hcg=not lcg)
        except CameraError as exc:
            QMessageBox.warning(self, "Konverzní gain", str(exc))
            self._sync_mode_checkboxes()
            return
        self.statusBar().showMessage(
            f"{'LCG' if lcg else 'HCG'} — pozor, DN stupnice se mění "
            "(~2,8×), přeřeš expozici", 6000)
        self._sync_mode_checkboxes()

    def _apply_mode_low_noise(self, low_noise: bool) -> None:
        self._want_low_noise = low_noise
        self._store_mode("capture/mode_low_noise", low_noise)
        if self.camera is None or self._echo:
            return
        try:
            self.camera.set_low_noise(low_noise)
        except CameraError as exc:
            QMessageBox.warning(self, "Low noise", str(exc))
            self._sync_mode_checkboxes()
            return
        self.statusBar().showMessage(
            f"Low noise {'zapnuto' if low_noise else 'vypnuto'} — "
            "kadence proudu a DN stupnice se mění, přeřeš expozici", 6000)
        self._sync_mode_checkboxes()

    def _capture_block_reason(self) -> str | None:
        """Why Capture must refuse right now, or None when it may run.

        The archival gain rule (scan only at 1.00×) was **removed on the
        operator's explicit order 2026-09-19** — gain is an ordinary exposure
        control now (the gain×SNR measurement showed lower gain with a
        longer, exposure-compensated shot is legitimate and quieter). What
        remains is only the physical precondition: a connected camera.
        """
        if self.camera is None:
            return "Fotoaparát není připojen."
        return None

    # ------------------------------------------------------------- cooling UI

    def _poll_temperature(self) -> None:
        """Cheap SDK read at 0.5 Hz: the semaphore must feel live, not laggy."""
        if self.camera is None:
            return
        try:
            current = self.camera.get_temperature_c()
            target = self.camera.get_target_temperature_c()
        except CameraError as exc:
            self._temp_timer.stop()
            self.statusBar().showMessage(f"Teplota: {exc}", 5000)
            return
        if current is None:
            self.temp_label.setText("— / — °C")
            return
        if target is not None:
            self.temp_label.setText(f"{current:+.1f} / {target:+.1f} °C")
            settled = abs(current - target) <= COOLING_SEMAPHORE_TOLERANCE_C
            self.cool_semaphore.setStyleSheet(
                f"color: {'#3c6' if settled else '#e55'}; font-size: 18px;")
            self.cool_note.setText(
                "u cíle — darky platí" if settled
                else f"mimo cíl o {abs(current - target):.1f} °C")
        else:
            self.temp_label.setText(f"{current:+.1f} °C")

    def _apply_target_temperature(self) -> None:
        if self.camera is None or self._echo:
            return
        try:
            applied = self.camera.set_target_temperature_c(self.target_spin.value())
        except CameraError as exc:
            QMessageBox.warning(self, "Cílová teplota", str(exc))
            return
        self._echo = True
        self.target_spin.setValue(applied)
        self._echo = False
        self.statusBar().showMessage(
            f"Cíl {applied:+.1f} °C — stabilizace trvá minuty", 5000)
        self._poll_temperature()

    def _apply_tec_enabled(self, on: bool) -> None:
        if self.camera is None or self._echo:
            return
        try:
            self.camera.set_tec_enabled(on)
        except CameraError as exc:
            QMessageBox.warning(self, "TEC", str(exc))
            self._echo = True
            self.tec_check.setChecked(not on)
            self._echo = False
            return
        self.statusBar().showMessage("TEC zapnut" if on else "TEC vypnut", 3000)

    # -------------------------------------------------------------- zoom wiring

    def _on_zoom_selected(self) -> None:
        self.view.set_zoom(self.zoom_select.currentData())
        self._apply_stream_mode()

    def _apply_stream_mode(self) -> None:
        """Ask the sensor for the stream that serves the selected display zoom.

        Below the overview's honesty limit (one stream px = 3×3 sensor px, so
        up to 3× display) keep streaming the binned whole sensor — switching
        costs a stream restart for pixels the overview already shows honestly.
        Past it, the only honest detail is a 1:1 hardware ROI window centred
        on the current view center. Runs on the UI thread: the stop/reconfigure/
        start cycle is tens of milliseconds and zoom must feel immediate; the
        SDK forbids doing it from the callback, not from here.
        """
        if self.camera is None:
            return
        caps = self.camera.capabilities()
        if not caps.live_view_zoom:
            return
        sensor = self._sensor_size()
        detail = stream_plan(self.view.zoom(), sensor,
                             center_uv=self._center_in_overview())
        want_roi: tuple[int, int, int, int] | None = None
        if detail is not None and detail.roi is not None:
            roi = detail.roi
            want_roi = (roi.x, roi.y, roi.width, roi.height)
        if want_roi == self._roi_applied:
            self._update_zoom_note()
            return
        # The backend's switch is Stop -> reconfigure -> Start; a poller
        # calling next_live_frame across that restart is a race on the
        # stream's lifetime. The poller stops first but leaves the stream up
        # (leave_live_view), so the backend's own restart is the only one.
        if self._worker is not None:
            self._worker.leave_live_view = True
        self._pause_live_view()
        try:
            self.camera.set_live_view_roi(want_roi)
        except CameraError as exc:
            self.statusBar().showMessage(f"Režim proudu se nepovedl: {exc}", 4000)
            self._update_zoom_note()
        self._resume_live_view()
        self._roi_applied = want_roi
        self._stream_binned = want_roi is None
        # The red rect is in *stream* pixels; the new stream delivers different
        # pixels at a different scale, so a kept rect would meter the wrong
        # film area. The blue base rect has the same contract.
        self.view.clear_ae_rect()
        self.view.clear_base_rect()
        self._update_zoom_note()

    def _center_in_overview(self) -> tuple[float, float]:
        """Current view centre expressed in overview px — the aim point
        ``stream_plan`` takes.

        ``center_uv`` divides the widget's centre back to *stream* px: in
        overview mode those already are overview px; inside a ROI window the
        ROI origin must be added to reach sensor coordinates and the binning
        divided out to land back on the overview grid. Panning inside a ROI
        deliberately does NOT re-centre the ROI (that would restart the
        stream under the user's hand); re-selecting the zoom level does.
        """
        cx, cy = self.view.center_uv()
        if not self._stream_binned and self._roi_applied is not None:
            x0, y0, _w, _h = self._roi_applied
            cx = (cx + x0) / OVERVIEW_SCALE
            cy = (cy + y0) / OVERVIEW_SCALE
        return cx, cy

    def _last_source_size(self) -> tuple[int, int]:
        if self._stream_size[0] > 0:
            return self._stream_size
        return (self._sensor_size().width // 3, self._sensor_size().height // 3)

    def _rect_in_sensor_px(self, rect: tuple[int, int, int, int] | None,
                           ) -> tuple[int, int, int, int] | None:
        """One rect (stream px) expressed in full-sensor px.

        Overview: one stream px is exactly the 3×3 bin of its sensor px.
        ROI mode: the stream *is* the ROI window 1:1, so add the ROI origin.
        Both are exact — no guessing, which is what lets the audit meter the
        same film area the red rect covers even over a moved ROI. The base
        rect rides the same conversion: it too is drawn on the stream and
        measured on a full-size frame.
        """
        if rect is None:
            return None
        x0, y0, x1, y1 = rect
        if self._stream_binned:
            sw, sh = self._last_source_size()
            sensor = self._sensor_size()
            sx = sensor.width / max(sw, 1)
            sy = sensor.height / max(sh, 1)
            return (round(x0 * sx), round(y0 * sy),
                    round(x1 * sx), round(y1 * sy))
        rx0, ry0, _, _ = self._roi_applied or (0, 0, 0, 0)
        return (round(x0) + rx0, round(y0) + ry0,
                round(x1) + rx0, round(y1) + ry0)

    def _ae_rect_in_sensor_px(self) -> tuple[int, int, int, int] | None:
        return self._rect_in_sensor_px(self._ae_rect)

    def _base_rect_in_sensor_px(self) -> tuple[int, int, int, int] | None:
        return self._rect_in_sensor_px(self._base_rect)

    def _update_zoom_note(self) -> None:
        w, h = self._last_source_size()
        zoom = self.view.zoom()
        if self._stream_binned:
            sensor = self._sensor_size()
            interp = sensor.width / max(w, 1)   # 3.0: one stream px = 3 sensor px
            note = (f"overview {w}×{h} px, 3×3 binnig — 1 px proudu = 3 px "
                    f"senzoru (lineární průměr, poctivé měření)")
        else:
            x0, y0, rw, rh = self._roi_applied or (0, 0, w, h)
            interp = 1.0
            note = f"ROI {rw}×{rh} px senzoru na [{x0}, {y0}] — 1:1 bez binningu"
        k = self.view.source_zoom()
        note += f" · zobrazení ×{k:g}"
        if zoom != FIT and k > interp + 0.01:
            note += f" (interpolace ×{k / interp:.1f}) — proud dál nedává detail"
        self.zoom_note.setText(note)
        # On the picture itself only when zoomed: Fit is an overview, the
        # overlay would just clutter it.
        self.view.set_detail_note("" if zoom == FIT else note)

    # ----------------------------------------------------------------- AE rect

    def _on_ae_rect(self, rect) -> None:
        if rect is None:
            self._ae_rect = None
            self.statusBar().showMessage("Měřicí rámeček zrušen", 2500)
            return
        self._ae_rect = (rect.x(), rect.y(),
                         rect.x() + rect.width(), rect.y() + rect.height())
        # Report in sensor px — the unit the audit meters in and the only one
        # that means the same thing over overview and ROI. "px proudu" read as
        # a claim about the picture; operators think in the film's frame.
        sensor = self._ae_rect_in_sensor_px()
        w = sensor[2] - sensor[0] if sensor else rect.width()
        h = sensor[3] - sensor[1] if sensor else rect.height()
        self.statusBar().showMessage(
            f"Měřicí rámeček {w}×{h} px senzoru — histogram a audit měří jen "
            "uvnitř; při snímku se uloží do sidecaru jako ořez "
            "(pravé tlačítko zruší)", 6000
        )

    def _on_base_rect(self, rect) -> None:
        if rect is None:
            self._base_rect = None
            self.statusBar().showMessage("Rámeček min point zrušen", 2500)
            return
        self._base_rect = (rect.x(), rect.y(),
                           rect.x() + rect.width(), rect.y() + rect.height())
        sensor = self._base_rect_in_sensor_px()
        w = sensor[2] - sensor[0] if sensor else rect.width()
        h = sensor[3] - sensor[1] if sensor else rect.height()
        self.statusBar().showMessage(
            f"Min point {w}×{h} px senzoru — míří na čirou základnu; "
            "měřit tlačítkem v Kalibraci (pravé tlačítko zruší)", 6000
        )

    # ----------------------------------------------------- film base / min point

    def _on_base_mode_toggled(self, on: bool) -> None:
        """Shift-drag draws the blue base rect or the red AE rect."""
        self.view.set_rect_mode("base" if on else "ae")
        self.statusBar().showMessage(
            "Tažení kreslí modrý rámeček film base / min point"
            if on else "Tažení kreslí červený měřicí rámeček", 4000)

    def _base_rect_missing_note(self) -> str | None:
        """Why a base measurement must refuse, or None when it may run."""
        if self.session is None:
            return "Nejdřív vytvoř film — měření se ukládá do projektu."
        if self._base_rect is None:
            return ("Zaškrtni 'Režim min point' a tažením SHIFT+mýš vymeď "
                    "modrý obdélník na čirou základnu filmu.")
        return None

    def _measure_base_from_stream(self) -> None:
        """Mean DN of the blue rect on a fresh Live View frame — no new exposure.

        The frame's own stamp (``expotime_us``) is the exposure the reading
        belongs to; settings and sensor temperature ride along so the stored
        sample can be shutter-scaled onto the frames, which may well be
        exposed differently. Metering needs a frame grab, so the poller yields
        the camera to the worker thread (one thread on the camera at a time).
        """
        if self.camera is None:
            return
        note = self._base_rect_missing_note()
        if note is not None:
            self.statusBar().showMessage(note, 6000)
            return
        rect = self._base_rect      # stream px — the stream's own grid, 1:1
        meter = self._meter

        def job():
            frame = fresh_live_frame(self.camera)
            data = LiveMeter.decode_live_frame(frame)
            black, white = meter.frame_levels(frame)
            mean = region_mean(data, rect)
            settings = self.camera.get_settings()
            temperature = None
            try:
                temperature = self.camera.get_temperature_c()
            except Exception:  # noqa: BLE001 - temperature is provenance, not fatal
                pass
            shutter = settings.shutter
            reported = getattr(frame, "expotime_us", None)
            if reported:
                shutter = reported / 1e6     # what this frame was really shot at
            return FilmBaseSample(
                kind="stream", mean_dn=mean,
                black_level=black, white_level=white,
                shutter=shutter, gain=settings.gain,
                sensor_temperature_c=temperature,
                rect=self._base_rect_in_sensor_px(),
                source="stream",
                captured_at=datetime.now().astimezone(),
            )

        self._set_actions_busy(True)
        if self._worker is not None:
            self._worker.leave_live_view = True
        self._pause_live_view()
        self.statusBar().showMessage("Měřím film base z proudu…")
        self._start_worker(job, self._on_base_measured)

    def _capture_base_frame(self) -> None:
        """Real exposure, archived like a dark/flat; the rect is measured on
        the full-size frame (sensor px — the frame's own grid)."""
        note = self._base_rect_missing_note()
        if note is not None:
            self.statusBar().showMessage(note, 6000)
            return
        self._capture(kind="base")

    def _on_base_measured(self, sample: FilmBaseSample) -> None:
        self._set_actions_busy(False)
        self._resume_live_view()
        path = self.session.record_base_sample(sample)
        shutter = ExposureSettings(shutter=sample.shutter, iso=None,
                                   gain=sample.gain)
        self._log(f"film base {sample.mean_dn:.1f} DN @ "
                  f"{shutter.shutter_string()} · {sample.source} "
                  f"→ {path.name}")
        self.statusBar().showMessage(
            f"Film base: {sample.mean_dn:.1f} DN (black {sample.black_level:.0f}, "
            f"čas {shutter.shutter_string()}) — uloženo do film_base.json", 8000)
        self._refresh_stage()

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
        h, w = image.shape[:2]
        self._stream_size = (w, h)
        # One stream px stands for this many sensor px: 3 binned, 1 in ROI —
        # what the view needs to keep its zoom labels honest. The measured
        # binned ratio (6224 / 2074 ≈ 3.00) beats the nominal one because the
        # SDK rounds the binned size to even pixels.
        self.view.set_source_scale(
            self._sensor_size().width / w if self._stream_binned and w else 1.0)
        self._last_reading = self._update_histogram(image, reading)
        self._update_meter_label(self._last_reading)
        self._last_linear = image
        self.view.set_image(self._display_for(image))
        # Recomputed per frame, not just on the zoom click: the honest note
        # (and the overlay on the picture) quotes the *delivered* stream —
        # right after a mode switch the click-time numbers described the old
        # one, and a stale "×12" over a fresh 1:1 ROI is exactly the kind of
        # lie this widget exists to prevent.
        self._update_zoom_note()

    def _display_for(self, image: np.ndarray) -> np.ndarray:
        """Preview transform for one Live View frame.

        The frame arrives normalised 0..1 linear (the worker divided by the
        frame's own white level), so both preview modes get black/white 0/1 —
        the same domains the developer works in, one divide apart.
        """
        if self.raw_view:
            # to_raw_view applies 1/2.2 display gamma to linear input.
            return preview(image, self.positive, raw_view=True,
                           black_level=0.0, white_level=1.0)
        # The archive has a real pedestal-free 0..1 range, so the raw's base
        # percentile (99) transfers to the stream without the D750's
        # measured-JPEG workaround.
        self._fast_preview.set_params(self.positive)
        return self._fast_preview.render(image)

    def _on_live_error(self, message: str) -> None:
        # Live View dying (USB hiccup, camera unplugged) must be visible but
        # must not close the window with captured frames on disk.
        self.statusBar().showMessage(f"Live View: {message}")
        self._log(f"Live View chyba: {message}")
        if self._worker is not None:
            self._worker.stop()
            self._worker = None
        # Self-help restart (2026-09): the operator's workaround for a wedged
        # stream was to kill the app and relaunch. Offer the same recovery in
        # one click — close the handle and reopen it — instead of leaving a
        # dead preview with no way back short of a restart.
        box = QMessageBox(self)
        box.setWindowTitle("Live View se zastavilo")
        box.setText(f"Proud kamery přestal: {message}")
        box.setInformativeText(
            "Zkusit kameru znovu spustit (zavřít a otevřít)? Rozpracovaný film "
            "i snímky na disku zůstanou."
        )
        retry = box.addButton("Restartovat proud", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Zavřít", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is retry:
            self._restart_stream()

    def _restart_stream(self) -> None:
        """Close the camera handle and reopen a fresh one, keeping the film.

        A stream wedged deep in the SDK (a full-sensor readout that never
        settles) is not recoverable by another ``put_*`` on the same handle —
        the handle has to be closed and reopened. Both the close and the reopen
        run on the camera worker thread: a handle wedged in the SDK can block
        ``Close()`` indefinitely, and doing that on the UI thread is exactly the
        freeze this button exists to escape. On success the open session is
        re-pointed at the new handle so the digitised frames and numbering
        continue; a MockCamera just relaunches its stream.
        """
        if self._connecting:
            return
        old = self.camera
        if isinstance(old, MockCamera):
            self._pause_live_view()
            self.start_live_view()
            self.statusBar().showMessage("Mock proud restartován", 3000)
            return
        self._pause_live_view()
        self._temp_timer.stop()
        self._connecting = True
        self.act_connect.setEnabled(False)
        self.act_restart.setEnabled(False)
        self.statusBar().showMessage("Restartuji proud kamery…")

        def job() -> TouptekCamera:
            # Runs on the worker thread, so a wedged Close() stalls only this
            # job — the window keeps painting and closing.
            if old is not None:
                try:
                    old.disconnect()
                except Exception:  # noqa: BLE001 - best effort on a wedged handle
                    log.exception("disconnect při obnově selhal")
            camera = TouptekCamera(default_lcg=self._want_lcg,
                                   default_low_noise=self._want_low_noise)
            camera.connect()
            return camera

        self._start_worker(job, self._on_stream_restarted,
                           on_failed=self._on_connect_failed)

    def _on_stream_restarted(self, camera: CameraBackend) -> None:
        if self.session is not None:
            # The session captured through the (now closed) handle; re-point it
            # so the film continues on the fresh one. Catalog and files on disk
            # are untouched — only the live camera reference changes.
            self.session.camera = camera
        self._on_camera_connected(camera)
        self.statusBar().showMessage("Proud obnoven", 4000)

    def _update_histogram(self, image: np.ndarray,
                          reading: MeterReading) -> MeterReading:
        # Linear domain always -- the brief is explicit and this is not a
        # toggle. Same source as Auto Exposure: when the red rect is set, the
        # histogram describes exactly the area AE meters, or the two tools
        # disagree and the operator starts trusting the wrong one. There is
        # no prediction layer any more: the stream's linear data *is* the
        # capture's radiometry, so frame and file agree by construction.
        region = image
        if self._ae_rect is not None:
            x0, y0, x1, y1 = self._ae_rect
            h, w = image.shape[:2]
            x0, y0 = max(int(x0), 0), max(int(y0), 0)
            x1, y1 = min(int(x1), w), min(int(y1), h)
            if x1 > x0 and y1 > y0:      # stale rect after a stream change
                region = image[y0:y1, x0:x1]
        hist = compute(region, black_level=0.0, white_level=1.0)
        if self._ae_rect is not None:
            from filmscan_studio.core.exposure import measure
            reading = measure(region, 0.0, 1.0, self._meter.percentile)
        self.histogram.set_histogram(hist)
        # The clip report the brief asks for: both rails, as a share of pixels,
        # each in the colour of the bar it counts — red bar (blown) and blue
        # bar (crushed) are drawn on the histogram itself; this is their
        # numeric counterpart. Rich text is what paints the numbers in those
        # colours instead of just naming them.
        self.clip_label.setText(
            f"clip: <span style='color:#e55'>bílá "
            f"{hist.clipped_high_fraction:.3%}</span> · "
            f"<span style='color:#58f'>černá "
            f"{hist.clipped_low_fraction:.3%}</span>"
        )
        scope = "měřicí výřez" if self._ae_rect is not None else "celý snímek"
        source = f"lineární data proudu · {scope}"
        if self.raw_view:
            self.histogram.set_curve(None)
            self.histogram_label.setText(f"histogram: {source} (RAW View)")
        else:
            self.histogram.set_curve(self.positive.profile.curve_table(256))
            self.histogram_label.setText(
                f"histogram: {source} — křivka je jen přiložený model"
            )
        return reading

    def _update_meter_label(self, reading: MeterReading) -> None:
        util = reading.highlight_utilisation
        # Both rails get a flag in their own colour: red blown base, blue
        # crushed density (the underexposure mirror the 2026-09 request asked
        # for — "když je nula, svítit modře").
        flags = ""
        if reading.clipped:
            flags += "  · <span style='color:#e55'>PŘEPAL</span>"
        if reading.crushed:
            flags += "  · <span style='color:#58f'>PODEXP</span>"
        self.meter_label.setText(
            f"p99.9 {util:6.1%} plného rozsahu{flags}"
        )

    # ------------------------------------------------------------------ modes

    def _set_mode(self, raw_view: bool) -> None:
        self.raw_view = raw_view
        # Mutually exclusive: exactly one box is ever ticked. setChecked is
        # signal-blocked, so the sibling's toggled handler never bounces a
        # second _set_mode call back (the old row's echo maze — and, with the
        # removed Positive box gone, the source of its double tick).
        for box, state in ((self.mode_raw, raw_view),
                           (self.neg_toggle, not raw_view)):
            box.blockSignals(True)
            box.setChecked(state)
            box.blockSignals(False)
        # The panel's invert checkbox must say what the picture is doing. It
        # defaulted to checked and never followed the mode, so a RAW View —
        # which ignores invert and paints un-inverted — sat under a ticked
        # "Invertovat (negativ)": the "RAW View + Negative najedou" complaint
        # (2026-09). Syncing it here keeps one truth per mode, and a RAW View
        # frame keeps its own per-channel DMax even though invert is now off.
        self.filmic.invert.blockSignals(True)
        self.filmic.invert.setChecked(not raw_view)
        self.filmic.invert.blockSignals(False)
        if self.positive.invert != (not raw_view):
            self.positive = replace(self.positive, invert=not raw_view)
            self._fast_preview.set_params(self.positive)
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
        reason = self._capture_block_reason()
        if reason is not None:
            self.statusBar().showMessage(reason, 6000)
            return
        if scan:
            number = self.frame_number.value() if self.frame_number.value() > 0 else None
            # The red frame doubles as the crop cue: stored in sensor px —
            # the full-size frame's own grid, never the 3×3-binned stream —
            # so the developer GUI can crop by it directly. No rect, no crop.
            crop = self._ae_rect_in_sensor_px()
            average = self.average_spin.value()
            label = "Snímek"
            fn, args = self.session.capture_scan, (
                number, crop, average, self._capture_progress_cb(label)
            )
        elif kind == "base":
            # The frame is full-size, so the rect must arrive in sensor px —
            # the same conversion the audit uses. Calibration averaging is
            # implicit (session default CALIBRATION_AVERAGE); the operator's
            # spinbox governs scans only.
            label = "Film base"
            fn = partial(self.session.capture_base,
                         progress=self._capture_progress_cb(label))
            args = (self._base_rect_in_sensor_px(),)
        else:
            label = "Dark" if kind == "dark" else "Flat"
            fn = partial((self.session.capture_dark if kind == "dark"
                          else self.session.capture_flat),
                         progress=self._capture_progress_cb(label))
            args = ()
        self._set_actions_busy(True)
        # The capture reconfigures the stream to full sensor internally; the
        # poller must not be pulling frames while it does (and on a long
        # exposure there would be no frames to pull anyway).
        self._pause_live_view()
        self.statusBar().showMessage(f"{label}: expozice…")
        self._start_worker(fn, lambda res: self._on_captured(label, res), *args)

    def _capture_progress_cb(self, label: str):
        """Backend progress -> status bar, via the relay (worker thread!).

        The averaging loop calls this from the camera thread after every
        pulled exposure; touching the status bar there directly is exactly
        what the ResultRelay exists to prevent (macOS aborts, see its
        docstring), so every tick rides the relay home to the UI thread.
        """
        def progress(k: int, n: int) -> None:
            if n > 1:
                self._relay.send(
                    lambda _unused: self.statusBar().showMessage(
                        f"{label}: average {k}/{n}…"), None)
        return progress

    def _store_average_frames(self, value: int) -> None:
        """Persist the averaging count; it is an operator habit, not a film
        property, so it lives in QSettings rather than the project."""
        QSettings("filmscan-studio", "Capture").setValue(
            "capture/average_frames", value)

    def _on_captured(self, label: str, results) -> None:
        self._set_actions_busy(False)
        self._resume_live_view()
        first = results[0] if isinstance(results, list) else results
        # capture_base returns (result, sample) — the capture result is the
        # first half; the sample's own numbers get their own log line below.
        sample = None
        if label == "Film base" and isinstance(first, tuple):
            first, sample = first
        self.statusBar().showMessage(
            f"{label} uložen: {first.path.name} ({first.size_bytes / 1e6:.1f} MB, {first.elapsed:.1f} s)"
        )
        temp = (f" · {first.sensor_temperature_c:+.1f} °C"
                if first.sensor_temperature_c is not None else "")
        self._log(f"{label}: {first.path.name} · {first.settings.shutter_string()} "
                  f"· {first.settings.sensitivity_string()}{temp}")
        if sample is not None:
            self._log(f"  min point {sample.mean_dn:.1f} DN v rámečku "
                      f"{sample.rect} → film_base.json")
        for note in first.notes:
            self._log(f"  pozn.: {note}")
        self._refresh_settings()
        self._refresh_stage()
        self._poll_temperature()
        self.act_export.setEnabled(True)
        if label == "Snímek" and first.path.suffix.lower() == ".tif":
            self._post_capture_check(first)

    # --------------------------------------------------- post-capture pipeline

    def _post_capture_check(self, result) -> None:
        """Audit the fresh archive frame and render its positive JPEG,
        off-thread.

        The captured TIFF is the honest metering source even though the stream
        is linear too — it is the actual pixels at full resolution, including
        whatever the binned overview averaged away. The audit says under/over,
        the shutter is corrected for the next frame, and the operator is told
        to repeat this one — an exposed frame cannot be rescued. File IO only;
        the camera is never touched, so the queue slot frees for the next shot.
        """
        if self.session is None:
            return
        # audit_frame maps its rect by a plain scale from the stream size it
        # is given — true for the binned overview, wrong for a moved ROI
        # (whose stream px carry an origin offset it cannot know). So the GUI
        # converts the rect to *sensor* px here, where both stream modes map
        # identically, and hands the audit the sensor size: the scale becomes
        # 1:1 and the ROI's offset is folded in.
        rect = self._ae_rect_in_sensor_px()
        sensor = self._sensor_size()
        lv_size = (sensor.width, sensor.height)
        settings = result.settings
        # Snapshot for the JPEG: the same look the live preview is showing.
        params = self.positive
        jpg = result.path.with_suffix(".jpg")

        def job():
            audit = None
            try:
                audit = audit_frame(result.path, rect, lv_size, settings)
            except Exception:  # noqa: BLE001 - audit must never eat the capture
                log.exception("audit %s selhal", result.path.name)
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
        # Fix the *next* frame with the shutter — never gain (archival rule) —
        # then tell the operator this frame must be repeated. Continuous
        # shutter: with no ladder the audit suggests nothing, so translate the
        # EV verdict into a shutter here.
        applied = ""
        if self.camera is not None:
            try:
                current = self.camera.get_settings()
                suggested = current.shutter * 2.0 ** audit.ev_change
                got = self.camera.set_shutter(suggested)
                self._refresh_settings()
                applied = (" — čas pro další snímek nastaven na "
                           f"{ExposureSettings(shutter=got).shutter_string()}")
                if abs(got - suggested) / max(suggested, 1e-9) > 0.02:
                    # The camera clamped: it could not deliver what the verdict
                    # asked for. Say so — the silent difference used to read as
                    # "applied" while the scene stayed out of range.
                    asked = ExposureSettings(shutter=suggested).shutter_string()
                    applied += (f" (scéna chtěla {asked}, kamera umí max "
                                f"{ExposureSettings(shutter=got).shutter_string()})")
            except Exception as exc:  # noqa: BLE001 - the verdict still stands
                applied = f" (čas se nepodařilo nastavit: {exc})"
        self.statusBar().showMessage(
            audit.message + applied + " — tento snímek zopakuj", 10000
        )

    # Auto Exposure's GUI wrapper is gone (2026-09): the rig is operated
    # manually and the operator solves exposure from the honest linear
    # histogram. The controller itself (capture/autoexposure.py) stays in the
    # codebase with its tests — LiveMeter and fresh_live_frame, which the Live
    # View worker and the film-base stream measurement both use, live there.

    # ------------------------------------------------------------------ film

    def _new_film(self) -> None:
        # The rig (camera, lens, light, holder) carries over from the
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
        # base nor the WB of the previous one may leak into it.
        self.positive = replace(self.positive, base_level=None)
        self._fast_preview.reset()
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
        path, unmatched = self.session.export_project()
        self.statusBar().showMessage(f"Projekt exportován: {path}")
        self._log(f"export {path.name}")
        if unmatched:
            # The cooling rule (INSTRUCTIONS §7): a scan whose darks sit a
            # different temperature away cannot be dark-subtracted honestly.
            # The export still happened — this is the loud, listed warning.
            QMessageBox.warning(
                self, "Chybí teplotně sedící dark",
                "Tyto skeny nemají dark pořízený ve stejné teplotě senzoru "
                f"(±0,5 °C):\n  snímky {', '.join(str(n) for n in unmatched)}\n\n"
                "Dark subtraction na chlazeném senzoru platí jen v úzkém "
                "teplotním okně — změřené darky k nim nepatří. Pořiď dark "
                "při stejné teplotě a export opakuj, nebo počítej s "
                "šumovým/zbytkovým gradientem.",
            )

    # ------------------------------------------------------------- one-shot IO

    def _start_worker(self, fn, on_done, *args, on_failed=None) -> None:
        """Queue ``fn(*args)`` on the camera thread; ``on_done(result)`` on the UI thread.

        One persistent worker thread, not a thread per action: the camera is
        single-session by nature, so every camera call must happen on the same
        thread it connected on, and a per-call QThread had to be kept alive
        against the garbage collector between ``start()`` and its first signal
        (which it was not, and the failure was a silently dropped capture).
        """
        self._camera_queue.submit(
            fn, on_done, on_failed or self._on_worker_failed, *args
        )

    def _on_worker_failed(self, message: str) -> None:
        # A failed AE/capture run must hand the buttons back and restart the
        # poller: the status bar alone leaves the window looking wedged.
        self._set_actions_busy(False)
        self._resume_live_view()
        self.statusBar().showMessage(f"Chyba: {message}")
        self._log(f"CHYBA: {message}")

    def _set_actions_busy(self, busy: bool) -> None:
        buttons = (self.btn_capture,
                   self.btn_dark, self.btn_flat,
                   self.btn_base_stream, self.btn_base_frame)
        if busy:
            for button in buttons:
                button.setEnabled(False)
        else:
            # _refresh_buttons is the single source of truth for who may be
            # enabled: captures need a camera and a film.
            self._refresh_buttons()

    # -------------------------------------------------------------- indicators

    def _refresh_settings(self) -> None:
        if self.camera is None:
            return
        settings: ExposureSettings = self.camera.get_settings()
        self.settings_label.setText(
            f"{settings.sensitivity_string()}   čas {settings.shutter_string()}"
        )
        # Echo-guarded so syncing the widgets never re-applies to the camera.
        self._echo = True
        try:
            if settings.gain is not None:
                self.gain_spin.setValue(settings.gain)
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
            f"base {state.base_count} · snímků {state.scan_count} · "
            f"další #{state.next_frame_number}"
        )
        self.frame_number.setValue(0)
        self.frame_number.setMinimum(0)

    def _refresh_buttons(self) -> None:
        has_camera = self.camera is not None
        has_session = self.session is not None
        for button in (self.btn_dark, self.btn_flat):
            button.setEnabled(has_camera and has_session)
        # The base frame is a real exposure under the film, so it obeys the
        # same session requirement as dark/flat. The stream reading needs no
        # session for the camera work but its sample must be stored somewhere
        # — same gate, and the blue drag toggle needs only the view.
        self.btn_base_frame.setEnabled(has_camera and has_session)
        self.btn_base_stream.setEnabled(has_camera and has_session)
        self.btn_base_mode.setEnabled(has_camera)
        # The archival gain rule is GONE (operator order 2026-09-19): gain is
        # a free exposure control, Capture no longer interrogates it.
        self.btn_capture.setEnabled(has_camera and has_session)
        self.btn_capture.setToolTip("")

    def _log(self, line: str) -> None:
        current = self.log_view.text()
        self.log_view.setText(f"{line}\n{current}".strip()[:2000])

    # ------------------------------------------------------------------ teardown

    def closeEvent(self, event) -> None:  # noqa: N102 - Qt naming
        self._temp_timer.stop()
        if self._worker is not None:
            self._worker.stop()
        self._camera_queue.stop()
        if self.camera is not None:
            try:
                self.camera.disconnect()
            except Exception:  # noqa: BLE001
                log.exception("camera disconnect failed")
        super().closeEvent(event)


def _connect_touptek(default_lcg: bool = True,
                     default_low_noise: bool = True) -> TouptekCamera:
    """Enumerate and open on a worker thread; USB discovery can take seconds.

    The mode defaults ride in as plain bools (read from QSettings on the UI
    thread) — apply_default_modes puts the sensor into LCG + low noise at
    connect unless the operator unchecked them.
    """
    camera = TouptekCamera(default_lcg=default_lcg,
                           default_low_noise=default_low_noise)
    camera.connect()
    return camera
