"""Capture session: the workflow the brief specifies.

    new film -> dark -> flat -> digitise frames -> metadata -> export

Design decisions worth stating:

* **Frames are never rewritten.** Every artefact this session produces is the
  16-bit TIFF the backend wrote plus a JSON sidecar. Nothing re-encodes pixels.
* **Dark and flat are captured, not assumed.** Each records its own shutter,
  gain and sensor temperature so the developer can normalise by exposure ratio
  later; nothing requires them to match the scans, but a temperature mismatch
  beyond ``DARK_TEMPERATURE_TOLERANCE_C`` is flagged at export.
* **Frame numbering follows the film**, not the capture order, because the
  operator advances the film holder by hand and needs the file name to match the
  frame number written on the canister.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from filmscan_studio.capture.camera import CameraBackend, CaptureResult
from filmscan_studio.core.catalog import Catalog
from filmscan_studio.core.filmbase import (
    FILM_BASE_FILENAME,
    FilmBaseSample,
    append_sample,
    load_samples,
    region_mean,
)
from filmscan_studio.core.models import (
    AcquisitionMetadata,
    CaptureRecord,
    FilmMetadata,
    FrameKind,
    to_json_dict,
)
from filmscan_studio.core.rawio import RawFrame, open_frame

#: A dark may only be subtracted from a scan captured within this thermal
#: window of it (degC). Dark current roughly halves per ~6 degC drop, so a
#: mismatch of more than half a degree leaves a visible residual gradient.
DARK_TEMPERATURE_TOLERANCE_C = 0.5

log = logging.getLogger(__name__)

#: Injectable so tests can stand in their own frame reader. Defaults to the
#: real TIFF reader.
FrameReader = Callable[[Path], RawFrame]


@dataclass
class SessionPaths:
    """Directory layout for one film's digitisation.

    Calibration frames sit beside the scans because they *are* scans of nothing;
    only the sidecar distinguishes them. That keeps a stray dark frame usable by
    any other tool that reads the folder.
    """

    root: Path

    @classmethod
    def create(cls, root: str | Path, film_id: str) -> SessionPaths:
        paths = cls(Path(root) / film_id)
        (paths.root / "frames").mkdir(parents=True, exist_ok=True)
        return paths

    @property
    def frames(self) -> Path:
        return self.root / "frames"

    @property
    def catalog(self) -> Path:
        return self.root / "catalog.sqlite"

    @property
    def project(self) -> Path:
        return self.root / "project.json"

    def sidecar(self, raw_file: Path) -> Path:
        return raw_file.with_name(raw_file.name + ".json")

    @property
    def film_base(self) -> Path:
        """Per-film JSON of film-base / min-point measurements (see filmbase)."""
        return self.root / FILM_BASE_FILENAME


@dataclass
class SessionState:
    """Progress through the capture workflow, for the GUI to render."""

    film: FilmMetadata
    dark_count: int = 0
    flat_count: int = 0
    base_count: int = 0
    scan_count: int = 0
    next_frame_number: int = 1
    last_error: str | None = None

    @property
    def has_calibration(self) -> bool:
        return self.dark_count > 0 and self.flat_count > 0

    @property
    def stage(self) -> str:
        """Which step of the documented workflow the operator is on."""
        if self.dark_count == 0:
            return "dark"
        if self.flat_count == 0:
            return "flat"
        return "frames"


class CaptureSession:
    """Owns one film's capture run."""

    def __init__(
        self,
        camera: CameraBackend,
        film: FilmMetadata,
        paths: SessionPaths,
        operator: str | None = None,
        frame_reader: FrameReader = open_frame,
        keep_live_view: bool = True,
    ) -> None:
        self.camera = camera
        self.paths = paths
        self.film = film
        self.catalog = Catalog(paths.catalog)
        self._read_frame = frame_reader
        self._operator = operator or film.operator
        self._last_error: str | None = None
        self._pending_frame_number: int | None = None
        #: When True the backend resumes the Live View mode it had before the
        #: capture (overview/ROI) instead of leaving the stream stopped.
        self.keep_live_view = keep_live_view
        self.catalog.upsert_film(film)

    # ---------------------------------------------------------------- workflow

    def capture_dark(self, count: int = 1) -> list[CaptureResult]:
        """Capture dark frames. Caller must have capped the light path first."""
        return self._capture_frames(count, FrameKind.DARK)

    def capture_flat(self, count: int = 3) -> list[CaptureResult]:
        """Capture flat-field frames.

        Defaults to three because flats are stacked, and a median of one gives no
        way to detect a cosmic-ray hit in the stack.
        """
        return self._capture_frames(count, FrameKind.FLAT)

    def capture_base(self, rect: tuple[int, int, int, int] | None = None,
                     ) -> tuple[CaptureResult, FilmBaseSample]:
        """Capture a film base / min point frame and measure the rect on it.

        A real exposure (unlike the stream reading the GUI can also take): the
        frame is archived as a ``base`` capture with its own sidecar — shutter,
        gain, temperature — exactly like a dark or a flat, because a later
        scaling of this reference onto other-exposure frames needs all three.
        ``rect`` is in *sensor* pixels (the frame is full-size; the GUI
        converts from stream px before calling). The measured mean lands in the
        per-film ``film_base.json`` next to the archive record.
        """
        result = self._capture_frames(1, FrameKind.BASE)[0]
        # The measurement is this capture's whole point: an unreadable frame
        # is an error here, not a sidecar-worth-saving degradation.
        frame = self._read_frame(result.path)
        mean = region_mean(frame.data, rect)
        sample = FilmBaseSample(
            kind="frame",
            mean_dn=mean,
            black_level=frame.black_level,
            white_level=frame.white_level,
            shutter=result.settings.shutter,
            gain=result.settings.gain,
            sensor_temperature_c=result.sensor_temperature_c,
            rect=rect,
            source=result.path.name,
            captured_at=datetime.now().astimezone(),
        )
        self.record_base_sample(sample)
        return result, sample

    def record_base_sample(self, sample: FilmBaseSample) -> Path:
        """Append one film-base measurement to the per-film JSON."""
        append_sample(self.paths.film_base, sample)
        return self.paths.film_base

    def base_samples(self) -> list[FilmBaseSample]:
        return load_samples(self.paths.film_base)

    def capture_scan(self, frame_number: int | None = None) -> CaptureResult:
        """Capture one film frame under its film-advance number."""
        number = (
            frame_number
            if frame_number is not None
            else self.catalog.next_frame_number(self.film.film_id)
        )
        if number < 1:
            raise ValueError("frame numbers start at 1")
        return self._capture_frames(1, FrameKind.SCAN, frame_number=number)[0]

    # ----------------------------------------------------------------- internals

    def _capture_frames(
        self, count: int, kind: FrameKind, frame_number: int | None = None
    ) -> list[CaptureResult]:
        results: list[CaptureResult] = []
        for _ in range(count):
            number = frame_number
            if kind is FrameKind.SCAN and number is None:
                number = self.catalog.next_frame_number(self.film.film_id)
            stem = (
                f"frame{number:03d}"
                if number is not None
                else f"{kind.value}_{len(self.catalog.captures(self.film.film_id, kind)) + 1:03d}"
            )
            try:
                result = self.camera.capture(
                    self.paths.frames, stem,
                    keep_live_view=self.keep_live_view,
                )
            except Exception as exc:  # noqa: BLE001 - surfaced to the GUI via last_error
                self._last_error = str(exc)
                log.exception("capture failed")
                raise
            self._record(result, kind, number)
            results.append(result)
        return results

    def _record(
        self, result: CaptureResult, kind: FrameKind, frame_number: int | None
    ) -> CaptureRecord:
        """Write the JSON sidecar and the catalog row for one captured file."""
        frame = self._safe_read(result.path)
        width = frame.width if frame else None
        height = frame.height if frame else None
        acquisition = (frame.acquisition if frame else None) or AcquisitionMetadata()
        # Settings at release win over anything embedded in the file: they are
        # what the operator asked for at the moment of exposure. (Aperture is
        # not recorded: the rig's lens is manual and the mono body never learns
        # an f-number — the D750-era f_number field is retired.)
        acquisition = acquisition.model_copy(
            update={
                "exposure_time": result.settings.shutter,
                "iso": result.settings.iso,
                "gain": result.settings.gain,
                "capture_date": acquisition.capture_date or datetime.now().astimezone(),
            }
        )
        record = CaptureRecord(
            film_id=self.film.film_id,
            frame_number=frame_number,
            kind=kind,
            filename=result.path.name,
            width=width,
            height=height,
            black_level=frame.black_level if frame else None,
            white_level=frame.white_level if frame else None,
            sensor_temperature_c=result.sensor_temperature_c,
            film=self.film,
            acquisition=acquisition,
        )
        self.paths.sidecar(result.path).write_text(
            json.dumps(to_json_dict(record), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self.catalog.add_capture(record)
        return record

    def _safe_read(self, path: Path) -> RawFrame | None:
        """Read frame geometry if possible; a sidecar is still worth writing.

        A raw file we cannot parse is no reason to lose the shot's metadata, so
        failures degrade to a partial sidecar plus a log line.
        """
        try:
            return self._read_frame(path)
        except Exception as exc:  # noqa: BLE001
            log.warning("metadata unreadable for %s: %s", path.name, exc)
            return None

    @property
    def state(self) -> SessionState:
        return SessionState(
            film=self.film,
            dark_count=len(self.catalog.captures(self.film.film_id, FrameKind.DARK)),
            flat_count=len(self.catalog.captures(self.film.film_id, FrameKind.FLAT)),
            base_count=len(self.catalog.captures(self.film.film_id, FrameKind.BASE)),
            scan_count=len(self.catalog.captures(self.film.film_id, FrameKind.SCAN)),
            next_frame_number=self.catalog.next_frame_number(self.film.film_id),
            last_error=self._last_error,
        )

    def unmatched_scan_frame_numbers(self) -> list[int]:
        """Scan frame numbers with no dark captured at (nearly) the same temperature.

        Dark subtraction on a TEC-cooled sensor is only valid inside a narrow
        thermal window — a dark taken 2 degC warmer leaves a positive residual
        everywhere, one 2 degC colder under-subtracts. A scan whose darks all
        sit outside ``DARK_TEMPERATURE_TOLERANCE_C`` of its own exposure
        temperature cannot be calibrated honestly, so export must name it.
        Scans or darks without a recorded temperature are reported too: unknown
        is not the same as matching.
        """
        darks = [r for r in self.catalog.captures(self.film.film_id, FrameKind.DARK)
                 if r.sensor_temperature_c is not None]
        unmatched: list[int] = []
        for scan in self.catalog.captures(self.film.film_id, FrameKind.SCAN):
            if scan.sensor_temperature_c is None:
                unmatched.append(scan.frame_number or 0)
                continue
            matched = any(
                abs(scan.sensor_temperature_c - d.sensor_temperature_c)
                <= DARK_TEMPERATURE_TOLERANCE_C
                for d in darks
            )
            if not matched:
                unmatched.append(scan.frame_number or 0)
        return unmatched

    def export_project(self) -> tuple[Path, list[int]]:
        """Write the project bundle; returns (path, scan frames lacking a dark).

        The second value is empty in the healthy case; non-empty frame numbers
        mean the GUI must warn the operator that those scans have no
        temperature-matched dark (see :meth:`unmatched_scan_frame_numbers`).
        """
        self.catalog.export_json(self.paths.root / "project_export.json")
        unmatched = self.unmatched_scan_frame_numbers()
        payload = {
            "schema_version": 1,
            "film": to_json_dict(self.film),
            "operator": self._operator,
            "exported_at": datetime.now().astimezone().isoformat(),
            "counts": {
                "scans": len(self.catalog.captures(self.film.film_id, FrameKind.SCAN)),
                "dark": len(self.catalog.captures(self.film.film_id, FrameKind.DARK)),
                "flat": len(self.catalog.captures(self.film.film_id, FrameKind.FLAT)),
                "base": len(self.catalog.captures(self.film.film_id, FrameKind.BASE)),
            },
            "scans_without_temperature_matched_dark": unmatched,
        }
        self.paths.project.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return self.paths.project, unmatched
