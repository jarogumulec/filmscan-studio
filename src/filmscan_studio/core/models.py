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
#: The old single boolean only. The 2026-09 orientation attributes
#: (``mirrored_horizontal`` / ``mirrored_vertical`` / ``rotated_180``) are new
#: field names by direct user instruction and are *not* retired.
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
    #: Camera maker / model as EXIF wants them apart (2026-09-22, operator
    #: order: „foťák neděl dle mezery — make je 'ERNST LEITZ WETZLAR GMBH',
    #: model 'Leica R4s MOD.2'"). Free-text one-field ``camera`` stays the
    #: human string; these two are the split pair — filled by the annotator's
    #: two boxes or migrated from ``camera`` by the split heuristic, and the
    #: developer's EXIF prefers them over re-splitting the free text.
    camera_make: str | None = None
    camera_model: str | None = None
    #: Lens the *photographs were taken with* (e.g. "Nikkor 50/2") — added by
    #: the annotator's film panel 2026-09-21 evening at the operator's order
    #: ("dej tam i políčko objektiv"). Deliberately NOT called ``lens``: that
    #: name is retired from the acquisition block (see RETIRED_ACQUISITION_
    #: FIELDS) and must not resurface; this is a different fact — the camera
    #: that exposed the film, not the digitising rig. Per-film like
    #: ``camera``: a roll is shot through one lens.
    shooting_lens: str | None = None
    #: Film speed as the operator records it (e.g. "100", "400/27°") — the
    #: scene's ISO, the photographic EXIF fact, free text so "100/21°" sur-
    #: vives. Distinct from the acquisition block's ``iso``/``gain`` (the
    #: digitising sensor's sensitivity, a machine reading). Added 2026-09-22
    #: at the operator's request for an EXIF group in the annotator
    #: ("pod blok název/geo budou exifové — foťák, ISO…").
    film_iso: str | None = None
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
    #: How the strip sits in the holder, recorded (not applied) — the operator
    #: notes the orientation and post-production flips accordingly. Freely
    #: combinable, but ``mirrored_horizontal`` + ``mirrored_vertical`` *is*
    #: ``rotated_180``, so all three at once is a no-op (identity) and is
    #: rejected (see :meth:`_orientation_not_identity`). Re-introduced 2026-09
    #: by direct user instruction; these are NEW field names, distinct from the
    #: retired boolean ``mirrored`` (still stripped on load, see
    #: :data:`RETIRED_FILM_FIELDS`), so old archives stay readable.
    mirrored_horizontal: bool = False
    mirrored_vertical: bool = False
    rotated_180: bool = False
    #: Also free text, but the dialog offers today's date as the default.
    digitisation_date: str | None = None
    operator: str | None = None
    box_number: str | None = None
    expiry: str | None = None
    pushed_stops: float = 0.0
    notes: str | None = None

    @model_validator(mode="after")
    def _orientation_not_identity(self) -> "FilmMetadata":
        """All three orientation flags at once is the identity — refuse it.

        Mirror H composed with mirror V *is* the 180° rotation, so
        H+V+rot180 undoes itself and a record claiming all three describes a
        strip sitting straight. The operator then has to say what they mean.
        H+V alone stays valid: it is the 180° rotation, just expressed through
        the two mirrors.
        """
        if self.mirrored_horizontal and self.mirrored_vertical \
                and self.rotated_180:
            raise ValueError(
                "mirrored_horizontal + mirrored_vertical je rotace 180° — "
                "se všemi třemi atributy se orientace vyruší (identity); "
                "zakřehni rotated_180, nebo nech jen jeden z mirrorů"
            )
        return self

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


