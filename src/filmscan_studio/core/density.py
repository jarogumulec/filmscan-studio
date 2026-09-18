"""Measurement layer: calibrated DN -> transmittance -> optical density.

This is the physics half of the developer (design doc
``Documentation_image_processing/01_fyzikalni_model.md``). The measurement layer
is deliberately free of aesthetics: its output is the film's optical density
map, and every pleasing-positive transform is a *derived rendering* of it
(:mod:`filmscan_studio.core.render`).

The chain, per pixel, all in float64:

    S'' = (scan - dark·ratio) / gain_map      (calibration.py, shared)
    F'' = flat signal rescaled onto the scan's shutter   (DN of light with no film)
    T   = S'' / F''                            (transmittance, absolute 0..1)
    D   = -log10(T)                            (optical density, [D])

Two choices set this apart from the display pipeline in ``positive.py``:

* **The denominator is the per-pixel flat, not the sensor's white level.**
  T = 1 means "all the no-film light of *this pixel* got through", which is the
  physical statement transmittance is supposed to be. Dividing by
  ``white - black`` instead would silently bake the lamp's working level into
  every density value.
* **Unmeasurable pixels keep their direction.** Where the film is denser
  than the dark residual (T <= 0) the density is +inf; where the raw
  saturated (film thinner than measurable) it is -inf -- both bounds of a
  scale, not unknowns. Where no light ever reached (outside the illuminated
  area) no density exists at all and the pixel is NaN. Clipping these to a
  number would put measurements in the archive that were never made;
  collapsing the infinities to NaN would throw away which end of the scale
  they belong to.

The archive format (``write_density_tiff``) is a float32 TIFF carrying its own
provenance JSON in the ImageDescription tag -- the same self-describing pattern
as :mod:`filmscan_studio.core.rawio`, with a different magic so the two file
families can never be confused for each other. Dmin is *recorded* there but
never subtracted from the pixels: re-measuring the base must not require
re-scanning, only re-rendering.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import tifffile

from filmscan_studio.core.filmbase import FilmBaseSample, region_mean

log = logging.getLogger(__name__)

#: Fraction of the illuminated flat level below which a pixel is "no light
#: reached me" -- the same physical region the flat correction floors at 0.05,
#: stated here in transmittance terms for the mask.
ILLUMINATED_FRACTION = 0.30

#: Magic tag of the density archive's embedded JSON, distinct from rawio's
#: "filmscan" so a density file is never opened as a raw frame or vice versa.
DENSITY_MAGIC = "filmscan-density"

DENSITY_SCHEMA_VERSION = 1


# --------------------------------------------------------------- measurement


def transmittance(
    scan_above_black: np.ndarray,
    flat_above_black: np.ndarray,
) -> np.ndarray:
    """T = scan / flat, both above-black, float64, per pixel.

    ``flat_above_black`` must already be rescaled to the *scan's* exposure
    (shutter and gain): transmittance is a ratio of two photon signals and is
    exposure-invariant only when they were integrated over the same time.
    Division is unguarded here -- invalid pixels are classified by
    :func:`valid_mask`, which is where the NaN policy lives, so this stays a
    plain formula.
    """
    scan = np.asarray(scan_above_black, dtype=np.float64)
    flat = np.asarray(flat_above_black, dtype=np.float64)
    if scan.shape != flat.shape:
        raise ValueError(
            f"scan/flat shape mismatch: {scan.shape} vs {flat.shape}"
        )
    with np.errstate(divide="ignore", invalid="ignore"):
        return scan / flat


def illuminated_mask(
    t: np.ndarray,
    flat_above_black: np.ndarray,
    illuminated_fraction: float = ILLUMINATED_FRACTION,
) -> np.ndarray:
    """True where light actually reached the pixel and T is a finite ratio.

    Outside the illuminated area the flat itself carries no light (frame
    borders, holder mask); its transmittance is noise divided by noise.
    "Illuminated" is a robust high percentile of the flat, not its mean: the
    mean of a frame whose borders are black would drag the threshold under
    real structure.
    """
    t = np.asarray(t)
    flat = np.asarray(flat_above_black, dtype=np.float64)
    flat_ceiling = float(np.percentile(flat, 99.0))
    mask = np.isfinite(t)
    mask &= flat > illuminated_fraction * flat_ceiling
    return mask


def valid_mask(
    t: np.ndarray,
    flat_above_black: np.ndarray,
    raw_scan: np.ndarray | None = None,
    white_level: float = 65535.0,
    illuminated_fraction: float = ILLUMINATED_FRACTION,
) -> np.ndarray:
    """True where the transmittance reading is a measurement, not an artefact.

    Three failure classes become invalid:

    * ``T <= 0`` -- the dark-subtracted scan is at or below the dark residual;
      the film is denser than anything measurable at this exposure (on B1 frame
      003 that is 12 % of the image, at D >= 3).
    * outside the illuminated area -- see :func:`illuminated_mask`.
    * raw saturation -- a clipped pixel only bounds T from above; recording a
      density for it would archive a measurement that was never made.
    """
    t = np.asarray(t)
    mask = illuminated_mask(t, flat_above_black, illuminated_fraction)
    mask &= t > 0.0
    if raw_scan is not None:
        mask &= np.asarray(raw_scan) < white_level
    return mask


def density(
    t: np.ndarray,
    mask: np.ndarray,
    clipped: np.ndarray | None = None,
) -> np.ndarray:
    """D = -log10(T) as float32; pixels outside ``mask`` become NaN.

    float32 carries relative precision ~1e-7 everywhere, which is orders below
    the scanner noise floor -- the point of the format is not precision but
    NaN (see module docstring) and letting D below the film base exist (fog
    patches, lamp drift) instead of clipping it away (01 §2.5).

    ``mask`` is the *illuminated* mask (light reached the pixel). Within it,
    two out-of-range classes keep their direction as an infinity instead of
    collapsing to NaN -- the renderer can then clip them to the correct end
    of the positive instead of painting them as unknown:

    * ``T <= 0`` -- darker than the dark residual allows: D = +inf.
    * ``clipped`` -- raw saturation: the *film* let more light through than
      the sensor could hold, D = -inf (below any measurable base).
    """
    t64 = np.asarray(t, dtype=np.float64)
    out = np.full(np.asarray(t).shape, np.nan, dtype=np.float32)
    m = np.asarray(mask, dtype=bool)
    measurable = m & (t64 > 0.0) & np.isfinite(t64)
    with np.errstate(divide="ignore", invalid="ignore"):
        d = -np.log10(t64)
    out[measurable] = d[measurable].astype(np.float32)
    out[m & ~np.isfinite(d) & ~measurable] = np.inf
    if clipped is not None:
        out[np.asarray(clipped, dtype=bool) & m] = -np.inf
    return out


def dmin_from_signals(
    base_mean_dn: float,
    base_black_level: float,
    base_shutter: float,
    base_gain: float | None,
    flat_mean_dn: float,
    flat_black_level: float,
    flat_shutter: float,
    flat_gain: float | None = None,
) -> float:
    """Dmin [D] from region means of a base frame and the flat over the same rect.

    Each mean is reduced to its above-black photon signal, the base is scaled
    onto the flat's exposure (photon signal scales with shutter and gain, the
    pedestal never does -- the :class:`FilmBaseSample` rule), and the ratio is
    the base's transmittance: ``Dmin = -log10 T_base``.
    """
    if base_shutter <= 0 or flat_shutter <= 0:
        raise ValueError("shutter times must be positive")
    base_sig = base_mean_dn - base_black_level
    flat_sig = flat_mean_dn - flat_black_level
    if base_sig <= 0 or flat_sig <= 0:
        raise ValueError("base/flat means must be above black")
    factor = flat_shutter / base_shutter
    if flat_gain is not None and base_gain:
        factor *= flat_gain / base_gain
    t_base = base_sig * factor / flat_sig
    if not 0.0 < t_base <= 1.5:
        raise ValueError(f"implausible base transmittance {t_base:.4f}")
    return float(-np.log10(t_base))


def measure_dmin(
    sample: FilmBaseSample,
    flat_above_black: np.ndarray,
    flat_shutter: float,
    flat_gain: float | None = None,
) -> float:
    """Dmin from a stored :class:`FilmBaseSample` and the flat signal array.

    The sample's rect is measured on the flat to get the T = 1 reference at
    exactly the place the base was measured; the sample scales its own signal
    onto the flat's exposure. Both signals stay *raw* (un-flattened): the
    vignetting under the small base rect divides out of the ratio, and mixing
    a flat-corrected numerator with a raw denominator was a measured 0.08 D
    error. Validated on B1 (2026-09-18): five base readings across 17 minutes
    gave Dmin = 0.427..0.432 (±0.005 D).
    """
    flat_level = region_mean(np.asarray(flat_above_black, dtype=np.float64),
                             sample.rect)
    return dmin_from_signals(
        base_mean_dn=sample.mean_dn,
        base_black_level=sample.black_level,
        base_shutter=sample.shutter,
        base_gain=sample.gain,
        # The flat array is already above black -- its pedestal is zero here.
        flat_mean_dn=flat_level,
        flat_black_level=0.0,
        flat_shutter=flat_shutter,
        flat_gain=flat_gain,
    )


def estimate_dmax(
    d: np.ndarray, percentile: float = 99.9, margin: float = 0.05
) -> float:
    """Dmax of a frame as a high percentile of its valid densities, + margin.

    A percentile, not the max: one noisy or scratched pixel must not stretch
    the film's working range for the whole render (01 §5, frame003's p99.9
    already hits the noise floor). The margin keeps the top of the real range
    inside the render's scale instead of exactly on its edge.

    ``per_frame vs per-film`` strategy is an open decision (04 Q1); this
    function is the per-frame side of it. Returns 0.05 for an empty/all-NaN map
    so a degenerate frame cannot divide by zero downstream. Infinities (the
    too-dense class) are excluded: a scale point must be a number.
    """
    valid = d[np.isfinite(d)]
    if valid.size == 0:
        return margin
    return float(np.percentile(valid, percentile)) + margin


# ----------------------------------------------------------------- archive


@dataclass(frozen=True)
class DensityProvenance:
    """What went into a density archive; serialized into its header.

    Filenames only, never pixel data -- enough to rebuild the same map from the
    same raw inputs, and enough to *notice* when the answer changed (a
    different dark set, a re-measured base) by diffing two sidecars.
    """

    source: str
    shutter: float
    gain: float | None
    black_level: float
    white_level: float
    sensor_temperature_c: float | None = None
    #: Files behind the dark/flat masters (median stacks are named by their
    #: members; the master itself is reproducible from them).
    dark_files: tuple[str, ...] = ()
    flat_files: tuple[str, ...] = ()
    dark_shutter: float | None = None
    flat_shutter: float | None = None
    #: The film-base measurement used as Dmin metadata (recorded, not applied).
    dmin_density: float | None = None
    dmin_source: str | None = None
    #: Pixel rect the map was cropped to, if any (x0, y0, x1, y1) in full-frame
    #: sensor pixels; the archive stores only this region (design 04 + user
    #: instruction: frame borders belong to the holder, not the film).
    crop_rect: tuple[int, int, int, int] | None = None
    #: Fraction of valid pixels after masking -- low values flag a suspect
    #: frame ("no film?" / wrong calibration) right in the header.
    valid_fraction: float = 1.0
    pipeline_version: int = DENSITY_SCHEMA_VERSION

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "magic": DENSITY_MAGIC,
            "schema_version": self.pipeline_version,
            "source": self.source,
            "shutter": self.shutter,
            "gain": self.gain,
            "black_level": self.black_level,
            "white_level": self.white_level,
            "sensor_temperature_c": self.sensor_temperature_c,
            "dark_files": list(self.dark_files),
            "flat_files": list(self.flat_files),
            "dark_shutter": self.dark_shutter,
            "flat_shutter": self.flat_shutter,
            "dmin_density": self.dmin_density,
            "dmin_source": self.dmin_source,
            "crop_rect": list(self.crop_rect) if self.crop_rect else None,
            "valid_fraction": self.valid_fraction,
        }


def write_density_tiff(
    path: str | Path,
    d: np.ndarray,
    provenance: DensityProvenance,
    processed_at: datetime | None = None,
) -> Path:
    """Write the float32 density archive with its embedded provenance JSON."""
    a = np.asarray(d, dtype=np.float32)
    if a.ndim != 2:
        raise ValueError(f"density map must be 2-D, got shape {a.shape}")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = provenance.to_json_dict()
    payload["processed_at"] = (
        processed_at or datetime.now().astimezone()).isoformat()
    tifffile.imwrite(
        p,
        np.ascontiguousarray(a),
        photometric="minisblack",
        description=json.dumps(payload, ensure_ascii=False),
        datetime=(processed_at or datetime.now().astimezone()),
    )
    return p


def read_density_tiff(path: str | Path) -> tuple[np.ndarray, dict[str, Any]]:
    """Read a density archive back: (float32 map with NaN, provenance dict)."""
    p = Path(path)
    with tifffile.TiffFile(p) as tf:
        page = tf.pages[0]
        raw = page.description
    meta = json.loads(raw) if raw else {}
    if meta.get("magic") != DENSITY_MAGIC:
        raise ValueError(f"{p.name} is not a filmscan density archive")
    data = tifffile.imread(p).astype(np.float32)
    return data, meta


def density_stats(d: np.ndarray) -> dict[str, float]:
    """Valid-only summary for display and sidecars: median/p1/p99 D, valid %.

    Percentiles of a density map are its sensitometric profile — the operator
    reads "median D 1.7, p99 2.5" the way a darkroom reader reads a strip.
    """
    valid = d[np.isfinite(d)]
    if valid.size == 0:
        return {"valid_fraction": 0.0, "d_min": float("nan"),
                "d_p50": float("nan"), "d_p99": float("nan"),
                "d_max": float("nan")}
    return {
        "valid_fraction": float(valid.size / d.size),
        "d_min": float(valid.min()),
        "d_p50": float(np.percentile(valid, 50.0)),
        "d_p99": float(np.percentile(valid, 99.0)),
        "d_max": float(valid.max()),
    }
