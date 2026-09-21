"""``filmscan-annotate`` — annotator of film metadata.

Between the studio and the developer: open a film folder, see its scans
with previews, add what the picture *is* (title, note, tags, rating, the
scene's date and place) — and write it additively into each frame's sidecar
under ``annotation`` (see :mod:`filmscan_studio.annotator.store` for the
merge-save contract; the developer reads the block at export to fill EXIF).

Bulk work is the point (ported from the scanner toolbox annotator): select
frames in the list, fill the shared fields, *Použít na vybrané* — only the
fields that carry a value are written (date/geo/rating/tags travel; per-frame
title and note never do). Datetime is normalised to EXIF
``YYYY:MM:DD HH:MM:SS`` — a bare year ``1968`` becomes ``1968:01:01`` so the
pictures still sort correctly in any photo manager.

Previews are the capture app's quick JPEGs when present (fast), else the
archival TIFF decoded on demand, and shown through the film's recorded
orientation so the operator sees the picture the way the developer renders
it.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from filmscan_studio.annotator.store import (
    AnnotatedProject,
    AnnotationItem,
    apply_common,
    build_common_patch,
    gps_fields,
    load_folder,
    parse_capture_datetime,
    save_annotation,
)
from filmscan_studio.core.models import FrameAnnotation
from filmscan_studio.core.orientation import apply_orientation

PREVIEW_MAX = 640


def _load_preview(path: Path) -> np.ndarray | None:
    """Preview pixels as 0..1 float (grey or RGB) from jpg or tif."""
    try:
        if path.suffix.lower() in (".jpg", ".jpeg"):
            import cv2
            bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if bgr is None:
                return None
            return bgr[:, :, ::-1].astype(np.float64) / 255.0
        import cv2
        import tifffile
        data = tifffile.imread(str(path))
        data = np.asarray(data, dtype=np.float64)
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


class MainWindow(QMainWindow):
    def __init__(self, project: AnnotatedProject | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Filmscan Studio — anotátor metadat")
        self.resize(1280, 860)
        self.project: AnnotatedProject | None = None
        self.current: AnnotationItem | None = None
        self._pixmap: QPixmap | None = None   # keep the preview alive
        self._loading_form = False

        central = QWidget()
        root = QHBoxLayout(central)
        left = QVBoxLayout()
        right = QVBoxLayout()
        root.addLayout(left, stretch=1)
        root.addLayout(right, stretch=2)
        self.setCentralWidget(central)

        bar = QHBoxLayout()
        self.btn_open = QPushButton("Otevřít složku…")
        self.btn_open.clicked.connect(self._open_folder)
        bar.addWidget(self.btn_open)
        bar.addStretch(1)
        left.addLayout(bar)

        self.frame_list = QListWidget()
        self.frame_list.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection)
        self.frame_list.currentItemChanged.connect(self._frame_selected)
        left.addWidget(self.frame_list, stretch=1)

        self.preview_label = QLabel("(bez náhledu)")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumSize(PREVIEW_MAX // 2, PREVIEW_MAX // 2)
        left.addWidget(self.preview_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(self._build_form())
        right.addWidget(scroll)

        self.statusBar().showMessage("Připraven — otevři složku s filmem.")
        if project is not None:
            self.set_project(project)

    # ------------------------------------------------------------------ form

    def _build_form(self) -> QWidget:
        box = QGroupBox("Anotace snímku")
        form = QFormLayout(box)

        self.ed_title = QLineEdit()
        self.ed_note = QLineEdit()
        self.ed_tags = QLineEdit()
        self.ed_tags.setPlaceholderText("odděleno ;")
        self.ed_date = QLineEdit()
        self.ed_date.setPlaceholderText("12.8.1968 | 1968-08-12 14:30 | 1968")
        self.ed_gps = QLineEdit()
        self.ed_gps.setPlaceholderText("49.1124306N, 9.7371244E")

        # Rating 0 is a real verdict; "unset" needs its own switch so an
        # untouched spin does not silently write a 0 into every frame.
        self.spin_rating = QSpinBox()
        self.spin_rating.setRange(0, 5)
        self.chk_rating_use = QCheckBox("použít")
        rating_row = QHBoxLayout()
        rating_row.addWidget(self.spin_rating)
        rating_row.addWidget(self.chk_rating_use)
        rating_w = QWidget()
        rating_w.setLayout(rating_row)

        self.lbl_gps = QLabel("—")
        self.lbl_gps.setWordWrap(True)
        self.lbl_gps.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)

        form.addRow("Název", self.ed_title)
        form.addRow("Popis", self.ed_note)
        form.addRow("Štítky", self.ed_tags)
        form.addRow("Datum záběru", self.ed_date)
        form.addRow("GPS", self.ed_gps)
        form.addRow("Geo rozklad", self.lbl_gps)
        form.addRow("Hodnocení", rating_w)

        btns = QHBoxLayout()
        self.btn_save = QPushButton("Uložit aktuální")
        self.btn_save.clicked.connect(self._save_current)
        self.btn_apply = QPushButton("Použít na vybrané")
        self.btn_apply.setToolTip(
            "Přepíše datum / geo / hodnocení / štítky u všech vybraných "
            "snímků; prázdná pole nechává být. Název a popis se nepřenášejí.")
        self.btn_apply.clicked.connect(self._apply_to_selected)
        self.btn_reload = QPushButton("Znovu načíst")
        self.btn_reload.clicked.connect(lambda: self._frame_selected(
            self.frame_list.currentItem(), None))
        btns.addWidget(self.btn_save)
        btns.addWidget(self.btn_apply)
        btns.addWidget(self.btn_reload)
        form.addRow(btns)
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
        self.set_project(project)

    def set_project(self, project: AnnotatedProject) -> None:
        self.project = project
        self.frame_list.clear()
        for item in project.items:
            QListWidgetItem(self._list_label(item), self.frame_list)
        note = f"{len(project.items)} snímků"
        if project.oriented:
            note += " · náhledy dle orientace filmu"
        self.statusBar().showMessage(f"Otevřeno: {project.root} — {note}")
        if project.items:
            self.frame_list.setCurrentRow(0)

    def _list_label(self, item: AnnotationItem) -> str:
        mark = "●" if item.is_annotated else "○"
        stem = Path(item.name).stem
        return f"{mark} {stem}"

    def _refresh_row(self, item: AnnotationItem) -> None:
        if self.project is None:
            return
        try:
            row = self.project.items.index(item)
        except ValueError:
            return
        self.frame_list.item(row).setText(self._list_label(item))

    # ------------------------------------------------------------ frame <-> form

    def _frame_selected(self, item: QListWidgetItem | None,
                        _prev: QListWidgetItem | None) -> None:
        if self.project is None or item is None:
            return
        row = self.frame_list.row(item)
        self.current = self.project.items[row]
        ann = self.current.annotation or {}
        self._loading_form = True
        self.ed_title.setText(str(ann.get("title", "")))
        self.ed_note.setText(str(ann.get("note", "")))
        tags = ann.get("tags") or []
        self.ed_tags.setText("; ".join(str(t) for t in tags))
        self.ed_date.setText(str(ann.get("capture_datetime", "")))
        self.ed_gps.setText(str(ann.get("gps_input", "")))
        rating = ann.get("rating")
        self.spin_rating.setValue(int(rating) if rating is not None else 0)
        self.chk_rating_use.setChecked(rating is not None)
        self._refresh_gps_label()
        self._loading_form = False
        self._update_preview()
        self.statusBar().showMessage(f"Načteno: {self.current.name}")

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

    def _update_preview(self) -> None:
        if self.current is None or self.project is None:
            return
        path = self.current.preview_path or self.current.image_path
        pixels = _load_preview(path)
        if pixels is None:
            self.preview_label.setText("(náhled nelze zobrazit)")
            self.preview_label.setPixmap(QPixmap())
            return
        pixels = apply_orientation(
            pixels,
            mirrored_horizontal=self.project.mirrored_horizontal,
            mirrored_vertical=self.project.mirrored_vertical,
            rotated_180=self.project.rotated_180)
        img = _to_qimage(pixels)
        pix = QPixmap.fromImage(img).scaled(
            PREVIEW_MAX, PREVIEW_MAX,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self._pixmap = pix
        self.preview_label.setPixmap(pix)

    # ---------------------------------------------------------------- collect

    def _form_values(self) -> dict:
        tags = [t.strip() for t in self.ed_tags.text().split(";") if t.strip()]
        return {
            "title": self.ed_title.text().strip(),
            "note": self.ed_note.text().strip(),
            "tags": tags,
            "capture_datetime": parse_capture_datetime(self.ed_date.text()),
            "gps_input": self.ed_gps.text().strip(),
            "rating": (self.spin_rating.value()
                       if self.chk_rating_use.isChecked() else None),
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
        self.statusBar().showMessage(f"Uloženo: {self.current.sidecar_path.name}")

    def _apply_to_selected(self) -> None:
        if self.project is None:
            return
        rows = sorted({self.frame_list.row(i)
                       for i in self.frame_list.selectedItems()})
        if not rows:
            QMessageBox.information(self, "Použít na vybrané",
                                    "Vyber jeden nebo více snímků.")
            return
        try:
            patch = build_common_patch(self._form_values())
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
            self._frame_selected(self.frame_list.currentItem(), None)
        self.statusBar().showMessage(
            f"Společná data zapsána do {count} sidecarů")

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
