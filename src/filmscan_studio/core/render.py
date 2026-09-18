"""Rendering layer: optical density map -> display-referred positive.

The second, interpretive half of the developer (design doc
``Documentation_image_processing/03_rendering_vrstva.md``). Everything here is
a *derived rendering*: it reads the density archive, it never writes to it,
and the same (archive, params) pair must always produce the same bytes —
hence the fingerprint.

The tonal scale works entirely in [D] (density units), which makes every
control physically readable ("the toe starts 0.3 D above base"):

    D_eff = D - dmin + 0.301 * exposure_ev     # net density, exposed (+ev brightens)
    x     = D_eff / span                       # span = dmax - dmin, 0..1
    out   = FilmicProfile.apply(x)             # monotone spline toe/gamma/shoulder

Inverting is *not* a step here, but it does set the sign: a negative's density
grows where the scene was bright (bright scene -> more exposure -> more
dye), so the positive's brightness GROWS WITH DENSITY -- ``out = y``, no
subtract-and-flip. An earlier revision rendered ``1 - y``, which is the
negative again (the print is bright where the negative is dense).

Pixels outside the measurable scale carry +/-inf density (too dense to
measure / clipped sensor) and clip to 1 / 0; only NaN -- no light ever
reached the pixel -- has no tone and stays NaN.

The curve itself is the existing :class:`~filmscan_studio.core.filmic.FilmicProfile`
(monotone Fritsch-Carlson spline), reused unchanged — the render layer changes
its *domain* (density, not inverted-linear 0..1), not the curve maths. A
negadoctor-style exponential "print" model is planned next (03 §3); the model
field exists so saved params stay forward-compatible.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace

import numpy as np

from filmscan_studio.core.filmic import FilmicProfile

#: Stops per natural density unit is a physical constant of the D scale:
#: one stop doubles/triples light, -log10(2) = 0.301 D separate 1-stop levels.
D_PER_STOP = np.log10(2.0)


@dataclass(frozen=True)
class RenderParams:
    """Everything that determines a positive render's pixels.

    ``dmin``/``dmax`` are densities in [D], *net of each other*: dmin is the
    measured base+fog level of the film, dmax the film's working density
    ceiling (per-frame or per-roll -- an open decision, 04 Q1; the source
    string records which so renders stay comparable).
    """

    #: Film base + fog density [D] (from a FilmBaseSample measurement; the
    #: fallback percentile estimate is a developer/project concern, not here).
    dmin: float = 0.20
    #: Working density ceiling [D]; span = dmax - dmin maps to 0..1.
    dmax: float = 2.60
    #: Exposure in stops; +1 stop = scene was brighter = *lower* film density,
    #: so the net-density axis shifts by -0.301·ev... but the rendered image
    #: should get brighter with +ev like every other exposure control, which
    #: the subtraction below implements.
    exposure_ev: float = 0.0
    profile: FilmicProfile = field(default_factory=FilmicProfile)
    #: "spline" now; "print" (negadoctor exponential) reserved -- saved param
    #: files must not break when it lands.
    curve_model: str = "spline"
    #: Provenance of dmax ("frame" | "film" | "manual"), informational only.
    dmax_source: str = "frame"

    def __post_init__(self) -> None:
        if self.dmax <= self.dmin:
            raise ValueError(
                f"dmax ({self.dmax}) must exceed dmin ({self.dmin})")
        if self.curve_model not in ("spline", "print"):
            raise ValueError(f"unknown curve_model {self.curve_model!r}")

    @property
    def span(self) -> float:
        return self.dmax - self.dmin

    def to_dict(self) -> dict[str, object]:
        return {
            "dmin": self.dmin,
            "dmax": self.dmax,
            "exposure_ev": self.exposure_ev,
            "curve_model": self.curve_model,
            "dmax_source": self.dmax_source,
            **self.profile.to_dict(),
        }

    def fingerprint(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:16]

    def with_exposure(self, ev: float) -> "RenderParams":
        return replace(self, exposure_ev=ev)

    def with_profile(self, profile: FilmicProfile) -> "RenderParams":
        return replace(self, profile=profile)

    @classmethod
    def from_dict(cls, d: dict) -> "RenderParams":
        return cls(
            dmin=float(d["dmin"]),
            dmax=float(d["dmax"]),
            exposure_ev=float(d.get("exposure_ev", 0.0)),
            profile=FilmicProfile.from_dict(d),
            curve_model=str(d.get("curve_model", "spline")),
            dmax_source=str(d.get("dmax_source", "frame")),
        )


def _positive_x(d: np.ndarray, params: RenderParams) -> np.ndarray:
    """Net-density axis of the positive: 0 = base (black print), 1 = dmax
    (white print). +/-inf densities (unmeasurably dense / clipped) survive as
    inf and clip to the ends below; NaN stays NaN."""
    d_eff = (np.asarray(d, dtype=np.float64) - params.dmin
             + D_PER_STOP * params.exposure_ev)
    return d_eff / params.span


def render_density(d: np.ndarray, params: RenderParams) -> np.ndarray:
    """Density map (float, NaN-invalid) -> display-referred float64 0..1.

    The positive is bright where the negative is dense (bright scene -> dense
    dye), so output grows with density. NaN in, NaN out: a pixel where no
    light ever reached must not acquire a tone. The GUI paints NaN as a
    visible mask; exports NaN-fill to black at quantisation time
    (:func:`quantise16`) — a silent black is the least inventive lie.
    """
    x = _positive_x(d, params)
    out = params.profile.apply(np.clip(x, 0.0, 1.0))
    out[np.isnan(np.asarray(d, dtype=np.float64))] = np.nan
    return out


def render_flat(d: np.ndarray, params: RenderParams) -> np.ndarray:
    """Tone-curve-free positive for external editors (03 §5).

    Same exposure and points, linear in net density -- the user's grading
    starts from an untouched response, not one with our S-curve baked in.
    """
    x = _positive_x(d, params)
    out = np.clip(x, 0.0, 1.0)
    out[np.isnan(np.asarray(d, dtype=np.float64))] = np.nan
    return out


def quantise16(image: np.ndarray, nan_fill: float = 0.0) -> np.ndarray:
    """0..1 float -> uint16 for the archival render TIFF; NaN -> ``nan_fill``."""
    filled = np.where(np.isfinite(image), image, nan_fill)
    return np.clip(filled * 65535.0 + 0.5, 0, 65535).astype(np.uint16)


def render_for_display(d: np.ndarray, params: RenderParams,
                       gamma: float = 2.2) -> np.ndarray:
    """sRGB-ish preview of a render (display gamma on top; NaN preserved)."""
    out = render_density(d, params)
    with np.errstate(invalid="ignore"):
        display = np.where(np.isfinite(out), np.power(np.maximum(out, 0.0),
                                                      1.0 / gamma), np.nan)
    return display
