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

Obsluha náhledu (rozkaz 2026-09-22 noc): **pinch dvěma prsty = zoom,
kolečko i dvouscroll = posun**, tažení myší = posun, **Shift+tažení =
nakreslení rámčku snímku (ROI)**. Zoom nikdy neklesne pod fit-to-screen;
záhlaví nad náhledem hlásí zoom a hodnotu pixelu pod kurzorem a nabízí
tlačítka Fit / 100 % (a přepínač plného rozlišení pro posuzování sharpenu).
Rámček je nutný tam, kde akvizice
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
Posledním krokem je volitelné **ostření** (unsharp mask ovládaný jako ve
Photoshopu: Množství v % 0–200, Poloměr v px; konzervativní předvolba
20 % / 1 px; rozkazy 2026-09-22), rovněž shodné v náhledu i exportu. Flat
pro Capture One zůstává bez křivky, bez gammy i bez profilu — je lineární
v hustotě a bez ostření.

Exportní panel (rozkaz 2026-09-22): kromě jednoho „Export" je i **„Export
vše"** (všechny snímky aktuálně vybraným formátem, každý se svými uloženými
parametry) a zaškrtátko **„Export včetně okraje filmu"** s počtem px (100
implicitně) — přidá kraj z filmu kolem ROI (v výměněch, kam to sahá; jinak
„po kraj"). Hustotní archiv okraj nikdy nedostává: je to měření, ne fotka.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QIcon, QImage, QPainter, QPen, QPixmap, QPolygon)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QScrollArea,
    QSlider, QSizePolicy, QSpinBox, QSplitter, QVBoxLayout, QWidget,
)

from filmscan_studio.core import density as dens
from filmscan_studio.core import exportmeta
from filmscan_studio.core import icc
from filmscan_studio.core import render as rnd
from filmscan_studio.core.filmic import FilmicProfile
from filmscan_studio.developer.project import DevelopProject

log = logging.getLogger(__name__)

#: Náhled se počítá z podvzorkované hustoty, aby reakce na slidery byla
#: okamžitá. Přepínačem 1:1 v záhlaví se podvzorek vypne (rozkaz 2026-09-22:
#: sharpening byl v podvzorku neviditelný); plné rozlišení viděl dřív jen export.
PREVIEW_MAX_DIM = 1200

