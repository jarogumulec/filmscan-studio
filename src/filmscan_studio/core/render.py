"""Rendering layer: optical density map -> display-referred positive.

The second, interpretive half of the developer (design doc
``Documentation_image_processing/03_rendering_vrstva.md``). Everything here is
a *derived rendering*: it reads the density archive, it never writes to it,
and the same (archive, params) pair must always produce the same bytes —
hence the fingerprint.

The tonal scale works entirely in [D] (density units), which makes every
control physically readable ("the toe starts 0.3 D above base"):

    D_eff = D - dmin + shadow_band + 0.301 * exposure_ev  # exposed net density
    x     = D_eff / (span + shadow_band)       # base sits at band/(span+band)
    out   = FilmicProfile.apply(x)             # monotone spline toe/gamma/shoulder
    disp  = out ^ (1/gamma_display)            # apply_display: monitor transfer

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
    #: Display transfer applied AFTER the tone curve (03 §2 ``gamma_display``).
    #: The profile's own ``gamma`` is midtone contrast -- a creative slope at
    #: the pivot, near 1 for a natural look; this is the technical monitor
    #: transform (darktable's output profile, which negadoctor does not even
    #: carry). Preview and export must apply it identically or WYSIWYG dies.
    gamma_display: float = 2.2
    #: How far below dmin [D] still maps into the toe instead of clipping to
    #: black. A floor applied *after* the curve cannot unfuse detail already
    #: clipped at base -- every clipped pixel becomes the same value. This
    #: widens the curve's *domain* below base, so the fog/shadow gradient
    #: under the measured base renders as dark tones instead of one flat 0.
    #: 0 = hard clip at base (the old behaviour). Keep it small: the display
    #: gamma lifts small values hard, so 0.01 D already puts base at ~7 % of
    #: the print -- enough gradient for fog, not a milky black.
    shadow_band: float = 0.01
    #: Display-space brightness, added AFTER gamma in display units (−0.5..0.5).
    #: Deliberately *not* exposure: exposure_ev shifts the density axis (what
    #: the film saw); this is the Photoshop-curve feel knob on already
    #: displayed pixels. Contrast pivots at display mid-grey 0.5.
    brightness: float = 0.0
    contrast: float = 1.0

    def __post_init__(self) -> None:
        if self.dmax <= self.dmin:
            raise ValueError(
                f"dmax ({self.dmax}) must exceed dmin ({self.dmin})")
        if self.curve_model not in ("spline", "print"):
            raise ValueError(f"unknown curve_model {self.curve_model!r}")
        if self.gamma_display <= 0:
            raise ValueError(f"gamma_display must be positive, got {self.gamma_display}")
        if self.shadow_band < 0.0:
            raise ValueError(f"shadow_band must be >= 0, got {self.shadow_band}")
        if not -0.5 <= self.brightness <= 0.5:
            raise ValueError(f"brightness must be within -0.5..0.5, got {self.brightness}")
        if not 0.1 <= self.contrast <= 4.0:
            raise ValueError(f"contrast must be within 0.1..4.0, got {self.contrast}")

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
            "gamma_display": self.gamma_display,
            "shadow_band": self.shadow_band,
            "brightness": self.brightness,
            "contrast": self.contrast,
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
            # Fallbacky se rovnají výchozím polí -- archivy z dob před těmito
            # páčkami se nahrají se současnými defaulty, ne s nulami.
            gamma_display=float(d.get("gamma_display", 2.2)),
            shadow_band=float(d.get("shadow_band", 0.01)),
            brightness=float(d.get("brightness", 0.0)),
            contrast=float(d.get("contrast", 1.0)),
        )


def positive_x(d: np.ndarray, params: RenderParams) -> np.ndarray:
    """Net-density axis of the positive: 0 = base (black print), 1 = dmax
    (white print). ``shadow_band`` stretches the negative axis: base sits at
    ``band / total`` above 0, so densities below the measured base keep a
    gradient through the toe instead of clipping to one flat black value.
    +/-inf densities (unmeasurably dense / clipped) survive as inf and clip
    to the ends below; NaN stays NaN."""
    d_eff = (np.asarray(d, dtype=np.float64) - params.dmin
             + D_PER_STOP * params.exposure_ev + params.shadow_band)
    return d_eff / (params.span + params.shadow_band)


def render_density(d: np.ndarray, params: RenderParams) -> np.ndarray:
    """Density map (float, NaN-invalid) -> display-referred float64 0..1.

    The positive is bright where the negative is dense (bright scene -> dense
    dye), so output grows with density. NaN in, NaN out: a pixel where no
    light ever reached must not acquire a tone. The GUI paints NaN as a
    visible mask; exports NaN-fill to black at quantisation time
    (:func:`quantise16`) — a silent black is the least inventive lie.
    """
    x = positive_x(d, params)
    out = params.profile.apply(np.clip(x, 0.0, 1.0))
    out[np.isnan(np.asarray(d, dtype=np.float64))] = np.nan
    return out


def apply_display(out: np.ndarray, params: RenderParams) -> np.ndarray:
    """Display transfer, the last steps after the tone curve.

    ``display = clamp((out^(1/g) - 0.5)·contrast + 0.5 + brightness)`` —
    the gamma maps to the monitor's native response (darktable's output
    profile; negadoctor stops before this and leaves it to the pipeline),
    and brightness/contrast then work in *display* space like every
    viewer-side tool: contrast pivots at displayed mid-grey, brightness
    shifts it. (This is why they sit after the gamma, not before — a
    pre-gamma brightness lift would ramp through the density domain, which
    is exposure's job.) NaN passes through: a pixel with no light must not
    acquire a tone.

    There is deliberately *no* paper-black lift here: a floor added after
    the curve cannot unfuse detail already clipped at base — every clipped
    pixel would become the same value, just a brighter one. Shadow
    separation is what :attr:`RenderParams.shadow_band` does, before the
    curve, where the gradient still exists.
    """
    with np.errstate(invalid="ignore"):
        g = np.where(np.isfinite(out),
                     np.power(np.maximum(out, 0.0), 1.0 / params.gamma_display),
                     np.nan)
    if params.contrast != 1.0:
        g = (g - 0.5) * params.contrast + 0.5
    if params.brightness != 0.0:
        g = g + params.brightness
    if params.contrast != 1.0 or params.brightness != 0.0:
        g = np.where(np.isfinite(g), np.clip(g, 0.0, 1.0), np.nan)
    return g


def render_flat(d: np.ndarray, params: RenderParams) -> np.ndarray:
    """Tone-curve-free positive for external editors (03 §5).

    Same exposure and points, linear in net density -- the user's grading
    starts from an untouched response, not one with our S-curve baked in.
    """
    x = positive_x(d, params)
    out = np.clip(x, 0.0, 1.0)
    out[np.isnan(np.asarray(d, dtype=np.float64))] = np.nan
    return out


def quantise16(image: np.ndarray, nan_fill: float = 0.0) -> np.ndarray:
    """0..1 float -> uint16 for the archival render TIFF; NaN -> ``nan_fill``."""
    filled = np.where(np.isfinite(image), image, nan_fill)
    return np.clip(filled * 65535.0 + 0.5, 0, 65535).astype(np.uint16)


def render_for_display(d: np.ndarray, params: RenderParams) -> np.ndarray:
    """Full on-screen render: density -> curve -> display transfer (NaN kept).

    This is what the preview shows and what export must match byte-for-byte:
    ``render_density`` followed by :func:`apply_display` with the params' own
    ``gamma_display``.
    """
    return apply_display(render_density(d, params), params)
