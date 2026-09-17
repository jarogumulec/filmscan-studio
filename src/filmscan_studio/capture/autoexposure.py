"""Live-View metering and the auto-exposure controller.

The acquisition camera's Live View stream is *linear sensor data*: the mono
IMX571 sends 16-bit grey through hardware binning with no ISP, no tone curve
and no auto-brightness in the way (unlike the D750's LV JPEG, which measured
2026-09-15 was bit-identical while the shutter moved three stops). Metering the
stream therefore meters the capture — one honest path, no body-meter detour.

The controller closes the loop empirically, one frame of feedback per step:

1. meter the current stream,
2. compute the stops missing against the 99.9-percentile target with headroom,
3. move the shutter to absorb them,
4. re-meter; iterate until converged, dithering, or the ladder runs out.

Actuator policy for archival digitising (2026-09 brief): the whole film is one
transmission measurement, so **shutter is the only actuator** while
``gain_lock`` is on — gain multiplies signal *and* noise, so raising it mid-
film changes the measurement's identity. With the lock off, extra light is
still bought with the shutter first and gain only when the shutter ladder has
bottomed out; excess light is shed by lowering gain before shortening the
shutter (the reverse asymmetry: gain is the free lever on the way down only
when it is already above the floor).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from filmscan_studio.capture.camera import CameraBackend
from filmscan_studio.core.exposure import (
    DEFAULT_HEADROOM_EV,
    DEFAULT_METER_PERCENTILE,
    ExposureSettings,
    MeterReading,
    choose_gain,
    choose_shutter,
    measure,
    required_ev_change,
)

log = logging.getLogger(__name__)

#: Corrections smaller than this are noise, not signal; stop iterating.
_CONVERGENCE_EPS_EV = 0.05
#: Shutter bounds for cameras with a continuous shutter (the Touptek speaks
#: microseconds and offers no choice list): 1 us to 30 minutes.
_CONTINUOUS_SHUTTER_RANGE = (1e-6, 1800.0)
#: Extra stops to shed per iteration while the frame is still clipped: the
#: meter cannot see past the rail, so ``required_ev_change`` reports at most
#: the headroom deficit no matter how far over the scene really is. Without a
#: clip-aware step the loop would crawl out of a 5-stop overexposure at
#: 0.4 EV per frame.
_CLIP_STEP_EV = 1.5


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
    #: Gain range exhausted (with the lock off) before the target was reached.
    limited_by_gain: bool = False
    #: Human-readable limit detail incl. the concrete numbers, e.g.
    #: "ani 30 s je scéna o 2.1 EV tmavší" — None when converged.
    limit_note: str | None = None
    history: list[float] = field(default_factory=list)


class LiveMeter:
    """Turns camera output into linear-domain statistics.

    The stream is already linear DN (see module docstring), so there is no
    display transform to undo — ``levels`` are read from the frame itself.
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
    def decode_live_frame(frame) -> np.ndarray:
        """The frame's data as float DN, ready for metering or display."""
        return np.asarray(frame.data, dtype=np.float64)

    def frame_levels(self, frame) -> tuple[float, float]:
        """Black/white for one frame: its own if it reports them."""
        black = getattr(frame, "black_level", None)
        white = getattr(frame, "white_level", None)
        if white:
            return (float(black or 0.0), float(white))
        return (self.black_level, self.white_level)

    def meter_frame(self, frame) -> MeterReading:
        """Meter one Live View frame in its native DN scale."""
        black, white = self.frame_levels(frame)
        return measure(self.decode_live_frame(frame), black, white, self.percentile)

    def normalized_frame(self, frame) -> tuple[np.ndarray, MeterReading]:
        """(0..1 float data, reading) for one frame — one pass, one truth.

        The GUI paints and histograms from the same normalised array, so the
        histogram cannot disagree with the picture the way a JPEG preview and
        a body meter used to.
        """
        black, white = self.frame_levels(frame)
        span = white - black
        if span <= 0:
            raise ValueError("white_level must exceed black_level")
        data = (self.decode_live_frame(frame) - black) / span
        return data, measure(data, 0.0, 1.0, self.percentile)


def _snap(value: float, ladder: list[float]) -> float:
    """Closest rung of a real camera ladder (log distance: stops are the unit)."""
    return min(ladder, key=lambda rung: abs(math.log2(rung / value)))


