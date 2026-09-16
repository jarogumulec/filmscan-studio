"""Live-View metering and the auto-exposure controller.

Why the controller does NOT meter the Live View JPEG stream on the Nikon SDK
backend (measured on the D750, 2026-09-15): the body serves LV as an
*auto-brightness preview* — the delivered JPEG median stayed bit-identical
(0.03026 linear) while the shutter moved from 1/2 s to 1/500 s and ISO from
100 to 1600. Metering that stream can never close the loop; it is the display,
not the measurement.

The trustworthy sensor is the body's own exposure meter, exposed over the SDK
as the ExposureStatus Float cap. Measured step response: −3.00 EV exactly for
3 stops of shutter, +4.00 EV for 4 stops of ISO, instantaneously. It reports
``log2(applied_exposure / correctly_exposed)``, so driving it to ~0 is exactly
what the body's viewfinder needle would show. The controller therefore

1. reads the body meter,
2. *solves* the actuator move in one pass — the meter says how many stops are
   missing, both ladders are written at once (one press = done),
3. verifies against the re-read meter and tops up only what ladder-snapping
   left over,
4. reports a genuine ladder limit with its concrete numbers when one runs out
   — never the old "already at minimum" lie.

Actuator policy for archival digitising: extra light is bought with the
*shutter* first (ISO stays at the noise floor and only rises when the shutter
ladder has bottomed out); excess light is shed by *lowering ISO* first (free
image quality) before shortening the shutter.

Backends without the body meter (mock, gphoto2) keep the Live View metering
path, which works there because their streams are exposure-faithful.

Metering a JPEG frame still happens where it is honest: ``LiveMeter`` un-warps
the display gamma first, so any LV-derived number is at least in linear terms.
"""

from __future__ import annotations

import logging
import math
import time
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
#: Let the body's meter settle after an actuator write before re-reading.
#: The D750 answers instantly in practice; the pause is insurance.
_METER_SETTLE_S = 0.3


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
    #: ISO ladder exhausted before the target was reached.
    limited_by_iso: bool = False
    #: Human-readable limit detail incl. the ladder rungs involved, e.g.
    #: "ani 30 s a ISO 6400 je scéna o 2.1 EV tmavší" — None when converged.
    limit_note: str | None = None
    #: Set when the controller ran on the body meter; None on the LV path.
    body_ev: float | None = None
    history: list[float] = field(default_factory=list)


