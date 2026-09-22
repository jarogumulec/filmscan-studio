"""Annotation store: read a film folder, merge annotations into sidecars.

The Qt layer (:mod:`filmscan_studio.annotator.gui`) knows nothing about JSON;
everything that can corrupt an archive lives here, where it can be tested.

**Merge, never rewrite.** A sidecar is the capture layer's property — the
annotator loads it as a plain dict, sets exactly one key (``annotation``)
and writes the dict back atomically. Unknown keys survive untouched, which
matters because the capture app and the developer keep adding fields
(``crop_rect``, ``frames_averaged``, …) the annotator knows nothing about.
The dict is validated through :class:`CaptureRecord` before it goes to disk:
an annotation must never produce a sidecar the capture app would refuse to
re-open.

Datetime and GPS parsing are a port of the scanner toolbox annotator
(``genealogy/scanning/workflow/07_flat_folder_metadata_gui.py``), so the same
operator habits ("49.1124306N, 9.7371244E", "12.8.1968") work in both apps.
The datetime is stored EXIF-strict ``YYYY:MM:DD HH:MM:SS`` — a photo manager
must be able to sort these pictures, so an unknown day is written as day 1
of January, not kept as free text.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from filmscan_studio.core.exportmeta import split_camera
from filmscan_studio.core.models import (
    AcquisitionMetadata,
    CaptureRecord,
    FilmMetadata,
    FrameAnnotation,
    FrameKind,
)

log = logging.getLogger(__name__)

#: Accepted human date spellings, ported from the scanner annotator. The
#: EXIF form is also accepted so an already-normalised value round-trips.
DATE_FORMATS = (
    "%Y:%m:%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%d.%m.%Y",
)
#: Formats without any time get 00:00:00 (EXIF demands a time).
_DATE_ONLY_FORMATS = {"%Y-%m-%d", "%d.%m.%Y"}
_YEAR_ONLY = re.compile(r"^\d{4}$")

#: Preview extensions tried before falling back to the TIFF itself.
PREVIEW_SUFFIXES = (".jpg", ".jpeg", ".JPG", ".JPEG")


def parse_capture_datetime(value: str) -> str:
    """Any reasonable spelling -> EXIF ``YYYY:MM:DD HH:MM:SS``.

    ``1968`` (a year alone) becomes ``1968:01:01 00:00:00``: the operator
    who knows only the year still wants the pictures to sort into the right
    place in a timeline, which free text never would. Returns "" for empty
    input (annotation deliberately unset); raises ValueError on garbage.
    """
    return _parse_datetime(value)[0]


def _parse_datetime(value: str) -> tuple[str, bool]:
    """(EXIF string, did the input carry a real time?)

    The time flag drives the sequential-minute numbering in
    :func:`apply_common`: a bare day (``1.1.2026``) means "I do not know the
    time", and frames then get minute offsets so they keep a sortable order;
    an explicit time (``14:30``) means "this exact time" and stays.
    """
    text = value.strip()
    if not text:
        return "", True
    if _YEAR_ONLY.match(text):
        return f"{text}:01:01 00:00:00", False
    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        has_time = fmt not in _DATE_ONLY_FORMATS
        return dt.strftime("%Y:%m:%d %H:%M:%S"), has_time
    raise ValueError(
        f"Nerozumím datu '{text}' — zkuste např. 12.8.1968, 1968-08-12 14:30 "
        f"nebo jen rok 1968"
    )


#: A real calendar date (day required) at the START of the Film-start free
#: text, optionally followed by a note: "8.11.2015 Vacation 2026" qualifies,
#: "cca 2/2017" or "?2025" does not — an approximate date written as one
#: must stay approximate (operator's rule 2026-09-22).
_FILM_START_DATE = re.compile(
    r"^\s*(\d{4}-\d{1,2}-\d{1,2}"
    r"|\d{1,2}\.\s*\d{1,2}\.\s*\d{2,4})(?=[^\d]|$)"
)


def parse_film_start_date(text: str) -> str:
    """Film-start free text -> EXIF ``YYYY:MM:DD 00:00:00``, or "".

    The film block keeps free text on purpose ('asi 12/25' must survive), so
    a date is only mined out when one is *readable*: a day-carrying date at
    the beginning of the string, with or without a trailing note. Anything
    vaguer (a bare year, "cca 2015", "?2025") returns "" and the annotator
    leaves the frames' dates alone — the operator fills those by hand.
    """
    m = _FILM_START_DATE.match(text or "")
    if not m:
        return ""
    raw = m.group(1).replace(" ", "")
    fmts = ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y")
    for fmt in fmts:
        try:
            dt = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if dt.year < 100:            # two-digit year, as darkroom logs write
            dt = dt.replace(year=2000 + dt.year)
        return dt.strftime("%Y:%m:%d 00:00:00")
    return ""


def auto_date_frames(project: "AnnotatedProject") -> tuple[int, str]:
    """Stamp frames that have no date yet from a readable Film start.

    Operator's rule 2026-09-22: „když film start je čitelné datum, aplikuj
    na všechny fotky jako datum záběru, já si kdyžtak upravím ručně. pokud
    tam je něco jako 'cca 2015' tak to nejde.“ Only frames whose annotation
    carries no ``capture_datetime`` are touched — a date the operator already
    set (or an earlier auto-run they edited) is never overwritten. The
    sequential-minute numbering of :func:`apply_common` gives the roll a
    sortable order; the counter starts at the first auto-stamped frame.

    Returns (frames stamped, the EXIF date used) — (0, "") when the film
    start holds no readable date.
    """
    film = {}
    for item in project.items:
        film = item.record.get("film") or {}
        if film:
            break
    if not film:
        film = _film_from_project_json(project.root)
    date = parse_film_start_date(str(film.get("development_start") or ""))
    if not date:
        return 0, ""
    todo = [i for i in project.items
            if not (i.annotation or {}).get("capture_datetime")]
    if not todo:
        return 0, date
    # The flag is what makes apply_common number the roll a minute apart —
    # the identical-midnight stamp is exactly what photo managers shuffle.
    count = apply_common(todo, {"capture_datetime": date,
                                "_sequential_time": True})
    return count, date


def migrate_camera_split(project: "AnnotatedProject") -> int:
    """Split the legacy free-text ``film.camera`` into make+model, once.

    Operator's order 2026-09-22: „foťák neděl dle mezery… systematické
    řešení: do anotátoru dej make a model, a tuto sadu už v jsonech rozděl."
    A sidecar that already carries either split field is left ALONE (the
    operator may have fixed 'ERNST LEITZ…' by hand); where both are empty
    and free text exists, :func:`~filmscan_studio.core.exportmeta.split_camera`
    proposes the split and it is written to every sidecar + project.json —
    so the JSONs are migrated and the developer reads make/model directly.

    Returns the number of sidecars rewritten (0 = nothing to migrate)."""
    todo: list[AnnotationItem] = []
    proposed: dict | None = None
    for item in project.items:
        film = item.record.get("film") or {}
        camera = str(film.get("camera") or "").strip()
        if not camera or film.get("camera_make") or film.get("camera_model"):
            continue
        todo.append(item)
        if proposed is None:
            proposed = dict(film)
            proposed["camera_make"], proposed["camera_model"] = \
                split_camera(camera)
    if not todo or proposed is None:
        return 0
    film_dump = FilmMetadata.model_validate(
        {**proposed, "film_id": proposed.get("film_id")
         or project.film_id or ""}).model_dump(mode="json")
    for item in todo:
        payload = dict(item.record)
        payload["film"] = film_dump
        _write_sidecar(item, payload)
        item.record["film"] = film_dump
    _sync_project_json(project.root, film_dump)
    return len(todo)


def _parse_one_coord(text: str, is_lat: bool) -> tuple[float, str]:
    """One coordinate with an optional N/S/E/W suffix -> (deg, ref)."""
    m = re.match(r"^\s*([+-]?\d+(?:\.\d+)?)\s*([NSEWnsew]?)\s*$", text)
    if not m:
        raise ValueError(f"Neznámá souřadnice: {text}")
    value = float(m.group(1))
    suffix = m.group(2).upper()
    if is_lat:
        if suffix == "S":
            value = -abs(value)
        elif suffix == "N":
            value = abs(value)
        if not -90.0 <= value <= 90.0:
            raise ValueError("Zeměpisná šířka mimo rozsah")
        return value, ("N" if value >= 0 else "S")
    if suffix == "W":
        value = -abs(value)
    elif suffix == "E":
        value = abs(value)
    if not -180.0 <= value <= 180.0:
        raise ValueError("Zeměpisná délka mimo rozsah")
    return value, ("E" if value >= 0 else "W")


def parse_wgs84_pair(text: str) -> tuple[float, float, str, str]:
    """'49.1124306N, 9.7371244E' (also signed, also space-separated) ->
    (lat, lon, lat_ref, lon_ref). Ported from the scanner annotator."""
    raw = text.strip().replace(";", ",")
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) == 1:
        parts = [p.strip() for p in raw.split() if p.strip()]
    if len(parts) == 4:
        # Space-separated with suffixes: "49.11 N 14.2 E" (the scanner app's
        # operator habit) — glue each value to its hemisphere letter.
        parts = [f"{parts[0]}{parts[1]}", f"{parts[2]}{parts[3]}"]
    if len(parts) != 2:
        raise ValueError("GPS musí být ve tvaru 'šířka, délka'")
    lat, lat_ref = _parse_one_coord(parts[0], is_lat=True)
    lon, lon_ref = _parse_one_coord(parts[1], is_lat=False)
    return lat, lon, lat_ref, lon_ref


def to_dms(value: float, ref: str) -> str:
    """Decimal degrees -> EXIF helper string, e.g. ``49 deg 6' 44.7502" N``."""
    v = abs(value)
    deg = int(v)
    minutes_f = (v - deg) * 60.0
    minutes = int(minutes_f)
    seconds = (minutes_f - minutes) * 60.0
    return f'{deg} deg {minutes}\' {seconds:.4f}" {ref}'


