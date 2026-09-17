"""Capture session: the workflow the brief specifies.

    new film -> dark -> flat -> digitise frames -> metadata -> export

Design decisions worth stating:

* **Frames are never rewritten.** Every artefact this session produces is a raw
  file copied off the camera plus a JSON sidecar. The NEF on disk is byte-for-byte
  what the camera wrote.
* **Dark and flat are captured, not assumed.** Each records its own shutter and
  ISO so the developer can normalise by exposure ratio later; nothing requires
  them to match the scans.
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
from filmscan_studio.core.models import (
    AcquisitionMetadata,
    CaptureRecord,
    FilmMetadata,
    FrameKind,
    ImageFormat,
    to_json_dict,
)
from filmscan_studio.core.rawio import RawFrame, jpeg_dimensions, open_frame

log = logging.getLogger(__name__)

#: Injectable so tests can supply frames without LibRaw. Defaults to the real reader.
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


@dataclass
class SessionState:
    """Progress through the capture workflow, for the GUI to render."""

    film: FilmMetadata
    dark_count: int = 0
    flat_count: int = 0
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
        #: When True (and the backend supports it) captures release the shutter
        #: without ending Live View — the mirror stays raised between frames.
        #: The GUI flips this off for a body that proved it refuses the path.
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
        is_jpeg = result.file_format == "jpeg"
        if is_jpeg:
            # Never rawpy: LibRaw reads it as a broken NEF (b'Input/output
            # error'). Geometry from the SOF marker, radiometry N/A — a
            # body-JPEG is a display-referred picture, not sensor data.
            dims = jpeg_dimensions(result.path)
            frame = None
            width, height = dims if dims else (None, None)
            log.warning(
                "%s je JPEG, ne RAW (tělo má Compression Level jinou než RAW) "
                "— pro archivní skenování je to nepoužitelné, viz tlačítko "
                "Nastavit RAW v okně Capture.", result.path.name)
        else:
            frame = self._safe_read(result.path)
            width = frame.width if frame else None
            height = frame.height if frame else None
        acquisition = (frame.acquisition if frame else None) or AcquisitionMetadata()
        # Settings at release win over EXIF for shutter and ISO: a long or bulb
        # exposure can report a rounded EXIF time that is not what was applied.
        acquisition = acquisition.model_copy(
            update={
                "exposure_time": result.settings.shutter,
                "iso": result.settings.iso,
                "f_number": acquisition.f_number or result.settings.aperture,
                "capture_date": acquisition.capture_date or datetime.now().astimezone(),
            }
        )
        record = CaptureRecord(
            film_id=self.film.film_id,
            frame_number=frame_number,
            kind=kind,
            filename=result.path.name,
            file_format=ImageFormat.JPEG if is_jpeg else ImageFormat.RAW,
            width=width,
            height=height,
            black_level=frame.black_level if frame else None,
            white_level=frame.white_level if frame else None,
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
            scan_count=len(self.catalog.captures(self.film.film_id, FrameKind.SCAN)),
            next_frame_number=self.catalog.next_frame_number(self.film.film_id),
            last_error=self._last_error,
        )

    def export_project(self) -> Path:
        """Write the project bundle: catalog dump plus the film record."""
        self.catalog.export_json(self.paths.root / "project_export.json")
        payload = {
            "schema_version": 1,
            "film": to_json_dict(self.film),
            "operator": self._operator,
            "exported_at": datetime.now().astimezone().isoformat(),
            "counts": {
                "scans": len(self.catalog.captures(self.film.film_id, FrameKind.SCAN)),
                "dark": len(self.catalog.captures(self.film.film_id, FrameKind.DARK)),
                "flat": len(self.catalog.captures(self.film.film_id, FrameKind.FLAT)),
            },
        }
        self.paths.project.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return self.paths.project
