"""Working Positive: turning a linear negative capture into something viewable.

This module implements the *preview* chain from the brief::

    linear raw -> dark/flat corrected -> normalised
               -> base subtraction -> invert -> exposure -> filmic

It is shared by the live preview and the developer pipeline so that what the
operator sees while framing is produced by exactly the same maths as the final
export. That shared code path is the point: it is what makes "the preview never
affects the stored raw" safe to guarantee rather than merely promised.

Orientation note
----------------
On a negative the film base is at the *top* of the sensor range: the clear base
passes the most light, so base + fog is the brightest thing the sensor sees, and
the scene's brightest highlights (the densest dye) are darkest. Base subtraction
and inversion are therefore a single operation, not two sequential ones::

    positive = (base - signal) / base

Subtracting a high base first and inverting afterwards would collapse the whole
tonal range, because all usable signal sits *below* the base.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from filmscan_studio.core.filmic import FilmicProfile, exposure_to_linear

DISPLAY_GAMMA = 2.2


@dataclass(frozen=True)
class PositiveParams:
    """Operator-facing preview parameters.

    ``base_level=None`` means auto-detect the film base from the frame.
    """

    exposure_ev: float = 0.0
    base_level: float | None = None
    profile: FilmicProfile = FilmicProfile()
    #: False for diapositiv, which is already positive and has no base to remove.
    invert: bool = True
    #: Percentile treated as base+fog when auto-detecting.
    base_percentile: float = 99.0

    def with_exposure(self, ev: float) -> PositiveParams:
        return replace(self, exposure_ev=ev)

    def with_profile(self, profile: FilmicProfile) -> PositiveParams:
        return replace(self, profile=profile)

    def with_base(self, base_level: float | None) -> PositiveParams:
        return replace(self, base_level=base_level)


def normalise(linear: np.ndarray, black_level: float, white_level: float) -> np.ndarray:
    """Subtract black and scale to 0..1. Input stays raw DN, output is linear."""
    span = white_level - black_level
    if span <= 0:
        raise ValueError("white_level must exceed black_level")
    return (np.asarray(linear, dtype=np.float64) - black_level) / span


def estimate_base(linear01: np.ndarray, percentile: float = 99.0) -> float:
    """Estimate base+fog from a normalised negative.

    A high percentile, not a low one: raw bright = clear film base. A frame that
    includes a strip of unexposed edge gives the most trustworthy value; the
    default percentile works when the whole frame is image.
    """
    return float(np.percentile(np.asarray(linear01, dtype=np.float64), percentile))


def subtract_base(linear01: np.ndarray, base: float) -> np.ndarray:
    """Remove film base and invert in one step.

    Maps base -> 0 and zero signal -> 1, so the usable range spans the full
    0..1 instead of the thin sliver below the base. Pixels above the base (fog,
    noise, dust on the clear side) clamp to 0, i.e. the deepest negative density.
    """
    if not 0.0 < base <= 1.0:
        raise ValueError(f"base must be within 0..1, got {base}")
    return np.clip((base - np.asarray(linear01, dtype=np.float64)) / base, 0.0, 1.0)


def invert(linear01: np.ndarray) -> np.ndarray:
    """Plain complement, for a positive original or for testing.

    The density-domain treatment belongs to the filmic stage; this only flips the
    transmission relationship.
    """
    return 1.0 - np.clip(np.asarray(linear01, dtype=np.float64), 0.0, 1.0)


def black_stretch(linear01: np.ndarray, black_percentile: float = 0.5) -> np.ndarray:
    """Slide/diapozitiv counterpart of :func:`subtract_base`.

    A positive original has no base to remove, but the film base still lifts its
    blacks, so the bottom of the range is pinned to zero instead.
    """
    x = np.asarray(linear01, dtype=np.float64)
    floor = float(np.percentile(x, black_percentile))
    span = 1.0 - floor
    if span <= 0:
        return np.clip(x, 0.0, 1.0)
    return np.clip((x - floor) / span, 0.0, 1.0)


def to_working_positive(
    linear: np.ndarray,
    black_level: float,
    white_level: float,
    params: PositiveParams | None = None,
) -> np.ndarray:
    """Full preview chain from raw DN to display-referred 0..1.

    The input is expected to be already dark/flat corrected; calibration belongs
    upstream so a preview and a final export cannot disagree about which
    calibration was applied.
    """
    params = params or PositiveParams()
    x = normalise(linear, black_level, white_level)

    if params.invert:
        base = (
            params.base_level
            if params.base_level is not None
            else estimate_base(x, params.base_percentile)
        )
        x = subtract_base(x, base)
    else:
        x = black_stretch(x)

    x = exposure_to_linear(x, params.exposure_ev)
    return params.profile.apply(x)


def to_raw_view(linear: np.ndarray, black_level: float, white_level: float) -> np.ndarray:
    """RAW View: display gamma only, no inversion and no tone curve.

    Used for exposure checking, where any tone curve would be a lie about the
    data. The gamma here is purely a display transform.
    """
    x = np.clip(normalise(linear, black_level, white_level), 0.0, 1.0)
    return x ** (1.0 / DISPLAY_GAMMA)