def gps_fields(text: str) -> dict[str, str]:
    """Parse a GPS input line into the full ``gps_*`` field set.

    Raises ValueError (GUI shows it); on success every gps_* key of
    :class:`FrameAnnotation` has a string value.
    """
    stripped = text.strip()
    if not stripped:
        return {}
    lat, lon, lat_ref, lon_ref = parse_wgs84_pair(stripped)
    return {
        "gps_input": stripped,
        "gps_lat": f"{lat:.8f}",
        "gps_lon": f"{lon:.8f}",
        "gps_lat_ref": lat_ref,
        "gps_lon_ref": lon_ref,
        "gps_lat_exif_dms": to_dms(lat, lat_ref),
        "gps_lon_exif_dms": to_dms(lon, lon_ref),
    }


@dataclass
class AnnotationItem:
    """One scan on disk with its sidecar dict and current annotation."""

    image_path: Path
    sidecar_path: Path
    #: Preview file (the capture app writes a quick .jpg next to each frame);
    #: None = fall back to decoding the TIFF.
    preview_path: Path | None
    #: Full sidecar dict as loaded (the merge base on save). Empty dict when
    #: no sidecar exists (a foreign TIF dropped into the folder).
    record: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.image_path.name

    @property
    def annotation(self) -> dict | None:
        ann = self.record.get("annotation")
        return ann if isinstance(ann, dict) else None

    #: Keys whose mere non-None presence counts as content (rating 0 is a
    #: verdict, not an absence — so truthiness would be the wrong test).
    _PRESENT_KEYS = ("rating",)
    #: Keys whose non-empty *string/list* value counts as content.
    _TEXT_KEYS = (
        "title", "note", "tags", "capture_datetime",
        "gps_input", "gps_lat", "gps_lon",
        "gps_lat_exif_dms", "gps_lon_exif_dms",
    )

    @property
    def is_annotated(self) -> bool:
        """Any real content counts — an all-empty block is still un-annotated.

        Drives the ●/○ marker in the list, so the operator can see at a glance
        which frames of the roll still need work.
        """
        ann = self.annotation or {}
        if any(ann.get(k) is not None for k in self._PRESENT_KEYS):
            return True
        return any(bool(ann.get(k)) for k in self._TEXT_KEYS)


