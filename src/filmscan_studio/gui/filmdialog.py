"""Dialog that starts a film: the metadata entry point.

Two field groups, mirroring how a session actually runs: the strip and its
development on top, the *digitising rig* below. Only the rig group prefills
from the previous film (:meth:`FilmMetadata.rig_defaults`) — light, holder and
lens are carried between films on purpose, while the film's own identity must
never be inherited by accident. Everything else, including ``film_id``,
starts empty on purpose.

Dates are line edits, not date picks: the darkroom log this mirrors contains
entries like ``"asi 12/25"`` and the archive must record what the operator
believed, not what a widget could parse.
"""

from __future__ import annotations

from datetime import date

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
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


def _line(default: str | None, placeholder: str = "") -> QLineEdit:
    edit = QLineEdit(default or "")
    edit.setPlaceholderText(placeholder)
    return edit


def _text_or_none(edit: QLineEdit) -> str | None:
    return edit.text().strip() or None


class FilmDialog(QDialog):
    """Collects :class:`FilmMetadata` before any frame is captured."""

    def __init__(
        self,
        parent: QWidget | None = None,
        defaults: FilmMetadata | None = None,
        rig_defaults: FilmMetadata | None = None,
    ) -> None:
        """``defaults`` re-fills the whole form (editing a film);
        ``rig_defaults`` prefills only the rig group (starting film N+1)."""
        super().__init__(parent)
        self.setWindowTitle("Nový film")
        blank = FilmMetadata(film_id="")
        film_src = defaults if defaults is not None else blank
        rig_src = defaults if defaults is not None else (rig_defaults or blank)
        layout = QVBoxLayout(self)

        hint = QLabel(
            "Metadata se ukládají do JSON sidecaru a SQLite katalogu, "
            "nikoli do DNG/NEF. Raw soubor zůstává nedotčený. "
            "Údaje o filmu lze nechat prázdné a doplnit později."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # ------------------------------------------------------------- film
        film_box = QGroupBox("Film")
        film_form = QFormLayout(film_box)
        layout.addWidget(film_box)

        self.film_id = _line(film_src.film_id, "K16O03_2025")
        film_form.addRow("Film ID *", self.film_id)

        self.film_name = _line(film_src.film_name, "Fomapan 100 Classic")
        film_form.addRow("Film (výrobce + typ)", self.film_name)

        # The *film's* camera — the body that exposed the negative, not the
        # D750 doing the digitising (that is the rig group's job).
        self.camera = _line(rig_src.camera, "Nikon FM2")
        film_form.addRow("Fotoaparát", self.camera)

        self.format = QComboBox()
        self.format.setEditable(True)
        self.format.addItems(["35mm", "120", "4x5", "110", "127"])
        if rig_src.format:
            self.format.setCurrentText(rig_src.format)
        film_form.addRow("Formát", self.format)

        self.type_class = QComboBox()
        for label, member in _FILM_TYPES:
            self.type_class.addItem(label, member)
        index = self.type_class.findData(rig_src.film_type_class)
        self.type_class.setCurrentIndex(max(index, 0))
        film_form.addRow("Třída", self.type_class)

        # -------------------------------------------------------- development
        dev_box = QGroupBox("Vyvolání")
        dev_form = QFormLayout(dev_box)
        layout.addWidget(dev_box)

        self.development = _line(film_src.development, "R 09 1:50 8 min @22C")
        dev_form.addRow("Vyvolání (lázeň, ředění, čas, teplota)", self.development)

        self.development_start = _line(film_src.development_start, "asi 12/25")
        dev_form.addRow("Datum start", self.development_start)

        self.development_end = _line(film_src.development_end, "12/25 14:30")
        dev_form.addRow("Datum konec", self.development_end)

        self.pushed = QDoubleSpinBox()
        self.pushed.setRange(-3.0, 5.0)
        self.pushed.setSingleStep(0.5)
        self.pushed.setValue(film_src.pushed_stops)
        dev_form.addRow("Push/pull (EV)", self.pushed)

        self.content = _line(film_src.content, "hrady, Křivoklát…")
        dev_form.addRow("Obsah filmu", self.content)

        # --------------------------------------------------------------- rig
        rig_box = QGroupBox("Digitalizační sestava")
        rig_form = QFormLayout(rig_box)
        layout.addWidget(rig_box)

        self.digitising_lens = _line(
            rig_src.digitising_lens, "Carl Zeiss MC Biometar 2.8/80, F8.0"
        )
        rig_form.addRow("Objektiv", self.digitising_lens)

        self.digitising_light = _line(rig_src.digitising_light, "LED11x15cm panel 4400 K")
        rig_form.addRow("Světlo", self.digitising_light)

        self.digitising_holder = _line(rig_src.digitising_holder, "PSI 35mm")
        rig_form.addRow("Držák", self.digitising_holder)

        self.digitisation_date = _line(
            film_src.digitisation_date or date.today().isoformat(), "2026-09-15"
        )
        rig_form.addRow("Datum digitalizace", self.digitisation_date)

        self.operator = _line(rig_src.operator or film_src.operator, "JG")
        rig_form.addRow("Operátor", self.operator)

        self.notes = QTextEdit()
        self.notes.setMaximumHeight(60)
        if film_src.notes:
            self.notes.setPlainText(film_src.notes)
        rig_form.addRow("Poznámky", self.notes)

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
            film_name=_text_or_none(self.film_name),
            camera=_text_or_none(self.camera),
            format=self.format.currentText().strip() or None,
            film_type_class=self.type_class.currentData(),
            development=_text_or_none(self.development),
            development_start=_text_or_none(self.development_start),
            development_end=_text_or_none(self.development_end),
            pushed_stops=self.pushed.value(),
            content=_text_or_none(self.content),
            digitising_lens=_text_or_none(self.digitising_lens),
            digitising_light=_text_or_none(self.digitising_light),
            digitising_holder=_text_or_none(self.digitising_holder),
            digitisation_date=_text_or_none(self.digitisation_date),
            operator=_text_or_none(self.operator),
            notes=self.notes.toPlainText().strip() or None,
        )
