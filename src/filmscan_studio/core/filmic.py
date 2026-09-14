"""General filmic tone curve: toe / gamma / shoulder.

The first version deliberately ships *no* film-specific profiles. What is
implemented here is a parametric model in the spirit of the Hurter-Driffield
characteristic curve, exposing the same three controls as darktable's Filmic RGB
(shadow compression, midtone contrast, highlight compression) as toe / gamma /
shoulder.

The curve is a monotone cubic Hermite spline (Fritsch-Carlson) through four
anchors::

    (0, 0)  (0.25, 0.25 - 0.15*toe)  (0.75, 0.75 + 0.15*shoulder)  (1, 1)

which is the classic S shape of a Hurter-Driffield characteristic curve: slope
approaching zero at both ends, steepest through the middle.

* ``toe`` pulls the shadow anchor down, flattening the curve near black so
  shadow detail is squeezed into a narrow band -- compression, not clipping.
* ``shoulder`` pushes the highlight anchor up, flattening the curve near white so
  the top of the range compresses into a narrow band before reaching white.
* ``gamma`` is a contrast control applied around an anchored mid-grey.

Both controls therefore *reduce local contrast at their end*, which is what
"shadow compression" and "highlight compression" mean. An earlier revision moved
the anchors the other way, which expanded the highlight band instead of
compressing it -- visible in tests as a shoulder that increased the 0.9-to-0.98
output interval.

Why a spline and not the compact power-law form ``x ** exponent(x)`` that tone
curves are often written with: a level-dependent exponent is **not monotone**
once the exponent falls below 1, and a folding curve is a real defect, not a
cosmetic one -- it shows up as posterised bands and locally inverted contrast in
shadow detail, exactly where a film scan carries its most information. The
Fritsch-Carlson construction guarantees a non-decreasing curve for any parameter
combination, and the endpoints stay pinned at 0 and 1 by construction.

Parameters are dimensionless and defined on normalised scene-linear input, so a
profile saved as JSON is portable between the live preview and the developer
pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

_SHADOW_ANCHOR = 0.25
_HIGHLIGHT_ANCHOR = 0.75
#: How far each knee can move its anchor, in normalised units. Capped below the
#: anchor spacing so a full-strength knee thins the band without flattening it:
#: at travel 0.25 a full toe would make the curve exactly flat below 0.25, which
#: would discard all shadow detail rather than compress it.
_TOE_TRAVEL = 0.15
_SHOULDER_TRAVEL = 0.15


@dataclass(frozen=True)
class FilmicProfile:
    """Parametric tone curve.

    ``toe`` and ``shoulder`` are strengths in 0..1 where 0 means no adjustment.
    ``gamma`` is midtone contrast, where 1.0 is linear.
    """

    toe: float = 0.35
    gamma: float = 1.10
    shoulder: float = 0.40
    name: str = "generic"

    def __post_init__(self) -> None:
        for attr in ("toe", "shoulder"):
            v = getattr(self, attr)
            if not 0.0 <= v <= 1.0:
                raise ValueError(f"{attr} must be within 0..1, got {v}")
        if self.gamma <= 0:
            raise ValueError("gamma must be positive")

    @classmethod
    def neutral(cls) -> FilmicProfile:
        """Identity curve -- the A/B reference against RAW View."""
        return cls(toe=0.0, gamma=1.0, shoulder=0.0, name="neutral")

    def to_dict(self) -> dict[str, float | str]:
        return {"name": self.name, "toe": self.toe, "gamma": self.gamma, "shoulder": self.shoulder}

    @classmethod
    def from_dict(cls, d: dict) -> FilmicProfile:
        return cls(
            toe=float(d.get("toe", 0.35)),
            gamma=float(d.get("gamma", 1.10)),
            shoulder=float(d.get("shoulder", 0.40)),
            name=str(d.get("name", "custom")),
        )

    def knots(self) -> tuple[np.ndarray, np.ndarray]:
        """Curve control points before gamma: (x, y), always starting (0,0), (1,1).

        At full toe and full shoulder the two middle knots both land on 0.5, so
        the heights are non-decreasing over the whole parameter range and never
        need reordering -- the spline just goes flat across the middle.
        """
        xs = np.array([0.0, _SHADOW_ANCHOR, _HIGHLIGHT_ANCHOR, 1.0])
        ys = np.array(
            [
                0.0,
                _SHADOW_ANCHOR - _TOE_TRAVEL * self.toe,
                _HIGHLIGHT_ANCHOR + _SHOULDER_TRAVEL * self.shoulder,
                1.0,
            ]
        )
        return xs, ys

    def apply(self, linear: np.ndarray) -> np.ndarray:
        """Map normalised scene-linear data to display-referred output in 0..1."""
        x = np.clip(np.asarray(linear, dtype=np.float64), 0.0, 1.0)
        xs, ys = self.knots()
        t = _monotone_interp(xs, ys, x)
        return np.clip(self._midtone(t), 0.0, 1.0)

    def _midtone(self, t: np.ndarray) -> np.ndarray:
        """Midtone contrast: a piecewise power pinned at 0, 0.5 and 1.

        The slope at the pivot is exactly ``gamma``, which is what makes this a
        contrast control in the photographic sense rather than an exposure
        shift. Pivoting at a fixed 0.5 (rather than at wherever the knees put
        mid-grey) keeps the three controls independent: moving the toe does not
        silently retune the gamma.
        """
        if self.gamma == 1.0:
            return t
        lo = 0.5 * (np.minimum(t, 0.5) * 2.0) ** self.gamma
        hi = 1.0 - 0.5 * ((1.0 - np.maximum(t, 0.5)) * 2.0) ** self.gamma
        return np.where(t <= 0.5, lo, hi)

    def curve_table(self, n: int = 4096) -> np.ndarray:
        """Sampled 1-D LUT, for plotting and for fast preview."""
        return self.apply(np.linspace(0.0, 1.0, n))


def _monotone_interp(xs: np.ndarray, ys: np.ndarray, x: np.ndarray) -> np.ndarray:
    """Fritsch-Carlson monotone cubic Hermite interpolation.

    Tangents are limited so the interpolant cannot overshoot between knots, which
    is what makes the result monotone for arbitrary (sorted) knot heights. The
    standard algorithm: secant slopes, harmonic mean of adjacent secants at
    interior knots, then the Fritsch-Carlson clip that enforces monotonicity.
    """
    h = np.diff(xs)
    delta = np.diff(ys) / h
    m = np.empty_like(ys)
    m[0] = delta[0]
    m[-1] = delta[-1]
    # Harmonic mean; a zero secant on either side forces a zero tangent.
    for i in range(1, len(ys) - 1):
        if delta[i - 1] * delta[i] <= 0:
            m[i] = 0.0
        else:
            m[i] = (delta[i - 1] + delta[i]) / 2.0

    # Enforce the Fritsch-Carlson bound |m_i| <= 3 * min(|delta_{i-1}|, |delta_i|).
    for i in range(1, len(ys) - 1):
        limit = 3.0 * min(abs(delta[i - 1]), abs(delta[i]))
        m[i] = float(np.clip(m[i], -limit, limit))

    idx = np.clip(np.searchsorted(xs, x, side="right") - 1, 0, len(xs) - 2)
    x0 = xs[idx]
    seg = h[idx]
    t = (x - x0) / seg
    t2, t3 = t * t, t * t * t
    h00 = 2 * t3 - 3 * t2 + 1
    h10 = t3 - 2 * t2 + t
    h01 = -2 * t3 + 3 * t2
    h11 = t3 - t2
    return h00 * ys[idx] + h10 * seg * m[idx] + h01 * ys[idx + 1] + h11 * seg * m[idx + 1]


def exposure_to_linear(linear: np.ndarray, ev: float) -> np.ndarray:
    """Apply exposure compensation in stops to linear data."""
    return np.asarray(linear, dtype=np.float64) * 2.0**ev