class FrameAnnotation(BaseModel):
    """Operator's description of *what the picture is* — added after capture.

    The sidecar's own ``acquisition.capture_date`` is when the rig exposed the
    negative (2026, in the studio). This block carries the *photographic*
    facts of the scene: title, note, tags, rating, when and where the film
    was exposed. A separate block, never folded into ``acquisition``, keeps
    machine-measured provenance and human annotation from being confused —
    and lets a re-capture rewrite one without silently destroying the other.

    Written by the annotator GUI (``filmscan-annotate``) directly into the
    sidecar JSON; read by the developer at export to fill EXIF. Datetime is
    EXIF-strict ``YYYY:MM:DD HH:MM:SS`` on purpose: a photo manager must be
    able to sort the picture, and an unparseable "léto 1968" would drop it
    out of every timeline. Unknown day → ``1968:01:01 00:00:00``.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    title: str = ""
    note: str = ""
    tags: list[str] = Field(default_factory=list)
    rating: int | None = Field(default=None, ge=0, le=5)
    #: Exposure moment of the *scene*, EXIF form; "" = unknown.
    capture_datetime: str = ""
    #: Raw operator input ("49.1124306N, 9.7371244E") kept for re-parsing.
    gps_input: str = ""
    #: Decimal degrees as strings — written by the GPS parser, kept verbatim
    #: so a round-trip through the GUI never rewrites precision.
    gps_lat: str = ""
    gps_lon: str = ""
    gps_lat_ref: str = ""      # N / S
    gps_lon_ref: str = ""      # E / W
    #: EXIF-style DMS helper strings, ready for a later GPS IFD write.
    gps_lat_exif_dms: str = ""
    gps_lon_exif_dms: str = ""
    #: Viewer rotation applied on top of the film's orientation flags
    #: (2026-09-21 evening: per-frame "rotate 90° left/right" in the
    #: annotator). CW degrees, multiples of 90; the density archive stays
    #: untouched — previews and exports rotate, like the film flips do.
    rotation_degrees: int = 0

    @field_validator("rotation_degrees")
    @classmethod
    def _rotation_multiple(cls, v: int) -> int:
        if v % 90 != 0:
            raise ValueError("rotation_degrees musí být násobek 90°")
        return v % 360

    @field_validator("capture_datetime")
    @classmethod
    def _exif_datetime(cls, v: str) -> str:
        if not v:
            return ""
        try:
            datetime.strptime(v, "%Y:%m:%d %H:%M:%S")
        except ValueError as exc:
            raise ValueError(
                "capture_datetime musí být EXIF tvar 'YYYY:MM:DD HH:MM:SS'"
            ) from exc
        return v


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
    #: How many consecutive exposures the stored frame is the mean of (2026-09-19
    #: averaging; the user's ruling — only the averaged TIFF is archived). 1 = a
    #: single exposure, which is also the value of every sidecar written before
    #: averaging existed, so no schema bump or migration is needed.
    frames_averaged: int = 1
    #: Conversion gain the frame was exposed at: "LCG" (max full well — the
    #: scanner mode) or "HCG" (min read noise — astro). None on bodies without
    #: the switch, and in every sidecar written before 2026-09-20. The
    #: DN↔electron mapping is mode-dependent (~2.8× measured on the ATR2600M),
    #: so this is reproducibility provenance, not decoration.
    conversion_gain: str | None = None
    #: Low-noise readout on/off at exposure. Stills are DN-neutral across it
    #: (measured 0.996×); it halves the frame rate and shifts only the live
    #: preview's DN scale (~0.83×). None = unsupported or unrecorded (every
    #: pre-2026-09-20 sidecar).
    low_noise: bool | None = None

    @field_validator("conversion_gain")
    @classmethod
    def _cg_known(cls, v: str | None) -> str | None:
        if v is not None and v not in ("LCG", "HCG"):
            raise ValueError("conversion_gain is 'LCG', 'HCG' or None")
        return v

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
    #: Crop rectangle the operator drew (the red frame = the picture's edge),
    #: as ``(x0, y0, x1, y1)`` in *full-size frame* pixels — the frame file's
    #: own grid, never the binned stream (the GUI converts before recording).
    #: A later developer GUI crops the frame by it. ``None`` = no crop asked
    #: for; the frame stands as captured.
    crop_rect: tuple[int, int, int, int] | None = None
    #: What the picture *is*: added after capture by the annotator GUI into
    #: the sidecar's ``annotation`` key. Additive like ``crop_rect`` — the
    #: capture layer never writes it, and every pre-annotation sidecar stays
    #: valid (default ``None``), so ``SCHEMA_VERSION`` does not move.
    annotation: FrameAnnotation | None = None

    @property
    def path_like(self) -> Path:
        return Path(self.filename)


def to_json_dict(model: BaseModel) -> dict[str, Any]:
    """Canonical serialisation used for both sidecars and the database blob.

    Datetimes become ISO-8601 with offset so a project remains readable outside
    Python. Enum values are stored as their plain strings.
    """
    return model.model_dump(mode="json")
