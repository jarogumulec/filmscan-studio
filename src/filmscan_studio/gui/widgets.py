"""Preview and histogram widgets.

The zoom widget exists because focusing a film scan is 100% pixel-peeping: the
operator must see film grain to judge focus, at 100% or more, centred wherever
they last clicked. A plain scaled QLabel cannot do that, and fitting the whole
frame on screen is precisely what is useless for focus.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from filmscan_studio.gui.imageutil import to_qimage
from filmscan_studio.core.histogram import Histogram

#: The brief's zoom ladder.
ZOOM_LEVELS = (0.25, 1.0, 2.0, 4.0)
ZOOM_LABELS = {"0.25": "Fit", "1.0": "100%", "2.0": "200%", "4.0": "400%"}

_CLIP_PEN = QPen(QColor(255, 80, 80))
_GRID_PEN = QPen(QColor(110, 110, 110))
_CURVE_PEN = QPen(QColor(235, 235, 235))
_BG = QColor(28, 28, 30)


class ZoomView(QWidget):
    """Pixel-exact scaled view with click-to-centre.

    Scaling is done here, at paint time, from the full-resolution float buffer.
    Scaling a pre-sized pixmap instead would either resample a crop (and lie
    about what 100% means) or resample the whole frame (and blur the grain the
    zoom exists to show). Nearest-neighbour is deliberate: at 200%+ an
    interpolated grain picture hides focus error.
    """

    zoomChanged = Signal(float)
    #: Pixel coordinates the view is centred on, for status display.
    centerChanged = Signal(int, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image: np.ndarray | None = None
        self._zoom = ZOOM_LEVELS[0]
        self._center = QPoint(0, 0)
        self._has_image = False
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(320, 240)
        self.setMouseTracking(True)

    # ------------------------------------------------------------------ state

    def set_image(self, image: np.ndarray) -> None:
        """Store the full-resolution float image and repaint the visible crop."""
        self._image = np.ascontiguousarray(image)
        self._has_image = True
        if self._zoom == ZOOM_LEVELS[0]:
            self._center = QPoint(self._image.shape[1] // 2, self._image.shape[0] // 2)
        self.update()

    def clear_image(self) -> None:
        self._image = None
        self._has_image = False
        self.update()

    @property
    def has_image(self) -> bool:
        return self._has_image

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

    def _clamp_center(self) -> None:
        if self._image is None:
            return
        h, w = self._image.shape[:2]
        half = QSize(int(self.width() / self._zoom / 2), int(self.height() / self._zoom / 2))
        self._center.setX(int(np.clip(self._center.x(), half.width(), max(half.width(), w - half.width()))))
        self._center.setY(int(np.clip(self._center.y(), half.height(), max(half.height(), h - half.height()))))

    # ------------------------------------------------------------------ painting

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.fillRect(self.rect(), _BG)
        if self._image is None:
            painter.setPen(QColor(160, 160, 160))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Live View")
            return

        h, w = self._image.shape[:2]
        if self._zoom == ZOOM_LEVELS[0]:
            scale = min(self.width() / w, self.height() / h)
            crop = QRect(0, 0, w, h)
            dest = QRect(
                int((self.width() - w * scale) / 2),
                int((self.height() - h * scale) / 2),
                int(w * scale),
                int(h * scale),
            )
        else:
            self._clamp_center()
            cw = min(w, int(self.width() / self._zoom))
            ch = min(h, int(self.height() / self._zoom))
            x0 = int(np.clip(self._center.x() - cw // 2, 0, w - cw))
            y0 = int(np.clip(self._center.y() - ch // 2, 0, h - ch))
            crop = QRect(x0, y0, cw, ch)
            dest = QRect(0, 0, int(cw * self._zoom), int(ch * self._zoom))

        image = to_qimage(self._image)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self._zoom == ZOOM_LEVELS[0])
        painter.drawImage(dest, image, crop)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._image is None or self._zoom == ZOOM_LEVELS[0]:
            return
        h, w = self._image.shape[:2]
        cw = min(w, int(self.width() / self._zoom))
        ch = min(h, int(self.height() / self._zoom))
        x0 = int(np.clip(self._center.x() - cw // 2, 0, w - cw))
        y0 = int(np.clip(self._center.y() - ch // 2, 0, h - ch))
        self._center = QPoint(
            x0 + int(event.position().x() / self._zoom),
            y0 + int(event.position().y() / self._zoom),
        )
        self._clamp_center()
        self.centerChanged.emit(self._center.x(), self._center.y())
        self.update()


class HistogramWidget(QWidget):
    """Linear-domain histogram with stop gridlines and clip flags.

    Deliberately fed linear data only -- see :mod:`filmscan_studio.core.histogram`.
    A histogram of an inverted or tone-mapped preview would tell the operator
    nothing usable about whether the film's base is inside the sensor's range.
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

        if self._hist.clipping_warning:
            painter.setPen(_CLIP_PEN)
            painter.drawText(6, 14, "PŘEPAL")
            painter.drawText(w - 66, 14, f"clip {self._hist.clipped_fraction:.2%}")


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
