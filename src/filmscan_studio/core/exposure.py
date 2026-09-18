"""Exposure arithmetic and metering helpers.

Two hard rules from the project brief live here:

1. Metering uses *linear sensor data only* -- never the inverted Working
   Positive preview. Film scanning is a transmission measurement, so the
   highlight side of the raw signal corresponds to the densest part of the
   negative, and clipping there is unrecoverable.
2. Auto-exposure targets a high percentile (99.9), not the brightest pixel, and
   keeps a safety margin of 0.3-0.5 EV below clipping so a single hot pixel or a
   dust speck cannot push real detail out of range.

The acquisition camera (Touptek TS2600MP-G2) has no ISO ladder: sensitivity is
a continuous analog ``gain`` (linear multiplier, 1.0 = 1x). ``iso`` remains on
:data:`ExposureSettings` as ``None``-able for backends that report one; the
archival rule is identical -- gain stays at the noise floor and exposure lives
in the shutter.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

import numpy as np

#: Required headroom below clipping, in stops. Brief specifies 0.3-0.5 EV.
DEFAULT_HEADROOM_EV = 0.4

#: The archival scan's fixed sensitivity (2026-09 brief): the camera's noise
#: floor. Scanning is a transmission measurement — exposure lives in the
#: shutter, gain stays pinned so every frame of a film is the same measurement.
#: On the IMX571 at HCG this is gain = 1.0 (the SDK's 1000 permille).
ARCHIVE_GAIN = 1.0

#: Percentile of the linear signal used as the "brightest real value".
DEFAULT_METER_PERCENTILE = 99.9

#: Above this clipped-pixel fraction the rails scream — shared by the live
#: meter (:meth:`MeterReading.clipped` / :attr:`MeterReading.crushed`) and the
#: post-capture audit. Tightened 2026-09-18 from 1e-4 to 5e-6 (one two-
#: hundred-thousandth) at the operator's request: on the full 6224×4168 frame
#: the verdict fires from ~130 clipped px, not ~2 600.
CLIP_TOLERANCE_FRACTION = 0.000005

#: Fraction of samples allowed to sit below black before black is considered
#: unreliable (a hint that the dark frame has drifted).
BLACK_FLOOR_SANITY_RATIO = 0.001

#: Full-scale value for the Touptek's native 16-bit ADC.
WHITE_LEVEL_16BIT = 65535.0


@dataclass(frozen=True)
class ExposureSettings:
    """Camera exposure state.

    Values are kept in physical units and converted to the camera's string
    vocabulary at the boundary, so the rest of the application never has to
    parse strings like ``'0.0400 s'``.
    """

    shutter: float = 1.0
    #: Legacy body sensitivity ladder value; ``None`` on cameras without one.
    iso: int | None = 100
    #: Analog gain as a linear multiplier (1.0 = 1x). The Touptek's only
    #: sensitivity control; ``None`` on bodies that speak ISO instead.
    gain: float | None = None
    #: No aperture: the rig's aperture is fixed on the lens and the acquisition
    #: camera cannot drive one, so it never entered the exposure math anyway.

    def __post_init__(self) -> None:
        # ``<=`` alone lets NaN through (every comparison with NaN is False),
        # and a NaN shutter renders as the string "1/nan" — reject at the gate.
        if not self.shutter > 0:
            raise ValueError("shutter must be a positive number of seconds")
        if self.iso is not None and self.iso <= 0:
            raise ValueError("iso must be positive")
        if self.gain is not None and self.gain <= 0:
            raise ValueError("gain must be a positive multiplier")

    def with_shutter(self, shutter: float) -> ExposureSettings:
        return ExposureSettings(shutter, self.iso, self.gain)

    def with_iso(self, iso: int) -> ExposureSettings:
        return ExposureSettings(self.shutter, iso, self.gain)

    def with_gain(self, gain: float) -> ExposureSettings:
        return ExposureSettings(self.shutter, self.iso, gain)

    def stops_between(self, other: ExposureSettings) -> float:
        """EV difference from this setting to ``other`` (positive = other is brighter)."""
        return math.log2(other.exposure_factor / self.exposure_factor)

    @property
    def exposure_factor(self) -> float:
        """Relative exposure, proportional to shutter/ISO (and inverse gain)."""
        base = self.shutter * (100.0 / self.iso if self.iso is not None else 1.0)
        if self.gain is not None:
            base /= self.gain
        return base

    def shutter_string(self) -> str:
        """Format for display and for the camera: '1/125' below a second."""
        if self.shutter >= 1:
            return f"{self.shutter:g}"
        return f"1/{round(1 / self.shutter)}"

    def sensitivity_string(self) -> str:
        """How the camera's sensitivity is labelled: ISO or gain."""
        if self.gain is not None:
            return f"gain {self.gain:.2f}x"
        return f"ISO {self.iso}" if self.iso is not None else "citlivost —"

    def __str__(self) -> str:
        return f"{self.shutter_string()}s {self.sensitivity_string()}"


