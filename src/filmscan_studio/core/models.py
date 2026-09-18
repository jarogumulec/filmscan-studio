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

#: v3, unchanged: the acquisition metadata lost the DSLR-era fields
#: (``lens``, ``lens_serial``, ``f_number``, ``focus_distance_m``,
#: ``white_balance``, ``raw_developer``) and ``FilmMetadata`` lost ``mirrored``
#: when the app became mono-Touptek-only. The version is deliberately *not*
#: bumped: every archived ``catalog.sqlite`` stamps its version and the catalog
#: refuses a mismatch, so a bump would lock the operator out of their existing
#: projects. Retirement is handled instead by :func:`_strip_retired`, which
#: drops the old keys on load; ``SCHEMA_VERSION`` stays a compatibility floor
#: for genuine format breaks, not for deleting a field nobody reads.
SCHEMA_VERSION = 3

#: Keys retired from ``AcquisitionMetadata`` and ``FilmMetadata`` across the
#: D750 -> mono Touptek move. ``extra="forbid"`` would otherwise reject any
#: sidecar written before they went away, so they are removed before
#: validation rather than tolerated forever.
RETIRED_ACQUISITION_FIELDS: tuple[str, ...] = (
    "lens",
    "lens_serial",
    "f_number",
    "focus_distance_m",
    "white_balance",
    "raw_developer",
)
RETIRED_FILM_FIELDS: tuple[str, ...] = ("mirrored",)


def _strip_retired(data: object, retired: tuple[str, ...]) -> object:
    """Drop retired keys from a sidecar dict before validation.

    Only rebuilds when something is actually retired-present, so the ordinary
    (and already-migrated) path pays nothing. A non-dict passes through for
    pydantic to reject on its own terms.
    """
    if not isinstance(data, dict) or not any(k in data for k in retired):
        return data
    return {k: v for k, v in data.items() if k not in retired}


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
    #: Film base / min point: the clear-base level of the *held film*, measured
    #: over the operator's rect. Unlike a flat (light with no film, division
    #: correction of vignetting/dust), this is a subtraction reference for the
    #: emulsion's own base+fog, recorded with its exposure so it can be scaled
    #: onto frames exposed at another shutter.
    BASE = "base"


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
    development; the second (``digitising_*``, ``camera``) describes
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
    def _migrate(cls, data: object) -> object:
        """Fold schema-v1 sidecars into the current field set.

        v1 split the stock name into ``manufacturer`` + ``film_type`` and the
        development log into three columns. Archived projects must stay
        readable without a migration pass over every sidecar on disk, so the
        merge happens here, once, on load; the on-disk v1 files are left as
        they are.

        The retired rig flag (``mirrored``) is stripped first, on every load —
        the emulsion-side flip is corrected in post now and the flag recorded
        nothing anywhere, so a v3 sidecar carrying it must not trip
        ``extra="forbid"``. Unknown keys *other* than the retired ones still
        fail loudly: that is what catches a typo'd field name, and a migration
        must not eat that.
        """
        data = _strip_retired(data, RETIRED_FILM_FIELDS)
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

    The mono Touptek reports only these: the body, its serial, the shutter, the
    analog gain and the moment of exposure. The D750-era fields (lens and its
    serial, ``f_number``, focus distance, white balance, camera-side raw
    developer) are gone — a fixed manual prime lens and a mono sensor give none
    of them a value, and sidecars that still carry them are migrated on load
    (see :data:`RETIRED_ACQUISITION_FIELDS`).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    camera: str | None = None
    camera_serial: str | None = None
    #: Legacy bodies only; gain cameras (Touptek) leave this None.
    iso: int | None = None
    #: Analog gain as a linear multiplier (1.0 = 1x), the Touptek's sensitivity.
    gain: float | None = None
    exposure_time: float | None = Field(default=None, description="Seconds.")
    capture_date: datetime | None = None
    copy_number: int = 1

    @model_validator(mode="before")
    @classmethod
    def _strip_retired(cls, data: object) -> object:
        """Drop the DSLR-era keys archived sidecars still carry."""
        return _strip_retired(data, RETIRED_ACQUISITION_FIELDS)

    @field_validator("exposure_time")
    @classmethod
    def _positive(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:
            raise ValueError("exposure_time must be positive")
        return v


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
    #: Sensor temperature at exposure, degC. A cooled sensor's dark current is
    #: a function of temperature, so a scan may only be subtracted by a dark
    #: captured within a narrow thermal window (see export validation).
    sensor_temperature_c: float | None = None
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
