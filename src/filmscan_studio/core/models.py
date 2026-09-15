"""Metadata models.

Metadata is deliberately kept *out* of the image files. Raw captures (NEF/DNG)
are never rewritten; all descriptive data lives in JSON sidecars and the SQLite
catalog. This keeps acquisition reproducible and archive-safe: an image plus its
sidecar can be re-interpreted years later without this application.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 2


def _now() -> datetime:
    return datetime.now().astimezone()


class FilmType(StrEnum):
    """What kind of film stock the negative/diapozitiv came from."""

    BW_NEGATIVE = "bw_negative"
    COLOR_NEGATIVE = "color_negative"
    SLIDE = "slide"

    @property
    def is_negative(self) -> bool:
        return self in (FilmType.BW_NEGATIVE, FilmType.COLOR_NEGATIVE)


class FrameKind(StrEnum):
    """Role a captured file plays in a session.

    Calibration frames are ordinary raw files. They are never treated specially
    on disk -- only the sidecar and the database say what a frame *is*.
    """

    SCAN = "scan"
    DARK = "dark"
    FLAT = "flat"


class ImageFormat(StrEnum):
    RAW = "raw"
    JPEG = "jpeg"


#: Which fields survive from one film to the next as dialog defaults.
#:
#: The digitising rig barely changes between films — same body, same lens, same
#: light, same holder — while the film's own identity and its development log are
#: written afresh every time. Prefilling the second group would mean clearing it
#: every session and risking a stale "developed at" on a strip that has not been
#: in chemistry yet.
RIG_FIELDS: tuple[str, ...] = (
    "camera",
    "format",
    "film_type_class",
    "mirrored",
    "digitising_lens",
    "digitising_light",
    "digitising_holder",
    "operator",
)

#: Always prefilled, but with *today* rather than the previous value.
DIGITISATION_DATE_FIELD = "digitisation_date"


class FilmMetadata(BaseModel):
    """Describes the physical film strip being digitised.

    Mirrors the archival fields a darkroom log would contain, so a scan remains
    interpretable without knowing anything about the scanner.

    Two field groups live here deliberately. The first is the strip and its
    development; the second (``digitising_*``, ``camera``, ``mirrored``) describes
    the *rig* it was scanned on, which is what makes a scan reproducible and is
    what carries over when a new film is started — see :data:`RIG_FIELDS`.

    Every field except ``film_id`` is optional: a strip is often started before
    its development is known, and dates are free text on purpose because a darkroom
    log records things like ``"asi 12/25"`` that no date parser should reject.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    film_id: str = Field(description="Stable identifier, e.g. 'HP5_001'.")
    #: Manufacturer and stock as one string, e.g. 'Fomapan 100 Classic' — they are
    #: always typed together and splitting them only lost information.
    film_name: str | None = None
    #: Body the scan was made on, typed rather than taken from EXIF: the operator
    #: records what they believe they used, and may be digitising on a spare body.
    camera: str | None = None
    format: str | None = Field(default=None, description="e.g. '35mm', '120', '4x5'.")
    film_type_class: FilmType = FilmType.BW_NEGATIVE
    #: Whole development line as one string, e.g. 'R 09 1:50 8 min @22C'.
    development: str | None = None
    #: Free text: 'asi 12/25' must survive, so no date type.
    development_start: str | None = None
    development_end: str | None = None
    content: str | None = Field(
        default=None, description="What is on the strip."
    )
    #: True when the strip was digitised emulsion-side to the lens, which flips
    #: the image horizontally. Recorded, not yet applied anywhere — the flip
    #: belongs in the developer's output, and applying it to a live preview
    #: before it is applied to the export would make the two disagree.
    mirrored: bool = False
    digitising_lens: str | None = None
    digitising_light: str | None = None
    digitising_holder: str | None = None
    #: Also free text, but the dialog offers today's date as the default.
    digitisation_date: str | None = None
    operator: str | None = None
    box_number: str | None = None
    expiry: str | None = None
    pushed_stops: float = 0.0
    notes: str | None = None

    def label(self) -> str:
        return self.film_name or self.film_id

    def rig_defaults(self) -> dict[str, object]:
        """The subset of fields that carry over to the next film; see RIG_FIELDS."""
        return {name: getattr(self, name) for name in RIG_FIELDS}

    @model_validator(mode="before")
    @classmethod
    def _migrate_v1(cls, data: object) -> object:
        """Fold schema-v1 sidecars into the current field set.

        v1 split the stock name into ``manufacturer`` + ``film_type`` and the
        development log into three columns. Archived projects must stay
        readable without a migration pass over every sidecar on disk, so the
        merge happens here, once, on load; the on-disk v1 files are left as
        they are. Unknown keys still fail loudly — ``extra="forbid"`` is what
        catches a typo'd field name, and a migration must not eat that.
        """
        if not isinstance(data, dict):
            return data
        if not any(k in data for k in ("manufacturer", "film_type",
                                       "developer", "developer_dilution",
                                       "development_time")):
            return data
        data = dict(data)
        name = " ".join(
            str(p).strip() for p in (data.pop("manufacturer", None),
                                     data.pop("film_type", None))
            if p and str(p).strip()
        )
        parts = [str(data.pop(k, "") or "").strip()
                 for k in ("developer", "developer_dilution", "development_time")]
        development = " ".join(p for p in parts if p)
        if name and not data.get("film_name"):
            data["film_name"] = name
        if development and not data.get("development"):
            data["development"] = development
        return data


