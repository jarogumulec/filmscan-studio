"""Samostatné GUI vyvolávače: hustotní archiv + pozitivní render.

``uv run filmscan-develop-gui [složka_projektu]``

Vrstvy jsou ty ze návrhových dokumentů (``Documentation_image_processing/``):

* measurement  -- :mod:`filmscan_studio.core.density` přes
  :class:`~filmscan_studio.developer.project.DevelopProject` (hustota se
  počítá jednou na snímek a cachuje),
* rendering    -- :mod:`filmscan_studio.core.render`; slidery mění jen
  :class:`RenderParams` a render přepočítává z cachované hustoty.

Výstup je **pozitiv**: bright scene = hustý zákal negativu = světlý pixel.
Pixely mimo měřitelný rozsah (±inf hustoty) se oříznou na konec stupnice
(bílá/černá); purpurová zůstává jen pro NaN = „sem nedosvětlo" (žádná data).
Rendery (náhled i export) se překlopí podle orientace filmu ze `project.json`
(``mirrored_horizontal`` / ``mirrored_vertical`` / ``rotated_180``); hustotní
archiv zůstává v surové orientaci senzoru.

Obsluha náhledu: kolečko = přiblížení, tažení = posun, **Shift+tažení =
nakreslení rámčku snímku (ROI)**. Rámček je nutný tam, kde akvizice
nezaznamenala ``image_rect`` do sidecaru (dokument 06): bez něj by se do
archivu počítaly i okraje držáku. Hustotní archiv se ukládá *ořezaný* na
rámček a render pak logicky vychází z něj.

Parametry (Dmin/Dmax, expozice, křivka) jsou **per-snímek** a pamatují se do
``develop_settings.json`` vedle projektu; při přepnutí snímku se nahrají
zase. Úplně první otevření snímku bez historie používá Proposal (auto Dmin
z film base, auto Dmax z p99,9, neutrální křivka).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import numpy as np
import tifffile
from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPolygon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGroupBox, QLabel, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QPushButton, QSlider, QSizePolicy, QSplitter,
    QVBoxLayout, QWidget,
)

from filmscan_studio.core import density as dens
from filmscan_studio.core import render as rnd
from filmscan_studio.core.filmic import FilmicProfile
from filmscan_studio.developer.project import DevelopProject

log = logging.getLogger(__name__)

#: Náhled se počítá z podvzorkované hustoty, aby reakce na slidery byla
#: okamžitá. Plné rozlišení vidí jen export.
PREVIEW_MAX_DIM = 1200

#: Barva neplatných (NaN) pixelů v náhledu -- „sem nedosvětlo".
NAN_COLOR = (255, 0, 255)


def density_to_qimage(image: np.ndarray) -> QImage:
    """0..1 float (s NaN) -> QImage; NaN se kreslí NAN_COLOR."""
    a = np.asarray(image, dtype=np.float64)
    if a.ndim != 2:
        raise ValueError(f"očekávám 2-D grey, tvar {a.shape}")
    rgb = np.empty(a.shape + (3,), dtype=np.uint8)
    good = np.isfinite(a)
    # NaN must not reach the uint8 cast (invalid-value warning; the masked
    # pixels are overwritten below anyway, but the cast sees them first).
    grey_u8 = (np.clip(np.where(good, a, 0.0), 0.0, 1.0) * 255.0
               + 0.5).astype(np.uint8)
    rgb[...] = grey_u8[..., None]
    rgb[~good] = NAN_COLOR
    h, w, _ = rgb.shape
    rgba = np.dstack([rgb, np.full((h, w), 255, dtype=np.uint8)])
    # QImage drží surový ukazatel -- copy nechává buffer přežít tento frame.
    return QImage(np.ascontiguousarray(rgba).data, w, h, 4 * w,
                  QImage.Format.Format_RGBA8888).copy()


class DensityView(QWidget):
    """Náhled renderu s zoom/pan a Shift+tažením rámčku (ROI)."""

    rect_chosen = Signal(object)      # (x0, y0, x1, y1) v whole-frame pixelech
    #: Kurzor nad pixel: (D, out) -- hustota a render 0..1 (už po úpravách);
    #: None když je kurzor mimo snímek.
    hovered = Signal(object)

    MIN_ZOOM, MAX_ZOOM = 0.05, 16.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image: QImage | None = None
        #: Rozměry *celého* (příp. oříznutého) snímku, pro převod ROI.
        self._map_wh: tuple[int, int] | None = None
        self._zoom = 1.0
        self._origin = QPoint(0, 0)
        self._drag_from: QPoint | None = None
        self._drag_pan_from: QPoint | None = None
        self._pan_origin = QPoint(0, 0)
        self._rect_px: QRect | None = None    # v pixelech náhledové mapy
        self._rect_src: tuple[int, int, int, int] | None = None
        self._placeholder = "Otevři složku projektu (s frames/*.tif)"
        self.setMinimumSize(320, 240)
        self.setMouseTracking(True)     # hover i bez tažení

    def set_placeholder(self, text: str) -> None:
        """Co kreslit, když není co; ať text nelže otevřené složce."""
        self._placeholder = text
        self.update()

    # ---------------------------------------------------------------- model

    def set_image(self, image: np.ndarray | None,
                  map_wh: tuple[int, int] | None,
                  densities: np.ndarray | None = None) -> None:
        """Podvzorek náhledu 0..1 (s NaN) + rozměry mapy, ze které vzešel.

        ``densities`` je týž podvzorek v hustotě D (před renderem) -- aby
        hover mohl hlásit obě čísla; má stejný tvar jako ``image``.
        """
        self._map_wh = map_wh
        self._out = None if image is None else np.asarray(image, np.float64)
        self._dens = (None if densities is None
                      else np.asarray(densities, np.float64))
        self._image = None if image is None else density_to_qimage(image)
        self._recompute_rect_px()
        self._fit()
        self.update()

    def set_rect(self, rect: tuple[int, int, int, int] | None) -> None:
        """Zobrazí existující ROI (whole-frame pixelech); None rámček smaže."""
        self._rect_src = rect
        self._recompute_rect_px()
        self.update()

    @property
    def roi(self) -> tuple[int, int, int, int] | None:
        # POZOR: nejmenujte `rect` -- clonovalo by QWidget.rect() a rozbilo
        # paintEvent (self.rect() by volalo tuple).
        return self._rect_src

    # ------------------------------------------------------------- geometry

    def _fit(self) -> None:
        if self._image is None:
            return
        zx = self.width() / max(self._image.width(), 1)
        zy = self.height() / max(self._image.height(), 1)
        self._zoom = min(1.0, zx, zy)
        self._origin = QPoint(0, 0)

    def _map_rect_to_src(self, r: QRect) -> tuple[int, int, int, int] | None:
        """Rámček v pixelech náhledové mapy -> whole-frame souřadnice."""
        if self._map_wh is None or self._image is None:
            return None
        sx = self._map_wh[0] / self._image.width()
        sy = self._map_wh[1] / self._image.height()
        x0 = max(0, min(int(r.left() * sx), self._map_wh[0] - 1))
        y0 = max(0, min(int(r.top() * sy), self._map_wh[1] - 1))
        x1 = max(x0 + 1, min(int(r.right() * sx), self._map_wh[0]))
        y1 = max(y0 + 1, min(int(r.bottom() * sy), self._map_wh[1]))
        return (x0, y0, x1, y1)

    def _recompute_rect_px(self) -> None:
        if (self._rect_src is None or self._map_wh is None
                or self._image is None):
            self._rect_px = None
            return
        x0, y0, x1, y1 = self._rect_src
        fx = self._image.width() / self._map_wh[0]
        fy = self._image.height() / self._map_wh[1]
        self._rect_px = QRect(int(x0 * fx), int(y0 * fy),
                              max(1, int((x1 - x0) * fx)),
                              max(1, int((y1 - y0) * fy)))

    # ------------------------------------------------------------- eventos

    def wheelEvent(self, event) -> None:  # noqa: N802
        if self._image is None:
            return
        old = self._zoom
        factor = 1.25 if event.angleDelta().y() > 0 else 0.8
        self._zoom = min(self.MAX_ZOOM, max(self.MIN_ZOOM, self._zoom * factor))
        pos = event.position().toPoint()
        map_pt = (pos - self._origin) / old   # bod pod kurzorem se drží
        self._origin = pos - map_pt * self._zoom
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self._drag_from = event.position().toPoint()
        else:
            self._drag_pan_from = event.position().toPoint()
            self._pan_origin = QPoint(self._origin)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position().toPoint()
        if self._drag_from is not None:
            self._rect_px = QRect(self._drag_from, pos).normalized()
            self.update()
        elif self._drag_pan_from is not None:
            self._origin = self._pan_origin + (pos - self._drag_pan_from)
            self.update()
        self._emit_hover(pos)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.hovered.emit(None)

    def _emit_hover(self, pos: QPoint) -> None:
        """Přepočítá kurzor na pixel mapy a pošle (D, out) nebo None."""
        if self._image is None or self._out is None:
            self.hovered.emit(None)
            return
        mx = int((pos.x() - self._origin.x()) / max(self._zoom, 1e-6))
        my = int((pos.y() - self._origin.y()) / max(self._zoom, 1e-6))
        if not (0 <= mx < self._out.shape[1] and 0 <= my < self._out.shape[0]):
            self.hovered.emit(None)
            return
        d = (float(self._dens[my, mx]) if self._dens is not None
             else float("nan"))
        self.hovered.emit((d, float(self._out[my, mx])))

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._drag_from is not None:
            r = QRect(self._drag_from, event.position().toPoint()).normalized()
            self._drag_from = None
            # Úplně malý tah = omylem; rámček ponecháme, nic Neděláme.
            if r.width() >= 12 and r.height() >= 12:
                src = self._map_rect_to_src(r)
                if src is not None:
                    self._rect_src = src
                    self._recompute_rect_px()
                    self.rect_chosen.emit(src)
            self.update()
        self._drag_pan_from = None

    def resizeEvent(self, event) -> None:  # noqa: N802
        # Zoomoval-li uživatel ručně (zoom > fit), respektuj ho; jinak přizpůsob.
        if self._image is not None and self._zoom <= 1.0:
            self._fit()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(30, 30, 30))
        if self._image is None:
            p.setPen(QColor(160, 160, 160))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       self._placeholder)
            return
        p.translate(self._origin)
        p.scale(self._zoom, self._zoom)
        p.drawImage(0, 0, self._image)
        if self._rect_px is not None:
            pen = QPen(QColor(0, 220, 255), 2)
            pen.setCosmetic(True)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(self._rect_px)


#: Počet-binový histogram hustoty; při rozsahu 0..3.2 D je bin 0.0125 D --
#: hrubší než šum měření, jemnější než rozlišení oka na křivce.
HIST_BINS = 256
#: Spodní zárukář osy: ani prázdný/rozmělněný snímek nesmí histogram roztáhnout
#: na mikro-rozsah, osový popisek by poskakoval pod každým snímkem.
D_HIST_MIN_SPAN = 0.5


def density_histogram(d: np.ndarray, xmax: float = 3.2,
                      xmin: float = 0.0,
                      bins: int = HIST_BINS) -> np.ndarray:
    """Histogram platných (konečných) hustot v D na [xmin, xmax].

    ±inf (saturace / neprostupná hustota) do histogramu nepatří -- nemají
    hodnotu, jen směr; počet na kolejnicích hlásí widget zvlášť. NaN taky ne.
    """
    fin = d[np.isfinite(d)]
    if fin.size == 0:
        return np.zeros(bins)
    counts, _ = np.histogram(fin, bins=bins, range=(xmin, xmax))
    peak = float(counts.max())
    return counts / peak if peak > 0 else np.zeros(bins)


class DensityHistogramWidget(QWidget):
    """Histogram hustoty v [D] + body stupnice + promítnutá S-křivka.

    Osa x je hustota, ne jas výstupu: to je doména měření, ve které se
    rozhoduje (Dmin ze měření film base, Dmax z rozložení snímku). Tři vrstvy
    nad jedinou osou:

    * histogram -- kde v obraze *jsou* data (odpověď na „v jakém rozsahu
      fotka je"); logaritmická výška, ať i slabá mása není neviditelná,
    * svislé čáry Dmin (šedá) a Dmax (oranžová) -- kde jsem *řekl*, že rozsah
      je; mimosvět mezi nimi je na tisku mrtvá zóna,
    * bílá křivka -- kam který D dopadá na výstupu (0 = černá, 1 = bílá),
      včetně expozice: posun křivky doleva = víc světla.

    ±inf hustoty se kreslí jako plné sloupce na kolejnicích (vlevo −inf =
    přepal, vpravo +inf = neprostupno) a NaN („bez světla") jen jako číslo --
    ani jedno nemá vlastní hustotu, ale operátor o něm musí vědět.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._hist: np.ndarray | None = None
        self._xmin = 0.0
        self._xmax = 3.2
        self._params: rnd.RenderParams | None = None
        self._neg_inf_fraction = 0.0
        self._pos_inf_fraction = 0.0
        self._nan_fraction = 0.0
        self._stats: dict[str, float] = {}
        self._cursor_d: float | None = None   # hustota pod kurzorem
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)

    def set_density(self, d: np.ndarray | None) -> None:
        if d is None:
            self._hist = None
            self.update()
            return
        # Osa se oběma směry přizpůsobí datům (i záporná D pod film base --
        # dřív se ořezala na nulu a data vlevo se "řízla"), ale nikdy se
        # ztenčí pod D_HIST_MIN_SPAN, aby škála neukazovala data jako proužek.
        fin = d[np.isfinite(d)]
        if fin.size:
            self._xmin = min(0.0, float(fin.min()) - 0.02)
            self._xmax = max(self._xmin + D_HIST_MIN_SPAN,
                             float(np.percentile(fin, 99.9)) * 1.05)
        else:
            self._xmin, self._xmax = 0.0, D_HIST_MIN_SPAN
        self._hist = density_histogram(d, self._xmax, self._xmin)
        self._neg_inf_fraction = float(np.isneginf(d).mean())
        self._pos_inf_fraction = float(np.isposinf(d).mean())
        self._nan_fraction = float(np.isnan(d).mean())
        self._stats = dens.density_stats(d)
        self.update()

    def set_cursor_density(self, d_val: float | None) -> None:
        """Zobrazí/zmaže svislou čáru na hustotě hodnoty pod kurzorem."""
        self._cursor_d = d_val
        self.update()

    def set_params(self, params: rnd.RenderParams | None) -> None:
        self._params = params
        self.update()

    def _d_to_x(self, d_val: float) -> int:
        w = self.width()
        span = max(self._xmax - self._xmin, 1e-9)
        return int(np.clip((d_val - self._xmin) / span, 0.0, 1.0) * (w - 1))

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor(30, 30, 30))
        if self._hist is None:
            p.setPen(QColor(160, 160, 160))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "histogram D")
            return

        # Mrtvá zóna tisku: mimo [dmin, dmax] nic není.
        if self._params is not None:
            x0, x1 = self._d_to_x(self._params.dmin), \
                self._d_to_x(self._params.dmax)
            p.fillRect(0, 0, x0, h, QColor(80, 40, 40, 90))
            p.fillRect(x1, 0, max(0, w - x1), h, QColor(40, 40, 80, 90))

        # Histogram: log škála výšky (lineární by maisu pod tlakem bitu
        # zakryla), kresleno jako plň pod křivkou.
        poly = [(0, h)]
        for i, v in enumerate(self._hist):
            x = int(i / max(len(self._hist) - 1, 1) * (w - 1))
            y = h - int(np.log1p(9.0 * float(v)) / np.log1p(9.0) * (h - 2))
            poly.append((x, y))
        poly.append((w - 1, h))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(220, 220, 220, 70))
        pts = [QPoint(*pt) for pt in poly]
        p.drawPolygon(QPolygon(pts))
        p.setPen(QColor(220, 220, 220))
        p.drawPolyline(QPolygon(pts[1:-1]))

        if self._params is not None:
            # Body stupně.
            for d_val, color, label in (
                    (self._params.dmin, QColor(0, 220, 255), "Dmin"),
                    (self._params.dmax, QColor(255, 170, 0), "Dmax")):
                x = self._d_to_x(d_val)
                pen = QPen(color, 1, Qt.PenStyle.DashLine)
                p.setPen(pen)
                p.drawLine(x, 0, x, h)
                p.drawText(x + 3, 12, label)
            # Promítnutá křivka: jaký D -> jaký tisk (vč. expozice).
            p.setPen(QPen(QColor(255, 255, 255), 2))
            xs = np.linspace(self._xmin, self._xmax, 256)
            net = (xs - self._params.dmin
                   + rnd.D_PER_STOP * self._params.exposure_ev
                   ) / self._params.span
            out = self._params.profile.apply(np.clip(net, 0.0, 1.0))
            p.drawPolyline(QPolygon([
                QPoint(self._d_to_x(float(dv)),
                       h - int(float(o) * (h - 2)))
                for dv, o in zip(xs, out)]))

        # Kurzor: oranžová svislá čára na D pixelu pod myší (z náhledu).
        if self._cursor_d is not None and np.isfinite(self._cursor_d):
            cx = self._d_to_x(self._cursor_d)
            p.setPen(QPen(QColor(255, 140, 0), 2))
            p.drawLine(cx, 0, cx, h)

        # Kolejnice: ±inf mají směr, ne hodnotu -- ať jsou vidět.
        if self._neg_inf_fraction > 0:
            bw = max(3, int(np.sqrt(self._neg_inf_fraction) * w * 0.5))
            p.fillRect(0, 0, bw, h, QColor(255, 255, 255, 150))
        if self._pos_inf_fraction > 0:
            bw = max(3, int(np.sqrt(self._pos_inf_fraction) * w * 0.5))
            p.fillRect(w - bw, 0, bw, h, QColor(0, 120, 255, 150))

        # Číselné konto: percentily + koš na kolejnicích a mimo světlo.
        # "přepal" = D -inf (senz. na maximu) -> v pozitivu černá;
        # "hustší než škála" = D +inf -> v pozitivu bílá. Černý pod je
        # opak: ten má nízké, konečné D a je vidět v histogramu.
        s = self._stats
        if s and np.isfinite(s.get("d_p50", float("nan"))):
            txt = (f"D min/p50/p99 {self._fmt1(s['d_min'])}/"
                   f"{self._fmt1(s['d_p50'])}/{self._fmt1(s['d_p99'])}"
                   f" · přepal(→černá) {self._neg_inf_fraction * 100:.2f} %"
                   f" · nad škálu(→bílá) {self._pos_inf_fraction * 100:.2f} %"
                   f" · bez světla {self._nan_fraction * 100:.1f} %")
            p.setPen(QColor(200, 200, 200))
            p.drawText(6, h - 6, txt)

    @staticmethod
    def _fmt1(v: float) -> str:
        return f"{v:.2f}".replace(".", ",")


