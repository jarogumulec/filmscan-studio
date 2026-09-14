"""Exposure arithmetic and the auto-exposure controller.

Two hard rules from the project brief live here:

1. Metering uses *linear sensor data only* -- never the inverted Working
   Positive preview. Film scanning is a transmission measurement, so the
   highlight side of the raw signal corresponds to the densest part of the
   negative, and clipping there is unrecoverable.
2. Auto-exposure targets a high percentile (99.9), not the brightest pixel, and
   keeps a safety margin of 0.3-0.5 EV below clipping so a single hot pixel or a
   dust speck cannot push real detail out of range.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

#: Required headroom below clipping, in stops. Brief specifies 0.3-0.5 EV.
DEFAULT_HEADROOM_EV = 0.4

#: Percentile of the linear signal used as the "brightest real value".
DEFAULT_METER_PERCENTILE = 99.9

#: Fraction of samples allowed to sit below black before black is considered
#: unreliable (a hint that the dark frame has drifted).
BLACK_FLOOR_SANITY_RATIO = 0.001

#: Full-scale value for the D750's 14-bit ADC.
D750_WHITE_LEVEL = 16383.0


@dataclass(frozen=True)
class ExposureSettings:
    """Camera exposure state.

    Values are kept in physical units and converted to the camera's string
    vocabulary at the boundary, so the rest of the application never has to
    parse strings like ``'0.0400 s'``.
    """

    shutter: float = 1.0
    iso: int = 100
    aperture: float | None = None

    def __post_init__(self) -> None:
        if self.shutter <= 0:
            raise ValueError("shutter must be a positive number of seconds")
        if self.iso <= 0:
            raise ValueError("iso must be positive")

    def with_shutter(self, shutter: float) -> ExposureSettings:
        return ExposureSettings(shutter, self.iso, self.aperture)

    def with_iso(self, iso: int) -> ExposureSettings:
        return ExposureSettings(self.shutter, iso, self.aperture)

    def with_aperture(self, aperture: float | None) -> ExposureSettings:
        return ExposureSettings(self.shutter, self.iso, aperture)

    def stops_between(self, other: ExposureSettings) -> float:
        """EV difference from this setting to ``other`` (positive = other is brighter)."""
        return math.log2(other.exposure_factor / self.exposure_factor)

    @property
    def exposure_factor(self) -> float:
        """Relative exposure, proportional to shutter/ISO and inversely to N^2."""
        base = self.shutter * (100.0 / self.iso)
        if self.aperture:
            base /= self.aperture**2
        return base

    def shutter_string(self) -> str:
        """Format for display and for the camera: '1/125' below a second."""
        if self.shutter >= 1:
            return f"{self.shutter:g}"
        return f"1/{round(1 / self.shutter)}"

    def __str__(self) -> str:
        ap = f" f/{self.aperture:g}" if self.aperture else ""
        return f"{self.shutter_string()}s ISO{self.iso}{ap}"


@dataclass(frozen=True)
class MeterReading:
    """Statistics of a linear capture, used for metering and clipping checks."""

    black_level: float
    white_level: float
    signal_min: float
    signal_median: float
    signal_p999: float
    signal_max: float
    clipped_fraction: float
    near_black_fraction: float

    @property
    def dynamic_range_stops(self) -> float:
        """Stops between the median floor and the 99.9th percentile."""
        floor = max(self.signal_p999, 1e-6)
        base = max(self.signal_median, 1e-6)
        return math.log2(floor / base)

    @property
    def highlight_utilisation(self) -> float:
        """Where the 99.9th percentile sits between black and clipping, 0..1."""
        span = self.white_level - self.black_level
        if span <= 0:
            return 0.0
        return (self.signal_p999 - self.black_level) / span

    @property
    def clipped(self) -> bool:
        return self.clipped_fraction > 0.0001

    @property
    def highlights_at_target(self) -> bool:
        return abs(self.highlight_utilisation - self._target_utilisation()) < 0.02

    def _target_utilisation(self) -> float:
        return 2.0 ** (-DEFAULT_HEADROOM_EV)


def measure(
    linear: np.ndarray,
    black_level: float,
    white_level: float,
    percentile: float = DEFAULT_METER_PERCENTILE,
) -> MeterReading:
    """Summarise a linear sensor array.

    ``linear`` is raw, demosaic-free sensor data (or a linear preview). No gamma
    and no inversion may have been applied -- see the module docstring.
    """
    data = np.asarray(linear, dtype=np.float64)
    if data.size == 0:
        raise ValueError("cannot meter an empty array")

    span = white_level - black_level
    if span <= 0:
        raise ValueError("white_level must exceed black_level")
    # One ten-thousandth of range: meaningful for raw DN (~1.6 DN) and for
    # normalised 0..1 data alike.
    near_black = black_level + span * 1e-4

    return MeterReading(
        black_level=black_level,
        white_level=white_level,
        signal_min=float(data.min()),
        signal_median=float(np.median(data)),
        signal_p999=float(np.percentile(data, percentile)),
        signal_max=float(data.max()),
        clipped_fraction=float(np.count_nonzero(data >= white_level) / data.size),
        near_black_fraction=float(np.count_nonzero(data <= near_black) / data.size),
    )


def signal_at_target(headroom_ev: float = DEFAULT_HEADROOM_EV,
                     black_level: float = 0.0,
                     white_level: float = D750_WHITE_LEVEL) -> float:
    """Raw DN that the 99.9th percentile should land on for a given headroom."""
    if headroom_ev < 0:
        raise ValueError("headroom must be non-negative")
    span = white_level - black_level
    return black_level + span * 2.0 ** (-headroom_ev)


def required_ev_change(
    reading: MeterReading,
    headroom_ev: float = DEFAULT_HEADROOM_EV,
    percentile: float = DEFAULT_METER_PERCENTILE,
) -> float:
    """Stops to add (positive) or remove (negative) so highlights land on target.

    Purely multiplicative in the linear domain, so it is valid for any ISO or
    shutter combination and independent of the film's density curve.
    """
    target = signal_at_target(headroom_ev, reading.black_level, reading.white_level)
    current = reading.signal_p999 - reading.black_level
    # Threshold is a fraction of the range, not an absolute DN: metering runs on
    # raw DN (span ~15800) and on normalised 0..1 data alike, and a "+1 DN" floor
    # would reject every normalised reading as "at black".
    floor = (reading.white_level - reading.black_level) * 1e-4
    if current <= floor:
        raise ValueError("signál je na černé; nelze měřit")
    return math.log2(target / current)


def choose_shutter(
    current: ExposureSettings,
    delta_ev: float,
    candidates: list[float],
    prefer_aperture: float | None = None,
) -> float:
    """Pick the closest available shutter speed that applies ``delta_ev``.

    Shutter is the only actuator the D750 exposes over USB (aperture is on the
    lens), so shutter alone absorbs the correction. If no candidate is within a
    stop of the ideal, the ideal is still clamped into the available range --
    better a partially corrected frame than a silently uncorrected one.
    """
    if not candidates:
        raise ValueError("no shutter candidates")
    ideal = current.shutter * 2.0**delta_ev
    ordered = sorted(candidates, key=lambda s: abs(math.log2(s / ideal)))
    best = ordered[0]
    if prefer_aperture is not None and abs(math.log2(best / ideal)) > 1.0:
        # Far from ideal: the operator should know, but we still return the best
        # available so the caller can report rather than guess.
        return best
    return best


def clamp_ev(ev: float, limits: tuple[float, float] = (-12.0, 20.0)) -> float:
    return max(limits[0], min(limits[1], ev))


def normalisation_exposure_factor(
    capture_exposure: ExposureSettings, reference_exposure: ExposureSettings
) -> float:
    """Factor to scale a calibration frame onto a scan's exposure.

    Assumes ``RAW signal ∝ exposure time`` at fixed illumination, which holds for
    the D750 in its linear range. Dark frames must be scaled by shutter *ratio*
    only -- ISO gain must not be folded in, because dark current does not scale
    with sensor gain the way photon signal does. Flat frames, being illuminated,
    do scale with the full exposure factor.
    """
    return capture_exposure.shutter / reference_exposure.shutter