class AcquisitionMetadata(BaseModel):
    """How a single frame was photographed.

    Normally filled from EXIF and then trusted; camera settings that EXIF cannot
    report (notably aperture on manual lenses) are entered by the operator and
    take precedence when present.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    camera: str | None = None
    camera_serial: str | None = None
    lens: str | None = None
    lens_serial: str | None = None
    iso: int | None = None
    exposure_time: float | None = Field(default=None, description="Seconds.")
    f_number: float | None = None
    focus_distance_m: float | None = None
    capture_date: datetime | None = None
    copy_number: int = 1
    white_balance: str | None = None
    raw_developer: str | None = Field(
        default=None, description="Camera-side render settings; irrelevant to raw data."
    )

    @field_validator("exposure_time")
    @classmethod
    def _positive(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:
            raise ValueError("exposure_time must be positive")
        return v

    def ev100(self) -> float | None:
        """Exposure value of the capture, or None if settings are incomplete.

        EV100 is the invariant that lets a flat frame shot at a different shutter
        speed be compared against a scan: radiometrically, sensor signal scales
        with exposure time at fixed illumination.
        """
        if self.exposure_time is None or self.iso is None or self.f_number is None:
            return None
        import math

        return math.log2(self.f_number**2 / self.exposure_time * 100.0 / self.iso)


class CaptureRecord(BaseModel):
    """One file on disk, plus everything we know about it."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    capture_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    film_id: str
    frame_number: int | None = None
    kind: FrameKind = FrameKind.SCAN
    filename: str
    file_format: ImageFormat = ImageFormat.RAW
    width: int | None = None
    height: int | None = None
    black_level: float | None = None
    white_level: float | None = None
    film: FilmMetadata
    acquisition: AcquisitionMetadata = Field(default_factory=AcquisitionMetadata)
    created_at: datetime = Field(default_factory=_now)
    notes: str | None = None

    @property
    def path_like(self) -> Path:
        return Path(self.filename)


def to_json_dict(model: BaseModel) -> dict[str, Any]:
    """Canonical serialisation used for both sidecars and the database blob.

    Datetimes become ISO-8601 with offset so a project remains readable outside
    Python. Enum values are stored as their plain strings.
    """
    return model.model_dump(mode="json")
