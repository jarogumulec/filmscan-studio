"""Dark-frame and flat-field correction, with exposure-time normalisation.

Both calibration types are ordinary raw frames. What makes them calibration is
only how they are combined, and the brief requires that combination to account
for exposure time rather than demanding identical settings:

    RAW signal ∝ exposure time

so a dark or flat shot at a different shutter speed is normalised by the shutter
ratio before use.

Two subtleties that matter for archival correctness:

* **Dark frames scale with shutter time only, not ISO.** Dark current is a
  photon-independent accumulation in the silicon; electronic gain is applied
  after it. Scaling a dark by the full exposure factor (shutter and ISO) would
  over-subtract when the dark was shot at a different ISO.
* **Flat frames scale with the full exposure factor**, because they are
  illuminated and therefore do follow photon statistics.

Flat fields are smoothed before use so that genuine image content in the flat
(not dust or vignetting) does not get divided out of the scan.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

#: Default Gaussian sigma for flat-field low-pass filtering, in pixels.
FLAT_SMOOTH_SIGMA = 64.0


@dataclass(frozen=True)
class CalibrationStack:
    """Averaged calibration frames in raw DN, plus the exposure they were shot at."""

    shutter: float
    #: Recorded provenance only — dark current does not scale with ISO or
    #: electronic gain, so calibration maths never reads it.
    iso: int | None = None
    #: Mean of the contributing frames, in raw DN, same mosaic geometry as scans.
    data: np.ndarray | None = None
    #: Per-pixel standard deviation, kept as a quality signal for the operator.
    noise: np.ndarray | None = None
    frame_count: int = 0
    #: Sensor pedestal present in ``data``, which must not be shutter-rescaled.
    black_level: float = 0.0

    def __post_init__(self) -> None:
        if self.shutter <= 0:
            raise ValueError("shutter must be positive")

    @property
    def ready(self) -> bool:
        return self.data is not None and self.frame_count > 0


def stack(frames: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """Median-of-frames stack.

    A median rather than a mean, so a cosmic-ray hit or a stray hot frame in a
    short stack cannot poison the master. Returns the median and the per-pixel
    spread as a quality indicator.
    """
    if not frames:
        raise ValueError("cannot stack an empty list")
    if len(frames) == 1:
        single = frames[0].astype(np.float64)
        return single, np.zeros_like(single)
    cube = np.stack([f.astype(np.float64) for f in frames], axis=0)
    median = np.median(cube, axis=0)
    spread = np.percentile(cube, 84.1, axis=0) - median
    return median, spread


def rescale_dark(
    dark: np.ndarray,
    dark_shutter: float,
    target_shutter: float,
    black_level: float = 0.0,
) -> np.ndarray:
    """Scale a dark frame to the scan's shutter time.

    Shutter ratio only -- see the module docstring for why ISO is excluded.

    Only the part of the dark *above the pedestal* is scaled. A dark frame's DN is
    pedestal + accumulated dark current, and while dark current integrates over
    exposure time, the pedestal is a fixed electronic offset that does not. Scaling
    the raw DN by the shutter ratio would multiply the pedestal too: on a D750 a
    dark shot at 1/4 of the scan's shutter would over-subtract three quarters of
    the 600 DN pedestal, ~1800 DN, which is a fifth of the whole range.
    """
    if dark_shutter <= 0 or target_shutter <= 0:
        raise ValueError("shutter times must be positive")
    ratio = target_shutter / dark_shutter
    return black_level + (np.asarray(dark, dtype=np.float64) - black_level) * ratio


def correct_dark(
    scan: np.ndarray, dark: CalibrationStack, scan_shutter: float
) -> np.ndarray:
    """Subtract a shutter-normalised dark frame.

    The stack's black level defaults to 0, meaning the stored data is already
    pedestal-free dark current. Pass the sensor pedestal to have only the
    above-black component rescaled -- see :func:`rescale_dark`.
    """
    if not dark.ready:
        raise ValueError("dark stack is empty")
    scaled = rescale_dark(dark.data, dark.shutter, scan_shutter, dark.black_level)
    return scan.astype(np.float64) - scaled


def stack_darks(
    frames: list[np.ndarray], shutters: list[float], black_level: float = 0.0
) -> tuple[np.ndarray, np.ndarray]:
    """Median-stack dark frames that were shot at *different* shutter times.

    :func:`stack` alone is wrong for mixed-shutter darks: dark current scales
    with shutter time, so medianing a 1.00 ms and a 1.17 ms dark together mixes
    two different dark currents into one master (measured on B1: ~44 DN bias,
    which at D=3 is a ~0.085 density error -- larger than the shot noise there).
    Each frame is first rescaled onto the *longest* shutter (the reference, so
    we only ever interpolate downward and never amplify), then medianed; the
    caller scales the master to the scan with :func:`rescale_dark` as usual.

    The returned spread is in reference-shutter units, like the median.
    """
    if len(frames) != len(shutters):
        raise ValueError("one shutter time required per dark frame")
    if not frames:
        raise ValueError("cannot stack an empty list")
    reference = max(shutters)
    normalised = [
        rescale_dark(f, shutter, reference, black_level)
        for f, shutter in zip(frames, shutters)
    ]
    return stack(normalised)


def build_flat(
    frames: list[np.ndarray], smooth_sigma: float = FLAT_SMOOTH_SIGMA
) -> np.ndarray:
    """Median-stack and low-pass a flat-field set.

    The blur is the whole point: a flat should contain illumination fall-off,
    dust and sensor vignetting, but not the structure of whatever was used as the
    diffuser. Smoothing keeps the correction gentle and prevents the flat's own
    texture from being etched into every scan.
    """
    median, _ = stack(frames)
    if smooth_sigma <= 0:
        return median
    return cv2.GaussianBlur(median, (0, 0), smooth_sigma)


def normalise_flat(flat: np.ndarray) -> np.ndarray:
    """Divide a flat by its own mean, so it becomes a unit-mean gain map."""
    mean = float(flat.mean())
    if mean <= 0:
        raise ValueError("flat field has non-positive mean")
    return flat.astype(np.float64) / mean


def rescale_flat(flat: np.ndarray, flat_exposure: float, target_exposure: float) -> np.ndarray:
    """Scale a flat to the scan's exposure using the full exposure factor.

    Operate on the *un-normalised* flat here; ``normalise_flat`` afterwards keeps
    the unit-mean property intact.
    """
    if flat_exposure <= 0 or target_exposure <= 0:
        raise ValueError("exposure factors must be positive")
    return flat.astype(np.float64) * (target_exposure / flat_exposure)


def correct_flat(
    scan: np.ndarray,
    flat: np.ndarray,
    flat_exposure: float,
    scan_exposure: float,
) -> np.ndarray:
    """Divide out a flat field, normalised to the scan's exposure.

    The flat is divided about its own unit mean, so the correction redistributes
    sensitivity without changing overall exposure. Regions where the flat is
    effectively black (no light reached them) carry no reliable information and
    are left uncorrected rather than amplified into noise.
    """
    scaled = rescale_flat(flat, flat_exposure, scan_exposure)
    gain = normalise_flat(scaled)
    # A minimum guard avoids dividing by near-zero where the diffuser blocked
    # light entirely; 0.05 of mean stops 20x amplification of noise.
    safe = np.maximum(gain, 0.05)
    return scan.astype(np.float64) / safe


def apply_calibration(
    scan: np.ndarray,
    dark: CalibrationStack | None,
    flat: np.ndarray | None,
    scan_shutter: float,
    flat_stack: CalibrationStack | None = None,
) -> np.ndarray:
    """Dark then flat, in the order given by the developer pipeline.

    Order matters: a flat is itself affected by dark current, and dividing first
    would amplify that offset by the flat's own gain.
    """
    out = scan
    if dark is not None and dark.ready:
        out = correct_dark(out, dark, scan_shutter)
    if flat is not None and flat_stack is not None:
        out = correct_flat(out, flat, flat_stack.shutter, scan_shutter)
    return out
