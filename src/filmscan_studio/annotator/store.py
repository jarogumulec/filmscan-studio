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
from datetime import datetime
from pathlib import Path

from filmscan_studio.core.models import (
    CaptureRecord,
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
    text = value.strip()
    if not text:
        return ""
    if _YEAR_ONLY.match(text):
        return f"{text}:01:01 00:00:00"
    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt in _DATE_ONLY_FORMATS:
            dt = dt.replace(hour=0, minute=0, second=0)
        return dt.strftime("%Y:%m:%d %H:%M:%S")
    raise ValueError(
        f"Nerozumím datu '{text}' — zkuste např. 12.8.1968, 1968-08-12 14:30 "
        f"nebo jen rok 1968"
    )


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
    tmp = item.sidecar_path.with_name(item.sidecar_path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(item.sidecar_path)
    item.record = payload


#: Fields the bulk action may touch. Title and note deliberately absent:
#: "same date+place for the whole roll" is the use case, "same title for
#: every frame" is not a thing anyone wants overwritten.
COMMON_FIELDS: tuple[str, ...] = (
    "capture_datetime",
    "gps_input", "gps_lat", "gps_lon",
    "gps_lat_ref", "gps_lon_ref",
    "gps_lat_exif_dms", "gps_lon_exif_dms",
    "tags", "rating",
)


def build_common_patch(form: dict) -> dict:
    """Non-empty common fields from form values -> patch dict.

    The bulk action overwrites exactly what the operator filled in; empty
    fields are skipped so "apply date" never erases a frame's GPS. The GPS
    group travels as a unit: with any GPS text on the form, all gps_* keys
    (including the cleared ones) move together — half-parsed location data
    is worse than none.
    """
    patch: dict = {}
    for key in COMMON_FIELDS:
        if key.startswith("gps_"):
            continue
        value = form.get(key)
        if value not in (None, "", []):
            patch[key] = value
    if (form.get("gps_input") or "").strip():
        patch.update(gps_fields(form["gps_input"]))
    return patch


def apply_common(items: list[AnnotationItem], patch: dict) -> int:
    """Merge the patch into every item's annotation; returns the count saved.

    Existing annotation keys not covered by the patch are kept — this is an
    update, not a replacement. An item without an annotation gets a fresh
    block built from the patch alone.
    """
    if not patch:
        return 0
    count = 0
    for item in items:
        merged = {**(item.annotation or {}), **patch}
        # Validate through the model: a rating "7" or an unparsable date must
        # fail here, not after half the roll was written to.
        annotation = FrameAnnotation.model_validate(merged).model_dump(
            mode="json")
        save_annotation(item, annotation)
        count += 1
    return count
