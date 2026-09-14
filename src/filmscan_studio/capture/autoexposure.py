"""Live-View metering and the auto-exposure controller.

The loop is deliberately small and auditable, because its output moves the
camera:

1. meter the current Live View frame **in linear terms**,
2. compute the EV correction that lands the 99.9th percentile a fixed headroom
   below clipping,
3. snap that correction onto the body's real shutter ladder,
4. re-meter, iterate while it is still improving, stop when converged.

Metering source matters. The D750's Live View frames are JPEG, so they are not
perfectly linear -- the body applies a display transform. Two consequences are
handled here rather than wished away:

* The meter un-warps the JPEG's display gamma before integrating, restoring an
  approximately linear scale. The correction estimate is then right in stops,
  not merely directionally right.
* Auto exposure can be told to meter a *test capture* (true raw, fully linear)
  instead, at the cost of one shutter release. That is the measurement the
  archival workflow actually trusts, and the GUI exposes both.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from collections.abc import Callable

import cv2
import numpy as np

from filmscan_studio.capture.camera import CameraBackend, LiveFrame
from filmscan_studio.core.exposure import (
    DEFAULT_HEADROOM_EV,
    DEFAULT_METER_PERCENTILE,
    ExposureSettings,
    MeterReading,
    choose_shutter,
    measure,
    required_ev_change,
)

log = logging.getLogger(__name__)

#: Live View JPEGs are display-encoded; undo that before integrating.
_LIVE_GAMMA = 2.2
#: Corrections smaller than this are noise, not signal; stop iterating.
_CONVERGENCE_EPS_EV = 0.05


@dataclass
class AutoExposureResult:
    """What the controller decided, and how close it got."""

    settings: ExposureSettings
    achieved_ev_change: float
    requested_ev_change: float
    reading: MeterReading
    iterations: int
    converged: bool
    #: Populated when clipping survived correction, so the GUI can refuse.
    clipped: bool = False
    #: Shutter ladder exhausted before the target was reached.
    limited_by_lens: bool = False
    history: list[float] = field(default_factory=list)


class LiveMeter:
    """Turns camera output into linear-domain statistics.

    ``jpeg_to_linear`` undoes the body's display transform. It is an
    approximation of the body's tone curve, not an exact inverse, which is why
    the controller re-meters after each adjustment instead of trusting one shot.
    """

    def __init__(
        self,
        black_level: float = 0.0,
        white_level: float = 1.0,
        percentile: float = DEFAULT_METER_PERCENTILE,
    ) -> None:
        self.black_level = black_level
        self.white_level = white_level
        self.percentile = percentile

    @staticmethod
    def decode_live_frame(frame: LiveFrame) -> np.ndarray:
        """Decode a Live View JPEG to float 0..1, no colour assumptions made."""
        buf = np.frombuffer(frame.jpeg, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("Live View JPEG nelze dekódovat")
        return img.astype(np.float64) / 255.0

    def jpeg_to_linear(self, encoded: np.ndarray) -> np.ndarray:
        return np.clip(encoded, 0.0, 1.0) ** _LIVE_GAMMA

    def meter_jpeg(self, frame: LiveFrame) -> MeterReading:
        encoded = self.decode_live_frame(frame)
        linear = self.jpeg_to_linear(encoded)
        # Luminance, not per-channel: the exposure controller has one actuator.
        lum = linear @ np.array([0.2126, 0.7152, 0.0722])
        return measure(lum, self.black_level, self.white_level, self.percentile)

    def meter_raw_signal(self, dn: np.ndarray, black: float, white: float) -> MeterReading:
        """Meter true sensor data -- the authoritative path."""
        return measure(np.asarray(dn, dtype=np.float64), black, white, self.percentile)


@dataclass
class AutoExposureController:
    """Closed-loop shutter selection from linear metering.

    ISO is held fixed by default. Raising ISO buys shutter speed but adds noise,
    and a film scan is a long-exposure studio measurement where noise floor is
    the whole game -- so the controller spends shutter first and only touches ISO
    when told to.
    """

    camera: CameraBackend
    meter: LiveMeter
    headroom_ev: float = DEFAULT_HEADROOM_EV
    max_iterations: int = 4

    def next_live_reading(self) -> MeterReading:
        frame = self.camera.next_live_frame()
        if frame is None:
            raise RuntimeError("Live View neběží - nelze měřit")
        return self.meter.meter_jpeg(frame)

    def run(
        self,
        read: Callable[[], MeterReading] | None = None,
        on_step: Callable[[ExposureSettings], None] | None = None,
    ) -> AutoExposureResult:
        """Iterate Live View metering until the target is hit or progress stops.

        ``read`` overrides the metering source (tests, or a true-raw test
        capture). ``on_step`` receives each proposed setting before it is applied,
        which is how the GUI gets to show what is about to happen.
        """
        meter_once = read or self.next_live_reading
        settings = self.camera.get_settings()
        reading = meter_once()
        requested = required_ev_change(reading, self.headroom_ev)
        start_shutter = settings.shutter
        ladder_choices = list(self.camera.info.shutter_choices)
        residuals: list[float] = []

        for iteration in range(1, self.max_iterations + 1):
            # Each pass closes the *remaining* gap, so ladder snapping cannot
            # compound an error from the previous pass.
            target = settings.shutter * 2.0 ** residuals[-1] if residuals else (
                start_shutter * 2.0**requested
            )
            candidate = choose_shutter(settings, math.log2(target / settings.shutter), ladder_choices)
            if candidate == settings.shutter:
                # Ladder exhausted: the body is already at its slowest usable
                # setting. That is a lighting or lens limit, not a bug to retry.
                return self._result(
                    settings, requested, start_shutter, reading, iteration - 1,
                    converged=False, limited=True, residuals=residuals,
                )
            if on_step is not None:
                on_step(settings.with_shutter(candidate))
            settings = settings.with_shutter(self.camera.set_shutter(candidate))

            reading = meter_once()
            residuals.append(required_ev_change(reading, self.headroom_ev))

            if abs(residuals[-1]) <= _CONVERGENCE_EPS_EV:
                return self._result(
                    settings, requested, start_shutter, reading, iteration,
                    converged=True, limited=False, residuals=residuals,
                )
            if len(residuals) >= 2 and abs(residuals[-1]) > abs(residuals[-2]):
                # Getting worse: dithering between two rungs. Keep the result.
                break

        return self._result(
            settings, requested, start_shutter, reading, len(residuals),
            converged=False, limited=True, residuals=residuals,
        )

    def _result(
        self,
        settings: ExposureSettings,
        requested: float,
        start_shutter: float,
        reading: MeterReading,
        iterations: int,
        converged: bool,
        limited: bool,
        residuals: list[float],
    ) -> AutoExposureResult:
        return AutoExposureResult(
            settings=settings,
            achieved_ev_change=math.log2(settings.shutter / start_shutter),
            requested_ev_change=requested,
            reading=reading,
            iterations=iterations,
            converged=converged,
            clipped=reading.clipped,
            limited_by_lens=limited,
            history=list(residuals),
        )
