"""Preview and histogram widgets.

The zoom widget exists because focusing a film scan is 100% pixel-peeping: the
operator must see film grain to judge focus. Its scale is expressed in *sensor*
pixels (see :mod:`filmscan_studio.core.zoom`): "100%" means one screen pixel
per pixel of the 6016x4016 NEF, and the widget states honestly how many sensor
pixels one delivered Live View pixel actually represents, because the app
cannot invent detail the USB stream does not carry.

The view also serves as the Auto Exposure framing tool: a Shift-drag draws the
thin red rectangle AE meters inside of, so a bright sky bordering the film
cannot drag the exposure down (or the frame's white border up). Plain drag when
zoomed in pans the picture instead — focusing wants the point of interest
wherever it sits, not just the centre — and a plain click re-centres.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QPoint, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from filmscan_studio.core.histogram import Histogram
from filmscan_studio.core.zoom import FIT, ZOOM_LEVELS, SensorSize
from filmscan_studio.gui.imageutil import to_qimage

_CLIP_PEN = QPen(QColor(255, 80, 80))
_GRID_PEN = QPen(QColor(110, 110, 110))
_CURVE_PEN = QPen(QColor(235, 235, 235))
_BG = QColor(28, 28, 30)
_AE_PEN = QPen(QColor(255, 40, 40))
_AE_PEN.setWidth(1)
#: A drag shorter than this is a re-centre click, not an AE rectangle.
_DRAG_THRESHOLD_PX = 8


class ZoomView(QWidget):
    """Pixel-exact scaled view with click-to-centre and AE-rectangle drag.

    Coordinates come in three units and the widget's job is to keep them
    straight: *screen* pixels (this widget), *source* pixels (the delivered
    Live View frame), and *sensor* pixels (the NEF grid zoom is labelled in).
    ``source_scale`` is sensor px per source px — 6016/640 ≈ 9.4 for a
    whole-frame stream, near 1 when the body sends a zoomed crop — and the
    crop math runs through it so a zoom label never lies about being 1:1 of
    the *sensor*.

    Scaling is done at paint time, from the full-resolution float buffer.
    Scaling a pre-sized pixmap instead would either resample a crop (and lie
    about what 100% means) or resample the whole frame (and blur the grain the
    zoom exists to show). Nearest-neighbour is deliberate: at 200%+ an
    interpolated grain picture hides focus error.
    """

    zoomChanged = Signal(float)
    #: Pixel coordinates the view is centred on, for status display.
    centerChanged = Signal(int, int)
    #: AE rectangle selected / cleared, in SOURCE pixel coordinates.
    aeRectChanged = Signal(object)   # QRect or None

    def __init__(self, parent: QWidget | None = None,
                 sensor: SensorSize | None = None) -> None:
        super().__init__(parent)
        self._image: np.ndarray | None = None
        self._zoom = FIT
        self._center = QPoint(0, 0)          # sensor coordinates
        self._source_scale = 1.0             # sensor px per source px
        self._has_image = False
        self.sensor = sensor or SensorSize()
        self._ae_rect: QRect | None = None   # source coordinates
        self._drag_origin: QPointF | None = None
        self._drag_current: QPointF | None = None
        self._drag_shift = False             # this gesture drags AE, not pans
        self._drag_pan_accum = QPointF(0, 0)
        self._detail_note = ""
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(320, 240)
        self.setMouseTracking(True)

    # ------------------------------------------------------------------ state

    def set_image(self, image: np.ndarray) -> None:
        """Store the full source-resolution float image, keep the visible crop.

        The crop is anchored on the sensor-pixel centre, so switching the body
        to a zoomed stream (which replaces the pixels but not the framed
        region) keeps the point being focused on roughly where it was. The
        D750 does not report *where* its LV crop sits (pan caps unverified), so
        "roughly" is honest here — TODO once a probe measures the pan.
        """
        self._image = np.ascontiguousarray(image)
        if not self._has_image:
            self._center = QPoint(
                int(self._image.shape[1] * self._source_scale / 2),
                int(self._image.shape[0] * self._source_scale / 2),
            )
        self._has_image = True
        self.update()

    def clear_image(self) -> None:
        self._image = None
        self._has_image = False
        self.update()

    @property
    def has_image(self) -> bool:
        return self._has_image

    def set_source_scale(self, sensor_px_per_source_px: float) -> None:
        if sensor_px_per_source_px <= 0:
            raise ValueError("source scale must be positive")
        self._source_scale = float(sensor_px_per_source_px)
        self.update()

    def source_scale(self) -> float:
        return self._source_scale

    def set_detail_note(self, note: str) -> None:
        """Status of the delivered stream, painted in the corner (never empty
        when zoomed — this is the widget's honesty channel)."""
        self._detail_note = note
        self.update()

    def zoom(self) -> float:
        return self._zoom

    def set_zoom(self, zoom: float) -> None:
        if zoom not in ZOOM_LEVELS:
            raise ValueError(f"unsupported zoom {zoom}")
        if zoom == self._zoom:
            return
        self._zoom = zoom
        self._clamp_center()
        self.zoomChanged.emit(zoom)
        self.update()

    # ---------------------------------------------------------------- AE rect

    def ae_rect(self) -> QRect | None:
        return QRect(self._ae_rect) if self._ae_rect is not None else None

    def clear_ae_rect(self) -> None:
        if self._ae_rect is None:
            return
        self._ae_rect = None
        self.aeRectChanged.emit(None)
        self.update()

    # ------------------------------------------------------------------ maths

    def _display_scale(self) -> float:
        """Screen pixels per sensor pixel for the current mode."""
        if self._zoom != FIT:
            return self._zoom
        return self.sensor.fit_scale(self.width(), self.height())

    def _crop_rect(self) -> tuple[QRectF, QRectF]:
        """(crop in source px, destination on screen) for the current state.

        Fractional maths on purpose: rounding the crop *size* down and then
        drawing it at an integer destination left empty margins (the 2026-09
        complaint: "jen od levého horního rohu, neplní okno"). Now the crop is
        exactly the widget at the display scale, clamped to the frame at its
        edges, and the destination covers the full crop — sub-pixel accuracy
        comes from drawImage's QRectF overload.
        """
        if self._image is None:
            return QRectF(), QRectF()
        h, w = float(self._image.shape[1]), float(self._image.shape[0])
        if self._zoom == FIT:
            # Fit means "see the whole frame": the entire stream, whatever the
            # body's crop — a body-side zoom at Fit would hide most of it.
            scale = min(self.width() / (w * self._source_scale),
                        self.height() / (h * self._source_scale))
            dw, dh = w * self._source_scale * scale, h * self._source_scale * scale
            return (QRectF(0, 0, w, h),
                    QRectF((self.width() - dw) / 2, (self.height() - dh) / 2, dw, dh))
        self._clamp_center()          # keeps the crop inside the frame
        # screen px -> source px: divide by (screen/sensor) * (sensor/source)
        per_source = max(self._display_scale() * self._source_scale, 1e-9)
        crop_w = min(self.width() / per_source, w)
        crop_h = min(self.height() / per_source, h)
        x0 = np.clip(self._center.x() / self._source_scale - crop_w / 2, 0, w - crop_w)
        y0 = np.clip(self._center.y() / self._source_scale - crop_h / 2, 0, h - crop_h)
        crop = QRectF(float(x0), float(y0), crop_w, crop_h)
        # A strict-subset crop fills the widget exactly, so this centring is a
        # no-op there; it only kicks in when the *whole* frame is smaller than
        # the viewport (low zoom) — letterbox it instead of parking it in the
        # top-left corner, which read as a broken scale.
        dw, dh = crop_w * per_source, crop_h * per_source
        dest = QRectF((self.width() - dw) / 2, (self.height() - dh) / 2, dw, dh)
        return crop, dest

    def _clamp_center(self) -> None:
        if self._image is None:
            return
        h, w = float(self._image.shape[1]), float(self._image.shape[0])
        per_source = max(self._display_scale() * self._source_scale, 1e-9)
        half_w = self.width() / per_source / 2
        half_h = self.height() / per_source / 2
        cx = self._center.x() / self._source_scale
        cy = self._center.y() / self._source_scale
        cx = float(np.clip(cx, min(half_w, w / 2), max(w - half_w, w / 2)))
        cy = float(np.clip(cy, min(half_h, h / 2), max(h - half_h, h / 2)))
        self._center = QPoint(int(round(cx * self._source_scale)),
                              int(round(cy * self._source_scale)))

    def _screen_to_source(self, pos: QPointF | QPoint) -> QPoint:
        """Widget position -> source pixel (for the current crop)."""
        crop, dest = self._crop_rect()
        if crop.isEmpty() or dest.isEmpty():
            return QPoint(0, 0)
        x = crop.x() + (pos.x() - dest.x()) * crop.width() / max(dest.width(), 1e-9)
        y = crop.y() + (pos.y() - dest.y()) * crop.height() / max(dest.height(), 1e-9)
        return QPoint(int(np.clip(x, 0, self._image.shape[1] - 1)),
                      int(np.clip(y, 0, self._image.shape[0] - 1)))

    def _source_to_screen(self, p: QPoint) -> QPointF:
        crop, dest = self._crop_rect()
        if crop.isEmpty() or dest.isEmpty():
            return QPointF(0, 0)
        return QPointF(
            dest.x() + (p.x() - crop.x()) * dest.width() / max(crop.width(), 1e-9),
            dest.y() + (p.y() - crop.y()) * dest.height() / max(crop.height(), 1e-9),
        )

    def _is_panning(self) -> bool:
        """Zoomed in (not Fit) and no Shift held: drag pans, Shift drags AE."""
        return (self._zoom != FIT
                and not self._drag_shift)

    # ------------------------------------------------------------------ painting

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.fillRect(self.rect(), _BG)
        if self._image is None:
            painter.setPen(QColor(160, 160, 160))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Live View")
            return

        crop, dest = self._crop_rect()
        if crop.isEmpty():
            return
        image = to_qimage(self._image)
        painter.setRenderHint(
            QPainter.RenderHint.SmoothPixmapTransform, self._zoom == FIT
        )
        painter.drawImage(dest, image, crop)

        if self._ae_rect is not None and not self._ae_rect.isEmpty():
            a = self._source_to_screen(self._ae_rect.topLeft())
            b = self._source_to_screen(self._ae_rect.bottomRight())
            painter.setPen(_AE_PEN)
            painter.drawRect(QRectF(a, b).normalized())
        if (self._drag_origin is not None and self._drag_current is not None
                and not self._is_panning()):
            painter.setPen(_AE_PEN)
            painter.drawRect(QRectF(self._drag_origin,
                                    self._drag_current).normalized())

        if self._detail_note and self._zoom != FIT:
            painter.setPen(QColor(255, 200, 120))
            painter.drawText(8, 16, self._detail_note)

    # ------------------------------------------------------------------- mouse

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._image is None:
            return
        if event.button() == Qt.MouseButton.RightButton:
            self.clear_ae_rect()
            return
        self._drag_origin = event.position()
        self._drag_current = self._drag_origin
        # Decided once per gesture so holding/releasing Shift mid-drag cannot
        # switch a pan into an AE rectangle under the pointer.
        self._drag_shift = bool(
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        self._drag_pan_accum = QPointF(0, 0)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._drag_origin is None:
            return
        pos = event.position()
        if self._is_panning():
            # Drag the picture: the sensor point grabbed stays under the
            # pointer, which is what "posunuji se fotkou" means physically.
            per_source = max(self._display_scale() * self._source_scale, 1e-9)
            d = pos - self._drag_current
            cx = self._center.x() - d.x() / per_source * self._source_scale
            cy = self._center.y() - d.y() / per_source * self._source_scale
            self._center = QPoint(int(round(cx)), int(round(cy)))
            self._clamp_center()
            self._drag_pan_accum += d
            self._drag_current = pos
            self.update()
            return
        self._drag_current = pos
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._drag_origin is None or self._image is None:
            return
        origin, current = self._drag_origin, event.position()
        panning = self._is_panning()
        self._drag_origin = None
        self._drag_current = None
        self._drag_pan_accum = QPointF(0, 0)
        drag = QRectF(origin, current).normalized()
        if drag.width() < _DRAG_THRESHOLD_PX or drag.height() < _DRAG_THRESHOLD_PX:
            # A click, not a drag: re-centre as before.
            if self._zoom != FIT:
                source = self._screen_to_source(current)
                self._center = QPoint(
                    int(source.x() * self._source_scale),
                    int(source.y() * self._source_scale),
                )
                self._clamp_center()
                self.centerChanged.emit(self._center.x(), self._center.y())
            self.update()
            return
        if panning:
            # A pan gesture ends silently — it moved the view, that was its
            # whole purpose. (panned is only used for the status line.)
            self.centerChanged.emit(self._center.x(), self._center.y())
            self.update()
            return
        tl = self._screen_to_source(drag.topLeft())
        br = self._screen_to_source(drag.bottomRight())
        self._ae_rect = QRect(tl, br).normalized()
        self.aeRectChanged.emit(self.ae_rect())
        self.update()


class HistogramWidget(QWidget):
    """Linear-domain histogram with stop gridlines and clip flags.

    Deliberately fed linear data only -- see :mod:`filmscan_studio.core.histogram`.
    A histogram of an inverted or tone-mapped preview would tell the operator
    nothing usable about whether the film's base is inside the sensor's range.

    Clipped pixels are painted as full-height bars on the rail they hit:
    red right (white overflow), blue left (black crush), so blown film base is
    visible at a glance rather than hidden in a corner of the plot.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._hist: Histogram | None = None
        self._curve: np.ndarray | None = None
        self.setMinimumHeight(96)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_histogram(self, hist: Histogram | None) -> None:
        self._hist = hist
        self.update()

    def set_curve(self, curve: np.ndarray | None) -> None:
        """Overlay the active filmic curve (Working Positive mode only)."""
        self._curve = None if curve is None else np.asarray(curve, dtype=np.float64)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.fillRect(self.rect(), _BG)
        w, h = self.width(), self.height()
        if self._hist is None:
            painter.setPen(QColor(160, 160, 160))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "histogram")
            return

        counts = self._hist.normalised()
        # Stop gridlines: x is linear 0..1, so equal pixel spacing is equal
        # signal ratio, and log2 marks exposure steps.
        painter.setPen(_GRID_PEN)
        for stop in range(1, 15):
            x = 2.0 ** (stop / 14.0) - 1.0  # normalised linear at 14 stops
            if x > 1.0:
                break
            px = int(x * (w - 1))
            painter.drawLine(px, h, px, 0)

        painter.setPen(QPen(QColor(220, 220, 220)))
        prev = None
        for i, value in enumerate(counts):
            x = int(i / max(len(counts) - 1, 1) * (w - 1))
            y = h - int(float(value) * (h - 2))
            if prev is not None:
                painter.drawLine(prev[0], prev[1], x, y)
            prev = (x, y)

        if self._curve is not None and len(self._curve) > 1:
            painter.setPen(_CURVE_PEN)
            prev = None
            for i, out in enumerate(self._curve):
                x = int(i / (len(self._curve) - 1) * (w - 1))
                y = h - int(float(out) * (h - 2))
                if prev is not None:
                    painter.drawLine(prev[0], prev[1], x, y)
                prev = (x, y)

        # Clip flags: bars on the rail, sized sqrt() so a tiny but real
        # overflow is still visible without a huge fraction being louder.
        if self._hist.clipped_high:
            frac = min(1.0, (self._hist.clipped_high / max(self._hist.total, 1)) ** 0.5)
            painter.fillRect(w - 6, h - int(frac * h), 6, int(frac * h), QColor(255, 80, 80))
        if self._hist.clipped_low:
            frac = min(1.0, (self._hist.clipped_low / max(self._hist.total, 1)) ** 0.5)
            painter.fillRect(0, h - int(frac * h), 6, int(frac * h), QColor(80, 120, 255))

        if self._hist.clipping_warning:
            painter.setPen(_CLIP_PEN)
            painter.drawText(6, 14, "PŘEPAL")
            painter.drawText(w - 66, 14, f"clip {self._hist.clipped_fraction:.2%}")


class CollapsibleBox(QWidget):
    """Section with a click-to-collapse header.

    Exposure *preview* controls (filmic, EV) used to sit next to the camera's
    own exposure controls and looked like they moved the shutter. Grouping them
    under a collapsed header by default states, visually, that they are a
    different layer — the preview, not the capture.
    """

    def __init__(self, title: str, expanded: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import QPushButton

        self._body = QWidget()
        self._body.setVisible(expanded)
        self._button = QPushButton(
            ("▾ " if expanded else "▸ ") + title, checkable=True, checked=expanded
        )
        self._button.setFlat(True)
        self._title = title
        self._button.toggled.connect(self._set_open)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self._button)
        layout.addWidget(self._body)

    def _set_open(self, open_: bool) -> None:
        self._button.setText(("▾ " if open_ else "▸ ") + self._title)
        self._body.setVisible(open_)

    def body_layout(self) -> QVBoxLayout:
        from PySide6.QtWidgets import QVBoxLayout as _VL
        if self._body.layout() is None:
            layout = _VL(self._body)
            layout.setContentsMargins(8, 0, 0, 0)
            return layout
        return self._body.layout()  # type: ignore[return-value]


class FilmicPanel(QWidget):
    """Live controls for the Working Positive preview.

    These adjust the *preview* only. The value that will be baked into an export
    lives in the developer module; nothing here writes to disk, and nothing here
    can reach a stored RAW file.
    """

    paramsChanged = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import (
            QCheckBox,
            QDoubleSpinBox,
            QFormLayout,
            QLabel,
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        layout.addLayout(form)

        self.exposure = QDoubleSpinBox()
        self.exposure.setRange(-6.0, 6.0)
        self.exposure.setSingleStep(0.1)
        self.exposure.setDecimals(2)
        self.exposure.setSuffix(" EV")
        self.exposure.valueChanged.connect(self.paramsChanged)
        form.addRow("Exposure (preview)", self.exposure)

        self.toe = QDoubleSpinBox()
        self.toe.setRange(0.0, 1.0)
        self.toe.setSingleStep(0.05)
        self.toe.valueChanged.connect(self.paramsChanged)
        form.addRow("Stíny — komprese", self.toe)

        self.gamma = QDoubleSpinBox()
        self.gamma.setRange(0.2, 4.0)
        self.gamma.setSingleStep(0.05)
        self.gamma.setValue(1.10)
        self.gamma.valueChanged.connect(self.paramsChanged)
        form.addRow("Středy — gamma", self.gamma)

        self.shoulder = QDoubleSpinBox()
        self.shoulder.setRange(0.0, 1.0)
        self.shoulder.setSingleStep(0.05)
        self.shoulder.setValue(0.40)
        self.shoulder.valueChanged.connect(self.paramsChanged)
        form.addRow("Světla — komprese", self.shoulder)

        self.invert = QCheckBox("Invertovat (negativ)")
        self.invert.setChecked(True)
        self.invert.toggled.connect(self.paramsChanged)
        form.addRow(self.invert)

        note = QLabel("Pouze náhled — uložený RAW nikdy není ovlivněn.")
        note.setStyleSheet("color: #9a9; font-style: italic;")
        layout.addWidget(note)