#: '1/60', '2.5', '0.0400 s' -> seconds. None for non-numeric speeds ('Bulb').
_SHUTTER_RE = re.compile(r"^\s*(?:(\d+)\s*/\s*(\d+)|(\d+(?:\.\d+)?))")


def parse_shutter(value: str) -> float | None:
    """Parse a shutter speed written by a human or a camera into seconds."""
    m = _SHUTTER_RE.match(value)
    if not m:
        return None
    num, den, whole = m.groups()
    if num is not None:
        return int(num) / int(den) if int(den) else None
    return float(whole)


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
    #: Fraction of samples *near the black rail* (see ``measure``), reported as
    #: its own number so the GUI's black-clip figure matches the histogram
    #: widget instead of being a looser "nearly black" count.
    black_fraction: float = 0.0

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
        return self.clipped_fraction > CLIP_TOLERANCE_FRACTION

    @property
    def crushed(self) -> bool:
        """Mirror of :attr:`clipped` on the black rail (underexposure).

        Same tolerance: dust and a hot/cold pixel here as there; real density
        crushed to zero is far more common than a tenth of a percent.
        """
        return self.black_fraction > CLIP_TOLERANCE_FRACTION

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

    ``linear`` is raw sensor data (or a linear preview). No gamma and no
    inversion may have been applied -- see the module docstring.
    """
    data = np.asarray(linear, dtype=np.float64)
    if data.size == 0:
        raise ValueError("cannot meter an empty array")

    span = white_level - black_level
    if span <= 0:
        raise ValueError("white_level must exceed black_level")
    # One ten-thousandth of range: meaningful for raw DN (span 65535 -> ~6.6 DN)
    # and for normalised 0..1 data alike.
    near_black = black_level + span * 1e-4

    span_norm = (data - black_level) / span
    return MeterReading(
        black_level=black_level,
        white_level=white_level,
        signal_min=float(data.min()),
        signal_median=float(np.median(data)),
        signal_p999=float(np.percentile(data, percentile)),
        signal_max=float(data.max()),
        clipped_fraction=float(np.count_nonzero(data >= white_level) / data.size),
        near_black_fraction=float(np.count_nonzero(data <= near_black) / data.size),
        # Exactly the histogram widget's black rail: normalised <= 0.
        black_fraction=float(np.count_nonzero(span_norm <= 0.0) / data.size),
    )


def signal_at_target(headroom_ev: float = DEFAULT_HEADROOM_EV,
                     black_level: float = 0.0,
                     white_level: float = WHITE_LEVEL_16BIT) -> float:
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

    Purely multiplicative in the linear domain, so it is valid for any
    shutter/gain combination and independent of the film's density curve.
    """
    target = signal_at_target(headroom_ev, reading.black_level, reading.white_level)
    current = reading.signal_p999 - reading.black_level
    # Threshold is a fraction of the range, not an absolute DN: metering runs on
    # raw DN (span ~65535) and on normalised 0..1 data alike, and a "+1 DN"
    # floor would reject every normalised reading as "at black".
    floor = (reading.white_level - reading.black_level) * 1e-4
    if current <= floor:
        raise ValueError("signál je na černé; nelze měřit")
    return math.log2(target / current)


def choose_shutter(
    current: ExposureSettings,
    delta_ev: float,
    candidates: list[float],
) -> float:
    """Pick the closest available shutter speed that applies ``delta_ev``.

    Shutter is the archival actuator, so shutter alone absorbs the correction.
    If no candidate is within a stop of the ideal, the ideal is still clamped
    into the available range -- better a partially corrected frame than a
    silently uncorrected one.
    """
    if not candidates:
        raise ValueError("no shutter candidates")
    ideal = current.shutter * 2.0**delta_ev
    ordered = sorted(candidates, key=lambda s: abs(math.log2(s / ideal)))
    return ordered[0]


def choose_gain(
    current: ExposureSettings,
    delta_ev: float,
    gain_range: tuple[float, float],
) -> float:
    """Gain multiplier that applies ``delta_ev``, clamped to the camera's range.

    Only consulted once the shutter has run out of practical range (the
    archival rule keeps gain at the noise floor otherwise); gain multiplies the
    signal linearly, so the ideal is ``current * 2**delta``.
    """
    low, high = gain_range
    ideal = (current.gain or 1.0) * 2.0**delta_ev
    return max(low, min(high, ideal))


def clamp_ev(ev: float, limits: tuple[float, float] = (-12.0, 20.0)) -> float:
    return max(limits[0], min(limits[1], ev))


def normalisation_exposure_factor(
    capture_exposure: ExposureSettings, reference_exposure: ExposureSettings
) -> float:
    """Factor to scale a calibration frame onto a scan's exposure.

    Assumes ``RAW signal ∝ exposure time`` at fixed illumination, which holds
    for the sensor in its linear range. Dark frames must be scaled by shutter
    *ratio* only -- gain must not be folded in, because dark current does not
    scale with sensor gain the way photon signal does. Flat frames, being
    illuminated, do scale with the full exposure factor.
    """
    return capture_exposure.shutter / reference_exposure.shutter
