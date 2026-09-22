"""Samostatné GUI vyvolávače: hustotní archiv + pozitivní render.

``uv run filmscan-develop-gui [složka_projektu]``

Vrstvy jsou ty ze návrhových dokumentů (``Documentation_image_processing/``):

* measurement  -- :mod:`filmscan_studio.core.density` přes
  :class:`~filmscan_studio.developer.project.DevelopProject` (hustota se
  počítá jednou na snímek a cachuje),
* rendering    -- :mod:`filmscan_studio.core.render`; slidery mění jen
  :class:`RenderParams` a render přepočítává z cachované hustoty.

Výstup je **pozitiv**: bright scene = hustý zákal negativu = světlý pixel.
Pixely mimo měřitelný rozsah (±inf hustoty) i NaN („sem nedosvětlo") se
oříznou na konec stupnice (bílá/černá) — v náhledu bez magentové masky;
přepaly a podexpozice odhalí zaškrtávací **Exposure warning** (overlay
červená = světla, modrá = stíny).
Rendery (náhled i export) se překlopí podle orientace filmu ze `project.json`
(``mirrored_horizontal`` / ``mirrored_vertical`` / ``rotated_180``); hustotní
archiv zůstává v surové orientaci senzoru. **ROI se ukládá v souřadnicích
senzoru** a pro kreslení do otočeného náhledu (i naopak) převádí
``DevelopProject.rect_apply`` — jinak by rámeček při zrcadlení krájel jinde.

Obsluha náhledu: kolečko = přiblížení, tažení = posun, **Shift+tažení =
nakreslení rámčku snímku (ROI)**. Rámček je nutný tam, kde akvizice
nezaznamenala ``image_rect`` do sidecaru (dokument 06): bez něj by se do
archivu počítaly i okraje držáku. Hustotní archiv se ukládá *ořezaný* na
rámček a render pak logicky vychází z něj.

Parametry (Dmin/Dmax, expozice, křivka) jsou **per-snímek** a pamatují se do
``develop_settings.json`` vedle projektu; při přepnutí snímku se nahrají
zase. Úplně první otevření snímku bez historie používá Proposal (auto Dmin
z film base, auto Dmax z p99,9, defaultní přirozená S-křivka).

Za tónovou křivkou následuje zobrazovací gamma 2,2 (``gamma_display``) —
shodná v náhledu i v exportu (WYSIWYG); export JPEG i TIFF se kvantizuje až
za ní. Hodnota se v UI **nenastavuje**: je definována ICC profilem
Gray Gamma 2.2, který exporty nesou (dokument 07; uživatel 2026-09-21).
Flat pro Capture One zůstává bez křivky, bez gammy i bez profilu — je
lineární v hustotě.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import numpy as np
from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPolygon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QScrollArea,
    QSlider, QSizePolicy, QSplitter, QVBoxLayout, QWidget,
)

from filmscan_studio.core import density as dens
from filmscan_studio.core import exportmeta
from filmscan_studio.core import icc
from filmscan_studio.core import render as rnd
from filmscan_studio.core.filmic import FilmicProfile
from filmscan_studio.developer.project import DevelopProject

log = logging.getLogger(__name__)

#: Náhled se počítá z podvzorkované hustoty, aby reakce na slidery byla
#: okamžitá. Plné rozlišení vidí jen export.
PREVIEW_MAX_DIM = 1200

#: Barvy overlaye exposure warning: světla (x >= 1, nad dmax) červeně,
#: stíny (x <= 0, pod base) modře. Magentová NaN maska byla pry --
#: NaN (bez světla) je v náhledu černý a koš o něm hlásí statistika.
WARN_HI_COLOR = (255, 40, 40)
WARN_LO_COLOR = (40, 90, 255)


def density_to_qimage(image: np.ndarray,
                      warn_hi: np.ndarray | None = None,
                      warn_lo: np.ndarray | None = None) -> QImage:
    """0..1 float (s NaN) -> QImage; NaN se kreslí černě.

    ``warn_hi``/``warn_lo`` jsou volitelné bool masky exposure warningu --
    pixelů, které render ořezl na horní/dolní konec stupnice.
    """
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
    if warn_hi is not None:
        rgb[warn_hi] = WARN_HI_COLOR
    if warn_lo is not None:
        rgb[warn_lo] = WARN_LO_COLOR
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
        self._out: np.ndarray | None = None
        self._dens: np.ndarray | None = None
        self._warn: tuple[np.ndarray, np.ndarray] | None = None
        self._warn_on = False
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
                  densities: np.ndarray | None = None,
                  warn: tuple[np.ndarray, np.ndarray] | None = None,
                  warn_on: bool = False) -> None:
        """Podvzorek náhledu 0..1 (s NaN) + rozměry mapy, ze které vzešel.

        ``densities`` je týž podvzorek v hustotě D (před renderem) -- aby
        hover mohl hlásit obě čísla; má stejný tvar jako ``image``.
        ``warn`` je (světla, stíny) bool maska exposure warningu -- masky se
        drží spolu s obrazem, aby překlop přepínače nepřepočítal render.
        """
        self._map_wh = map_wh
        self._out = None if image is None else np.asarray(image, np.float64)
        self._dens = (None if densities is None
                      else np.asarray(densities, np.float64))
        self._warn = warn
        self._warn_on = bool(warn_on)
        self._image = None if image is None else self._compose()
        self._recompute_rect_px()
        self._fit()
        self.update()

    def _compose(self) -> QImage:
        assert self._out is not None
        hi = self._warn[0] if (self._warn_on and self._warn) else None
        lo = self._warn[1] if (self._warn_on and self._warn) else None
        return density_to_qimage(self._out, hi, lo)

    def set_warning(self, on: bool) -> None:
        """Překlopí overlay bez nového renderu (masky už spočítané)."""
        self._warn_on = bool(on)
        if self._out is None:
            return
        self._image = self._compose()
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
    * bílá křivka -- diagnostika tónové mapy: kam který D dopadá v lineárním
      pozitivu (0 = černá, 1 = bílá), včetně expozice a stínového pásu.
      BEZ zobrazovací gammy (dokument 07): zobrazovací transfer patří do
      dolního výstupního histogramu a do náhledu, ne do tvaru S-křivky --
      jinak v ní uživatel vidí hrb monitorové charakteristiky.

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
        self.setMinimumHeight(100)
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
            # Promítnutá křivka: jaký D -> jaký tisk (vč. expozice i stínového
            # pásu; positive_x je jedný zdroj pravdy pro obě osy). Jen DO
            # lineárního pozitivu -- bez display gammy (dokument 07): bílá
            # křivka je diagnostika tónové mapy, monitorový transfer patří
            # do dolního histogramu a náhledu.
            p.setPen(QPen(QColor(255, 255, 255), 2))
            xs = np.linspace(self._xmin, self._xmax, 256)
            out = rnd.render_density(xs, self._params)
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

    def stats_text(self) -> str:
        """Číselné konto pod histogramem: percentily + koš na kolejnicích.

        Vytaženo z překreslení do samostatného labelu — histogram je v pravém
        panelu malý a text přes data by byl nečitelný.
        "přepal" = D -inf (senz. na maximu) -> v pozitivu černá;
        "hustší než škála" = D +inf -> v pozitivu bílá.
        """
        s = self._stats
        if not s or not np.isfinite(s.get("d_p50", float("nan"))):
            return "—"
        return (f"D min/p50/p99 {self._fmt1(s['d_min'])}/"
                f"{self._fmt1(s['d_p50'])}/{self._fmt1(s['d_p99'])}"
                f" · přepal(→černá) {self._neg_inf_fraction * 100:.2f} %"
                f" · nad škálu(→bílá) {self._pos_inf_fraction * 100:.2f} %"
                f" · bez světla {self._nan_fraction * 100:.1f} %")

    @staticmethod
    def _fmt1(v: float) -> str:
        return f"{v:.2f}".replace(".", ",")


class OutputHistogramWidget(QWidget):
    """Histogram výstupu 0..1 — jak vypadá vyvolaný obraz, ne film.

    D histogram výše odpovídá „kde film má data"; tenhle „co z toho zbylo po
    křivce a gammě". Počítá se jen z výřezu (ROI) — mimo rámček nic neexportu-
    jeme. Ořez na koncích stupnice (saturace bílá / podčerně) měříme výhradně
    na ose renderu x, tedy ve stejné doméně jako overlay exposure warningu —
    ne na hodnotě po gammě: páčky jas/kontrast uříznou display na 1,0 i když
    křivka ani zdaleka nedosáhla bílé, a histogram by křičel „přepálená
    bílá", kde overlay nemá jedinou červenou. Data končí v otevřených binech,
    takže signál s maximem 0,98 už u pravé hrany nedělá falešný hřeben.
    Jediné překryv je oranžová kurzorová čára (uživatel 2026-09-21 večer:
    „udělej ještě v dolním histogramu podobnou jezdící oranžovou linku co je
    v horním“ — ruší ranní zákaz překryvů); kolejnice tu pořád nejsou.
    """

    BINS = 96

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._hist: np.ndarray | None = None
        self._hi_pct = 0.0
        self._lo_pct = 0.0
        self._cursor_out: float | None = None   # display hodnota pod kurzorem
        self.setMinimumHeight(90)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)

    def set_output(self, out: np.ndarray | None,
                   x: np.ndarray | None = None) -> None:
        """Render 0..1 z výřezu a `x` — týž výřez na ose křivky *před ořezem*.

        Bez `x` se ořez nedá změřit (display už je oříznutý), pak se počítá
        z hodnot po gammě — proto ho ``rerender`` posílá vždycky společně.
        """
        if out is None:
            self._hist = None
            self.update()
            return
        a = np.asarray(out, dtype=np.float64).ravel()
        finite = np.isfinite(a)
        # Ořez na koncích stupnice se počítá VÝHRADNĚ na ose x — tj. ve stejné
        # doméně jako overlay exposure warningu v náhledu. Hodnota po gammě do
        # toho nepatří: páčky jas/kontrast uříznou display na 1,0 i když se
        # křivka k bílé ani nepřiblížila (x < 1), a histogram by křičel
        # „přepálená bílá", kde overlay nemá jedinou červenou. Světlý pixel
        # 0,98 do saturace taky nepatří — saturace je x >= 1, ne „blízko bílé".
        # NaN („bez světla") se v exportu lijí do černé, proto spadá pod stíny.
        if x is not None:
            xv = np.asarray(x, dtype=np.float64).ravel()
            clip_hi = (xv >= 1.0) | np.isposinf(xv)
            clip_lo = ((xv <= 0.0) & ~np.isnan(xv)) | np.isneginf(xv)
        else:
            # Bez x se ořez změřit nedá (display už je oříznutý) — aspoň
            # odhad z hodnot po gammě; rerender posílá x vždycky.
            clip_hi = (a >= 1.0) | np.isposinf(a)
            clip_lo = ((a <= 0.0) & finite) | np.isneginf(a) | np.isnan(a)
        self._hi_pct = float(clip_hi.mean()) * 100.0 if a.size else 0.0
        self._lo_pct = float(clip_lo.mean()) * 100.0 if a.size else 0.0
        # Saturované pixily (== 1,0) nepatří do posledního datového sloupce,
        # jehož pravá hrana je uzavřeně 1,0 — jinak by hřeben u zdi křičel
        # „bílá!", i když maximum signálu je 0,98; oznamuje je jen text.
        vals = np.clip(a[finite], 0.0, 1.0)
        vals = vals[vals < 1.0]
        if vals.size == 0:
            self._hist = np.zeros(self.BINS)
        else:
            counts, _ = np.histogram(vals, bins=self.BINS, range=(0.0, 1.0))
            peak = float(counts.max())
            self._hist = counts / peak if peak > 0 else np.zeros(self.BINS)
        self.update()

    def set_cursor_output(self, out_val: float | None) -> None:
        """Zobrazí/zmaže oranžovou svislou čáru na výstupu pod kurzorem.

        Hodnota je display 0..1 (táž osa jako histogram); mimoscope hodnoty
        (NaN „bez světla", ±inf) čáru nemažou pozicí — widget nemá co ukázat,
        jde pryč.
        """
        self._cursor_out = (out_val if out_val is not None
                            and np.isfinite(out_val) else None)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor(30, 30, 30))
        if self._hist is None:
            p.setPen(QColor(160, 160, 160))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "histogram výstupu")
            return
        # Data končí na středu posledního binu (osa 0..1), ne na samotné
        # hraně widgetu — mezera za posledním datem je pravda, ne artefakt.
        right = int((self.BINS - 0.5) / self.BINS * (w - 1))
        poly = [(0, h)]
        for i, v in enumerate(self._hist):
            x_i = int(i / max(len(self._hist) - 1, 1) * (right - 1))
            y = h - int(np.log1p(9.0 * float(v)) / np.log1p(9.0) * (h - 16))
            poly.append((x_i, y))
        poly.append((right, h))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(220, 220, 220, 70))
        pts = [QPoint(*pt) for pt in poly]
        p.drawPolygon(QPolygon(pts))
        p.setPen(QColor(220, 220, 220))
        p.drawPolyline(QPolygon(pts[1:-1]))

        # Kurzor: oranžová svislá čára na display hodnotě pixelu pod myší —
        # stejná barva a gesto jako v horním histogramu (osa je ale 0..1,
        # ne hustota). Data mapují na (right-1), takže čára sedí na sloupek.
        if self._cursor_out is not None:
            cx = int(self._cursor_out * (right - 1))
            p.setPen(QPen(QColor(255, 140, 0), 2))
            p.drawLine(cx, 0, cx, h)

        p.setPen(QColor(200, 200, 200))
        p.drawText(6, 12, f"výstup (výřez) · podčerně {self._lo_pct:.1f} %"
                          f" · saturace {self._hi_pct:.1f} %")


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

        # -- střed: náhled ---------------------------------------------------
        centre = QWidget()
        cv = QVBoxLayout(centre)
        cv.setContentsMargins(0, 0, 0, 0)
        self.view = DensityView()
        self.view.rect_chosen.connect(self.rect_selected)
        self.view.hovered.connect(self._pixel_hovered)
        cv.addWidget(self.view, 1)
        splitter.addWidget(centre)

        # -- pravý panel: ladící histogram → stats → výstupní histogram →
        #    status → nastavovací boxy; vše v scroll area (nevejde se) ------
        side = QWidget()
        sv = QVBoxLayout(side)
        self.histogram = DensityHistogramWidget()
        sv.addWidget(self.histogram)
        self.lbl_hist_stats = QLabel("—")
        self.lbl_hist_stats.setWordWrap(True)
        sv.addWidget(self.lbl_hist_stats)
        self.out_histogram = OutputHistogramWidget()
        sv.addWidget(self.out_histogram)
        # (status řádek tu byl — uživatel 2026-09-21 večer „celé to dej pryč":
        # duplikoval stats pod horním histogramem a při hoveru přeskakoval
        # nastavovátka pod sebou. Zpráwy chodí do spodní lišty okna, ta se
        # nikdy nepřeskupuje.)
        self.chk_warn = QCheckBox("Exposure warning (světla červeně, stíny modře)")
        self.chk_warn.toggled.connect(self.view.set_warning)
        sv.addWidget(self.chk_warn)
        sv.addWidget(self._build_scale_box())
        sv.addWidget(self._build_curve_box())
        sv.addWidget(self._build_display_box())
        sv.addWidget(self._build_export_box())
        sv.addStretch(1)
        side_scroll = QScrollArea()
        side_scroll.setWidget(side)
        side_scroll.setWidgetResizable(True)
        side_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        side_scroll.setMinimumWidth(360)
        splitter.addWidget(side_scroll)
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

    @staticmethod
    def _spin(lo: float, hi: float, value: float, step: float,
              decimals: int = 2, suffix: str = "") -> QDoubleSpinBox:
        sp = QDoubleSpinBox()
        sp.setRange(lo, hi)
        sp.setSingleStep(step)
        sp.setDecimals(decimals)
        sp.setValue(value)
        if suffix:
            sp.setSuffix(suffix)
        return sp

    def _bind_row(self, form: QFormLayout, name: str, slider: QSlider,
                  spin: QDoubleSpinBox, scale: float = 100.0) -> None:
        """Řádek «jezdec | editovatelné pole» — hodnota je napravo, ne pod.

        Obousměrné spojení v celých číslech: obě páčky mají rozlišení 1/scale,
        takže se navzájem rozkmitat nemohou (setValue bez změny signál pošle
        až na výstřel, ten druhý setValue už změnu nevidí).

        Jezdec má od reorganizace 08 *běžný* rozsah, pole nad ním (kolena
        do 1,66, gamma do 4,0). Hodnota mimo jezdec se nesmí ztratit: pole si
        ji drží, jezdec se jen zaparkuje na kraj bez zpětného přepsání — jinak
        by načtené starší nastavení (toe 1,0) řetězec jezdec→pole ořízl na
        hranu jezdce (0,8) a tiše přepsal export i uložená nastavení.
        """

        def s2p(v: int) -> None:
            spin.setValue(v / scale)

        def p2s(v: float) -> None:
            target = int(round(v * scale))
            if slider.minimum() <= target <= slider.maximum():
                slider.setValue(target)
            else:
                was = slider.blockSignals(True)
                slider.setValue(max(slider.minimum(),
                                    min(target, slider.maximum())))
                slider.blockSignals(was)

        slider.valueChanged.connect(s2p)
        spin.valueChanged.connect(p2s)
        spin.valueChanged.connect(self._setting_changed)
        row = QHBoxLayout()
        row.addWidget(slider, 1)
        row.addWidget(spin)
        form.addRow(name, row)

    def _build_scale_box(self) -> QGroupBox:
        """Meritko filmu (dok 08 §5): Dmin, Dmax, tolerance pod Dmin.

        Dmin/Dmax nejsou kontrastové ovladače — určují, jaká část hustotní
        osy filmu se mapuje do výstupu (08 §1). Kontrast je až křivka níž.
        """
        box = QGroupBox("Meritko filmu")
        form = QFormLayout(box)
        self.spin_dmin = self._spin(0.0, 2.0, 0.2, 0.01, decimals=3)
        self.chk_dmin_auto = QCheckBox("auto: z měření film base")
        self.chk_dmin_auto.setChecked(True)
        self.spin_dmax = self._spin(0.2, 5.0, 2.6, 0.05, decimals=3)
        # Stav i přepínač v jedné masce (08 §5): auto je PRACOVNÍ návrh
        # p99,9 + 0,05 D, ne vlastnost emulze; odškrtnutím ho převezmeš ručně.
        self.chk_dmax_auto = QCheckBox("auto: návrh p99,9 + 0,05 D")
        self.chk_dmax_auto.setChecked(True)
        # Tolerance pod Dmin (shadow band) rozšiřuje definiční obor křivky POD
        # Dmin — co se slilo do černé už nevytáhne, ale gradient mléka mezi
        # prahem měření a Dmin zůstane. Není to druhé Dmin (08 §3 krok 6).
        # Jezdec jen běžné ladění 0,00–0,08 D; pole pojme i starší uložené
        # hodnoty až 0,30 — při načtení se nesmí tiše oříznout (data už
        # exportovaná s 0,30 musí zůstat reprodukovatelná).
        self.sl_sb = self._slider(0, 8, 1)            # 0,00 … 0,08 D
        self.spin_sb = self._spin(0.0, 0.30, 0.01, 0.01, decimals=2)
        # Výstředník: interně 1/100 EV (jemný krok), jezdec ladí po 0,05,
        # textové pole pojme i hodnotu mimo krok (0,15 = 3 ticky).
        self.sl_ev = self._slider(-600, 600, 0)     # ±6 EV
        self.sl_ev.setSingleStep(5)                 # 0,05 EV
        self.sl_ev.setPageStep(20)                  # 0,2 EV (kolečko/klik do dráhy)
        self.spin_ev = self._spin(-6.0, 6.0, 0.0, 0.05, suffix=" EV")
        self.btn_defaults = QPushButton("Proposal (auto body, přirozená křivka)")
        self.btn_defaults.clicked.connect(self.apply_defaults)
        form.addRow("Dmin [D]", self.spin_dmin)
        form.addRow("", self.chk_dmin_auto)
        form.addRow("Dmax [D]", self.spin_dmax)
        form.addRow("", self.chk_dmax_auto)
        self._bind_row(form, "Tolerance pod Dmin [D]", self.sl_sb, self.spin_sb)
        self._bind_row(form, "Expozice", self.sl_ev, self.spin_ev)
        form.addRow("", self.btn_defaults)

        self.spin_dmin.setToolTip("Čiré podloží = nejčernější pozitiv. "
                                  "Bez měření film base je ruční hodnota jen "
                                  "relativní odhad (08 §3).")
        self.spin_dmax.setToolTip("Pracovní maximum hustoty: posuň tak, aby "
                                  "obsáhlo nejhustší užitečné obrazové hodnoty "
                                  "z horního histogramu, ale zbytečně "
                                  "nesahalo daleko za ně (08 §3 krok 2).")
        self.spin_sb.setToolTip("Tolerance měření pod Dmin — mírně podbase "
                                "hodnoty nesplývají do jedné černě. "
                                "Nevrací detail fyzicky ořezaný; vyšší hodnota "
                                "zvedá a vyprává černou (08 §3 krok 6).")

        self.spin_dmin.valueChanged.connect(self._setting_changed)
        self.spin_dmax.valueChanged.connect(self._setting_changed)
        self.chk_dmin_auto.toggled.connect(self._dmin_toggled)
        self.chk_dmax_auto.toggled.connect(self._scale_toggled)
        self.spin_dmin.setEnabled(False)   # auto zapnuto
        return box

    def _build_curve_box(self) -> QGroupBox:
        """Fotografická křivka (dok 08 §2): Toe / Kontrast středu / Rameno.

        Rozsahy jezdce jsou *běžné ladění* (08 §2): toe/shoulder 0,00–0,80,
        gamma 0,80–2,50. Model i textová pole ponechávají nadrazec — kolena
        až 1,666 (= kotva 0,25 / dráha 0,15: koleno dosáhne konce a daný
        konec se stlačí na šepot, stále monotónně), gamma 0,10–4,00, aby se
        hodnota ze starých uložených nastavení nenačetla tiše oříznutá.
        Nad rozsah jezdce je jen speciální komprese konců, ne fotografie.
        """
        box = QGroupBox("Tónová křivka")
        form = QFormLayout(box)
        self.sl_toe = self._slider(0, 80, 20)
        self.spin_toe = self._spin(0.0, 1.66, 0.20, 0.01)
        self.sl_gamma = self._slider(80, 250, 135)
        self.spin_gamma = self._spin(0.10, 4.0, 1.35, 0.01)
        self.sl_shoulder = self._slider(0, 80, 20)
        self.spin_shoulder = self._spin(0.0, 1.66, 0.20, 0.01)
        self._bind_row(form, "Patka (toe)", self.sl_toe, self.spin_toe)
        # „Kontrast středu (gamma)", ne „sklon křivky": kombinovaná křivka má
        # kvůli spline před gamma krokem vlastní sklon (dok 07 §7); název
        # podle dok 08 §5 nahrazuje dřívější „Středový kontrast".
        self._bind_row(form, "Kontrast středu (gamma)", self.sl_gamma,
                       self.spin_gamma)
        self._bind_row(form, "Rameno (shoulder)", self.sl_shoulder,
                       self.spin_shoulder)
        self.spin_toe.setToolTip("Komprese spodního konce — vyšší hodnota "
                                 "stlačí stíny, NENÍ záchrana ztracených "
                                 "pixelů (08 §3 krok 4).")
        self.spin_gamma.setToolTip("Hlavní ovladač oddělení stínů a světel: "
                                   "1,00 téměř lineární, 1,35–1,70 běžný "
                                   "kontrast, nad 2,20 tvrdý speciál "
                                   "(08 §2, začni kolem 1,35).")
        self.spin_shoulder.setToolTip("Komprese horního konce — jemné gradace "
                                      "před bílým ořezem. Než ji zvedneš, "
                                      "zkontroluj Dmax (08 §3 krok 5).")
        return box

    def _build_display_box(self) -> QGroupBox:
        """Zobrazovací přenos za křivkou — technický, ne kreativní (08 §5).

        Display gamma je pevných 2,2 daných profilem Gray Gamma 2.2 — v UI
        žádná páčka (uživatel 2026-09-21: „odstranit možnost výstupní gammu
        upravovat — bude definována profilem") a při tónování se nemá měnit
        (08 §2). Náhled i export použijí totéž — WYSIWYG, export navíc ponese
        ICC profil se stejnou TRC.
        """
        box = QGroupBox("Zobrazení")
        form = QFormLayout(box)
        # Jas a kontrast v display prostoru (za gammou) — Photoshop zvyk.
        # kontrast kolem zobrazené střední šedi 0,5; jas posun. Nejsou
        # expozice: ta sahá na hustotní osu (co film viděl).
        self.sl_br = self._slider(-50, 50, 0)         # −0,50 … +0,50
        self.spin_br = self._spin(-0.5, 0.5, 0.0, 0.01)
        self.sl_ct = self._slider(10, 400, 100)       # 0,10 … 4,00
        self.spin_ct = self._spin(0.1, 4.0, 1.0, 0.01)
        form.addRow("Display gamma", QLabel("2,2 — dáno profilem"))
        self._bind_row(form, "Jas (display)", self.sl_br, self.spin_br)
        self._bind_row(form, "Kontrast (display)", self.sl_ct, self.spin_ct)
        return box

    #: Formáty exportu: (popisek combo, metoda). Popisky říkají bitovou hloubku
    #: a barevný prostor — gray cesty nesou Gray Gamma 2.2 (dok 07), RGB cesty
    #: kompatibilní sRGB (uživatel 2026-09-21: „místo několika čudlíů
    #: rozbalovací seznam a vedle export" + „přidej jpeg v sRGB a 10bit heic").
    EXPORT_FORMATS = (
        ("Pozitiv — TIFF 16b gray (Gray Gamma 2,2)", "save_render"),
        ("JPEG 8b gray (Gray Gamma 2,2)", "save_jpeg"),
        ("JPEG 8b sRGB (kompatibilita)", "save_jpeg_srgb"),
        ("HEIC 10b sRGB (Apple, RGB)", "save_heic"),
        ("HEIF 10b mono (Gray Gamma 2,2)", "save_heic_mono"),
        ("Flat pro Capture One — TIFF 16b", "save_flat"),
        ("Hustotní archiv — TIFF 32b", "save_density"),
    )

    def _build_export_box(self) -> QGroupBox:
        box = QGroupBox("Export")
        h = QVBoxLayout(box)
        # Jeden seznam + jedno tlačítko místo čtyř čudlíů (uživatel
        # 2026-09-21). Combo nese i formáty, které nejsou display-referred
        # (flat, archiv) — pravidla pro ICC/encoding mají vlastní, ta
        # vyřizuje metoda, ne výběr.
        self.cmb_export = QComboBox()
        self.cmb_export.addItems([label for label, _ in self.EXPORT_FORMATS])
        self.btn_export = QPushButton("Export")
        self.btn_export.clicked.connect(self.export_current)
        self.btn_export.setEnabled(False)
        row = QHBoxLayout()
        row.addWidget(self.cmb_export, 1)
        row.addWidget(self.btn_export)
        h.addLayout(row)
        return box

    def export_current(self) -> None:
        """Volba ze seznamu → příslušná exportová metoda."""
        _label, method = self.EXPORT_FORMATS[self.cmb_export.currentIndex()]
        getattr(self, method)()

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
            self.statusBar().showMessage(f"Chyba měření {name}: {exc}")
            return
        d, prov = self._density
        self._current = name
        if getattr(prov, "flat_fallback", False):
            # Varování z bývalého status řádku — spodní lišta se nepřeskupuje
            # a hover ji nepřepíše (zpráwy pixelu z ní odešly taky).
            self.statusBar().showMessage(
                "⚑ bez flat snímku — náhled je relativní (k nejjasnějším "
                "0,1 %); Dmin zadej ručně")
        self._loading = True
        try:
            # Uložený ROI je v souřadnicích senzoru; náhled je otočený —
            # rámeček se kreslí převrácený, aby seděl na to, co vidíš.
            self.view.set_rect(self.project.rect_apply(
                self.project.rect_for(self.project.entry(name)),
                (d.shape[1], d.shape[0])))
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
        self.histogram.set_density(d)
        self.lbl_hist_stats.setText(self.histogram.stats_text())
        self.btn_export.setEnabled(True)
        self.rerender()

    # ----------------------------------------------------------- parametry

    def _proposal(self, name: str) -> rnd.RenderParams:
        """První náhled snímku bez historie: auto body + přirozená křivka.

        Výchozí S-ko je pracovní start dok 08 (toe 0,20 / gamma 1,35 /
        shoulder 0,20): jemný přirozený kontrast, kolena jen lehce zabraná —
        patka a rameno komprimují konce, nenahrazují špatně nastavené
        Dmin/Dmax. (Starší negadoctor-svah 0,35/1,10/0,45 nahrazen
        2026-09-21 reorganizací 08.)"""
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
            profile=FilmicProfile(),
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
        self.spin_toe.setValue(params.profile.toe)
        self.sl_gamma.setValue(int(round(params.profile.gamma * 100)))
        self.spin_gamma.setValue(params.profile.gamma)
        self.sl_shoulder.setValue(int(round(params.profile.shoulder * 100)))
        self.spin_shoulder.setValue(params.profile.shoulder)
        # gamma_display nema ovladac — je dana profilem (icc.GAMMA_DISPLAY)
        self.sl_sb.setValue(int(round(params.shadow_band * 100)))
        self.spin_sb.setValue(params.shadow_band)
        self.sl_br.setValue(int(round(params.brightness * 100)))
        self.spin_br.setValue(params.brightness)
        self.sl_ct.setValue(int(round(params.contrast * 100)))
        self.spin_ct.setValue(params.contrast)
        self.spin_dmin.setEnabled(not self.chk_dmin_auto.isChecked())
        self.spin_dmax.setEnabled(not self.chk_dmax_auto.isChecked())

    def current_params(self) -> rnd.RenderParams:
        return rnd.RenderParams(
            dmin=self.spin_dmin.value(),
            dmax=self.spin_dmax.value(),
            exposure_ev=self.spin_ev.value(),
            profile=FilmicProfile(toe=self.spin_toe.value(),
                                  gamma=self.spin_gamma.value(),
                                  shoulder=self.spin_shoulder.value()),
            dmax_source="frame" if self.chk_dmax_auto.isChecked()
            else "manual",
            # Gamma display se nenastavuje: drzi se kroky ICC profilu
            # (Gray Gamma 2,2), ktery exporty nesou. Ulozene hodnoty z drivych
            # sezeni se pri nacteni prepisou — kontrakt nadejsi.
            gamma_display=icc.GAMMA_DISPLAY,
            shadow_band=self.spin_sb.value(),
            brightness=self.spin_br.value(),
            contrast=self.spin_ct.value(),
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
        """Vehne snímku Proposal (auto body, přirozená křivka) -- i později."""
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
        d, prov = self._density
        params = self.current_params()
        # Náhled vidí orientaci diváka + otočení snímku z anotátoru;
        # archiv zůstává surový.
        sub = _subsample(self.project.rotate_frame(
            self.project.orientation_apply(d), prov.source), PREVIEW_MAX_DIM)
        display = rnd.render_for_display(sub, params)
        # Exposure warning: co render ořízl na konce stupnice. Měří se na
        # čisté ose x před ořezem -- NaN (bez světla) do neither koše.
        x = rnd.positive_x(sub, params)
        hi = (x >= 1.0) | np.isposinf(x)
        lo = ((x <= 0.0) & ~np.isnan(x)) | np.isneginf(x)
        # _subsample je stride-sám o sobě deterministický: display[i,j]
        # pochází z sub[i,j], takže kurzor vidí týž pixel v D i v renderu.
        self.view.set_image(display, (d.shape[1], d.shape[0]), densities=sub,
                            warn=(hi, lo),
                            warn_on=self.chk_warn.isChecked())
        self.histogram.set_params(params)
        self._update_output_histogram(display, x)

    def _update_output_histogram(self, display: np.ndarray,
                                 x: np.ndarray) -> None:
        """Výstupní histogram jen z výřezu — exportuje se přece taky jen on.

        Ořez se počítá z `x` (osa křivky před clipem), ne z `display`: po
        gammě je 0,98 světlý pixel, ne saturace, a štěrbina clipu by ji
        namalovala jako hřeben u pravé hrany.
        """
        rect = self.view.roi        # displayed whole-frame souřadnice
        if rect is None:
            self.out_histogram.set_output(display, x=x)
            return
        h, w = display.shape
        fx, fy = w / max(self._whole_wh()[0], 1), h / max(self._whole_wh()[1], 1)
        x0, y0, x1, y1 = rect
        sl = (slice(int(y0 * fy), max(int(y1 * fy), 1)),
              slice(int(x0 * fx), max(int(x1 * fx), 1)))
        crop = display[sl]
        self.out_histogram.set_output(crop if crop.size else None, x=x[sl])

    def _whole_wh(self) -> tuple[int, int]:
        assert self._density is not None
        h, w = self._density[0].shape
        return w, h

    def rect_selected(self, rect: tuple[int, int, int, int]) -> None:
        """ROI z Shift+kreslení: přepočítá měření (archiv se krájí) i status.

        Rámeček přišel v souřadnicích otočeného náhledu; senzorový
        (uložitelný) tvar dá týž ``rect_apply`` — zrcadlení i rotace 180°
        jsou involuce, převod je sám sobě invertní.
        """
        item = self.frame_list.currentItem()
        if item is None or self.project is None:
            return
        stored = self.project.rect_apply(rect, self._whole_wh())
        assert stored is not None
        self.project.set_rect(item.text(), stored)
        self.project.save_settings()      # rámček přežije reopen
        try:
            self._density = self.project.build_density(item.text(),
                                                       crop=False)
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"Chyba měření: {exc}")
            return
        self.histogram.set_density(self._density[0])
        self.lbl_hist_stats.setText(self.histogram.stats_text())
        self.rerender()

    # -------------------------------------------------------------- status

    def _pixel_hovered(self, payload) -> None:
        """Kurzor v náhledu: oranžová čára v obou histogramech.

        ``payload`` je (D, out) z :class:`DensityView`, nebo None mimo snímek
        -- pak se čáry smažou. Žádný text: numerický status řádek v pravém
        panelu byl odstraněn (uživatel 2026-09-21 večer — duplikoval stats
        a při hoveru přeskakoval nastavovátka pod sebou).
        """
        if payload is None:
            self.histogram.set_cursor_density(None)
            self.out_histogram.set_cursor_output(None)
            return
        d_val, out_val = payload
        self.histogram.set_cursor_density(
            None if not np.isfinite(d_val) else d_val)
        self.out_histogram.set_cursor_output(
            None if not np.isfinite(out_val) else out_val)

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

    # -------------------------------------------------------------- export

    def _derived_dir(self) -> Path:
        assert self.project is not None
        out = self.project.root / "derived"
        out.mkdir(exist_ok=True)
        return out

    def _out_name(self, stem: str, suffixes: str) -> str:
        """Jméno exportu: prefix filmu + stem + přípony.

        Povel 2026-09-22: „do exportů přidej první část názvu identifikátoru:
        z 'K16O04_2026-07-22' udělej název 'K16O04_frame001.mono10.heic'".
        Prefix je první část film_id před podtržítkem; bez film_id zůstává
        staré jméno (testy i archivy bez filmu se nemusí měnit)."""
        assert self.project is not None
        prefix = self.project.export_prefix
        return f"{prefix}_{stem}.{suffixes}" if prefix else \
            f"{stem}.{suffixes}"

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
            self._derived_dir() / self._out_name(stem, "density.tif"), d, prov)
        self.statusBar().showMessage(f"Uloženo: {path}", 8000)

    def save_render(self) -> None:
        self._export_render(flat=False)

    def save_flat(self) -> None:
        self._export_render(flat=True)

    def _display_render(self) -> tuple[np.ndarray, rnd.RenderParams, str] \
            | None:
        """Společný základ display-referred exportů: ROI → orientace → render.

        Vrací (display 0..1 s NaN, params, stem) nebo None. Uloží nastavení,
        aby export i metadata sdílely tytéž parametry. NaN nechává na
        volajícím — každý formát ho lijí do černé jinak (8b rint, 10b posun).
        """
        cropped = self._cropped_density()
        if cropped is None or self.project is None:
            return None
        d, prov = cropped
        # Uložená orientace + otočení snímku: totéž co vidí náhled. Archiv
        # above flipem zůstává.
        d = self.project.rotate_frame(self.project.orientation_apply(d),
                                      prov.source)
        params = self.current_params()
        self.project.frame_settings[prov.source] = self._settings_payload()
        self.project.save_settings()
        display = rnd.render_for_display(d, params)
        return display, params, Path(prov.source).stem

    def _export_metadata(self, stem: str) -> tuple[bytes | None,
                                                   bytes | None]:
        """EXIF + XMP byty ze sidecaru snímku (kontrakt dok. 09).

        `stem` je ze `_display_render()` — FrameEntry se jmenuje i s příponou,
        proto shoda podle `Path(name).stem`. Žádný record → (None, None) a
        export proběhne beze změny; vlastní chyba nikdy nezahodí export —
        metadata jsou bonus, ne podmínka."""
        if self.project is None:
            return None, None
        entry = next((f for f in self.project.frames
                      if Path(f.name).stem == stem), None)
        if entry is None:
            return None, None
        try:
            return (exportmeta.build_exif_bytes(entry.record),
                    exportmeta.build_xmp_bytes(entry.record))
        except Exception:                       # nepolevit z exportu kvůli EXIFu
            log.exception("EXIF metadata selhala, exportuji bez nich")
            return None, None

    def save_jpeg(self) -> None:
        """8b gray JPEG = totéž co náhled (vč. gammy 2,2) + ICC profil.

        Profil se jen ZAPÍŠE (APP2), pixely se nepřevádějí — gamma už proběhla
        právě jednou v apply_display(). Skutečný jednokanálový gray (režim L),
        ne BGR se třemi identickými kanály jako kdysi přes cv2.
        """
        got = self._display_render()
        if got is None:
            return
        display, params, stem = got
        # NaN = „bez světla" -> černá (nan_fill); JPEG nezná NaN.
        data = np.rint(np.where(np.isfinite(display), display, 0.0)
                       * 255.0).astype(np.uint8)
        out = self._derived_dir() / self._out_name(stem, "jpg")
        profile = icc.profile_for(params.gamma_display)
        exif, xmp = self._export_metadata(stem)
        icc.write_gray_jpeg(out, data, profile, exif=exif, xmp=xmp)
        self.statusBar().showMessage(
            f"Exportováno: {out.name} · fingerprint {params.fingerprint()}"
            f" · {icc.PROFILE_NAME}"
            + (" · EXIF+XMP vloženo" if exif or xmp else " · bez anotací"))

    def save_jpeg_srgb(self) -> None:
        """8b RGB JPEG — gray pixely jako R=G=B, kompatibilní sRGB profil.

        Pro čtečky/prohlížeče, se kterými single-channel gray JPEG dělá potíže.
        Pixely se NEPŘEVÁDĚJÍ do sRGB gammy: gamma 2,2 z apply_display() už v
        nich je, R=G=B je achromatické (primáry nejsou co rozmotat), profil je
        kompatibilní obal. Odliší se od gray cesty jen příponou _srgb.
        """
        got = self._display_render()
        if got is None:
            return
        display, params, stem = got
        g = np.rint(np.where(np.isfinite(display), display, 0.0)
                    * 255.0).astype(np.uint8)
        rgb = np.dstack([g, g, g])
        out = self._derived_dir() / self._out_name(stem, "srgb.jpg")
        profile = icc.build_srgb_profile()
        exif, xmp = self._export_metadata(stem)
        icc.write_srgb_jpeg(out, rgb, profile, exif=exif, xmp=xmp)
        self.statusBar().showMessage(
            f"Exportováno: {out.name} · fingerprint {params.fingerprint()}"
            f" · {icc.SRGB_PROFILE_NAME}"
            + (" · EXIF+XMP vloženo" if exif or xmp else " · bez anotací"))

    def save_heic(self) -> None:
        """10b RGB HEIC — hlubší bitová hloubka, Apple/ProApps kompatibilní.

        Totéž renderování co JPEG, jen 10 bitů/kanál přes HEVC (pillow-heif),
        R=G=B + sRGB profil. Apple monochrom HEIC sice zvládá, ale RGB je pro
        uživatele jistota napříč čtečkami (2026-09-21).

        Pozor na konvenci `pillow_heif.encode("RGB;16", ...)`: 16b vstup se
        bere jako MAX-ŠKÁLOVANÝ (0..65535 → 0..2^bit_depth); enkoder sám
        škáluje `>>6` na 10b. Dřívější ruční `>>6` předání hodnot 0..1023
        enkoder posunul podruhé → HEVC uložil jen ~1 % světla a náhledy
        byly rozbité (uživatel 2026-09-21: „heic zrovnatak“).
        """
        got = self._display_render()
        if got is None:
            return
        display, params, stem = got
        q16 = rnd.quantise16(display)                 # 0..65535, NaN -> černá
        rgb = np.dstack([q16, q16, q16]).astype(">u2")  # pillow_heif sám škáluje
        out = self._derived_dir() / self._out_name(stem, "srgb10.heic")
        profile = icc.build_srgb_profile()
        exif, xmp = self._export_metadata(stem)
        icc.write_srgb_heic(out, rgb, profile, exif=exif, xmp=xmp)
        self.statusBar().showMessage(
            f"Exportováno: {out.name} · fingerprint {params.fingerprint()}"
            f" · {icc.SRGB_PROFILE_NAME} 10b"
            + (" · EXIF+XMP vloženo" if exif or xmp else " · bez anotací"))

    def save_heic_mono(self) -> None:
        """10b monochromatické HEIF — skutečný single-channel, ne R=G=B.

        Uživatel 2026-09-21: „přidej 10bit heif mono“. Apple to zvládá (ověřeno:
        pixi 1 kanál @ 10 bpc, ColorSync s Gray Gamma 2.2 profilem kreslí
        správně). Menší než RGB cesta (třetinová data), plně gray — totéž
        renderování co JPEG, 16b quantise → enkoder sám škáluje na 10b.
        """
        got = self._display_render()
        if got is None:
            return
        display, params, stem = got
        q16 = rnd.quantise16(display)                 # 0..65535, NaN -> černá
        out = self._derived_dir() / self._out_name(stem, "mono10.heic")
        profile = icc.profile_for(params.gamma_display)
        exif, xmp = self._export_metadata(stem)
        icc.write_mono_heic(out, q16, profile, exif=exif, xmp=xmp)
        self.statusBar().showMessage(
            f"Exportováno: {out.name} · fingerprint {params.fingerprint()}"
            f" · {icc.PROFILE_NAME} 10b"
            + (" · EXIF+XMP vloženo" if exif or xmp else " · bez anotací"))

    def _export_render(self, flat: bool) -> None:
        cropped = self._cropped_density()
        if cropped is None or self.project is None:
            return
        d, prov = cropped
        # Uložená orientace + otočení snímku: totéž co vidí náhled. Archiv
        # above flipem zůstává.
        d = self.project.rotate_frame(self.project.orientation_apply(d),
                                      prov.source)
        params = self.current_params()
        self.project.frame_settings[prov.source] = self._settings_payload()
        self.project.save_settings()
        # Pozitiv včetně zobrazovacího přenosu = přesně to, co vidí náhled
        # (WYSIWYG). Flat zůstává bez křivky i bez gammy — grading od nuly.
        full = (rnd.render_flat(d, params) if flat
                else rnd.render_for_display(d, params))
        # NaN = „bez světla" -> černá; ±inf hustoty už render ořezal na 0/1.
        data = rnd.quantise16(full)
        stem = Path(prov.source).stem
        kind = "flat" if flat else "positive"
        out = self._derived_dir() / self._out_name(stem, f"{kind}.tif")
        # Kontrakt dokumentu 07: display-referred pozitiv nese ICC profil
        # Gray Gamma 2.2 (stejná TRC jako apply_display — žádná druhá gamma
        # do pixelů, profil jen říká čtečkám, jak data interpretovat). Flat
        # profil NENESÉ — je lineární v hustotě a profil by o datech lhal;
        # neliší se jen absencí profilu, ale i encoding v metadatech.
        encoding = (icc.ENCODING_LINEAR_DENSITY if flat
                    else icc.ENCODING_GRAY_GAMMA_22)
        profile = None if flat else icc.profile_for(params.gamma_display)
        meta = {
            "magic": "filmscan-render",
            "kind": kind,
            "encoding": encoding,
            "parameters": params.to_dict(),
            "fingerprint": params.fingerprint(),
            "density_source": self._out_name(stem, "density.tif"),
        }
        if profile is not None:
            meta["icc_profile"] = icc.PROFILE_NAME
            meta["icc_profile_fingerprint"] = icc.profile_fingerprint(profile)
        icc.write_gray_tiff(out, data, profile,
                            description=json.dumps(meta, ensure_ascii=False))
        extra = f" · {icc.PROFILE_NAME}" if profile is not None else ""
        self.statusBar().showMessage(
            f"Exportováno: {out.name} · fingerprint {params.fingerprint()}"
            f"{extra}")


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
