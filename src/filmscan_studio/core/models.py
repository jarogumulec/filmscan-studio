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

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = 1


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


class FilmMetadata(BaseModel):
    """Describes the physical film strip being digitised.

    Mirrors the archival fields a darkroom log would contain, so a scan remains
    interpretable without knowing anything about the scanner.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    film_id: str = Field(description="Stable identifier, e.g. 'HP5_001'.")
    manufacturer: str | None = None
    film_type: str | None = None
    format: str | None = Field(default=None, description="e.g. '35mm', '120', '4x5'.")
    film_type_class: FilmType = FilmType.BW_NEGATIVE
    developer: str | None = None
    developer_dilution: str | None = None
    development_time: str | None = None
    operator: str | None = None
    box_number: str | None = None
    expiry: str | None = None
    pushed_stops: float = 0.0
    notes: str | None = None

    def label(self) -> str:
        parts = [p for p in (self.manufacturer, self.film_type) if p]
        return " ".join(parts) if parts else self.film_id


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
