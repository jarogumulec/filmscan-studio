"""Histograms computed from linear sensor data.

The brief is explicit: the histogram must derive from linear data. A histogram
taken after the filmic curve is a picture of the curve, not of the exposure, and
would make the clipping indicators useless.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Histogram:
    """Linear-domain luminance histogram with clipping markers.

    ``counts`` indexes normalised linear signal, so equal-width bins mean equal
    exposure intervals -- one bin width per stop divided by ``bins``. That is what
    lets the UI draw stop-relative gridlines over the histogram.
    """

    counts: np.ndarray
    bins: int
    black_level: float
    white_level: float
    clipped_low: int
    clipped_high: int
    total: int

    @property
    def stops_per_bin(self) -> float:
        return 1.0 / self.bins

    def normalised(self) -> np.ndarray:
        """Counts scaled to 0..1 for drawing."""
        peak = self.counts.max(initial=0)
        if peak == 0:
            return np.zeros_like(self.counts, dtype=np.float64)
        return self.counts / peak

    @property
    def clipped_total(self) -> int:
        return self.clipped_low + self.clipped_high

    @property
    def clipped_fraction(self) -> float:
        return self.clipped_total / self.total if self.total else 0.0

    @property
    def clipping_warning(self) -> bool:
        """True when any pixel sits on the ADC rail.

        Deliberately strict. On film, clipping in the raw means lost density, and
        unlike a bright sky there is no way to recover it in the developer.
        """
        return self.clipped_high > 0


def compute(
    linear: np.ndarray,
    black_level: float,
    white_level: float,
    bins: int = 256,
    weights: np.ndarray | None = None,
) -> Histogram:
    """Histogram of linear sensor data, normalised so black=0 and clipping=1.

    Values are clipped into range for binning but counted separately as clipped
    so rail-hitting pixels remain visible rather than piling into the last bin
    and hiding the problem.
    """
    if weights is not None and weights.shape != np.shape(linear):
        raise ValueError("weights must match linear shape")

    data = np.asarray(linear, dtype=np.float64)
    if data.size == 0:
        raise ValueError("cannot histogram an empty array")

    span = white_level - black_level
    if span <= 0:
        raise ValueError("white_level must exceed black_level")

    norm = (data - black_level) / span
    clipped_low = int(np.count_nonzero(norm <= 0.0))
    clipped_high = int(np.count_nonzero(norm >= 1.0))

    in_range = np.clip(norm, 0.0, np.nextafter(1.0, 0.0))
    counts, _ = np.histogram(in_range, bins=bins, range=(0.0, 1.0), weights=weights)
    return Histogram(
        counts=counts.astype(np.float64),
        bins=bins,
        black_level=black_level,
        white_level=white_level,
        clipped_low=clipped_low,
        clipped_high=clipped_high,
        total=int(data.size),
    )