@dataclass
class AnnotatedProject:
    """A film folder opened for annotation."""

    root: Path
    items: list[AnnotationItem]
    #: Film orientation as recorded (project.json, else first sidecar) — the
    #: annotator shows previews the way the developer will render them.
    mirrored_horizontal: bool = False
    mirrored_vertical: bool = False
    rotated_180: bool = False
    film_id: str = ""

    @property
    def oriented(self) -> bool:
        return bool(self.mirrored_horizontal or self.mirrored_vertical
                    or self.rotated_180)


def load_folder(folder: str | Path) -> AnnotatedProject:
    """Open a capture-session (or flat) folder for annotation.

    Lists ``frames/*.tif`` (tolerating a flat folder, as the developer does)
    and keeps ``kind == "scan"`` only — annotating a dark frame is a category
    error. Reads sidecars only, never pixels, so a whole roll opens instantly.
    Unreadable sidecars degrade to an empty record with a warning, mirroring
    ``DevelopProject.open``: a project opens even when half its files are
    foreign.
    """
    root = Path(folder)
    frames_dir = root / "frames"
    if not frames_dir.is_dir():
        frames_dir = root
    if not frames_dir.is_dir():
        raise ValueError(f"{folder} neobsahuje složku frames/")

    proj = AnnotatedProject(root=root, items=[])
    # Orientation: project.json first, sidecars as fallback — some older
    # sessions never wrote project.json to disk (same lesson as the
    # developer's _load_orientation, K16_O02).
    _take_orientation(proj, _film_from_project_json(root))
    for path in sorted(frames_dir.glob("*.tif")) + sorted(
            frames_dir.glob("*.tiff")):
        sidecar = path.with_suffix(path.suffix + ".json")
        record: dict = {}
        if sidecar.exists():
            try:
                record = json.loads(sidecar.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("sidecar %s unreadable: %s", sidecar.name, exc)
        kind = record.get("kind", _kind_from_name(path.name))
        if kind != FrameKind.SCAN.value and kind != FrameKind.SCAN:
            continue
        preview = _find_preview(path)
        proj.items.append(AnnotationItem(
            image_path=path, sidecar_path=sidecar,
            preview_path=preview, record=record))
        film = record.get("film") or {}
        if film and not proj.film_id:
            _take_orientation(proj, film)
    if not proj.film_id:
        _take_orientation(proj, _film_from_project_json(root))
    return proj


def _kind_from_name(filename: str) -> str:
    lower = filename.lower()
    for kind in (FrameKind.DARK, FrameKind.FLAT, FrameKind.BASE):
        if lower.startswith(kind.value):
            return kind.value
    return FrameKind.SCAN.value


def _find_preview(image_path: Path) -> Path | None:
    for suffix in PREVIEW_SUFFIXES:
        cand = image_path.with_suffix(suffix)
        if cand.exists():
            return cand
    return None


def _take_orientation(proj: AnnotatedProject, film: dict) -> None:
    proj.mirrored_horizontal = bool(film.get("mirrored_horizontal"))
    proj.mirrored_vertical = bool(film.get("mirrored_vertical"))
    proj.rotated_180 = bool(film.get("rotated_180"))
    proj.film_id = str(film.get("film_id") or "")


def _film_from_project_json(root: Path) -> dict:
    try:
        data = json.loads((root / "project.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    film = data.get("film")
    return film if isinstance(film, dict) else {}


def save_annotation(item: AnnotationItem, annotation: dict) -> None:
    """Merge one annotation block into the sidecar, atomically.

    Everything the dict already had is preserved verbatim (dict round-trip,
    not model round-trip — a model round-trip would silently drop keys this
    app's model does not know yet). The annotation block is validated through
    :class:`FrameAnnotation` *before* touching the disk; the rest of the file
    is untouched by construction, so anything that was loadable before stays
    loadable. A block that fails validation leaves the sidecar exactly as it
    was and the error propagates to the GUI.

    (Full-``CaptureRecord`` validation was tried first and is wrong here: the
    model is ``extra="forbid"``, so it would refuse sidecars carrying another
    tool's keys — keys that were already on disk and already the capture
    app's problem to tolerate or not. The annotator guards only what it
    writes.)

    A frame with no sidecar at all (foreign TIF) gets a minimal
    ``kind/filename/annotation`` sidecar: better an annotation in a thin
    sidecar than nowhere.
    """
    # Raises before anything is written on a malformed block.
    annotation = FrameAnnotation.model_validate(annotation).model_dump(
        mode="json")
    payload = dict(item.record)  # shallow copy: the merge base is preserved
    payload["annotation"] = annotation
    if not item.record:
        payload.setdefault("kind", FrameKind.SCAN.value)
        payload.setdefault("filename", item.image_path.name)
    _write_sidecar(item, payload)


#: Fields the bulk action carries by default: the per-roll facts. Title and
#: note travel only when the operator explicitly checks them (per-frame
#: labels exist and an accidental bulk overwrite would erase them silently).
COMMON_FIELDS: tuple[str, ...] = (
    "capture_datetime",
    "gps_input", "gps_lat", "gps_lon",
    "gps_lat_ref", "gps_lon_ref",
    "gps_lat_exif_dms", "gps_lon_exif_dms",
    "tags", "rating",
)
#: Bulk-transferable only by explicit opt-in.
OPT_IN_FIELDS: tuple[str, ...] = ("title", "note")


def build_common_patch(form: dict, transfer: dict | None = None) -> dict:
    """Form values -> patch of what the bulk action may write.

    Empty fields are skipped so "apply date" never erases a frame's GPS.
    The GPS group travels as a unit: with GPS text on the form, all gps_*
    keys (including cleared ones) move together — half-parsed location data
    is worse than none. ``transfer`` opts title/note in (GUI checkboxes);
    without it they are never part of the patch.

    A date input without a time (``1.1.2026``, bare year) also marks the
    patch ``_sequential_time``: :func:`apply_common` will number the frames
    a minute apart instead of stamping them all to the same instant.
    """
    transfer = transfer or {}
    patch: dict = {}
    for key in (*COMMON_FIELDS, *OPT_IN_FIELDS):
        if key.startswith("gps_"):
            continue
        if key in OPT_IN_FIELDS and not transfer.get(key):
            continue
        value = form.get(key)
        if value not in (None, "", []):
            patch[key] = value
    if (form.get("gps_input") or "").strip():
        patch.update(gps_fields(form["gps_input"]))
    if "capture_datetime" in patch:
        raw, has_time = _parse_datetime(form["capture_datetime"])
        patch["capture_datetime"] = raw
        if not has_time:
            patch["_sequential_time"] = True
    return patch


def apply_common(items: list[AnnotationItem], patch: dict) -> int:
    """Merge the patch into every item's annotation; returns the count saved.

    Existing annotation keys not covered by the patch are kept — this is an
    update, not a replacement. An item without an annotation gets a fresh
    block built from the patch alone.

    **Sequential time.** When the patch came from a time-less date input
    (``_sequential_time``), frame *i* of *this call* gets base + (i+1)
    minutes: every frame a unique, sortable timestamp, in film order
    (00:01, 00:02 …) rather than the identical 00:00 that made photo
    managers shuffle the roll. A date applied with an explicit time starts
    the numbering *at* that time. The counter lives inside one call, so
    applying a second date to a later stretch of the roll resets it —
    exactly what the operator ordered for a film carrying two dates.
    """
    if not patch:
        return 0
    patch = dict(patch)
    sequential = patch.pop("_sequential_time", False)
    base: datetime | None = None
    if sequential and patch.get("capture_datetime"):
        base = datetime.strptime(patch["capture_datetime"], "%Y:%m:%d %H:%M:%S")
    count = 0
    for i, item in enumerate(items):
        merged = {**(item.annotation or {}), **patch}
        if base is not None:
            merged["capture_datetime"] = (
                base + timedelta(minutes=i + 1)).strftime("%Y:%m:%d %H:%M:%S")
        # Validate through the model: a rating "7" or an unparsable date must
        # fail here, not after half the roll was written to.
        annotation = FrameAnnotation.model_validate(merged).model_dump(
            mode="json")
        save_annotation(item, annotation)
        count += 1
    return count


#: Operator-editable acquisition fields shown in the annotator (2026-09-21
#: evening: "ukaž mi tam i kolonky co vyčteš foťák… ať je editovatelné").
#: Only descriptive facts a human can correct — never black/white levels or
#: the averaging internals, which belong to the capture layer.
ACQUISITION_FIELDS: tuple[str, ...] = (
    "camera", "camera_serial", "exposure_time", "gain", "capture_date",
    "copy_number",
)

#: Film-level fields the annotator exposes; ``film_id`` and the orientation
#: flags are NOT here — id is the folder's identity and orientation belongs
#: to how the strip sat in the holder (developer's business).
FILM_FIELDS: tuple[str, ...] = (
    "film_name", "camera", "camera_make", "camera_model",
    "shooting_lens", "film_iso", "format",
    "film_type_class",
    "development", "development_start", "development_end", "content",
    "digitising_lens", "digitising_light", "digitising_holder",
    "digitisation_date", "operator", "box_number", "expiry",
    "pushed_stops", "notes",
)

#: Fields whose model type is not ``str``. An empty text box on a *required*
#: numeric (pushed_stops, copy_number) means "leave the stored value alone"
#: (None would fail validation); on an *optional* numeric (exposure_time,
#: gain) empty means "unset" -> None. Garbage digits are always an error,
#: never a silent 0.
_NUMERIC_KEEP_ON_EMPTY = {"pushed_stops": float, "copy_number": int}
_NUMERIC_NULL_ON_EMPTY = {"exposure_time": float, "gain": float}


def _typed_patch_value(key: str, text: str) -> object:
    """Text-box string -> typed value (``None`` = unset; ``_KEEP`` = leave
    the stored value); raises ValueError on garbage numerics."""
    text = text.strip()
    for table in (_NUMERIC_KEEP_ON_EMPTY, _NUMERIC_NULL_ON_EMPTY):
        numeric = table.get(key)
        if numeric is not None:
            if not text:
                return _KEEP if table is _NUMERIC_KEEP_ON_EMPTY else None
            try:
                return numeric(text.replace(",", "."))
            except ValueError as exc:
                raise ValueError(f"{key}: nerozumím číslu '{text}'") from exc
    return text or None


_KEEP = object()   # sentinel: an empty numeric box keeps the stored value


def _merge_typed(base: dict, patch: dict, allowed: tuple[str, ...]) -> dict:
    """Typed merge of string patches into a stored block, dropping _KEEP."""
    merged = dict(base)
    for key, value in patch.items():
        if key not in allowed:
            continue
        val = _typed_patch_value(key, str(value)) if isinstance(value, str) \
            else value
        if val is not _KEEP:
            merged[key] = val
    return merged


def save_acquisition(item: AnnotationItem, patch: dict) -> None:
    """Merge operator-corrected acquisition facts into one sidecar.

    Typed merge: the patch lands on the existing ``acquisition`` dict and
    the whole block validates through :class:`AcquisitionMetadata` before
    anything is written, so "0,011" cannot become a string the developer
    chokes on and a blank copy-number box cannot erase the stored 1.
    """
    current = _merge_typed(item.record.get("acquisition") or {}, patch,
                           ACQUISITION_FIELDS)
    AcquisitionMetadata.model_validate(current)  # raises -> disk untouched
    payload = dict(item.record)
    payload["acquisition"] = current
    _write_sidecar(item, payload)


def save_film(project: AnnotatedProject, patch: dict) -> int:
    """Merge a film-level edit into every scan sidecar (+ project.json).

    The film block is duplicated into every sidecar by the capture app, so
    an edit to "what film is this" is an edit to all of them — otherwise
    the developer (which reads the film block from sidecars as a fallback)
    would see a different film per frame. Validated through
    :class:`FilmMetadata` before writing; the orientation flags ride along
    untouched because the patch never contains them.

    Returns the number of sidecars updated. Missing project.json is not
    created — it belongs to the capture app; where it exists, its ``film``
    block is kept in sync (the developer reads it first).
    """
    # Two passes: validate every sidecar's merged film block *before* the
    # first write — a rejected edit must leave the whole roll untouched,
    # not half-updated. _merge_typed runs per-item so a _KEEP numeric
    # (empty box) preserves each sidecar's own stored value.
    payloads: list[tuple[AnnotationItem, dict]] = []
    film_dump: dict | None = None
    for item in project.items:
        current = _merge_typed(item.record.get("film") or {}, patch,
                               FILM_FIELDS)
        current.setdefault("film_id", item.record.get("film_id")
                           or project.film_id or "")
        film_dump = FilmMetadata.model_validate(
            current).model_dump(mode="json")
        payloads.append((item, film_dump))
    for item, film in payloads:
        payload = dict(item.record)
        payload["film"] = film
        _write_sidecar(item, payload)
    if film_dump is not None:
        _sync_project_json(project.root, film_dump)
        _take_orientation(project, film_dump)
    return len(payloads)


def _sync_project_json(root: Path, film: dict) -> None:
    path = root / "project.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return                      # no capture project.json: nothing to sync
    data["film"] = film
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(path)


def _write_sidecar(item: AnnotationItem, payload: dict) -> None:
    tmp = item.sidecar_path.with_name(item.sidecar_path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(item.sidecar_path)
    item.record = payload
