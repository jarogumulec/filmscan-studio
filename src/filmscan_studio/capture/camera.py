"""Camera abstraction.

Everything above this layer talks to :class:`CameraBackend`, never to a vendor
SDK. The seam stays narrow for two reasons:

* The acquisition camera has changed once already (D750 -> Touptek TS2600MP-G2)
  and a swap should touch one file.
* Live View and exposure cannot be tested without a sensor in hand, so a
  deterministic mock backend is what lets the GUI, session logic and auto
  exposure be covered by the test suite at all.

A frame here is *linear sensor data*, never an encoded picture: the mono
IMX571 delivers 16-bit grey with no ISP in the way, so Live View and archive
frames live in the same radiometric domain.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from filmscan_studio.core.exposure import ExposureSettings


@dataclass(frozen=True)
class CameraInfo:
    """What the connected camera reported about itself."""

    model: str
    manufacturer: str | None = None
    serial: str | None = None
    battery_percent: int | None = None
    #: Shutter speeds the camera actually offers, parsed to seconds.
    shutter_choices: tuple[float, ...] = ()
    #: ISO values the camera offers. Empty on gain-only cameras like the
    #: Touptek, which reports ``gain_range`` instead.
    iso_choices: tuple[int, ...] = ()
    #: Analog gain range as (min, max) linear multipliers, e.g. (1.0, 8.0).
    gain_range: tuple[float, float] | None = None
    #: Full-resolution sensor size — what 1:1 zoom and ROI coordinates are
    #: measured against (6224x4168 on the IMX571).
    sensor_width: int | None = None
    sensor_height: int | None = None

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
    """One Live View frame: linear mono sensor data, already de-interleaved.

    ``data`` is a 2-D ``uint16`` (or float) array in native DN — no demosaic,
    no gamma, no auto-brightness. ``black_level``/``white_level`` describe that
    frame's range, which may be wider than 16 bits when binning sums pixels.
    """

    data: np.ndarray
    width: int
    height: int
    black_level: float = 0.0
    white_level: float = 65535.0
    #: Exposure the frame was *actually shot with*, µs, as reported in the
    #: frame's own SDK info record — None when the backend does not report it.
    #: A frame pulled right after ``set_shutter`` was still exposed with the
    #: old shutter (it was in flight over USB when the setting landed); AE
    #: must not meter those, or every reading lags one frame behind.
    expotime_us: int | None = None


@dataclass
class CaptureResult:
    """A still frame written to disk, with the settings that produced it."""

    path: Path
    size_bytes: int
    settings: ExposureSettings
    #: Seconds spent on the exposure plus readout, measured rather than estimated.
    elapsed: float = 0.0
    #: Sensor temperature at exposure, in degC. Recorded on every frame
    #: (dark, flat and scan) because dark subtraction is only valid within a
    #: narrow thermal window — see ``CaptureSession`` export validation.
    sensor_temperature_c: float | None = None
    #: Bits actually delivered, so the developer knows the scale to expect.
    bit_depth: int = 16
    notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class CameraCapabilities:
    """Which controls the camera exposes over the wire.

    Modelled explicitly rather than probed by trial so the UI stays honest
    instead of showing a slider that silently does nothing.
    """

    live_view: bool = True
    shutter: bool = True
    #: Sensitivity control in use: ISO ladder, analog gain, or neither.
    iso: bool = False
    gain: bool = False
    focus_drive: bool = False
    #: Stream can be reframed by the sensor itself (hardware ROI / binning).
    live_view_zoom: bool = True
    #: TEC cooler with a readable sensor temperature and a setpoint.
    cooling: bool = False
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

    def set_iso(self, iso: int) -> int:
        """Apply the closest supported ISO (bodies with an ISO ladder only)."""
        raise NotImplementedError(
            f"{type(self).__name__} ovládá citlivost gainem, ne ISO"
        )

    def set_gain(self, gain: float) -> float:
        """Apply analog gain as a linear multiplier; returns what was accepted.

        Optional: only cameras without an ISO ladder (the Touptek) implement
        it, and the caller checks ``capabilities().gain`` first.
        """
        raise NotImplementedError(
            f"{type(self).__name__} neumí nastavit gain"
        )

    @abstractmethod
    def start_live_view(self) -> None: ...

    @abstractmethod
    def stop_live_view(self) -> None: ...

    @abstractmethod
    def next_live_frame(self) -> LiveFrame | None:
        """Blocking single frame. Returns None when the camera offers nothing."""

    def set_live_view_roi(self, roi: tuple[int, int, int, int] | None) -> None:
        """Reframe the Live View stream: a sensor-pixel ROI, or None for overview.

        ``None`` asks for the binned full-sensor overview; an
        ``(x, y, width, height)`` tuple asks for a 1:1-pixel crop around a point
        of interest. Coordinates are always in full-sensor pixels.

        Optional: backends without hardware ROI raise, and callers check
        ``capabilities().live_view_zoom`` first. Must not be called from a
        streaming callback — route it through the camera worker thread.
        """
        raise NotImplementedError(
            f"{type(self).__name__} neumí měnit řez Live View proudem"
        )

    @abstractmethod
    def capture(self, destination: Path, filename_stem: str,
                keep_live_view: bool = True) -> CaptureResult:
        """Expose and write the 16-bit frame to ``destination``.

        Always full sensor resolution at native bit depth, whatever the Live
        View stream was doing — the archive must not inherit a binned or
        cropped preview. ``keep_live_view`` asks the backend to resume the
        previous Live View mode afterwards.
        """

    @property
    @abstractmethod
    def info(self) -> CameraInfo: ...

    # ------------------------------------------------------------ cooling (optional)

    def get_temperature_c(self) -> float | None:
        """Current sensor temperature in degC, or None when uncooled."""
        return None

    def get_target_temperature_c(self) -> float | None:
        return None

    def set_target_temperature_c(self, temperature_c: float) -> float:
        raise NotImplementedError(
            f"{type(self).__name__} nemá chlazení"
        )

    def set_tec_enabled(self, enabled: bool) -> None:
        raise NotImplementedError(
            f"{type(self).__name__} nemá chlazení"
        )

    def __enter__(self) -> CameraBackend:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.disconnect()


class CameraError(RuntimeError):
    """Any failure talking to the camera."""


class NotConnectedError(CameraError):
    pass