@dataclass
class AutoExposureController:
    """Closed-loop exposure from the Live View stream's own linear data.

    See the module docstring for the actuator policy.
    """

    camera: CameraBackend
    meter: LiveMeter
    #: Where to park the metering, in EV relative to the headroom target:
    #: 0 = "the framed scene lands ``DEFAULT_HEADROOM_EV`` below clipping".
    #: A red-rect bias is passed here when the operator points at a region.
    target_ev: float = 0.0
    headroom_ev: float = DEFAULT_HEADROOM_EV
    max_iterations: int = 4
    #: Archival mode: the shutter is the only actuator; gain is the
    #: measurement's identity and must not move. With the lock on, a residual
    #: the shutter alone cannot close is reported as a lighting limit, never
    #: fixed by raising gain.
    gain_lock: bool = False

    def next_live_reading(self) -> MeterReading:
        frame = self.camera.next_live_frame()
        if frame is None:
            raise RuntimeError("Live View neběží - nelze měřit")
        return self.meter.meter_frame(frame)

    # ---------------------------------------------------------------- entry

    def run(
        self,
        read: Callable[[], MeterReading] | None = None,
        on_step: Callable[[ExposureSettings], None] | None = None,
    ) -> AutoExposureResult:
        """Converge the exposure.

        ``read`` overrides metering entirely (tests; an AE rect the caller
        crops before metering). Without it, one Live View frame per step.
        """
        meter_once = read or self.next_live_reading
        settings = self.camera.get_settings()
        try:
            reading = meter_once()
        except ValueError:
            # Signal at black: there is nothing to meter yet, aim one big
            # step up and let the loop find its way from a lit frame.
            reading = None
        start = settings
        # A body with a shutter ladder offers a choice list; a continuous
        # backend (the Touptek, µs-resolution) clamps against its exposure
        # range instead — there are no rungs, only the two ends.
        ladder = tuple(self.camera.info.shutter_choices) or None
        gain_range = self.camera.info.gain_range
        residuals: list[float] = []
        requested: float | None = None
        limited_shutter = limited_gain = False
        note: str | None = None

        for iteration in range(1, self.max_iterations + 1):
            if reading is None:
                delta = self.headroom_ev + 4.0     # grossly under; big step up
            else:
                residual = required_ev_change(reading, self.headroom_ev) - self.target_ev
                residuals.append(residual)
                if requested is None:
                    requested = residual
                if abs(residual) <= _CONVERGENCE_EPS_EV:
                    return self._result(
                        settings, requested or 0.0, start, reading, iteration - 1,
                        converged=True, limited_shutter=False, limited_gain=False,
                        residuals=residuals,
                    )
                if (len(residuals) >= 2
                        and abs(residuals[-1]) > abs(residuals[-2])):
                    # Getting worse: dithering between two rungs. Keep what we have.
                    break
                if reading.clipped:
                    # The meter is blind above the rail: a scene five stops
                    # over still reports at most the headroom deficit (-0.4).
                    # Step in big until the frame un-clips, then trust the
                    # residual. Convergence already returned above, so getting
                    # here clipped *is* being over.
                    residual = min(residual, -_CLIP_STEP_EV)
                delta = residual

            shutter, gain = self._plan(delta, settings, ladder, gain_range)
            if (shutter, gain) == (settings.shutter, settings.gain):
                # Nothing left to give in the requested direction.
                limited_shutter = True
                if not self.gain_lock and gain_range is not None:
                    limited_gain = True
                break
            settings = ExposureSettings(
                shutter=self.camera.set_shutter(shutter),
                iso=settings.iso,
                gain=(self.camera.set_gain(gain)
                      if gain is not None and gain != settings.gain
                      else settings.gain),
                aperture=settings.aperture,
            )
            if on_step is not None:
                on_step(settings)
            try:
                reading = meter_once()
            except ValueError:
                reading = None

        converged = False
        if residuals and abs(residuals[-1]) <= _CONVERGENCE_EPS_EV:
            converged = True
        elif residuals:
            ends = ladder or _CONTINUOUS_SHUTTER_RANGE
            slowest, fastest = max(ends), min(ends)
            dark = residuals[-1] > 0
            pinned = (settings.shutter >= slowest * (1 - 1e-6)) if dark \
                else (settings.shutter <= fastest * (1 + 1e-6))
            if pinned and self.gain_lock:
                extreme = ExposureSettings(shutter=slowest if dark else fastest,
                                           iso=None)
                note = (f"ani {extreme.shutter_string()}s je scéna o "
                        f"{abs(residuals[-1]):.1f} EV "
                        + ("tmavší, než cílí — přidej světlo" if dark
                           else "světlejší — ztmavi scénu")
                        + " (gain uzamčen)")
            else:
                note = (f"AE neskonvergovalo — zůstává {abs(residuals[-1]):.1f} EV "
                        f"({settings.shutter_string()}); zkus Auto Exposure znovu")
        final_reading = reading or self.meter.meter_frame(
            self.camera.next_live_frame()
        )
        return self._result(
            settings, requested or 0.0, start, final_reading, len(residuals),
            converged=converged, limited_shutter=limited_shutter,
            limited_gain=limited_gain, residuals=residuals, note=note,
        )

    def _plan(self, needed_ev: float, settings: ExposureSettings,
              shutter_ladder: tuple[float, ...] | None,
              gain_range: tuple[float, float] | None,
              ) -> tuple[float, float | None]:
        """Split a needed EV change into (shutter, gain), quality-first.

        Needed_ev > 0 = scene darker than target = more exposure. With the
        archival lock the shutter absorbs everything — snapped to a rung on a
        body with a ladder, clamped to the exposure range when the shutter is
        continuous. Unlocked, shutter moves first (time is free, gain is
        noise); gain only takes the residual the shutter range cannot reach.
        """
        ideal = settings.shutter * 2.0 ** needed_ev
        if shutter_ladder:
            rung = _snap(ideal, list(shutter_ladder))
        else:
            fastest, slowest = _CONTINUOUS_SHUTTER_RANGE
            rung = max(fastest, min(slowest, ideal))
        if self.gain_lock or gain_range is None or settings.gain is None:
            return rung, settings.gain
        shutter_done = math.log2(rung / settings.shutter)
        leftover = needed_ev - shutter_done
        if abs(leftover) <= _CONVERGENCE_EPS_EV:
            return rung, settings.gain
        # Gain direction that helps, clamped to what the camera can do:
        gain = choose_gain(settings, leftover, gain_range)
        return rung, gain

    def _result(
        self,
        settings: ExposureSettings,
        requested: float,
        start: ExposureSettings,
        reading: MeterReading,
        iterations: int,
        converged: bool,
        limited_shutter: bool,
        limited_gain: bool,
        residuals: list[float],
        note: str | None = None,
    ) -> AutoExposureResult:
        return AutoExposureResult(
            settings=settings,
            achieved_ev_change=start.stops_between(settings),
            requested_ev_change=requested,
            reading=reading,
            iterations=iterations,
            converged=converged,
            clipped=reading.clipped,
            limited_by_lens=limited_shutter,
            limited_by_gain=limited_gain,
            limit_note=note,
            history=list(residuals),
        )
