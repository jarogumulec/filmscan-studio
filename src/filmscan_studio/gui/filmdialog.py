"""Dialog that starts a film: the metadata entry point.

Field order follows the brief's example sidecar, not alphabetical convenience --
someone reading a filled-in form should recognise their own darkroom log.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from filmscan_studio.core.models import FilmMetadata, FilmType

#: Display name -> enum. Order is the order a scan session usually runs in.
_FILM_TYPES = (
    ("BW negative", FilmType.BW_NEGATIVE),
    ("Color negative", FilmType.COLOR_NEGATIVE),
    ("Slide", FilmType.SLIDE),
)


class FilmDialog(QDialog):
    """Collects :class:`FilmMetadata` before any frame is captured."""

    def __init__(self, parent: QWidget | None = None, defaults: FilmMetadata | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Nový film")
        defaults = defaults or FilmMetadata(film_id="")
        layout = QVBoxLayout(self)

        hint = QLabel(
            "Metadata se ukládají do JSON sidecaru a SQLite katalogu, "
            "nikoli do DNG/NEF. Raw soubor zůstává nedotčený."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()
        layout.addLayout(form)

        self.film_id = QLineEdit(defaults.film_id)
        self.film_id.setPlaceholderText("HP5_001")
        form.addRow("Film ID *", self.film_id)

        self.manufacturer = QLineEdit(defaults.manufacturer or "")
        self.manufacturer.setPlaceholderText("Ilford")
        form.addRow("Výrobce", self.manufacturer)

        self.film_type = QLineEdit(defaults.film_type or "")
        self.film_type.setPlaceholderText("HP5+")
        form.addRow("Typ filmu", self.film_type)

        self.format = QComboBox()
        self.format.setEditable(True)
        self.format.addItems(["35mm", "120", "4x5", "110", "127"])
        if defaults.format:
            self.format.setCurrentText(defaults.format)
        form.addRow("Formát", self.format)

        self.type_class = QComboBox()
        for label, member in _FILM_TYPES:
            self.type_class.addItem(label, member)
        index = self.type_class.findData(defaults.film_type_class)
        self.type_class.setCurrentIndex(max(index, 0))
        form.addRow("Třída", self.type_class)

        self.developer = QLineEdit(defaults.developer or "")
        self.developer.setPlaceholderText("ID-11")
        form.addRow("Vyvolávací lázeň", self.developer)

        self.dilution = QLineEdit(defaults.developer_dilution or "")
        self.dilution.setPlaceholderText("1+1")
        form.addRow("Ředění", self.dilution)

        self.development_time = QLineEdit(defaults.development_time or "")
        self.development_time.setPlaceholderText("13 min")
        form.addRow("Čas vyvolávání", self.development_time)

        self.operator = QLineEdit(defaults.operator or "")
        self.operator.setPlaceholderText("JG")
        form.addRow("Operátor", self.operator)

        self.pushed = QDoubleSpinBox()
        self.pushed.setRange(-3.0, 5.0)
        self.pushed.setSingleStep(0.5)
        self.pushed.setValue(defaults.pushed_stops)
        form.addRow("Push/pull (EV)", self.pushed)

        self.notes = QTextEdit()
        self.notes.setMaximumHeight(60)
        if defaults.notes:
            self.notes.setPlainText(defaults.notes)
        form.addRow("Poznámky", self.notes)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self) -> None:
        try:
            self.metadata()
        except ValueError as exc:
            QMessageBox.warning(self, "Chybějící údaje", str(exc))
            return
        self.accept()

    def metadata(self) -> FilmMetadata:
        """Build the record, raising ``ValueError`` if required fields are empty."""
        film_id = self.film_id.text().strip()
        if not film_id:
            raise ValueError("film_id je povinné (např. HP5_001)")
        return FilmMetadata(
            film_id=film_id,
            manufacturer=self.manufacturer.text().strip() or None,
            film_type=self.film_type.text().strip() or None,
            format=self.format.currentText().strip() or None,
            film_type_class=self.type_class.currentData(),
            developer=self.developer.text().strip() or None,
            developer_dilution=self.dilution.text().strip() or None,
            development_time=self.development_time.text().strip() or None,
            operator=self.operator.text().strip() or None,
            pushed_stops=self.pushed.value(),
            notes=self.notes.toPlainText().strip() or None,
        )