class MainWindow(QMainWindow):
    """Okno vyvolávače: snímky | náhled | parametry."""

    def __init__(self, project: DevelopProject | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Filmscan Studio — vyvolávač")
        self.resize(1440, 900)
        self.project: DevelopProject | None = None
        self._density: tuple[np.ndarray, dens.DensityProvenance] | None = None
        self._dmin_auto: float | None = None
        self._loading = False       # tlumí rerendery při přepínání snímku
        self._current: str | None = None

        splitter = QSplitter()
        self.setCentralWidget(splitter)

        # -- levý sloupec: složka + seznam snímků ---------------------------
        left = QWidget()
        lv = QVBoxLayout(left)
        self.btn_open = QPushButton("Otevřít složku projektu…")
        self.btn_open.clicked.connect(self.choose_folder)
        lv.addWidget(self.btn_open)
        self.frame_list = QListWidget()
        self.frame_list.currentItemChanged.connect(self._frame_selected)
        lv.addWidget(self.frame_list, 1)
        self.lbl_dmin = QLabel("Dmin: —")
        lv.addWidget(self.lbl_dmin)
        self.lbl_orient = QLabel("Orientace: 1:1")
        lv.addWidget(self.lbl_orient)
        splitter.addWidget(left)

        # -- střed: náhled + histogram ------------------------------------
        centre = QWidget()
        cv = QVBoxLayout(centre)
        cv.setContentsMargins(0, 0, 0, 0)
        self.view = DensityView()
        self.view.rect_chosen.connect(self.rect_selected)
        self.view.hovered.connect(self._pixel_hovered)
        cv.addWidget(self.view, 1)
        self.histogram = DensityHistogramWidget()
        cv.addWidget(self.histogram)
        splitter.addWidget(centre)

        # -- pravý sloupec: parametry + export -------------------------------
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.addWidget(self._build_scale_box())
        rv.addWidget(self._build_curve_box())
        rv.addWidget(self._build_export_box())
        self.lbl_status = QLabel("—")
        self.lbl_status.setWordWrap(True)
        rv.addWidget(self.lbl_status)
        rv.addStretch(1)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)

        if project is not None:
            self.set_project(project)

    # ---------------------------------------------------------- widgets

    @staticmethod
    def _slider(lo: int, hi: int, value: int) -> QSlider:
        s = QSlider(Qt.Orientation.Horizontal)
        s.setRange(lo, hi)
        s.setValue(value)
        return s

    def _build_scale_box(self) -> QGroupBox:
        box = QGroupBox("Stupně (hustoty)")
        form = QFormLayout(box)
        self.spin_dmin = QDoubleSpinBox()
        self.spin_dmin.setRange(0.0, 2.0)
        self.spin_dmin.setSingleStep(0.01)
        self.spin_dmin.setDecimals(3)
        self.spin_dmin.setValue(0.2)
        self.chk_dmin_auto = QCheckBox("z měření film base")
        self.chk_dmin_auto.setChecked(True)
        self.spin_dmax = QDoubleSpinBox()
        self.spin_dmax.setRange(0.2, 5.0)
        self.spin_dmax.setSingleStep(0.05)
        self.spin_dmax.setDecimals(3)
        self.spin_dmax.setValue(2.6)
        self.chk_dmax_auto = QCheckBox("auto ze snímku (p99,9 + okraj)")
        self.chk_dmax_auto.setChecked(True)
        # Výstředník: interně 1/100 EV (jemný krok), jezdec ladí po 0,05,
        # textové pole pojme i hodnotu mimo krok (0,15 = 3 ticky).
        self.sl_ev = self._slider(-600, 600, 0)     # ±6 EV
        self.sl_ev.setSingleStep(5)                 # 0,05 EV
        self.sl_ev.setPageStep(20)                  # 0,2 EV (kolečko/klik do dráhy)
        self.spin_ev = QDoubleSpinBox()
        self.spin_ev.setRange(-6.0, 6.0)
        self.spin_ev.setSingleStep(0.05)
        self.spin_ev.setDecimals(2)
        self.spin_ev.setSuffix(" EV")
        self.btn_defaults = QPushButton("Proposal (auto body, linear)")
        self.btn_defaults.clicked.connect(self.apply_defaults)
        form.addRow("Dmin [D]", self.spin_dmin)
        form.addRow("", self.chk_dmin_auto)
        form.addRow("Dmax [D]", self.spin_dmax)
        form.addRow("", self.chk_dmax_auto)
        form.addRow("Expozice", self.sl_ev)
        form.addRow("", self.spin_ev)
        form.addRow("", self.btn_defaults)

        self.spin_dmin.valueChanged.connect(self._setting_changed)
        self.spin_dmax.valueChanged.connect(self._setting_changed)
        # obousmerne spojeni jezdec <-> spin; oba maji rozliseni 0,01,
        # takze se signal nemohou rozkmitat (setValue bez zmeny signál pošle)
        self.sl_ev.valueChanged.connect(
            lambda v: self.spin_ev.setValue(v / 100.0))
        self.spin_ev.valueChanged.connect(
            lambda v: self.sl_ev.setValue(int(round(v * 100.0))))
        self.spin_ev.valueChanged.connect(self._setting_changed)
        self.chk_dmin_auto.toggled.connect(self._dmin_toggled)
        self.chk_dmax_auto.toggled.connect(self._scale_toggled)
        self.spin_dmin.setEnabled(False)   # auto zapnuto
        return box

    def _build_curve_box(self) -> QGroupBox:
        box = QGroupBox("Tónová křivka (S, monotónní)")
        form = QFormLayout(box)
        self.sl_toe = self._slider(0, 100, 35)
        self.lbl_toe = QLabel("0,35")
        self.sl_gamma = self._slider(10, 300, 110)
        self.lbl_gamma = QLabel("1,10")
        self.sl_shoulder = self._slider(0, 100, 40)
        self.lbl_shoulder = QLabel("0,40")
        for name, slider, lbl in (
                ("Patka (toe)", self.sl_toe, self.lbl_toe),
                ("Gamma (prostřed)", self.sl_gamma, self.lbl_gamma),
                ("Rameno (shoulder)", self.sl_shoulder, self.lbl_shoulder)):
            slider.valueChanged.connect(
                lambda _v, l=lbl, s=slider: self._curve_moved(s, l))
            form.addRow(name, slider)
            form.addRow("", lbl)
        return box

    def _build_export_box(self) -> QGroupBox:
        box = QGroupBox("Export")
        h = QVBoxLayout(box)
        self.btn_save_density = QPushButton("Uložit hustotní archiv (32b)")
        self.btn_save_density.clicked.connect(self.save_density)
        self.btn_save_render = QPushButton("Exportovat pozitiv (16b)")
        self.btn_save_render.clicked.connect(self.save_render)
        self.btn_save_flat = QPushButton("Exportovat flat pro Capture One")
        self.btn_save_flat.clicked.connect(self.save_flat)
        for b in (self.btn_save_density, self.btn_save_render,
                  self.btn_save_flat):
            b.setEnabled(False)
            h.addWidget(b)
        return box

    # ------------------------------------------------------------- actions

    def choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Složka projektu", str(Path.home() / "Downloads"))
        if folder:
            try:
                self.set_project(DevelopProject.open(folder))
            except Exception as exc:  # noqa: BLE001 - GUI musí přežít
                QMessageBox.critical(self, "Nelze otevřít", str(exc))

    def set_project(self, project: DevelopProject) -> None:
        self.project = project
        self.frame_list.clear()
        for name in project.frame_names:
            QListWidgetItem(name, self.frame_list)
        self._dmin_auto = project.dmin_auto()
        if self._dmin_auto is not None:
            self.spin_dmin.setValue(round(self._dmin_auto, 3))
            self.lbl_dmin.setText(f"Dmin z měření: {self._dmin_auto:.3f} D")
        else:
            self.lbl_dmin.setText("Dmin: bez měření — zadej ručně")
            self.chk_dmin_auto.setChecked(False)
        self.lbl_orient.setText("Orientace: " + self._orientation_text())
        if self.frame_list.count():
            self.frame_list.setCurrentRow(0)

    def _orientation_text(self) -> str:
        p = self.project
        if p is None:
            return "—"
        parts = [n for n, on in (
            ("zrcadlo H", p.mirrored_horizontal),
            ("zrcadlo V", p.mirrored_vertical),
            ("rot 180°", p.rotated_180)) if on]
        return " · ".join(parts) if parts else "1:1"

    def _frame_selected(self, item: QListWidgetItem | None,
                        _prev=None) -> None:
        if item is None or self.project is None:
            return
        name = item.text()
        try:
            # Náhled i status pracují s celým snímkem (crop=False): ROI rámeček
            # se kreslí do whole-frame souřadnic a ořez se uplatní až při
            # exportu archivu -- jinak by rámeček zmizel/zajel.
            self._density = self.project.build_density(name, crop=False)
        except Exception as exc:  # noqa: BLE001
            self._density = None
            self.histogram.set_density(None)
            self.view.set_image(None, None)
            self.view.set_placeholder(f"Snímek nelze změřit: {exc}")
            self.lbl_status.setText(f"Chyba měření {name}: {exc}")
            return
        d, prov = self._density
        self._current = name
        self._loading = True
        try:
            self.view.set_rect(self.project.rect_for(self.project.entry(name)))
            saved = self.project.frame_settings.get(name)
            if saved:
                self._load_params(rnd.RenderParams.from_dict(saved),
                                  dmin_manual=bool(
                                      saved.get("dmin_manual", False)))
            else:
                self._load_params(self._proposal(name),
                                  dmin_manual=self._dmin_auto is None)
        finally:
            self._loading = False
        self._update_status(d, prov)
        self.histogram.set_density(d)
        for b in (self.btn_save_density, self.btn_save_render,
                  self.btn_save_flat):
            b.setEnabled(True)
        self.rerender()

    # ----------------------------------------------------------- parametry

    def _proposal(self, name: str) -> rnd.RenderParams:
        """První náhled snímku bez historie: auto body + lineární křivka.

        Žádné volitelné S-ko -- to je rozhodnutí operátora, ne výchoziny.
        """
        assert self.project is not None
        dmin = self._dmin_auto if self._dmin_auto is not None else 0.2
        # Bez měření base (typicky flatless náhled) může relativní D spodek
        # ležet pod výchozích 0,2 -- dmax musí_scale přesahovat, ať
        # RenderParams nespadou a náhled vyjde.
        suggested = self.project.suggested_dmax(name)
        if suggested <= dmin:
            dmin = max(0.0, suggested - 0.2)
        return rnd.RenderParams(
            dmin=dmin,
            dmax=suggested,
            exposure_ev=0.0,
            profile=FilmicProfile.neutral(),
            dmax_source="frame",
        )

    def _load_params(self, params: rnd.RenderParams,
                     dmin_manual: bool = False) -> None:
        """Nahraje parametry do widgetů (volá se s ``self._loading``).

        ``dmin_manual`` je flag mimo RenderParams: jestli Dmin pochází z
        měření (auto) nebo ho zadal operátor. RenderParams to neumí nést,
        proto putuje zvlášť přes uložený payload.
        """
        # Auto Dmin má smysl jen když existuje měření film base.
        self.chk_dmin_auto.setChecked(not dmin_manual
                                      and self._dmin_auto is not None)
        self.chk_dmax_auto.setChecked(params.dmax_source == "frame")
        self.spin_dmin.setValue(round(params.dmin, 3))
        self.spin_dmax.setValue(round(params.dmax, 3))
        self.sl_ev.setValue(int(round(params.exposure_ev * 100.0)))
        self.spin_ev.setValue(round(params.exposure_ev, 2))
        self.sl_toe.setValue(int(round(params.profile.toe * 100)))
        self.lbl_toe.setText(self._fmt(params.profile.toe))
        self.sl_gamma.setValue(int(round(params.profile.gamma * 100)))
        self.lbl_gamma.setText(self._fmt(params.profile.gamma))
        self.sl_shoulder.setValue(int(round(params.profile.shoulder * 100)))
        self.lbl_shoulder.setText(self._fmt(params.profile.shoulder))
        self.spin_dmin.setEnabled(not self.chk_dmin_auto.isChecked())
        self.spin_dmax.setEnabled(not self.chk_dmax_auto.isChecked())

    def current_params(self) -> rnd.RenderParams:
        return rnd.RenderParams(
            dmin=self.spin_dmin.value(),
            dmax=self.spin_dmax.value(),
            exposure_ev=self.spin_ev.value(),
            profile=FilmicProfile(toe=self.sl_toe.value() / 100.0,
                                  gamma=self.sl_gamma.value() / 100.0,
                                  shoulder=self.sl_shoulder.value() / 100.0),
            dmax_source="frame" if self.chk_dmax_auto.isChecked()
            else "manual",
        )

    def _settings_payload(self) -> dict:
        """Uložená podoba parametrů: RenderParams + jestli byl Dmin ruční."""
        d = self.current_params().to_dict()
        d["dmin_manual"] = not self.chk_dmin_auto.isChecked()
        return d

    def _setting_changed(self, *_a) -> None:
        """Jakákoliv změna parametru: ulož per-snímkově a přerekni."""
        if self._loading:
            return
        self._remember_params()
        self.rerender()

    def _remember_params(self) -> None:
        if self.project is not None and self._current is not None:
            self.project.frame_settings[self._current] = self._settings_payload()
            self.project.save_settings()

    def apply_defaults(self) -> None:
        """Vehne snímku Proposal (auto body, linear) -- i později."""
        if self.project is None or self._current is None:
            return
        self._loading = True
        try:
            self._load_params(self._proposal(self._current))
        finally:
            self._loading = False
        self._remember_params()
        self.rerender()

    def rerender(self) -> None:
        if self._density is None or self.project is None:
            return
        d, _prov = self._density
        params = self.current_params()
        # Náhled vidí orientaci diváka; archiv zůstává surový.
        sub = _subsample(self.project.orientation_apply(d), PREVIEW_MAX_DIM)
        display = rnd.render_for_display(sub, params)
        # _subsample je stride-sám o sobě deterministický: display[i,j]
        # pochází z sub[i,j], takže kurzor vidí týž pixel v D i v renderu.
        self.view.set_image(display, (d.shape[1], d.shape[0]), densities=sub)
        self.histogram.set_params(params)

    def rect_selected(self, rect: tuple[int, int, int, int]) -> None:
        """ROI z Shift+kreslení: přepočítá měření (archiv se krájí) i status."""
        item = self.frame_list.currentItem()
        if item is None or self.project is None:
            return
        self.project.set_rect(item.text(), rect)
        self.project.save_settings()      # rámček přežije reopen
        try:
            self._density = self.project.build_density(item.text(),
                                                       crop=False)
        except Exception as exc:  # noqa: BLE001
            self.lbl_status.setText(f"Chyba měření: {exc}")
            return
        self._update_status(*self._density)
        self.histogram.set_density(self._density[0])
        self.rerender()

    # -------------------------------------------------------------- status

    def _pixel_hovered(self, payload) -> None:
        """Kurzor v náhledu: oranžová čára v histogramu + čísla do statusu.

        ``payload`` je (D, out) z :class:`DensityView`, nebo None mimo snímek
        -- pak se smaže čára a status se vrátí na popis měření.
        """
        if payload is None:
            self.histogram.set_cursor_density(None)
            if self._density is not None:
                self._update_status(*self._density)
            return
        d_val, out_val = payload
        self.histogram.set_cursor_density(
            None if not np.isfinite(d_val) else d_val)
        d_txt = ("—          " if not np.isfinite(d_val)
                 else f"{d_val:+.3f} D")
        inf_txt = (" (přepal→černá)" if d_val == -np.inf
                   else " (nad škálu→bílá)" if d_val == np.inf
                   else " (bez světla)" if np.isnan(d_val) else "")
        self.lbl_status.setText(
            f"pixel D {d_txt}{inf_txt} · pozitiv {out_val:.3f}")

    @staticmethod
    def _fmt(v: float) -> str:
        return f"{v:.2f}".replace(".", ",")

    def _curve_moved(self, slider: QSlider, label: QLabel) -> None:
        label.setText(self._fmt(slider.value() / 100.0))
        self._setting_changed()

    def _dmin_toggled(self) -> None:
        if self._loading:
            return
        self.spin_dmin.setEnabled(not self.chk_dmin_auto.isChecked())
        self._setting_changed()

    def _scale_toggled(self) -> None:
        if self._loading:
            return
        self.spin_dmin.setEnabled(not self.chk_dmin_auto.isChecked())
        self.spin_dmax.setEnabled(not self.chk_dmax_auto.isChecked())
        if (self.chk_dmax_auto.isChecked() and self._density is not None
                and self.project is not None and self._current is not None):
            dmax = round(self.project.suggested_dmax(self._current), 3)
            # Relativní škála bez flatu může sahát pod aktuální Dmin --
            # dmax ji musí přesáhnout, ať render nespadne.
            dmax = max(dmax, round(self.spin_dmin.value()
                                   + self.spin_dmax.singleStep(), 3))
            self.spin_dmax.setValue(dmax)
        self._setting_changed()

    def _update_status(self, d: np.ndarray,
                       prov: dens.DensityProvenance) -> None:
        s = dens.density_stats(d)
        parts = [
            prov.source,
            f"{prov.shutter * 1000:.2f} ms",
            f"platno {s['valid_fraction'] * 100:.1f} %",
            f"D p50/p99: {self._fmt(s['d_p50'])}/{self._fmt(s['d_p99'])}",
        ]
        if prov.flat_fallback:
            parts.append("⚑ bez flat snímku — náhled je relativní "
                         "(k nejjasnějším 0,1 %); Dmin zadej ručně")
        nan_fraction = float(np.isnan(d).mean())
        if nan_fraction > 0.0:
            parts.append(f"purpur (bez světla) {nan_fraction * 100:.1f} %")
        if s["valid_fraction"] < 0.85:
            parts.append("⚑ málo platných pixelů — zkontroluj ROI")
        if (s["valid_fraction"] > 0 and s["d_p50"] < 0.05
                and not prov.flat_fallback):
            # u relativního měření bez flatu je nula definiční, ne signál
            parts.append("⚑ medián D ~ 0 — je ve snímku film?")
        if self._dmin_auto is not None:
            parts.append(f"Dmin měřeno {self._dmin_auto:.3f}")
        self.lbl_status.setText(" · ".join(parts))

    # -------------------------------------------------------------- export

    def _derived_dir(self) -> Path:
        assert self.project is not None
        out = self.project.root / "derived"
        out.mkdir(exist_ok=True)
        return out

    def _cropped_density(self) -> tuple[np.ndarray, dens.DensityProvenance] \
            | None:
        """Archiv i rendery se krájí na ROI (vize uživatele); náhled ne."""
        if self._density is None or self.project is None:
            return None
        return self.project.build_density(self._density[1].source, crop=True)

    def save_density(self) -> None:
        cropped = self._cropped_density()
        if cropped is None:
            return
        d, prov = cropped
        stem = Path(prov.source).stem
        path = dens.write_density_tiff(
            self._derived_dir() / f"{stem}.density.tif", d, prov)
        self.lbl_status.setText(f"Uloženo: {path}")

    def save_render(self) -> None:
        self._export_render(flat=False)

    def save_flat(self) -> None:
        self._export_render(flat=True)

    def _export_render(self, flat: bool) -> None:
        cropped = self._cropped_density()
        if cropped is None or self.project is None:
            return
        d, prov = cropped
        # Uložená orientace: totéž co vidí náhled. Archiv above flipem zůstává.
        d = self.project.orientation_apply(d)
        params = self.current_params()
        self.project.frame_settings[prov.source] = self._settings_payload()
        self.project.save_settings()
        full = (rnd.render_flat(d, params) if flat
                else rnd.render_density(d, params))
        # NaN = „bez světla" -> černá; ±inf hustoty už render ořízl na 0/1.
        data = rnd.quantise16(full)
        stem = Path(prov.source).stem
        kind = "flat" if flat else "positive"
        out = self._derived_dir() / f"{stem}.{kind}.tif"
        tifffile.imwrite(out, np.ascontiguousarray(data),
                         photometric="minisblack",
                         description=json.dumps({
                             "magic": "filmscan-render",
                             "kind": kind,
                             "parameters": params.to_dict(),
                             "fingerprint": params.fingerprint(),
                             "density_source": f"{stem}.density.tif",
                         }, ensure_ascii=False))
        self.lbl_status.setText(
            f"Exportováno: {out.name} · fingerprint {params.fingerprint()}")


def _subsample(d: np.ndarray, max_dim: int) -> np.ndarray:
    """Stride subsample; NaN/inf se zachová (průměr by je zprůměroval na číslo)."""
    h, w = d.shape
    step = max(1, max(h, w) // max_dim)
    return np.ascontiguousarray(d[::step, ::step])


def main(argv: list[str] | None = None) -> int:
    """``filmscan-develop-gui [složka_projektu]``."""
    logging.basicConfig(level=logging.INFO)
    args = list(sys.argv[1:] if argv is None else argv)
    app = QApplication.instance() or QApplication(args)
    project = None
    if args and not args[0].startswith("-"):
        try:
            project = DevelopProject.open(args[0])
        except Exception as exc:  # noqa: BLE001
            print(f"Složku nelze otevřít: {exc}", file=sys.stderr)
            return 2
    win = MainWindow(project=project)
    win.show()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