class LiveMeter:
    """Turns camera output into linear-domain statistics.

    ``jpeg_to_linear`` undoes the body's display transform. It is an
    approximation of the body's tone curve, not an exact inverse — and on the
    Nikon SDK backend the LV frame is additionally auto-brightnessed, so these
    numbers describe the *preview*, never the capture; see module docstring.
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


def _snap(value: float, ladder: list[float]) -> float:
    """Closest rung of a real camera ladder (log distance: stops are the unit)."""
    return min(ladder, key=lambda rung: abs(math.log2(rung / value)))


@dataclass
class AutoExposureController:
    """Closed-loop exposure from the body's own meter when it has one.

    See the module docstring for the actuator policy and the measurements that
    forced the design.
    """

    camera: CameraBackend
    meter: LiveMeter
    #: Where to park the body meter, in its own EV units: 0 = the body's own
    #: "correct exposure" verdict (what the viewfinder needle aims at).
    target_ev: float = 0.0
    headroom_ev: float = DEFAULT_HEADROOM_EV
    max_iterations: int = 4

    def next_live_reading(self) -> MeterReading:
        frame = self.camera.next_live_frame()
        if frame is None:
            raise RuntimeError("Live View neběží - nelze měřit")
        return self.meter.meter_jpeg(frame)

    # ---------------------------------------------------------------- entry

    def run(
        self,
        read: Callable[[], MeterReading] | None = None,
        on_step: Callable[[ExposureSettings], None] | None = None,
    ) -> AutoExposureResult:
        """Converge in one press.

        ``read`` overrides metering entirely (tests; a true-raw test capture
        later). Without it, the body meter is used where the backend offers
        one and Live View metering otherwise.
        """
        read_body = self._body_meter()
        if read is None and read_body is not None:
            return self._run_on_body_meter(read_body, on_step)
        return self._run_on_lv(read, on_step)

    # ------------------------------------------------------------- body meter

    def _body_meter(self) -> Callable[[], float] | None:
        """The backend's EV meter readout, or None where it has none.

        Deliberately not ``hasattr(camera, 'exposure_ev')`` alone: the method
        must be callable *and* succeed once; a backend that raises
        NotImplementedError on every read falls back to the LV path instead
        of breaking the run.
        """
        fn = getattr(self.camera, "exposure_ev", None)
        if fn is None:
            return None
        try:
            fn()
        except NotImplementedError:
            return None
        except Exception:  # noqa: BLE001 - transient USB trouble: retry later,
            return None    # but LV metering can proceed right now

        def read() -> float:
            time.sleep(_METER_SETTLE_S)
            return float(fn())

        return read

    def _run_on_body_meter(self, body: Callable[[], float],
                           on_step) -> AutoExposureResult:
        start = self.camera.get_settings()
        ev = body()
        ladders = (list(self.camera.info.shutter_choices or [start.shutter]),
                   list(self.camera.info.iso_choices or [start.iso]))
        settings = start
        requested = self.target_ev - ev
        history: list[float] = []
        iterations = 0
        #: True when the loop ended because no ladder move exists that would
        #: step closer (the two neighbouring rungs straddle the target).
        straddled = False

        for _ in range(self.max_iterations):
            needed = self.target_ev - ev
            if abs(needed) <= _CONVERGENCE_EPS_EV:
                break
            shutter, iso = self._plan(needed, settings, ladders)
            if (shutter, iso) == (settings.shutter, settings.iso):
                straddled = True
                break                       # nothing left to give (see below)
            applied_iso = self.camera.set_iso(iso)
            applied_shutter = self.camera.set_shutter(shutter)
            settings = ExposureSettings(shutter=applied_shutter, iso=applied_iso)
            iterations += 1
            if on_step is not None:
                on_step(settings)
            ev = body()
            history.append(ev)
            if len(history) >= 2 and (history[-2] - self.target_ev) * (
                    history[-1] - self.target_ev) < 0:
                # Two consecutive re-reads on opposite sides of the target:
                # the next move would only cross again. Keep what we have.
                break

        residual = self.target_ev - ev
        limited_shutter = limited_iso = False
        note: str | None = None
        residual_note: str | None = None
        if abs(residual) > _CONVERGENCE_EPS_EV:
            slowest, fastest = max(ladders[0]), min(ladders[0])
            iso_lo, iso_hi = min(ladders[1]), max(ladders[1])
            dark = residual > 0
            # Blame only what is actually pinned at the rung the residual
            # needs. The previous version declared "ladder exhausted" from the
            # residual alone — that is the lie the operator saw as "už je na
            # nejmenším čase" while 1/8000 was sitting one rung away.
            direction = "tmavší, než cílí — přidej světlo" if dark \
                else "světlejší — ztmavi scénu"
            # Only `straddled` proves no closer rung exists. A residual left by
            # the iteration cap or a dither break is not a hardware limit, and
            # claiming it was is the lie the operator saw as "už je na
            # nejmenším čase" while 1/8000 sat one rung away.
            if straddled:
                limited_shutter = settings.shutter >= slowest - 1e-9 if dark \
                    else settings.shutter <= fastest + 1e-9
                limited_iso = (settings.iso >= iso_hi if dark
                               else settings.iso <= iso_lo)
                if limited_shutter or limited_iso:
                    extremes = []
                    if limited_shutter:
                        extremes.append(ExposureSettings(
                            shutter=slowest if dark else fastest).shutter_string())
                    if limited_iso:
                        extremes.append(f"ISO {iso_hi if dark else iso_lo}")
                    note = (f"ani {' a '.join(extremes)} je scéna o "
                            f"{abs(residual):.1f} EV {direction}")
                else:
                    # No actuator is pinned; neighbouring rungs straddle the
                    # target and no finer step exists. That is convergence as
                    # good as the hardware gets — report it as such.
                    residual_note = (f"zůstává {abs(residual):.2f} EV — "
                                     "jemnější krok tělo nemá")
                    residual = 0.0
            else:
                note = (f"AE neskonvergovalo — zůstává {abs(residual):.1f} EV "
                        f"({settings.shutter_string()} · ISO {settings.iso}); "
                        "zkus Auto Exposure znovu")
        if note is None and residual_note is not None:
            note = residual_note       # converged as good as the body can step
        return AutoExposureResult(
            settings=settings,
            achieved_ev_change=start.stops_between(settings),
            requested_ev_change=requested,
            reading=_body_ev_as_reading(ev, self.meter),
            iterations=iterations,
            converged=abs(residual) <= _CONVERGENCE_EPS_EV,
            limited_by_lens=limited_shutter,
            limited_by_iso=limited_iso,
            limit_note=note,
            body_ev=ev,
            history=history,
        )

    def _plan(self, needed_ev: float, settings: ExposureSettings,
              ladders: tuple[list[float], list[float]]
              ) -> tuple[float, int]:
        """Split a needed EV change into (shutter, iso), quality-first.

        ``needed_ev`` > 0 = scene darker than target = more exposure. The
        shutter and ISO ladders are coarse (whole stops / thirds) and coupled:
        the right answer is the *pair* that lands nearest the target, so the
        two rungs bracketing the ideal shutter are each tried with the ISO
        they imply (clamped to the ladder). Selection: land within
        convergence if any pair can; among those prefer the pair that keeps
        ISO low (ISO is noise, shutter is free — the archival rule, in both
        directions); if none lands, prefer the smallest miss.
        """
        shutter_ladder, iso_ladder = ladders
        iso_lo, iso_hi = min(iso_ladder), max(iso_ladder)
        ideal_shutter = settings.shutter * 2.0 ** needed_ev
        below = max((r for r in shutter_ladder if r <= ideal_shutter),
                    default=min(shutter_ladder))
        above = min((r for r in shutter_ladder if r >= ideal_shutter),
                    default=max(shutter_ladder))
        best = None
        for shutter in {below, above, settings.shutter}:
            shutter_done = math.log2(shutter / settings.shutter)
            ideal_iso = settings.iso * 2.0 ** (needed_ev - shutter_done)
            iso = _snap(min(max(ideal_iso, iso_lo), iso_hi), iso_ladder)
            iso = int(round(iso))
            achieved = shutter_done + math.log2(iso / settings.iso)
            err = abs(achieved - needed_ev)
            tier = 0 if err <= _CONVERGENCE_EPS_EV else 1
            key = (tier, math.log2(iso / settings.iso) if tier == 0 else 0.0,
                   err)
            if best is None or key < best[0]:
                best = (key, (shutter, iso))
        return best[1]

    # --------------------------------------------------------------- legacy path

    def _run_on_lv(self, read, on_step) -> AutoExposureResult:
        """Metering through Live View frames — for backends whose stream
        responds to the exposure settings (mock, gphoto2). Iterates because
        each LV metering step is one frame of empirical feedback."""
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


def _body_ev_as_reading(body_ev: float, meter: LiveMeter) -> MeterReading:
    """Present the body meter EV in the MeterReading shape the GUI prints.

    Mid-grey (the meter's reference) sits at 18% of a correctly exposed
    scale; the highlight rail estimate scales with the EV offset. Synthetic
    on purpose — it feeds status labels, not pixel maths, and says so by
    carrying exactly one sample's worth of certainty.
    """
    mid = 0.18 * 2.0 ** body_ev
    rail = min(1.0, mid * 4.0)      # ~2.3 EV above mid = highlight estimate
    return MeterReading(
        black_level=meter.black_level,
        white_level=meter.white_level,
        signal_min=mid,
        signal_median=mid,
        signal_p999=rail,
        signal_max=rail,
        clipped_fraction=0.0,
        near_black_fraction=0.0,
    )
