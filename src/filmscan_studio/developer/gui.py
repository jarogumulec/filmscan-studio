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
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGroupBox, QLabel, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QPushButton, QSlider, QSplitter, QVBoxLayout, QWidget,
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
        self.setMinimumSize(320, 240)

    # ---------------------------------------------------------------- model

    def set_image(self, image: np.ndarray | None,
                  map_wh: tuple[int, int] | None) -> None:
        """Podvzorek náhledu 0..1 (s NaN) + rozměry mapy, ze které vzešel."""
        self._map_wh = map_wh
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
                       "Otevři složku projektu (s frames/*.tif)")
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
        self.view = DensityView()
        self.view.rect_chosen.connect(self.rect_selected)
        splitter.addWidget(self.view)

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
        self.lbl_ev = QLabel("0,0 EV")
        self.sl_ev = self._slider(-60, 60, 0)      # EV po třetinách
        self.btn_defaults = QPushButton("Proposal (auto body, linear)")
        self.btn_defaults.clicked.connect(self.apply_defaults)
        form.addRow("Dmin [D]", self.spin_dmin)
        form.addRow("", self.chk_dmin_auto)
        form.addRow("Dmax [D]", self.spin_dmax)
        form.addRow("", self.chk_dmax_auto)
        form.addRow("Expozice", self.sl_ev)
        form.addRow("", self.lbl_ev)
        form.addRow("", self.btn_defaults)

        self.spin_dmin.valueChanged.connect(self._setting_changed)
        self.spin_dmax.valueChanged.connect(self._setting_changed)
        self.sl_ev.valueChanged.connect(self._ev_moved)
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
        return rnd.RenderParams(
            dmin=dmin,
            dmax=self.project.suggested_dmax(name),
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
        self.sl_ev.setValue(int(round(params.exposure_ev * 3.0)))
        self.lbl_ev.setText(f"{params.exposure_ev:+.1f} EV")
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
            exposure_ev=self.sl_ev.value() / 3.0,
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
        display = rnd.render_for_display(
            _subsample(self.project.orientation_apply(d), PREVIEW_MAX_DIM),
            params)
        self.view.set_image(display, (d.shape[1], d.shape[0]))

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
        self.rerender()

    # -------------------------------------------------------------- status

    def _ev_moved(self, value: int) -> None:
        self.lbl_ev.setText(f"{value / 3.0:+.1f} EV")
        self._setting_changed()

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
            self.spin_dmax.setValue(
                round(self.project.suggested_dmax(self._current), 3))
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
        nan_fraction = float(np.isnan(d).mean())
        if nan_fraction > 0.0:
            parts.append(f"purpur (bez světla) {nan_fraction * 100:.1f} %")
        if s["valid_fraction"] < 0.85:
            parts.append("⚑ málo platných pixelů — zkontroluj ROI")
        if s["valid_fraction"] > 0 and s["d_p50"] < 0.05:
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
