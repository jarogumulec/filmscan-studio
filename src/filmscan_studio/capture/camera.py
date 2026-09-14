"""Camera abstraction.

Everything above this layer talks to :class:`CameraBackend`, never to gphoto2.
Two reasons:

* The brief makes gphoto2 a *first prototype* that Nikon SDK may replace. Keeping
  the seam narrow means that swap touches one file.
* Live View and shutter release cannot be tested without a body in hand, so a
  deterministic mock backend is what lets the GUI, session logic and auto
  exposure be covered by the test suite at all.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from filmscan_studio.core.exposure import ExposureSettings


@dataclass(frozen=True)
class CameraInfo:
    """What the connected body reported about itself."""

    model: str
    manufacturer: str | None = None
    serial: str | None = None
    battery_percent: int | None = None
    #: Shutter speeds the body actually offers, parsed to seconds.
    shutter_choices: tuple[float, ...] = ()
    #: ISO values the body actually offers.
    iso_choices: tuple[int, ...] = ()

    def nearest_shutter(self, wanted: float) -> float | None:
        return _nearest(self.shutter_choices, wanted)

    def nearest_iso(self, wanted: int) -> int | None:
        return _nearest(self.iso_choices, wanted)


def _nearest[T](choices: tuple[T, ...], wanted: T) -> T | None:
    if not choices:
        return None
    return min(choices, key=lambda c: abs(_num(c) - _num(wanted)))


def _num(v: object) -> float:
    return float(v)


@dataclass
class LiveFrame:
    """One Live View frame.

    ``jpeg`` holds the compressed frame as delivered by the body. The D750 sends
    a small (640x424) JPEG over PTP; decoding is left to the caller so this layer
    stays free of image libraries.
    """

    jpeg: bytes
    width: int | None = None
    height: int | None = None


@dataclass
class CaptureResult:
    """A still frame written to disk, with the settings that produced it."""

    path: Path
    size_bytes: int
    settings: ExposureSettings
    #: Seconds spent on the exposure plus readout, measured rather than estimated.
    elapsed: float = 0.0
    #: Where the file was written: memory card or internal RAM.
    capture_target: str = "card"


@dataclass
class CameraCapabilities:
    """Which controls the body exposes over the wire.

    The D750 notably has no aperture control: on a manual Micro-Nikkor the
    operator sets f-stop on the lens, and the value is only known afterwards from
    EXIF. Modelling this explicitly keeps the UI honest instead of showing a
    slider that silently does nothing.
    """

    live_view: bool = True
    shutter: bool = True
    iso: bool = True
    aperture: bool = False
    focus_drive: bool = True
    notes: tuple[str, ...] = field(default_factory=tuple)


class CameraBackend(ABC):
    """Contract every camera implementation must satisfy."""

    @abstractmethod
    def connect(self) -> CameraInfo: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def capabilities(self) -> CameraCapabilities: ...

    @abstractmethod
    def get_settings(self) -> ExposureSettings: ...

    @abstractmethod
    def set_shutter(self, seconds: float) -> float:
        """Apply the closest supported speed; returns what the camera accepted."""

    @abstractmethod
    def set_iso(self, iso: int) -> int:
        """Apply the closest supported ISO; returns what the camera accepted."""

    @abstractmethod
    def start_live_view(self) -> None: ...

    @abstractmethod
    def stop_live_view(self) -> None: ...

    @abstractmethod
    def next_live_frame(self) -> LiveFrame | None:
        """Blocking single frame. Returns None when the body offers nothing."""

    @abstractmethod
    def capture(self, destination: Path, filename_stem: str) -> CaptureResult:
        """Release the shutter and write the raw file to ``destination``."""

    @property
    @abstractmethod
    def info(self) -> CameraInfo: ...

    def __enter__(self) -> CameraBackend:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.disconnect()


class CameraError(RuntimeError):
    """Any failure talking to the body."""


class NotConnectedError(CameraError):
    pass