#: Velikost miniatur v levém seznamu (rozkaz 2026-09-22: „nejen textově,
#: ale malé náhledy pod sebou"). Generují se lenivě ze surových dat v RAM —
#: bez hustoty, bez křivky — jsou to orientační náhledy, ne výstupy.
THUMB_ICON = QSize(88, 60)
THUMB_MAX_DIM = 160

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
    #: Zoom se změnil (kolečkem, pinchem, tlačítkem, resize) -- pro záhlaví.
    zoom_changed = Signal(float)

    #: Spodní mez je fit-to-screen (počítá se za chodu, nikdy pod něj);
    #: strop 16x -- rozumná mez pro pixelovou kontrolu sharpenu
    #: (uživatel 2026-09-22: „nenech zmenšovat se pod fit to screen").
    MAX_ZOOM = 16.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image: QImage | None = None
        #: Rozměry *celého* (příp. oříznutého) snímku, pro převod ROI.
        self._map_wh: tuple[int, int] | None = None
        self._zoom = 1.0
        #: User ručně přiblížil nad fit; resize pak zoom nenechá zaniknout
        #: (jen ořízne na nový fit). Nový snímek / jiný rozměr obrazu ho maže.
        self._user_zoomed = False
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
        old_size = (self._image.width(), self._image.height()) \
            if self._image is not None else None
        self._map_wh = map_wh
        self._out = None if image is None else np.asarray(image, np.float64)
        self._dens = (None if densities is None
                      else np.asarray(densities, np.float64))
        self._warn = warn
        self._warn_on = bool(warn_on)
        self._image = None if image is None else self._compose()
        self._recompute_rect_px()
        same_size = (self._image is not None and old_size is not None
                     and (self._image.width(), self._image.height())
                     == old_size)
        if same_size and self._user_zoomed:
            # Rerender téhož snímku (tah sliderem): ruční zoom nesmí skočit na
            # fit — operátor přece přibližuje sharpened pixel, aby na něj
            # sahal. Jen oříznout na případný nový floor (resize mezi tím).
            self._zoom = max(self._fit_zoom(), self._zoom)
            self.zoom_changed.emit(self._zoom)
        else:
            # Jiný obraz (snímek, nebo přepnutí plného rozlišení) — ruční zoom
            # se vztahoval k pixelům tenkratného podvzorku.
            self._user_zoomed = False
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

    @property
    def zoom(self) -> float:
        return self._zoom

    @property
    def at_fit(self) -> bool:
        return not self._user_zoomed

    def _fit_zoom(self) -> float:
        """Fit-to-screen: celý snímek do widgetu, nikdy nezvětšovat nad 1:1."""
        if self._image is None:
            return 1.0
        zx = self.width() / max(self._image.width(), 1)
        zy = self.height() / max(self._image.height(), 1)
        return min(1.0, zx, zy)

    def _fit(self) -> None:
        if self._image is None:
            return
        self._zoom = self._fit_zoom()
        self._clamp_origin()      # fit = vycentrovaný, ne vlevo nahoře
        self.zoom_changed.emit(self._zoom)

    def _clamp_origin(self) -> None:
        """Fotka nesmí zmizet z dohledu (rozkaz 2026-09-22).

        Menší než widget: sedí na střed (při fit tudíž nejde posouvat —
        tah je mrtvý). Větší: origin ve mezích [widget − obraz, 0], tj.
        posun končí na okraji fotky, žádná černá za ní."""
        if self._image is None:
            self._origin = QPoint(0, 0)
            return
        ow = self._image.width() * self._zoom
        oh = self._image.height() * self._zoom
        if ow <= self.width():
            x = (self.width() - ow) / 2.0
        else:
            x = min(0.0, max(self.width() - ow, float(self._origin.x())))
        if oh <= self.height():
            y = (self.height() - oh) / 2.0
        else:
            y = min(0.0, max(self.height() - oh, float(self._origin.y())))
        self._origin = QPoint(int(round(x)), int(round(y)))

    def _apply_zoom(self, new_zoom: float, anchor: QPoint) -> None:
        """Zoom s kotvou pod bodem; pod fit floor se nejdou (rozkaz 2026-09-22)."""
        if self._image is None:
            return
        floor = self._fit_zoom()
        new_zoom = min(self.MAX_ZOOM, max(floor, new_zoom))
        if abs(new_zoom - self._zoom) < 1e-9:
            return
        map_pt = (anchor - self._origin) / max(self._zoom, 1e-6)
        self._zoom = new_zoom
        self._origin = anchor - map_pt * new_zoom
        self._clamp_origin()
        self._user_zoomed = new_zoom > floor + 1e-9
        self.zoom_changed.emit(self._zoom)
        self.update()

    def show_fit(self) -> None:
        """Výhled whole-frame: zoom = fit, vlevo nahoře (původní chování)."""
        self._user_zoomed = False
        self._fit()
        self.update()

    def show_100(self) -> None:
        """100 %: jeden pixel náhledové mapy = jeden pixel obrazovky.

        Pri zapnutem podvzorkovani je to 100 % *nahledu* (1 px = ``step``
        senzory) -- skutecny pixel posoudi jen rezim plneho rozliseni nebo
        export. Stejně je to ale úhel pohledu, kvůli kterému uživatel 100 %
        chtěl: sharpening v náhledu je jinak neviditelný."""
        if self._image is None:
            return
        self._apply_zoom(1.0, QPoint(self.width() // 2,
                                     self.height() // 2))

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_1, Qt.Key.Key_Percent):
            self.show_100()
        elif event.key() in (Qt.Key.Key_0, Qt.Key.Key_F):
            self.show_fit()
        else:
            super().keyPressEvent(event)

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
        # Rozkazy 2026-09-22: zoom dělá JEN pinch (native gesture níže) a
        # Ctrl/Cmd+kolečko jako záchrana pro myš; posun dělá JEN tažení
        # myší. Dvouscroll touchpadu tudíž nebývá nic — dřív posouval a
        # operátor si stěžoval, že mu fotka ujíždí sama.
        if self._image is None:
            return
        mods = event.modifiers()
        if mods & (Qt.KeyboardModifier.ControlModifier
                   | Qt.KeyboardModifier.MetaModifier):
            factor = 1.25 if event.angleDelta().y() > 0 else 0.8
            self._apply_zoom(self._zoom * factor,
                             event.position().toPoint())
        event.accept()

    def event(self, ev) -> bool:
        # macOS pinch (trackpad, Magic Mouse) posílá QNativeGestureEvent —
        # wheelEvent na něj nikdy nedorazí, musí se přes event().
        if (ev.type() == QEvent.Type.NativeGesture
                and ev.gestureType()
                == Qt.NativeGestureType.ZoomNativeGesture
                and self._image is not None):
            self._apply_zoom(self._zoom * (1.0 + ev.value()),
                             ev.position().toPoint())
            ev.accept()
            return True
        return super().event(ev)

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
            self._clamp_origin()   # na konci dráhy fotka stojí, žádná černá
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
        if self._image is None:
            return
        if not self._user_zoomed:
            self._fit()          # výhled: velikost obrazu vždy přesně na míru
            return
        # Ruční zoom přežívá resize okna; pod nový fit-padě jen tehdy, když
        # se okno zmenší tolik, že ani fit už nedosáhne na dosavadní zoom.
        floor = self._fit_zoom()
        if self._zoom < floor - 1e-9:
            cx = QPoint(self.width() // 2, self.height() // 2)
            self._apply_zoom(floor, cx)
        else:
            self._clamp_origin()  # po změně rozměru musí fotka znova do mezí
            self.update()

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
    * svislé čáry Dmin (azurová) a Dmax (žlutá) -- kde jsem *řekl*, že rozsah
      je; pod každou čárou je její hodnota a podíl pixelů, které za ni
      přeteknou (pod Dmin = černý řez, nad Dmax = bílý řez). Text pod
      histogramem byl nečitelný a duplicitní (rozkaz 2026-09-22: vše
      dovnitř); statistika se počítá jen z ROI -- kovový rámec držáku do
      ní nepatří,
    * bílá křivka -- diagnostika tónové mapy: kam který D dopadá v lineárním
      pozitivu (0 = černá, 1 = bílá), včetně expozice a stínového pásu.
      BEZ zobrazovací gammy (dokument 07): zobrazovací transfer patří do
      dolního výstupního histogramu a do náhledu, ne do tvaru S-křivky --
      jinak v ní uživatel vidí hrb monitorové charakteristiky.

    ±inf hustoty se kreslí jako plné sloupce na kolejnicích (vlevo −inf =
    přepal, vpravo +inf = neprostupno); NaN („bez světla") se vypíše vpravo
    nahoře jen když nenulový -- ani jedno nemá vlastní hustotu, ale operátor
    o něm musí vědět.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._hist: np.ndarray | None = None
        self._d_arr: np.ndarray | None = None   # reference pro řezové podíly
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
            self._d_arr = None
            self.update()
            return
        self._d_arr = d      # reference (ne kopie) -- percentila i řezové
        # podíly se počítají až při kresbě, kdy už známe dmin/dmax z params.
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

    def _cut_fraction(self, d_val: float, below: bool) -> float | None:
        """Podíl pixelů ROI pod Dmin / nad Dmax (řezové kolik vyříznu).

        ±inf i NaN do jmenovatele nepatří (řez je otázka pro měřenou część);
        NaN hlásí vlastní číslici vpravo nahoře."""
        if self._d_arr is None:
            return None
        fin = self._d_arr[np.isfinite(self._d_arr)]
        if fin.size == 0:
            return None
        frac = (fin < d_val) if below else (fin > d_val)
        return float(frac.mean())

    def _draw_scale_label(self, p: QPainter, x: int, name: str,
                          d_val: float, below: bool) -> None:
        """Tři řádky pod čárou: Dmin / 0,432 / <18,9 % — u samotné čáry.

        Dmax label zrcadlí dovnitř (vpravo od čáry by přetekl přes kraj);
        Dmin zrcadlí doprostřed, když sedí na levé kolejnici."""
        cut = self._cut_fraction(d_val, below)
        lines = (name, f"{d_val:.3f}".replace(".", ","),
                 ("<" if below else ">")
                 + (f"{cut * 100:.1f} %" if cut is not None else "?"))
        p.setPen(QColor(200, 200, 200))
        # Pod čárou, začíná až pod hlavičkou (p50/NaN řádek má horních 12 px).
        y = 30
        for t in lines:
            if x > self.width() // 2:
                p.drawText(QRect(x - 46, y, 43, 12),
                           Qt.AlignmentFlag.AlignRight, t)
            else:
                p.drawText(QRect(x + 3, y, 46, 12),
                           Qt.AlignmentFlag.AlignLeft, t)
            y += 12

    def _draw_summary(self, p: QPainter, w: int) -> None:
        """Střední hodnota (p50) uprostřed nahoře + NaN vpravo (rozkaz:
        text pod histogramem je spotřebovaný — percentily jedou dovnitř).

        p50 se kreslí jako tečkovaná svislá linka — u filmového histogramu
        'prostřed' nic neznamená bez vizuálního kotviště; linka ukáže, jestli
        je mediál vlevo (podexpozice) nebo uprostřed. Číselná hodnota je
        vedle ní v hlavičce, nikdy ne přes Data."""
        s = self._stats
        p50 = s.get("d_p50", float("nan"))
        if np.isfinite(p50):
            x = self._d_to_x(p50)
            p.setPen(QPen(QColor(255, 140, 0, 160), 1,
                          Qt.PenStyle.DotLine))
            p.drawLine(x, 13, x, self.height())
            p.setPen(QColor(255, 180, 100))
            mid = w // 2
            p.drawText(QRect(mid - 60, 0, 120, 12),
                       Qt.AlignmentFlag.AlignCenter,
                       f"p50 {self._fmt1(p50)}")
        if self._nan_fraction > 0:
            p.setPen(QColor(180, 180, 180))
            p.drawText(QRect(w - 96, 0, 94, 12),
                       Qt.AlignmentFlag.AlignRight,
                       f"bez světla {self._nan_fraction * 100:.1f} %")

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
            # Body stupně: azurová Dmin, žlutá Dmax (barvy ROI rámečku i
            # výstražných overlayů). Pod čárou název, hodnota a podíl pixelů
            # za ní (< dmin = černý řez, > dmax = bílý řez) -- veškerý text,
            # který dřív stál pod grafem (nečitelný), žije tady uvnitř.
            pens = (QPen(QColor(0, 220, 255), 1, Qt.PenStyle.DashLine),
                    QPen(QColor(255, 210, 0), 1, Qt.PenStyle.DashLine))
            for (d_val, color, name, below), pen in zip(
                    ((self._params.dmin, QColor(0, 220, 255), "Dmin", True),
                     (self._params.dmax, QColor(255, 210, 0), "Dmax", False)),
                    pens):
                x = self._d_to_x(d_val)
                p.setPen(pen)
                p.drawLine(x, 0, x, h)
                self._draw_scale_label(p, x, name, d_val, below)
            self._draw_summary(p, w)
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

        # Rolová procenta místo titulkového textu (rozkaz 2026-09-22):
        # "výstup (výřez)" je zbytečný (z výřezu jsou teď oba grafy),
        # pojmenovávat se nemá -- jen čísla, barvou podle exposure
        # warning overlaye: stíny modře vlevo, světla červeně vpravo.
        if self._lo_pct > 0:
            p.setPen(QColor(90, 160, 255))
            p.drawText(6, 12, f"{self._lo_pct:.1f} %")
        if self._hi_pct > 0:
            p.setPen(QColor(255, 90, 90))
            p.drawText(QRect(w - 66, 0, 60, 12),
                       Qt.AlignmentFlag.AlignRight, f"{self._hi_pct:.1f} %")


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
        self.frame_list.setViewMode(QListWidget.ViewMode.IconMode)
        # Název POD náhledem (rozkaz 2026-09-22): vedle ikony by panel musel
        # být široký jako náhled+popisek. Static = žádné ruční přeskupování,
        # Adjust = layout se přizpůsobí resize panelu.
        self.frame_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.frame_list.setMovement(QListWidget.Movement.Static)
        self.frame_list.setWordWrap(True)
        # Mřížka = ikona + dva řádky popisku pod ni; panel stačí široký jako
        # náhled, ne náhled+popisek (88 px ikona + pár px dech).
        self.frame_list.setGridSize(QSize(THUMB_ICON.width() + 20,
                                          THUMB_ICON.height() + 36))
        self.frame_list.setSpacing(4)
        self.frame_list.currentItemChanged.connect(self._frame_selected)
        lv.addWidget(self.frame_list, 1)
        # (lbl_dmin pry — rozkaz 2026-09-22: hodnota je vidět na azurové
        # čáře uvnitř histogramu i v Dmin spinu, tady jen zabírala místo.)
        self.lbl_orient = QLabel("Orientace: 1:1")
        lv.addWidget(self.lbl_orient)
        splitter.addWidget(left)

        # -- střed: záhlaví + náhled ------------------------------------------
        # Záhlaví (rozkaz 2026-09-22): zoom a hodnota pixelu pod kurzorem —
        # numerický report se prý „ztratil" právě proto, že sedel v pravém
        # panelu a odtud byl odstěhován do spodní lišty. Patří nad obraz.
        centre = QWidget()
        cv = QVBoxLayout(centre)
        cv.setContentsMargins(0, 0, 0, 0)
        self.view = DensityView()
        self.view.rect_chosen.connect(self.rect_selected)
        self.view.hovered.connect(self._pixel_hovered)
        self.view.zoom_changed.connect(self._zoom_shown)
        header = QHBoxLayout()
        self.btn_fit = QPushButton("Fit")
        self.btn_fit.setToolTip("Celý snímek v okně (klávesa 0)")
        self.btn_fit.clicked.connect(self.view.show_fit)
        self.btn_100 = QPushButton("100 %")
        self.btn_100.setToolTip(
            "1 pixel náhledu = 1 pixel obrazovky (klávesa 1) — pro posouzení "
            "sharpenu zapni „1:1 plné rozlišení“, jinak je náhled "
            "podvzorkovaný")
        self.btn_100.clicked.connect(self.view.show_100)
        self.chk_fullres = QCheckBox("1:1 plné rozlišení")
        self.chk_fullres.setToolTip(
            "Náhled se nepočítá z podvzorku — každý pixel je ze senzoru. "
            "OSTŘENÍ SE V NÁHLEDU UKAZUJE JEN S TÍMTO ZAPNUTÝM (na "
            "podvzorku nemá smysl ho zobrazovat); pomalejší tah.")
        self.chk_fullres.toggled.connect(self._fullres_toggled)
        self.lbl_zoom = QLabel("—")
        self.lbl_pixel = QLabel("D: —  out: —")
        self.lbl_zoom.setMinimumWidth(90)
        self.lbl_pixel.setMinimumWidth(170)
        header.addWidget(self.btn_fit)
        header.addWidget(self.btn_100)
        header.addWidget(self.chk_fullres)
        header.addStretch(1)
        header.addWidget(self.lbl_zoom)
        header.addSpacing(16)
        header.addWidget(self.lbl_pixel)
        cv.addLayout(header)
        cv.addWidget(self.view, 1)
        splitter.addWidget(centre)

        # -- pravý panel: ladící histogram → stats → výstupní histogram →
        #    status → nastavovací boxy; vše v scroll area (nevejde se) ------
        side = QWidget()
        sv = QVBoxLayout(side)
        self.histogram = DensityHistogramWidget()
        sv.addWidget(self.histogram)
        # (stats řádek pod histogramem pry — rozkaz 2026-09-22: veškeré
        # číslo přesunuto dovnitř grafu, ke čárám Dmin/Dmax a nad peak.)
        self.out_histogram = OutputHistogramWidget()
        sv.addWidget(self.out_histogram)
        # (status řádek tu byl — uživatel 2026-09-21 večer „celé to dej pryč":
        # duplikoval stats pod horním histogramem a při hoveru přeskakoval
        # nastavovátka pod sebou. Zpráwy chodí do spodní lišty okna, ta se
        # nikdy nepřeskupuje.)
        # Stručný text (QCheckBox se neumí zabalit): dlouhý label táhl
        # minimumSizeHint celého úzkého panelu do šířky jednoho řádku.
        self.chk_warn = QCheckBox("Exposure warning")
        self.chk_warn.setToolTip("Přepaly červeně, podexpozice modře.")
        self.chk_warn.toggled.connect(self.view.set_warning)
        sv.addWidget(self.chk_warn)
        sv.addWidget(self._build_scale_box())
        sv.addWidget(self._build_curve_box())
        sv.addWidget(self._build_display_box())
        sv.addWidget(self._build_export_box())
        # „Default" (bývalý Proposal) na úplný konec panelu, pod export
        # (rozkaz 2026-09-22 noc III).
        self.btn_defaults = QPushButton("Default")
        self.btn_defaults.setToolTip(
            "Auto body (Dmin z měření, Dmax z p99,9) + přirozená křivka — "
            "stejný návrh jako při prvním otevření snímku.")
        self.btn_defaults.clicked.connect(self.apply_defaults)
        sv.addWidget(self.btn_defaults)
        sv.addStretch(1)
        side_scroll = QScrollArea()
        side_scroll.setWidget(side)
        side_scroll.setWidgetResizable(True)
        side_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        # Svisle ano, vodorovně ne (rozkaz 2026-09-22): obsah se musí vejít —
        # formuláře se balí (WrapAllRows), texty zabalují. Vodorovný scroll by
        # jen maskoval ořez histogramu, který operátor nesmí vidět.
        side_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Panel zůstal úzký, jen o kousek wider (rozkaz 2026-09-22 noc III:
        # „rozšiř jej přeci jen o kousek a popisky a slider na 1 řádek") —
        # single-line rows need ~300 px. Natahuje se JEN střed (stretch
        # 0/1/0) — panel už nikdy neztloustne na úkor náhledu.
        self.side_scroll = side_scroll
        side_scroll.setMinimumWidth(220)
        splitter.addWidget(side_scroll)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([220, 940, 300])

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
                  spin: QDoubleSpinBox, scale: float = 100.0,
                  remember: bool = True) -> None:
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
        if remember:
            spin.valueChanged.connect(self._setting_changed)
        else:
            # Ostření: project-level, ne per-frame (rozkaz 2026-09-22).
            spin.valueChanged.connect(self._global_sharpen_changed)
        # V úzkém panelu (WrapAllRows) má řádek patřit jezdci: spin ataka
        # AllNonFixedFieldsGrow neroste sám od sebe, jezdec bez explicitní
        # Expanding politiky zůstal na sizeHintu — "slidery jen do půlky"
        # (stížnost 2026-09-22).
        slider.setSizePolicy(QSizePolicy.Policy.Expanding,
                             QSizePolicy.Policy.Fixed)
        row = QHBoxLayout()
        row.addWidget(slider, 1)
        row.addWidget(spin)
        form.addRow(name, row)

    @staticmethod
    def _field_row(spin: QDoubleSpinBox, check: QCheckBox) -> QHBoxLayout:
        """Řádek «spin + auto» vedle labelu (rozkaz 2026-09-22 noc III:
        „na jednom řádku s text polem i auto")."""
        row = QHBoxLayout()
        row.addWidget(spin)
        row.addWidget(check)
        row.addStretch(1)
        return row

    @staticmethod
    def _narrow_form(box: QGroupBox) -> QFormLayout:
        """Formulář pro úzký panel (rozkaz 2026-09-22).

        Noc III: labely zkráceny na jediná slova (Dmin/Toe/Gamma/…), takže se
        vejdou NA JEDEN ŘÁDEK vedle jezdce (rozkaz „popisky a slider na 1
        řádek") — WrapAllRows už není potřeba. macOS default drží pole na
        sizeHintu, proto AllNonFixedFieldsGrow: jezdce rostou do zbylé šířky.
        """
        form = QFormLayout(box)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(8)
        return form

    def _build_scale_box(self) -> QGroupBox:
        """Meritko filmu (dok 08 §5): Dmin, Dmax, tolerance pod Dmin.

        Dmin/Dmax nejsou kontrastové ovladače — určují, jaká část hustotní
        osy filmu se mapuje do výstupu (08 §1). Kontrast je až křivka níž.
        """
        box = QGroupBox("Meritko filmu")
        form = self._narrow_form(box)
        self.spin_dmin = self._spin(0.0, 2.0, 0.2, 0.01, decimals=3)
        # „auto" jen tak (rozkaz 2026-09-22 noc III) — význam nese tooltip;
        # checkboxy jsou vedle spinu na témž řádku, ne pod ním.
        self.chk_dmin_auto = QCheckBox("auto")
        self.chk_dmin_auto.setToolTip("Dmin se přebírá z posledního měření "
                                      "film base; odškrtnutím převezmeš "
                                      "hodnotu v poli ručně (08 §5).")
        self.chk_dmin_auto.setChecked(True)
        self.spin_dmax = self._spin(0.2, 5.0, 2.6, 0.05, decimals=3)
        # Stav i přepínač v jedné masce (08 §5): auto je PRACOVNÍ návrh
        # p99,9 + 0,05 D, ne vlastnost emulze; odškrtnutím ho převezmeš ručně.
        self.chk_dmax_auto = QCheckBox("auto")
        self.chk_dmax_auto.setToolTip("Pracovní návrh Dmax = p99,9 + 0,05 D; "
                                      "odškrtnutím ho převezmeš ručně "
                                      "(08 §5).")
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
        # „Default" (bývalý Proposal) už tu není — rozkaz 2026-09-22 noc III:
        # „čudl proposal na úplný konec pod export a přejmenuj na default".
        # Dmin/Dmax: spin + auto na JEDNOM řádku, label bez „[D]" (jednotka
        # je v tooltipu a je všem jasná).
        form.addRow("Dmin", self._field_row(self.spin_dmin,
                                            self.chk_dmin_auto))
        form.addRow("Dmax", self._field_row(self.spin_dmax,
                                            self.chk_dmax_auto))
        self._bind_row(form, "Dmin offset", self.sl_sb, self.spin_sb)
        self._bind_row(form, "Exposure", self.sl_ev, self.spin_ev)

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
        box = QGroupBox("Tone curve")   # anglicky — rozkaz 2026-09-22 noc III
        form = self._narrow_form(box)
        self.sl_toe = self._slider(0, 80, 20)
        self.spin_toe = self._spin(0.0, 1.66, 0.20, 0.01)
        self.sl_gamma = self._slider(80, 250, 135)
        self.spin_gamma = self._spin(0.10, 4.0, 1.35, 0.01)
        self.sl_shoulder = self._slider(0, 80, 20)
        self.spin_shoulder = self._spin(0.0, 1.66, 0.20, 0.01)
        # Popsiky jen anglicky (rozkaz 2026-09-22 noc III): „a pak jen ty
        # anglické toe gamma shoulder". Česky zůstávají tooltipy.
        self._bind_row(form, "Toe", self.sl_toe, self.spin_toe)
        # „gamma", ne „sklon křivky": kombinovaná křivka má kvůli spline před
        # gamma krokem vlastní sklon (dok 07 §7); význam hlídá tooltip.
        self._bind_row(form, "Gamma", self.sl_gamma, self.spin_gamma)
        self._bind_row(form, "Shoulder", self.sl_shoulder, self.spin_shoulder)
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
        box = QGroupBox("Post-curve")   # bývalé „Zobrazení" — rozkaz noc III
        form = self._narrow_form(box)
        # Jas a kontrast v display prostoru (za gammou) — Photoshop zvyk.
        # kontrast kolem zobrazené střední šedi 0,5; jas posun. Nejsou
        # expozice: ta sahá na hustotní osu (co film viděl).
        self.sl_br = self._slider(-50, 50, 0)         # −0,50 … +0,50
        self.spin_br = self._spin(-0.5, 0.5, 0.0, 0.01)
        self.sl_ct = self._slider(10, 400, 100)       # 0,10 … 4,00
        self.spin_ct = self._spin(0.1, 4.0, 1.0, 0.01)
        # (Řádek „Display gamma 2,2 — dáno profilem" odstraněn —
        #  rozkaz 2026-09-22 noc III: „display gamma 2,2 dáno profilem
        #  odstraň". Gamma v datech zůstává, jen se neukazuje.)
        self._bind_row(form, "Brightness", self.sl_br, self.spin_br)
        self._bind_row(form, "Contrast", self.sl_ct, self.spin_ct)
        # (Ostření tu bývalo — rozkaz 2026-09-22: „přesuň do menu Export
        # a ať funguje v režimu stejné nastavení pro všechny fotky"; je to
        # vlastnost výstupu celého projektu, ne snímku. Viz _build_export_box.)
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
        # vyřizuje metoda, ne výběr. „Export vše" (rozkaz 2026-09-22) přepne
        # snímek po snímku a spustí vždy týž vybraný formát.
        self.cmb_export = QComboBox()
        self.cmb_export.addItems([label for label, _ in self.EXPORT_FORMATS])
        # Bez tohoto se combo táhne na nejdelší položku ("Pozitiv — TIFF 16b
        # gray (Gray Gamma 2,2)") a v úzkém panelu roztlačí vše kolem sebe.
        self.cmb_export.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.cmb_export.setMinimumContentsLength(14)
        self.btn_export = QPushButton("Export")
        self.btn_export.clicked.connect(self.export_current)
        self.btn_export.setEnabled(False)
        self.btn_export_all = QPushButton("Export vše")
        self.btn_export_all.setToolTip(
            "Exportuje všechny snímky projektu aktuálně vybraným formátem; "
            "každý se svými uloženými parametry.")
        self.btn_export_all.clicked.connect(self.export_all)
        self.btn_export_all.setEnabled(False)
        h.addWidget(self.cmb_export)      # combo samo: v 260 px se trio nevedlo
        row = QHBoxLayout()
        row.addWidget(self.btn_export, 1)
        row.addWidget(self.btn_export_all, 1)
        h.addLayout(row)
        # Okraj z filmu kolem ořezu (rozkaz 2026-09-22, implicitně odškrtnutý):
        # hrana držáku, která vadila při korekci, se do exportu vrátí až teď —
        # o px na každou stranu od ROI, ale „po kraj", kdyby jich bylo málo.
        # Hustotní archiv okraj nedostává: je to měření, ne fotka.
        self.chk_border = QCheckBox("Okraj filmu")
        # Stručný label + suffix: checkbox se neumí zabalit a "px/stranu"
        # navyšoval spin podél sizeHintu — duo táhlo box na 333 px, což byl
        # přesně ořez, na který si operátor stěžoval (2026-09-22).
        self.chk_border.setToolTip(
            "Přidá ke každému renderu kraj z filmu kolem ořezu (ROI) — ten, "
            "který při korekci vadí, o px na každou stranu. Nemá-li snímek "
            "v daném směru tolik pixelů, jde až po kraj.")
        self.spin_border = QSpinBox()
        self.spin_border.setRange(1, 4000)
        self.spin_border.setValue(100)
        self.spin_border.setSuffix(" px")
        self.spin_border.setEnabled(False)
        self.chk_border.toggled.connect(self.spin_border.setEnabled)
        border_row = QHBoxLayout()
        border_row.addWidget(self.chk_border)
        border_row.addWidget(self.spin_border)
        border_row.addStretch(1)
        h.addLayout(border_row)
        # Ostření = poslední krok řetězce (unsharp mask na display pixelech),
        # od 2026-09-22 tady a GLOBÁLNÍ: „přesuň do menu Export a ať funguje
        # v režimu stejné nastavení pro všechny fotky — tohle se nebude
        # upravovat per fotka". Hodnoty žijí v project.global_sharpen*, ne
        # v per-frame dictu; Photoshop konvence 0–200 % / px, předvolba 20 %.
        # (stejná pravidla jako _narrow_form — box už ale vlastní QVBoxLayout,
        # form se musí vložit do něj, ne na něj)
        # Oddělená rubrika „Sharpening" (rozkaz 2026-09-22 noc III: „ostření
        # odděl jako Sharpening v rubrice export, dej tam fajfku ano/ne a pod
        # tím slidery pojmenované strength a radius"). Fajfka = zap/vyp pro
        # celý projekt; odpovídá project.global_sharpen_on.
        h.addWidget(QLabel("<b>Sharpening</b>"))
        self.chk_sharpen = QCheckBox("Zapnuto")
        self.chk_sharpen.setToolTip(
            "Ostření (unsharp mask) jako poslední krok exportu — stejné "
            "pro všechny snímky projektu. V náhledu se ukazuje jen při "
            "1:1 plném rozlišení.")
        self.chk_sharpen.toggled.connect(self._sharpen_toggled)
        h.addWidget(self.chk_sharpen)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(8)
        h.addLayout(form)
        self.sl_sh = self._slider(0, 200, 20)          # 0 … 200 %
        self.spin_sh = self._spin(0.0, 200.0, 20.0, 1.0, decimals=0,
                                  suffix=" %")
        self.sl_shr = self._slider(0, 20, 10)          # 0,0 … 2,0 px (po 0,1)
        self.spin_shr = self._spin(0.0, 50.0, 1.0, 0.1, decimals=1,
                                   suffix=" px")
        self._bind_row(form, "Strength", self.sl_sh, self.spin_sh,
                       scale=1.0, remember=False)
        self._bind_row(form, "Radius", self.sl_shr,
                       self.spin_shr, scale=10.0, remember=False)
        self.spin_sh.setToolTip("Množství ostření (unsharp mask) v procentech "
                                "jako ve Photoshopu — PRO VŠECHNY SNÍMKY "
                                "stejně. 10–30 % prokreslí hrany bez bílých "
                                "lemov; 0 vypíná.")
        self.spin_shr.setToolTip("Poloměr Gaussovy masky v pixelech — jako "
                                 "ve Photoshopu. Malý poloměr (0,5–1,5 px) "
                                 "opatrně zvedne detail; nad ~3 px rostou "
                                 "bílá lemování kolem hran.")
        return box

    def _border_px(self) -> int:
        """Okraj exportu [px]: zaškrtnuto → hodnota, odškrtnuto → 0."""
        return int(self.spin_border.value()) if self.chk_border.isChecked() \
            else 0

    def export_current(self) -> None:
        """Volba ze seznamu → příslušná exportová metoda."""
        _label, method = self.EXPORT_FORMATS[self.cmb_export.currentIndex()]
        getattr(self, method)()

    def export_all(self) -> None:
        """Všechny snímky aktuálním formátem (rozkaz 2026-09-22).

        Přepínání přes QListWidget je záměr: ``_frame_selected`` nahraje
        per-snímková nastavení i ROI a naředí ``_density`` — týž kód, který
        obsluhuje ruční klik, takže batch nemůže používat jiné parametry
        než co by operátor viděl v náhledu. Selhavší snímek se přeskočí."""
        if self.project is None or not self.frame_list.count():
            return
        total = self.frame_list.count()
        previous = self.frame_list.currentRow()
        done, skipped = 0, []
        for i in range(total):
            self.frame_list.setCurrentRow(i)
            if self._density is None:
                skipped.append(self.frame_list.item(i).text())
                continue
            self.export_current()
            done += 1
        if previous != self.frame_list.currentRow():
            self.frame_list.setCurrentRow(max(previous, 0))
        msg = f"Export vše: {done}/{total} snímků → {self.cmb_export.currentText()}"
        if skipped:
            msg += f" · přeskočeno {len(skipped)} (nelze měřit)"
        self.statusBar().showMessage(msg, 10000)

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
        self.frame_list.setIconSize(THUMB_ICON)
        for name in project.frame_names:
            QListWidgetItem(name, self.frame_list)
        # Miniatury se generují až po prvním vykreslení seznamu: roll má
        # desítky snímků a jejich render by otevření okna protáhl o vteřiny.
        QTimer.singleShot(0, self._populate_thumbnails)
        self._dmin_auto = project.dmin_auto()
        if self._dmin_auto is not None:
            self.spin_dmin.setValue(round(self._dmin_auto, 3))
        else:
            self.chk_dmin_auto.setChecked(False)
        # Globální ostření (Export box) se načítá jednou ze projektu —
        # per-snímkové přepínání do něj nesahá (rozkaz 2026-09-22).
        self._loading = True
        try:
            self.chk_sharpen.setChecked(project.global_sharpen_on)
            self.spin_sh.setValue(project.global_sharpen)
            self.spin_shr.setValue(project.global_sharpen_radius)
        finally:
            self._loading = False
        self._sync_sharpen_enabled()
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

    # ---------------------------------------------------------- miniatury

    def _populate_thumbnails(self) -> None:
        """Levné miniatury do seznamu (rozkaz 2026-09-22).

        Surová data už jsou v RAM; miniatura je lineární normalace nad
        podvzorkem, žádná hustota ani křivka — z seznamu se orientuješ,
        negadoctor je až náhled. Negativ se invertuje (divák myslí
        pozitivem), orientace filmu i rotace snímku se promítnou, aby
        miniatura odpovídala tomu, co uvidíš v náhledu."""
        p = self.project
        if p is None:
            return
        for i in range(self.frame_list.count()):
            item = self.frame_list.item(i)
            try:
                item.setIcon(QIcon(self._thumbnail(p.entry(item.text()))))
            except Exception:  # noqa: BLE001 - seznam musí přežít i vadný snímek
                log.warning("miniatura %s selhala", item.text(),
                            exc_info=True)

    def _thumbnail(self, entry) -> QIcon:
        # Podvzorek FIRST: full-frame float64 by byl 4× bigger than the
        # uint16 original, and for what — an 88 px icon.
        a = _subsample(entry.frame.data, THUMB_MAX_DIM)
        a = self.project.rotate_frame(
            self.project.orientation_apply(a), entry.name)
        a = a.astype(np.float64)
        lo, hi = np.nanmin(a), np.nanmax(a)
        a = (a - lo) / (hi - lo) if hi > lo else np.zeros_like(a)
        q = density_to_qimage(np.clip(1.0 - a, 0.0, 1.0))
        return QIcon(QPixmap.fromImage(q).scaled(
            THUMB_ICON, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

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
                (d.shape[1], d.shape[0]),
                self.project.frame_rotation(name)))
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
        self._update_density_histogram()
        self.btn_export.setEnabled(True)
        self.btn_export_all.setEnabled(True)
        self.rerender()

    def _update_density_histogram(self) -> None:
        """Horní histogram jen z výřezu (rozkaz 2026-09-22).

        „Nezajímá mě histogram kovového rámečku" — statistika i řezové podíly
        se počítají z ROI, ne z celého senzoru. Uložený ROI je v souřadnicích
        senzoru a `self._density` taky, žádné převody. Bez ROI zbývá celý
        snímek — tam se ořezat nedá."""
        if self._density is None or self.project is None:
            self.histogram.set_density(None)
            return
        d = self._density[0]
        rect = None
        if self._current is not None:
            rect = self.project.rect_for(self.project.entry(self._current))
        if rect is not None:
            x0, y0, x1, y1 = rect
            h, w = d.shape
            x0, y0 = max(0, min(x0, w - 1)), max(0, min(y0, h - 1))
            x1, y1 = max(x0 + 1, min(x1, w)), max(y0 + 1, min(y1, h))
            d = d[y0:y1, x0:x1]
        self.histogram.set_density(d)

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
        # Auto Dmin má smysl jen když existuje měření film base. Při autu
        # je závazné AKTUÁLNÍ měření, ne uložená hodnota z dřívějška —
        # uložená nula z rozbitého base vzorku by jinak přežívala navěky
        # i po opravě měření (K16O02, 2026-09-22). Ruční hodnotu respektujeme.
        auto_dmin = not dmin_manual and self._dmin_auto is not None
        self.chk_dmin_auto.setChecked(auto_dmin)
        self.chk_dmax_auto.setChecked(params.dmax_source == "frame")
        self.spin_dmin.setValue(
            round(self._dmin_auto if auto_dmin else params.dmin, 3))
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
        # (Ostření se tu nenačítá — je globální pro celý projekt, sedí v
        # Export boxu a jego hodnoty drží project.global_sharpen*; při
        # přepnutí snímku se mají zachovat, ne přepisovat per-frame dictem.
        # Nastavuje se jednou v set_project. Rozkaz 2026-09-22.)
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
            # Fajfka „Zapnuto" (rozkaz noc III) — vypnuto == sharpen 0,
            # hence i export the hodnoty nepoužije.
            sharpen=self.spin_sh.value()
            if self.chk_sharpen.isChecked() else 0.0,
            sharpen_radius=self.spin_shr.value(),
        )

    def _settings_payload(self) -> dict:
        """Uložená podoba parametrů: RenderParams + jestli byl Dmin ruční.

        Ostření se vyhazuje — žije JAKO projektová hodnota `sharpen`
        (řád 2026-09-22); per-frame kopie by byla mátlící duplicita."""
        d = self.current_params().to_dict()
        d.pop("sharpen", None)
        d.pop("sharpen_radius", None)
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

    def _global_sharpen_changed(self, *_a) -> None:
        """Ostření je vlastnost celého exportu, ne snímku (rozkaz 2026-09-22):
        uloží se do project-level klíče a přerekne jen náhled."""
        if self._loading or self.project is None:
            return
        self.project.global_sharpen = self.spin_sh.value()
        self.project.global_sharpen_radius = self.spin_shr.value()
        self.project.save_settings()
        self.rerender()

    def _sharpen_toggled(self, *_a) -> None:
        """Fajfka ano/ne pro ostření (rozkaz 2026-09-22 noc III): vypnutím
        se hodnota zachová, jen se nepoužije (render vidí sharpen 0)."""
        if self._loading:
            return
        if self.project is not None:
            self.project.global_sharpen_on = self.chk_sharpen.isChecked()
            self.project.save_settings()
        self._sync_sharpen_enabled()
        self.rerender()

    def _sync_sharpen_enabled(self) -> None:
        on = self.chk_sharpen.isChecked()
        for w in (self.sl_sh, self.spin_sh, self.sl_shr, self.spin_shr):
            w.setEnabled(on)

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
        view_d = self.project.rotate_frame(
            self.project.orientation_apply(d), prov.source)
        # Režim 1:1 (záhlaví): bez podvzorku — sharpen je v podvzorku
        # neviditelný (náhled kreslí každý N-tý senzorový pixel, ostření
        # sousedů zahodí). Plný snímek je pomalejší, ale vidí skutečný pixel.
        fullres = self.chk_fullres.isChecked()
        sub = view_d if fullres else _subsample(view_d, PREVIEW_MAX_DIM)
        # Sharpening se v NÁHLEDU uplatní jen při 1:1 (řád 2026-09-22):
        # na podvzorku je neviditelný (sousedé pro masku jsou ta tam) a jeho
        # aplikace by jen lhala. Export ostří vždy — tady jde o poctivý dohled.
        preview_params = params if fullres else replace(params, sharpen=0.0)
        display = rnd.render_for_display(sub, preview_params)
        # Exposure warning: co render ořízl na konce stupnice. Měří se na
        # čisté ose x před ořezem -- NaN (bez světla) do neither koše.
        x = rnd.positive_x(sub, params)
        hi = (x >= 1.0) | np.isposinf(x)
        lo = ((x <= 0.0) & ~np.isnan(x)) | np.isneginf(x)
        # _subsample je stride-sám o sobě deterministický: display[i,j]
        # pochází z sub[i,j], takže kurzor vidí týž pixel v D i v renderu.
        # map_wh jsou rozměry DIVÁKOVA (po zrcadlech i rotaci) — display i
        # ROI se počítají v nich; senzorové (d.shape) by při 90° prohodily
        # osy a škálování rámčku i ořez histogramu sedly vedle.
        self.view.set_image(display, (view_d.shape[1], view_d.shape[0]),
                            densities=sub,
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
        vw, vh = self._viewer_wh()
        fx, fy = w / max(vw, 1), h / max(vh, 1)
        x0, y0, x1, y1 = rect
        sl = (slice(int(y0 * fy), max(int(y1 * fy), 1)),
              slice(int(x0 * fx), max(int(x1 * fx), 1)))
        crop = display[sl]
        self.out_histogram.set_output(crop if crop.size else None, x=x[sl])

    def _whole_wh(self) -> tuple[int, int]:
        """Senzorové (W, H) aktuálního snímku — doména uloženého ROI."""
        assert self._density is not None
        h, w = self._density[0].shape
        return w, h

    def _viewer_wh(self) -> tuple[int, int]:
        """(W, H) náhledu = senzor po zrcadlech; při 90°/270° prohozené."""
        w, h = self._whole_wh()
        if self.project is not None and \
                self.project.frame_rotation(self._current) % 180 == 90:
            return h, w
        return w, h

    def rect_selected(self, rect: tuple[int, int, int, int]) -> None:
        """ROI z Shift+kreslení: přepočítá měření (archiv se krájí) i status.

        Rámeček přišel v souřadnicích otočeného náhledu; senzorový
        (uložitelný) tvar dává ``rect_unapply`` — zpětná cesta řetězcem
        zrcadlo → rotace. Rotace 90° NENÍ involuce (naruby je 270°),
        ``rect_apply`` zpět by rámeček uložel otočený kolem špatného rohu
        (maska v rotovaném náhledu, operátor 2026-09-22).
        """
        item = self.frame_list.currentItem()
        if item is None or self.project is None:
            return
        stored = self.project.rect_unapply(rect, self._whole_wh(),
                                           self.project.frame_rotation(
                                               item.text()))
        assert stored is not None
        self.project.set_rect(item.text(), stored)
        self.project.save_settings()      # rámček přežije reopen
        try:
            self._density = self.project.build_density(item.text(),
                                                       crop=False)
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"Chyba měření: {exc}")
            return
        self._update_density_histogram()
        self.rerender()

    # -------------------------------------------------------------- status

    def _zoom_shown(self, zoom: float) -> None:
        """Záhlaví nad náhledem: kolik procent a jestli je to výhled na fit."""
        pct = round(zoom * 100.0)
        mode = "" if self.view.at_fit else " · ruční"
        if self.chk_fullres.isChecked():
            mode += " · 1:1 plné rozlišení"
        self.lbl_zoom.setText(f"Zoom {pct} %{mode}")

    def _fullres_toggled(self) -> None:
        if self._loading:
            return
        self.rerender()          # přepne podvzorek × plný snímek v náhledu

    def _pixel_hovered(self, payload) -> None:
        """Kurzor v náhledu: čáry v histogramech + čísla v záhlaví.

        ``payload`` je (D, out) z :class:`DensityView`, nebo None mimo snímek
        -- pak se čáry smažou a záhlaví se vrátí na pomlčky. Textová reportáž
        je od roku 2026-09-22 v záhlaví středního panelu (v pravém panelu
        status řádek duplikoval stats a při hoveru přeskakoval nastavovátka).
        """
        if payload is None:
            self.histogram.set_cursor_density(None)
            self.out_histogram.set_cursor_output(None)
            self.lbl_pixel.setText("D: —  out: —")
            return
        d_val, out_val = payload
        self.histogram.set_cursor_density(
            None if not np.isfinite(d_val) else d_val)
        self.out_histogram.set_cursor_output(
            None if not np.isfinite(out_val) else out_val)
        d_txt = "—" if not np.isfinite(d_val) else f"{d_val:.3f} D"
        o_txt = "—" if not np.isfinite(out_val) else f"{out_val:.3f}"
        self.lbl_pixel.setText(f"D: {d_txt}  out: {o_txt}")

    def _dmin_toggled(self) -> None:
        if self._loading:
            return
        auto = self.chk_dmin_auto.isChecked()
        if auto and self._dmin_auto is not None:
            # Zapnutí auta znamená "chci aktuální měření", ne "ponech si
            # co v spinu je" (stejný kontrakt jako _load_params).
            self.spin_dmin.setValue(round(self._dmin_auto, 3))
        self.spin_dmin.setEnabled(not auto)
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

    def _cropped_density(self,
                         border: int = 0) -> tuple[np.ndarray, dens.DensityProvenance] \
            | None:
        """Archiv i rendery se krájí na ROI (vize uživatele); náhled ne.

        ``border`` px navíc na každou stranu (zaškrtátko „včetně okraje",
        rozkaz 2026-09-22) — klade se na STAROU ROI, ne na už oříznutou mapu:
        hustota se počítá z celého snímku, kraj dráždku v ní už je."""
        if self._density is None or self.project is None:
            return None
        return self.project.build_density(self._density[1].source, crop=True,
                                          border=border)

    def save_density(self) -> None:
        # Archiv = měření: okraj fotky sem nepatří ani zaškrtnuté (zdokumentované
        # rozhodnutí — okraj je výpravný trik pro render, ne pro D data).
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
        cropped = self._cropped_density(border=self._border_px())
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
        cropped = self._cropped_density(border=self._border_px())
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
