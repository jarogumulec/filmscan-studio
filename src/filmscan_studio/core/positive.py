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
    #: Locked RGB white-balance gains (r, g, b), applied before base subtraction.
    #: None = no correction. These are measured once (Auto WB on a pressed AE
    #: rect or the frame's base region) and then *held* for the whole film:
    #: re-solving per frame would let the orange mask of a colour negative pull
    #: the neutral point around frame to frame, which reads as a breathing cast.
    wb_gains: tuple[float, float, float] | None = None

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


def apply_wb(rgb: np.ndarray, gains: tuple[float, float, float] | None) -> np.ndarray:
    """Scale the three channels of a float RGB image by locked WB gains."""
    if gains is None:
        return rgb
    g = np.asarray(gains, dtype=np.float64)
    return np.clip(np.asarray(rgb, dtype=np.float64) * g, None, None)


def estimate_wb_gains(rgb: np.ndarray, reference: str = "white") -> tuple[float, float, float]:
    """Grey-world white balance on the brightest percentile of a *negative*.

    ``reference="white"`` targets the film base — the brightest thing a
    negative frame carries (clear base + fog), so metering the top luminance
    percentile per channel and normalising to its mean answers "what does
    clear film look like under this light", which is exactly what a held WB
    must neutralise. Metering the whole negative would average the image's own
    colours into the neutral point; the caller is expected to pass a frame (or
    the inside of the AE rect) chosen for its film-base or white-border area.
    Gains are centred on 1.0 so the overall luminance does not jump on press.
    """
    x = np.clip(np.asarray(rgb, dtype=np.float64), 1e-6, 1.0)
    if x.ndim != 3 or x.shape[2] != 3:
        raise ValueError("WB se počítá z RGB obrazu")
    lum = x @ np.array([0.2126, 0.7152, 0.0722])
    threshold = float(np.percentile(lum, 98.0))
    mask = lum >= threshold
    if not mask.any():
        return (1.0, 1.0, 1.0)
    means = x[mask].mean(axis=0)
    if np.any(means <= 1e-6):
        return (1.0, 1.0, 1.0)
    # Centre on green: scaling all three by a common factor only moves overall
    # brightness, which is exposure's job, not WB's.
    gains = means[1] / means
    return (float(gains[0]), 1.0, float(gains[2]))


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
    if params.wb_gains is not None and x.ndim == 3:
        x = apply_wb(x, params.wb_gains)

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


#: LUT resolution of the fast live preview. 4096 levels of linear input are
#: finer than the 8-bit JPEG the Live View carries, so the table quantises
#: nothing the stream had.
_LIVE_LUT_SIZE = 4096

#: Rec. 709 — same luminance weights the metering path uses.
_LUMA = np.array([0.2126, 0.7152, 0.0722])


class FastPositivePreview:
    """Live-View Working Positive as one table lookup per pixel.

    The full :func:`to_working_positive` chain on every frame was the fps
    complaint (2026-09: "S-křivka je výpočetně složitá, zjednoduš"). The chain
    — WB gains, base subtract + invert, exposure, filmic spline — is three
    per-channel scalar maps and nothing else once the base level is fixed, so
    it is *precomputed* into per-channel LUTs; the spline then runs at
    parameter-change time, not per frame. The curve in the LUT is the same
    :class:`FilmicProfile` the developer exports with, so the fast path is a
    quantised version of the slow one, never a different look.

    The one genuinely per-frame quantity is the auto-detected base. Measuring
    it on every frame costs a percentile over the frame and makes the preview
    breathe; it is re-estimated every ``base_refresh`` frames (or when the
    parameters change) and held in between — which is what the operator wants
    anyway: a base that jitters frame to frame reads as flicker.
    """

    def __init__(
        self,
        params: PositiveParams,
        black_level: float = 0.0,
        white_level: float = 1.0,
        base_refresh: int = 12,
    ) -> None:
        self._params = params
        self._black = black_level
        self._white = white_level
        self._base_refresh = max(1, base_refresh)
        self._lut: np.ndarray | None = None      # (3, LUT_SIZE) float32
        self._base: float | None = None
        self._frames_since_measure = 0

    @property
    def base_level(self) -> float | None:
        return self._base

    def set_params(self, params: PositiveParams) -> None:
        if params != self._params:
            self._params = params
            self._lut = None                 # rebuild on the next render

    def reset(self) -> None:
        self._lut = None
        self._base = None
        self._frames_since_measure = 0

    def render(self, linear: np.ndarray) -> np.ndarray:
        """linear: float 0..1 normalised RGB (or grey) frame -> display 0..1."""
        x = np.clip(np.asarray(linear, dtype=np.float32), 0.0, 1.0)
        if not self._params.invert:
            # Slides/positives keep the exact path: black_stretch needs a
            # per-frame percentile anyway and is cheap.
            return to_working_positive(x.astype(np.float64), self._black,
                                       self._white, self._params).astype(np.float32)
        rgb = x if x.ndim == 3 else np.repeat(x[:, :, None], 3, axis=2)
        if self._lut is None or self._needs_base(rgb):
            self._measure_base(rgb)
            self._build_lut()
        assert self._lut is not None
        # Round, don't truncate: truncation costs a full LUT step of index
        # error (times the curve's local slope); rounding halves it.
        idx = np.rint(rgb * (_LIVE_LUT_SIZE - 1)).astype(np.uint16)
        # Per-channel gather; the float32 table keeps full depth — the LUT is
        # the whole per-pixel cost, one take() per channel.
        return np.stack([self._lut[c].take(idx[:, :, c]) for c in range(3)],
                        axis=2)

    # -------------------------------------------------------------- internals

    def _needs_base(self, rgb: np.ndarray) -> bool:
        if self._params.base_level is not None or self._lut is None:
            return self._lut is None or self._base is None
        self._frames_since_measure += 1
        return self._frames_since_measure >= self._base_refresh

    def _measure_base(self, rgb: np.ndarray) -> None:
        if self._params.base_level is not None:
            self._base = float(self._params.base_level)
            return
        # Subsample: every 4th column of a 640px stream is still 160 samples
        # per row; a percentile is a rank statistic and does not need every px.
        sub = rgb[:, ::4, :] if rgb.shape[1] >= 64 else rgb
        # Measure on WB-corrected luminance — the LUT subtracts this base from
        # WB-corrected pixels, and measuring the two in different domains is
        # how the base would drift off the neutral every time WB moved.
        if self._params.wb_gains is not None:
            gains = np.asarray(self._params.wb_gains, dtype=np.float32)
            sub = np.clip(sub * gains, 0.0, 1.0)
        lum = sub @ _LUMA.astype(np.float32)
        self._base = estimate_base(lum, self._params.base_percentile)
        self._frames_since_measure = 0

    def _build_lut(self) -> None:
        p = self._params
        base = self._base if self._base is not None and 0.0 < self._base <= 1.0 else 1.0
        xs = np.linspace(0.0, 1.0, _LIVE_LUT_SIZE, dtype=np.float64)
        gains = p.wb_gains or (1.0, 1.0, 1.0)
        lut = np.empty((3, _LIVE_LUT_SIZE), dtype=np.float32)
        for c, gain in enumerate(gains):
            v = np.clip(xs * float(gain), 0.0, 1.0)
            v = subtract_base(v, base)
            v = exposure_to_linear(v, p.exposure_ev)
            lut[c] = p.profile.apply(v).astype(np.float32)
        self._lut = lut
