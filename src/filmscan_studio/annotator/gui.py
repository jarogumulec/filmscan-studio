"""``filmscan-annotate`` — annotator of film metadata.

Between the studio and the developer: open a film folder, see its scans,
add what the picture *is* (title, note, tags, rating, the scene's date and
place, a per-frame 90° rotation) — and write it additively into each frame's
sidecar (see :mod:`filmscan_studio.annotator.store` for the merge-save
contract; the developer reads the blocks at export to fill EXIF, see
Documentation_image_processing/09_anotace_a_exif.md).

Layout (operator's order 2026-09-21 night, Adobe-Bridge style; reordered
2026-09-22): the main area is a thumbnail grid, four thumbs across the
window — no separate file list, no big always-on preview (the old one blew
past the panels at 100%). Hover/select a thumb and **Space** throws it full
screen; Space (or Esc) again returns to the grid. With no folder open the
window shows an empty-state page (big button, last folder remembered)
instead of half a dead splitter. Editing fields own the right side, in the
order the operator thinks in: frame annotation → the shot's EXIF facts
(shooting camera/lens/film ISO — typed by hand, saved to the film block of
every sidecar) → the digitising-rig facts → the whole film log.

Opening a folder whose Film start holds a readable date stamps every frame
that has none yet (sequential minutes, never overwriting a set date);
"cca 2017" is not a readable date and changes nothing (store.auto_date_frames).

Bulk work is the point: select thumbs, fill the shared fields, *Použít na
vybrané*. A date without a time (``1.1.2026``) numbers the selected frames
a minute apart (00:01, 00:02 …) so they keep a sortable order; each bulk
apply restarts the counter, so a second date later on the roll starts
fresh. Title and note transfer in bulk only when explicitly checked.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import numpy as np
from PySide6.QtCore import QSettings, QSize, Qt
from PySide6.QtGui import QImage, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog,
    QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QRadioButton, QScrollArea, QSpinBox, QSplitter,
    QStackedWidget, QVBoxLayout, QWidget,
)

from filmscan_studio.annotator.store import (
    ACQUISITION_FIELDS,
    AnnotatedProject,
    AnnotationItem,
    apply_common,
    auto_date_frames,
    build_common_patch,
    gps_fields,
    load_folder,
    parse_capture_datetime,
    save_acquisition,
    save_annotation,
    save_film,
)
from filmscan_studio.core.models import FrameAnnotation
from filmscan_studio.core.orientation import apply_orientation

THUMB_SRC = 512      # px; cached thumb source, downscaled to the grid cell
THUMB_COLS = 4       # thumbs per row (operator's order 2026-09-21 night)
FULLSCREEN_MAX = 3200  # px; the Space-view pixmap cap (screens are <4k wide)


def _load_preview(path: Path) -> np.ndarray | None:
    """Preview pixels as 0..1 float (grey or RGB) from jpg or tif."""
    try:
        import cv2
        if path.suffix.lower() in (".jpg", ".jpeg"):
            bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if bgr is None:
                return None
            return bgr[:, :, ::-1].astype(np.float64) / 255.0
        import tifffile
        data = np.asarray(tifffile.imread(str(path)), dtype=np.float64)
        data = np.nan_to_num(data, nan=0.0, neginf=0.0, posinf=0.0)
        span = data.max() - data.min()
        if span <= 0:
            return np.zeros(data.shape, dtype=np.float64)
        data = (data - data.min()) / span
        if data.ndim == 2:
            return data
        return np.moveaxis(data, 0, -1) if data.shape[0] in (1, 3, 4) \
            else data
    except Exception as exc:  # noqa: BLE001 - a preview is never fatal
        logging.getLogger(__name__).warning("preview %s: %s", path, exc)
        return None


def _to_qimage(array: np.ndarray) -> QImage:
    """0..1 float grey/RGB -> QImage owning its buffer (gui.imageutil shape,
    with RGB kept RGB — the capture jpgs are colour)."""
    a = np.clip(np.asarray(array, dtype=np.float64), 0.0, 1.0)
    if a.ndim == 2:
        rgb = np.repeat((a * 255.0 + 0.5).astype(np.uint8)[:, :, None],
                        3, axis=2)
    else:
        rgb = (a * 255.0 + 0.5).astype(np.uint8)[:, :, :3]
    h, w, _ = rgb.shape
    rgba = np.dstack([rgb, np.full((h, w), 255, dtype=np.uint8)])
    return QImage(np.ascontiguousarray(rgba).data, w, h, 4 * w,
                  QImage.Format.Format_RGBA8888).copy()


def _rotate_cw(array: np.ndarray, degrees: int) -> np.ndarray:
    """Rotate clockwise by a multiple of 90 (rot90 turns counter-clockwise)."""
    k = (int(degrees) // 90) % 4
    return np.rot90(array, -k) if k else array


class ThumbGrid(QListWidget):
    """Bridge-style thumbnail grid: THUMB_COLS across, Space = full screen.

    It replaces the old left file list *and* the always-on centre preview —
    one widget owns the main area, multi-select (for *Použít na vybrané*)
    included.
    """

    def __init__(self, on_space, on_columns_changed=None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._on_space = on_space
        self._on_columns_changed = on_columns_changed
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setFlow(QListWidget.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setUniformItemSizes(True)
        self.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection)
        self.setSpacing(10)
        self.setIconSize(QSize(220, 150))
        self.setGridSize(QSize(240, 196))
        self.setWordWrap(True)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.key() == Qt.Key.Key_Space:
            self._on_space()   # never reaches the view's own handling
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self.relayout_columns()

    def relayout_columns(self) -> None:
        """Four cells per row whatever the window width (thumb keeps 3:2)."""
        margin = self.spacing() * 2 + 8
        w = max(self.viewport().width() // THUMB_COLS - margin, 96)
        h = int(w * 0.66)
        self.setIconSize(QSize(w, h))
        self.setGridSize(QSize(w + margin, h + 46))   # + the name row
        if self._on_columns_changed is not None:
            self._on_columns_changed()   # rescale cached icons to the new cell


class FullscreenViewer(QWidget):
    """Space-in, Space (or Esc) out — the frame big, nothing else."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("Snímek — Space zpět")
        self._pix: QPixmap | None = None
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        self.lbl = QLabel("(žádný snímek)")
        self.lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(self.lbl, stretch=1)
        self.lbl_caption = QLabel("")
        self.lbl_caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_caption.setStyleSheet(
            "background: rgba(0,0,0,150); color: white; padding: 6px;")
        col.addWidget(self.lbl_caption)

    def show_frame(self, pix: QPixmap | None, caption: str) -> None:
        self._pix = pix
        self.lbl_caption.setText(caption)
        self._rescale()

    def _rescale(self) -> None:
        if self._pix is None or self._pix.isNull():
            self.lbl.setText("(náhled nelze zobrazit)")
            self.lbl.setPixmap(QPixmap())
            return
        size = self.size()
        scaled = self._pix.scaled(
            max(size.width(), 64), max(size.height() - 40, 64),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self.lbl.setPixmap(scaled)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._rescale()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Escape,
                           Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.close()
            return
        super().keyPressEvent(event)


class MainWindow(QMainWindow):
    def __init__(self, project: AnnotatedProject | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Filmscan Studio — anotátor metadat")
        self.resize(1600, 900)
        self.project: AnnotatedProject | None = None
        self.current: AnnotationItem | None = None
        self.current_row: int = -1
        self._loading_form = True   # until the panels are built (Qt signals fire)
        self._thumbs: dict[int, QPixmap] = {}   # row -> cached source thumb
        self._fs = FullscreenViewer(self)      # before the panel: signals fire

        central = QWidget()
        self.setCentralWidget(central)   # parented or the GC deletes the tree
        outer = QVBoxLayout(central)
        bar = QHBoxLayout()
        self.btn_open = QPushButton("Otevřít složku…")
        self.btn_open.clicked.connect(self._open_folder)
        bar.addWidget(self.btn_open)
        self.btn_reopen = QPushButton("Znovu otevřít poslední")
        self.btn_reopen.clicked.connect(self._reopen_last)
        bar.addWidget(self.btn_reopen)
        hint = QLabel(f"dvojklik nebo mezerník = fullscreen, mezerník "
                      "znovu = zpět · "
                      f"{THUMB_COLS} náhledy na řádek")
        hint.setStyleSheet("color: gray;")
        bar.addWidget(hint)
        bar.addStretch(1)
        outer.addLayout(bar)

        splitter = QSplitter()

        # -- main area: the thumbnail grid (replaces list + big preview)
        self.grid = ThumbGrid(
            self.toggle_fullscreen,
            on_columns_changed=lambda: [
                self._apply_icon(r) for r in self._thumbs])
        self.grid.currentItemChanged.connect(self._frame_selected)
        self.grid.itemDoubleClicked.connect(
            lambda _item: self.toggle_fullscreen())
        splitter.addWidget(self.grid)

        # -- right: the editing panel
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(self._build_panel())
        splitter.addWidget(scroll)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)   # grid ~3/5, panel ~2/5
        splitter.setSizes([980, 620])

        # An empty app was half a dead window with one small button up top
        # (operator 2026-09-22): page 0 is a real empty state — big button,
        # last folder — page 1 the working splitter.
        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_empty_page())
        self.stack.addWidget(splitter)
        outer.addWidget(self.stack)
        self._last_folder = str(QSettings("FilmscanStudio",
                                          "filmscan-annotate")
                                .value("last_folder", ""))
        self.btn_reopen.setEnabled(bool(self._last_folder))
        self._show_empty(not self._has_project(project))

        self._loading_form = False
        self.statusBar().showMessage("Připraven — otevři složku s filmem.")
        if self._has_project(project):
            self.set_project(project)

    @staticmethod
    def _has_project(project: "AnnotatedProject | None"
                     ) -> bool:
        """A folder that opened but holds zero scans is an empty app too."""
        return project is not None and bool(project.items)

    def _build_empty_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.addStretch(3)
        title = QLabel("Filmscan Studio — anotátor metadat")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 20px; color: gray;")
        col.addWidget(title)
        self.lbl_last = QLabel("")
        self.lbl_last.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_last.setStyleSheet("color: gray;")
        col.addWidget(self.lbl_last)
        col.addSpacing(12)
        btn = QPushButton("Otevřít složku s filmem…")
        btn.setMinimumHeight(48)
        btn.setMaximumWidth(360)
        btn.setStyleSheet("font-size: 16px;")
        btn.clicked.connect(self._open_folder)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(btn)
        row.addStretch(1)
        col.addLayout(row)
        col.addStretch(5)
        return page

    def _show_empty(self, empty: bool) -> None:
        self.stack.setCurrentIndex(0 if empty else 1)
        if empty and self._last_folder:
            self.lbl_last.setText(f"Naposledy otevřeno: {self._last_folder}")

    def _remember_folder(self, folder: str | Path) -> None:
        self._last_folder = str(folder)
        QSettings("FilmscanStudio", "filmscan-annotate").setValue(
            "last_folder", self._last_folder)
        self.btn_reopen.setEnabled(True)

    def _reopen_last(self) -> None:
        folder = Path(self._last_folder) if self._last_folder else None
        if folder is None or not folder.is_dir():
            QMessageBox.warning(
                self, "Poslední složka",
                f"Složku nelze otevřít:\n{self._last_folder}")
            return
        try:
            project = load_folder(folder)
        except ValueError as exc:
            QMessageBox.critical(self, "Složku nelze otevřít", str(exc))
            return
        if not project.items:
            QMessageBox.information(
                self, "Složka neobsahuje snímky",
                f"Ve složce {folder} nejsou žádné skeny.")
            return
        self.set_project(project)

    # ------------------------------------------------------------------ panel

    def _build_panel(self) -> QWidget:
        holder = QWidget()
        col = QVBoxLayout(holder)

        # Order per operator 2026-09-22: what the picture *is* (name, geo,
        # rating), then the photographic EXIF facts (camera, lens, film ISO),
        # then the digitising-rig facts, then the whole film log. The shooting
        # camera lives in the EXIF box and is typed by hand ("Nikon FM2") —
        # the acquisition camera (ATR2600M) is a different machine.
        col.addWidget(self._build_frame_box())
        col.addWidget(self._build_shot_box())
        col.addWidget(self._build_acquisition_box())
        col.addWidget(self._build_film_box())
        col.addStretch(1)
        return holder

    # ---------------------------------------------------------- frame box

    def _build_frame_box(self) -> QGroupBox:
        box = QGroupBox("Snímek")
        form = QFormLayout(box)

        self.ed_title = QLineEdit()
        self.ed_note = QPlainTextEdit()      # the comment — give it room
        self.ed_note.setFixedHeight(110)
        self.ed_note.setPlaceholderText(
            "Komentář. Při exportu se píše do EXIF komentáře; ostatní "
            "údaje (digitis. kamera, objektiv, vývoj…) se za něj "
            "dopisují za středníky.")
        self.ed_tags = QLineEdit()
        self.ed_tags.setPlaceholderText("odděleno ;")
        self.ed_date = QLineEdit()
        self.ed_date.setPlaceholderText("12.8.1968 | 1968-08-12 14:30 | 1968")
        self.ed_gps = QLineEdit()
        self.ed_gps.setPlaceholderText("49.1124306N, 9.7371244E")
        self.lbl_gps = QLabel("—")
        self.lbl_gps.setWordWrap(True)
        self.lbl_gps.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)

        self.spin_rating = QSpinBox()
        self.spin_rating.setRange(0, 5)
        # Rating 0 is a real verdict; "unset" needs its own switch so an
        # untouched spin does not silently write a 0 into every frame.
        self.chk_rating_use = QCheckBox("použít")
        rating_w = QWidget()
        rating_row = QHBoxLayout(rating_w)
        rating_row.setContentsMargins(0, 0, 0, 0)
        rating_row.addWidget(self.spin_rating)
        rating_row.addWidget(self.chk_rating_use)

        # Per-frame rotation: which way is up. Recorded, applied to previews
        # and (later) exports — the density archive stays raw.
        self.rot_group = QButtonGroup(self)
        rot_w = QWidget()
        rot_row = QHBoxLayout(rot_w)
        rot_row.setContentsMargins(0, 0, 0, 0)
        for label, value in (("0°", 0), ("⟲ 90°", 270), ("180°", 180),
                             ("⟳ 90°", 90)):
            rb = QRadioButton(label)
            rb.setProperty("degrees", value)
            rb.toggled.connect(self._rotation_changed)
            self.rot_group.addButton(rb, value)
            rot_row.addWidget(rb)
        self.rot_group.button(0).setChecked(True)

        form.addRow("Název", self.ed_title)
        form.addRow("Popis / komentář", self.ed_note)
        form.addRow("Štítky", self.ed_tags)
        form.addRow("Datum záběru", self.ed_date)
        form.addRow("", self._hint_widget())
        form.addRow("GPS", self.ed_gps)
        form.addRow("Geo rozklad", self.lbl_gps)
        form.addRow("Hodnocení", rating_w)
        form.addRow("Otočit", rot_w)

        # Bulk-transfer opt-ins: per-frame titles survive "apply to selected"
        # unless the operator asks for them to travel (user order 2026-09-21:
        # title/note must be bulk-applicable too — but never by accident).
        self.chk_transfer_title = QCheckBox("přenášet název")
        self.chk_transfer_note = QCheckBox("přenášet popis")
        transfer_row = QHBoxLayout()
        transfer_row.addWidget(self.chk_transfer_title)
        transfer_row.addWidget(self.chk_transfer_note)
        transfer_row.addStretch(1)
        transfer_w = QWidget()
        transfer_w.setLayout(transfer_row)
        form.addRow("Hromadně", transfer_w)

        btns = QHBoxLayout()
        self.btn_save = QPushButton("Uložit aktuální")
        self.btn_save.clicked.connect(self._save_current)
        self.btn_apply = QPushButton("Použít na vybrané")
        self.btn_apply.setToolTip(
            "Přepíše datum / geo / hodnocení / štítky u všech vybraných "
            "snímků; prázdná pole nechává být. Název a popis se přenášejí "
            "jen zaškrtnutím políčka „přenášet název / popis“.")
        self.btn_apply.clicked.connect(self._apply_to_selected)
        self.btn_reload = QPushButton("Znovu načíst")
        self.btn_reload.clicked.connect(lambda: self._frame_selected(
            self.grid.currentItem(), None))
        btns.addWidget(self.btn_save)
        btns.addWidget(self.btn_apply)
        btns.addWidget(self.btn_reload)
        form.addRow(btns)
        return box

    @staticmethod
    def _hint_widget() -> QWidget:
        lbl = QLabel("bez času → po „Použít na vybrané“ se snímky seřadí "
                     "po minutách (0:01, 0:02…)")
        lbl.setStyleSheet("color: gray;")
        return lbl

    def _rotation_changed(self) -> None:
        if not self._loading_form:
            self._regen_thumb(self.current_row)
            if self._fs.isVisible():
                self._fs_show_current()

    # ---------------------------------------------------------- shot box

    def _build_shot_box(self) -> QGroupBox:
        """Photographic facts that travel into EXIF at export.

        Film-level fields (a roll is shot with one body, one lens, one film)
        — editing here edits the film block of every sidecar, exactly like
        the Film box below, only pulled up next to the frame's own date/geo
        because that is where the operator thinks of them together
        (2026-09-22). The values themselves stay English/machine-neutral in
        the metadata; only the labels are Czech.
        """
        box = QGroupBox("Záběr (foťák na filmu — jde do EXIFu, uloží se "
                        "do všech snímků)")
        form = QFormLayout(box)
        self.ed_shot: dict[str, QLineEdit] = {}
        for key, label in (("camera", "Foťák"),
                           ("shooting_lens", "Objektiv"),
                           ("film_iso", "ISO filmu")):
            edit = QLineEdit()
            edit.setPlaceholderText(
                "Nikon FM2" if key == "camera" else
                "Nikkor 50/2" if key == "shooting_lens" else "100")
            self.ed_shot[key] = edit
            form.addRow(label, edit)
        self.btn_save_shot = QPushButton("Uložit záběr do všech snímků")
        self.btn_save_shot.clicked.connect(self._save_shot)
        form.addRow(self.btn_save_shot)
        return box

    def _save_shot(self) -> None:
        if self.project is None:
            return
        patch = {key: edit.text().strip()
                 for key, edit in self.ed_shot.items()}
        try:
            count = save_film(self.project, patch)
        except (ValueError, OSError) as exc:
            QMessageBox.critical(self, "Záběr", f"Nelze zapsat:\n{exc}")
            return
        self.statusBar().showMessage(
            f"Záběr zapsán do {count} sidecarů")

    # ------------------------------------------------------- acquisition box

    def _build_acquisition_box(self) -> QGroupBox:
        box = QGroupBox("Digitalizace (údaje ze studia, editovatelné)")
        form = QFormLayout(box)
        self.ed_acq: dict[str, QLineEdit] = {}
        labels = {
            "camera": "Digitis. kamera",
            "camera_serial": "S/N kamery",
            "exposure_time": "Expozice [s]",
            "gain": "Zesílení [×]",
            "capture_date": "Čas digitalizace",
            "copy_number": "Kopie",
        }
        for key in ACQUISITION_FIELDS:
            edit = QLineEdit()
            self.ed_acq[key] = edit
            form.addRow(labels[key], edit)
        # The operator read "(ISO)" as film speed and the offset as noise —
        # both were the machine's timestamp (2026-09-22). The raw ISO-8601
        # string stays (it round-trips through the model untouched); the
        # tooltip says what it is.
        self.ed_acq["capture_date"].setToolTip(
            "Kdy rig exponoval tento TIFF (strojový čas digitalizace).\n"
            "ISO 8601 s časovým pásmem: +02:00 = náš letní čas.\n"
            "Datum záběru (ruční) je v bloku Snímek.")
        self.btn_save_acq = QPushButton("Uložit akvizici tohoto snímku")
        self.btn_save_acq.clicked.connect(self._save_acquisition)
        form.addRow(self.btn_save_acq)
        return box

    # ------------------------------------------------------------- film box

    def _build_film_box(self) -> QGroupBox:
        box = QGroupBox("Film (uloží se do všech snímků)")
        grid = QGridLayout(box)
        grid.setColumnStretch(1, 1)
        self.ed_film: dict[str, QLineEdit] = {}
        self.cmb_film_class = QComboBox()
        from filmscan_studio.core.models import FilmType
        for ft in FilmType:
            self.cmb_film_class.addItem(ft.value, ft.value)
        # camera / shooting_lens / film_iso moved up to the Záběr box
        # (2026-09-22); they are still FILM_FIELDS and still saved by
        # save_film — the two boxes edit one block, never the same keys.
        rows = [
            ("film_name", "Páska"),
            ("format", "Formát"),
            ("film_type_class", None),            # combo
            ("development", "Vývojka"),
            ("development_start", "Film start"),
            ("development_end", "Film end"),
            ("content", "Obsah"),
            ("digitising_lens", "Digitalis. objektiv"),
            ("digitising_light", "Digitalis. světlo"),
            ("digitising_holder", "Digitalis. držák"),
            ("digitisation_date", "Datum digitalisace"),
            ("operator", "Operátor"),
            ("box_number", "Číslo krabičky"),
            ("expiry", "Expirace"),
            ("pushed_stops", "Push [stopy]"),
        ]
        r = 0
        for key, label in rows:
            if label is None:
                grid.addWidget(QLabel("Třída filmu"), r, 0)
                grid.addWidget(self.cmb_film_class, r, 1)
            else:
                edit = QLineEdit()
                self.ed_film[key] = edit
                grid.addWidget(QLabel(label), r, 0)
                grid.addWidget(edit, r, 1)
            r += 1
        self.ed_film_notes = QPlainTextEdit()
        self.ed_film_notes.setFixedHeight(70)
        grid.addWidget(QLabel("Poznámky filmu"), r, 0)
        grid.addWidget(self.ed_film_notes, r, 1)
        r += 1
        self.btn_save_film = QPushButton("Uložit film do všech snímků")
        self.btn_save_film.setToolTip(
            "Filmový blok je zopakován v každém sidecaru — editace se "
            "zapíše do všech a do project.json.")
        self.btn_save_film.clicked.connect(self._save_film)
        grid.addWidget(self.btn_save_film, r, 0, 1, 2)
        return box

    # ------------------------------------------------------------- folder I/O

    def _open_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Složka s filmem (frames/ + sidecary)")
        if not folder:
            return
        try:
            project = load_folder(folder)
        except ValueError as exc:
            QMessageBox.critical(self, "Složku nelze otevřít", str(exc))
            return
        if not project.items:
            QMessageBox.information(
                self, "Složka neobsahuje snímky",
                f"Ve složce {folder} nejsou žádné skeny (frames/*.tif "
                "s kind=scan).")
            return
        self.set_project(project)

    def set_project(self, project: AnnotatedProject) -> None:
        self.project = project
        self.current_row = -1   # stale rows of the previous project must not
        self.current = None     # borrow its radio state via _rotation_for
        self._show_empty(False)
        self._remember_folder(project.root)   # a CLI-opened folder counts too
        # A readable Film start dates the whole roll on open (operator
        # 2026-09-22): frames that already carry a date are never touched.
        stamped, film_date = auto_date_frames(project)
        self.grid.clear()
        self._thumbs.clear()
        for row, item in enumerate(project.items):
            QListWidgetItem(self._list_label(item), self.grid)
            self._regen_thumb(row)
        self.grid.relayout_columns()
        note = f"{len(project.items)} snímků"
        if project.oriented:
            note += " · náhledy dle orientace filmu"
        if stamped:
            note += (f" · {stamped} datováno z Film startu {film_date} "
                     "(sekvenčně po minutách, uprav si ručně)")
        self.statusBar().showMessage(f"Otevřeno: {project.root} — {note}")
        self._load_film_form()
        if project.items:
            self.grid.setCurrentRow(0)

    def _list_label(self, item: AnnotationItem) -> str:
        mark = "●" if item.is_annotated else "○"
        ann = item.annotation or {}
        marks = ""
        if ann.get("capture_datetime"):
            marks += " 📅"
        if ann.get("gps_lat"):
            marks += " 📍"
        if ann.get("rotation_degrees"):
            marks += " ⟳"
        stem = Path(item.name).stem
        return f"{mark} {stem}{marks}"

    def _refresh_row(self, item: AnnotationItem) -> None:
        if self.project is None:
            return
        try:
            row = self.project.items.index(item)
        except ValueError:
            return
        self.grid.item(row).setText(self._list_label(item))
        self._regen_thumb(row)

    # -------------------------------------------------------------- thumbnails

    def _rotation_for(self, row: int) -> int:
        """Rotation the grid must show for *row* — the radio button wins on
        the selected frame. Reading the sidecar alone meant a 90° click only
        rotated the thumbnail after Uložit (operator complaint 2026-09-22:
        „klidnu otočit 90 st ať se otočí i náhledový thumbnail“)."""
        if row == self.current_row and not self._loading_form:
            return int(self.rot_group.checkedId() or 0) % 360
        if self.project is None:
            return 0
        ann = self.project.items[row].annotation or {}
        return int(ann.get("rotation_degrees") or 0) % 360

    def _oriented_pixels(self, row: int,
                         max_px: int) -> np.ndarray | None:
        """Frame pixels with film orientation + per-frame rotation applied."""
        if self.project is None or not (0 <= row < len(self.project.items)):
            return None
        item = self.project.items[row]
        pixels = _load_preview(item.preview_path or item.image_path)
        if pixels is None:
            return None
        pixels = apply_orientation(
            pixels,
            mirrored_horizontal=self.project.mirrored_horizontal,
            mirrored_vertical=self.project.mirrored_vertical,
            rotated_180=self.project.rotated_180)
        pixels = _rotate_cw(pixels, self._rotation_for(row))
        h, w = pixels.shape[:2]
        if max(h, w) > max_px:            # downscale before QImage to stay quick
            step = max(1, int(np.ceil(max(h, w) / max_px)))
            pixels = pixels[::step, ::step]
        return pixels

    def _regen_thumb(self, row: int) -> None:
        """Rebuild the cached thumbnail pixmap + icon for one row."""
        if self.project is None or not (0 <= row < self.grid.count()):
            return
        pixels = self._oriented_pixels(row, THUMB_SRC)
        if pixels is None:
            self._thumbs.pop(row, None)
            return
        pix = QPixmap.fromImage(_to_qimage(pixels))
        self._thumbs[row] = pix
        self._apply_icon(row)

    def _apply_icon(self, row: int) -> None:
        item = self.grid.item(row)
        pix = self._thumbs.get(row)
        if item is None or pix is None:
            return
        icon_size = self.grid.iconSize()
        item.setIcon(QPixmap.fromImage(pix.toImage().scaled(
            icon_size, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)))

    def _regen_all_thumbs(self) -> None:
        for row in range(len(self.project.items) if self.project else 0):
            self._regen_thumb(row)

    # ------------------------------------------------------------------ fullscreen

    def toggle_fullscreen(self) -> None:
        """Space in the grid: show the selected frame full screen; again: back."""
        if self._fs.isVisible():
            self._fs.close()
            return
        if self.project is None or not (0 <= self.current_row
                                        < len(self.project.items)):
            return
        self._fs_show_current()

    def _fs_show_current(self) -> None:
        item = self.project.items[self.current_row]
        pixels = self._oriented_pixels(self.current_row, FULLSCREEN_MAX)
        pix = QPixmap.fromImage(_to_qimage(pixels)) if pixels is not None \
            else None
        ann = item.annotation or {}
        stem = Path(item.name).stem
        title = str(ann.get("title") or "").strip()
        date = str(ann.get("capture_datetime") or "—")
        parts = [stem + (f" — {title}" if title else ""), f"📅 {date}"]
        if ann.get("gps_lat"):
            parts.append(
                f"📍 {ann['gps_lat'].rstrip('0').rstrip('.')} "
                f"{ann.get('gps_lat_ref', '')} "
                f"{ann['gps_lon'].rstrip('0').rstrip('.')} "
                f"{ann.get('gps_lon_ref', '')}")
        deg = int(ann.get("rotation_degrees") or 0)
        if deg:
            parts.append(f"⟳ {deg}°")
        self._fs.show_frame(pix, "    ".join(parts))
        self._fs.showFullScreen()

    # ------------------------------------------------------------ frame <-> form

    def _frame_selected(self, item: QListWidgetItem | None,
                        _prev: QListWidgetItem | None) -> None:
        if self.project is None or item is None:
            return
        row = self.grid.row(item)
        self.current_row = row
        self.current = self.project.items[row]
        ann = self.current.annotation or {}
        self._loading_form = True
        self.ed_title.setText(str(ann.get("title", "")))
        self.ed_note.setPlainText(str(ann.get("note", "")))
        tags = ann.get("tags") or []
        self.ed_tags.setText("; ".join(str(t) for t in tags))
        self.ed_date.setText(str(ann.get("capture_datetime", "")))
        self.ed_gps.setText(str(ann.get("gps_input", "")))
        rating = ann.get("rating")
        self.spin_rating.setValue(int(rating) if rating is not None else 0)
        self.chk_rating_use.setChecked(rating is not None)
        deg = int(ann.get("rotation_degrees") or 0) % 360
        (self.rot_group.button(deg) or self.rot_group.button(0)
         ).setChecked(True)
        self._loading_form = False
        self._refresh_gps_label()
        self._load_acq_form()
        self.statusBar().showMessage(
            f"Načteno: {self.current.name} — mezerník = fullscreen")

    def _refresh_gps_label(self) -> None:
        text = self.ed_gps.text().strip()
        if not text:
            self.lbl_gps.setText("—")
            return
        try:
            fields = gps_fields(text)
        except ValueError as exc:
            self.lbl_gps.setText(f"✗ {exc}")
            return
        self.lbl_gps.setText(
            f"{fields['gps_lat']} {fields['gps_lat_ref']} · "
            f"{fields['gps_lon']} {fields['gps_lon_ref']}\n"
            f"{fields['gps_lat_exif_dms']} · {fields['gps_lon_exif_dms']}")

    # ------------------------------------------------------------ sub-forms

    def _load_acq_form(self) -> None:
        acq = (self.current.record.get("acquisition") or {}) if self.current \
            else {}
        for key, edit in self.ed_acq.items():
            value = acq.get(key)
            if key == "capture_date" and value:
                value = str(value)
            edit.setText("" if value is None else str(value))

    def _load_film_form(self) -> None:
        film = {}
        if self.project and self.project.items:
            film = self.project.items[0].record.get("film") or {}
        for key, edit in self.ed_film.items():
            value = film.get(key)
            edit.setText("" if value is None else str(value))
        for key, edit in self.ed_shot.items():   # same block, upper box
            value = film.get(key)
            edit.setText("" if value is None else str(value))
        idx = self.cmb_film_class.findData(
            film.get("film_type_class") or "bw_negative")
        self.cmb_film_class.setCurrentIndex(max(0, idx))
        self.ed_film_notes.setPlainText(str(film.get("notes") or ""))

    # ---------------------------------------------------------------- collect

    def _form_values(self) -> dict:
        tags = [t.strip() for t in self.ed_tags.text().split(";") if t.strip()]
        return {
            "title": self.ed_title.text().strip(),
            "note": self.ed_note.toPlainText().strip(),
            "tags": tags,
            "capture_datetime": parse_capture_datetime(self.ed_date.text()),
            "gps_input": self.ed_gps.text().strip(),
            "rating": (self.spin_rating.value()
                       if self.chk_rating_use.isChecked() else None),
            "rotation_degrees": self.rot_group.checkedId(),
        }

    def _annotation_from_form(self) -> dict:
        values = self._form_values()
        fields = gps_fields(values.pop("gps_input"))
        return FrameAnnotation(**values, **fields).model_dump(mode="json")

    # ------------------------------------------------------------------ save

    def _save_current(self) -> None:
        if self.current is None:
            QMessageBox.information(self, "Uložit", "Vyber snímek.")
            return
        try:
            annotation = self._annotation_from_form()
        except ValueError as exc:
            QMessageBox.critical(self, "Uložit", str(exc))
            return
        try:
            save_annotation(self.current, annotation)
        except OSError as exc:
            QMessageBox.critical(self, "Uložit", f"Zápis selhal: {exc}")
            return
        self._refresh_row(self.current)
        self.statusBar().showMessage(
            f"Uloženo: {self.current.sidecar_path.name}")

    def _apply_to_selected(self) -> None:
        if self.project is None:
            return
        rows = sorted({self.grid.row(i)
                       for i in self.grid.selectedItems()})
        if not rows:
            QMessageBox.information(self, "Použít na vybrané",
                                    "Vyber jeden nebo více snímků.")
            return
        try:
            patch = build_common_patch(
                self._form_values(),
                transfer={"title": self.chk_transfer_title.isChecked(),
                          "note": self.chk_transfer_note.isChecked()})
        except ValueError as exc:
            QMessageBox.critical(self, "Použít na vybrané", str(exc))
            return
        if not patch:
            QMessageBox.information(
                self, "Použít na vybrané",
                "Ve formuláři není žádná společná hodnota (datum / GPS / "
                "hodnocení / štítky).")
            return
        items = [self.project.items[r] for r in rows]
        try:
            count = apply_common(items, patch)
        except (ValueError, OSError) as exc:
            QMessageBox.critical(self, "Použít na vybrané", str(exc))
            return
        for item in items:
            self._refresh_row(item)
        if self.current in items:
            self._frame_selected(self.grid.currentItem(), None)
        msg = f"Společná data zapsána do {count} sidecarů"
        if patch.get("_sequential_time"):
            msg += " · časování po minutách (0:01, 0:02, …)"
        self.statusBar().showMessage(msg)

    def _save_acquisition(self) -> None:
        if self.current is None:
            QMessageBox.information(self, "Akvizice", "Vyber snímek.")
            return
        patch = {key: edit.text().strip()
                 for key, edit in self.ed_acq.items()}
        try:
            save_acquisition(self.current, patch)
        except ValueError as exc:
            QMessageBox.critical(
                self, "Akvizice",
                f"Hodnoty nelze zapsat (číselná pole musí být čísla):\n{exc}")
            return
        except OSError as exc:
            QMessageBox.critical(self, "Akvizice", f"Zápis selhal: {exc}")
            return
        self.statusBar().showMessage(
            f"Akvizice uložena: {self.current.sidecar_path.name}")

    def _save_film(self) -> None:
        if self.project is None:
            return
        patch = {key: edit.text().strip()
                 for key, edit in self.ed_film.items()}
        patch["film_type_class"] = self.cmb_film_class.currentData()
        patch["notes"] = self.ed_film_notes.toPlainText().strip()
        try:
            count = save_film(self.project, patch)
        except ValueError as exc:
            QMessageBox.critical(self, "Film",
                                 f"Film nelze zapsat:\n{exc}")
            return
        except OSError as exc:
            QMessageBox.critical(self, "Film", f"Zápis selhal: {exc}")
            return
        # Orientation may have been edited — every thumbnail follows it.
        self._regen_all_thumbs()
        self.statusBar().showMessage(f"Film zapsán do {count} sidecarů")

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API
        """Cmd/Ctrl+S saves the current frame — annotation is a lot of saving."""
        if event.matches(QKeySequence.StandardKey.Save):
            self._save_current()
            return
        super().keyPressEvent(event)


def main(argv: list[str] | None = None) -> int:
    """``filmscan-annotate [složka_projektu]``."""
    logging.basicConfig(level=logging.INFO)
    args = list(sys.argv[1:] if argv is None else argv)
    app = QApplication.instance() or QApplication(args)
    project = None
    if args and not args[0].startswith("-"):
        try:
            project = load_folder(args[0])
        except Exception as exc:  # noqa: BLE001
            print(f"Složku nelze otevřít: {exc}", file=sys.stderr)
            return 2
    win = MainWindow(project=project)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
