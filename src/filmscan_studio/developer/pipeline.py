"""Developer pipeline.

The chain the brief specifies::

    RAW (16-bit mono TIFF)
     -> dark correction
     -> flat correction
     -> base subtraction
     -> invert
     -> exposure adjustment
     -> filmic curve
     -> 16-bit TIFF

Two structural choices worth stating:

* **Calibration happens on raw sensor data, per site.** Dark current and pixel
  sensitivity are per-pixel properties, so the correction precedes every
  interpolation or resize. The mono sensor makes this the whole story: there
  is no demosaic step anywhere.
* **The pipeline is pure.** Same inputs, same bytes. Applied parameters come out
  in the result with a fingerprint, so reproducibility is checkable rather than
  claimed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from filmscan_studio.core.calibration import (
    CalibrationStack,
    build_flat,
    normalise_flat,
    rescale_dark,
    stack,
)
from filmscan_studio.core.exposure import ExposureSettings
from filmscan_studio.core.filmic import exposure_to_linear
from filmscan_studio.core.positive import PositiveParams, estimate_base, subtract_base
from filmscan_studio.core.rawio import RawFrame, open_frame

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeveloperParams:
    """Everything that determines an export's pixels."""

    positive: PositiveParams = field(default_factory=PositiveParams)

    def to_dict(self) -> dict[str, object]:
        return {
            **self.positive.profile.to_dict(),
            "exposure_ev": self.positive.exposure_ev,
            "base_level": self.positive.base_level,
            "invert": self.positive.invert,
            "base_percentile": self.positive.base_percentile,
        }

    def fingerprint(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:16]


@dataclass
class DeveloperResult:
    """A developed frame in display-referred float 0..1, plus provenance."""

    image: np.ndarray
    params: DeveloperParams
    source: Path | None
    calibration_used: tuple[str, ...] = ()
    fingerprint: str = ""

    def as_uint16(self) -> np.ndarray:
        """16-bit image with no further gamma: the curve is already applied."""
        return np.clip(self.image * 65535.0, 0, 65535).astype(np.uint16)

    def as_uint8(self) -> np.ndarray:
        return np.clip(self.image * 255.0, 0, 255).astype(np.uint8)


@dataclass
class DeveloperPipeline:
    """Applies the calibration and tone chain to raw frames.

    Built once per session with the calibration stacks, then called per frame, so
    the flat is blurred once rather than per scan.
    """

    dark: CalibrationStack | None = None
    flat: np.ndarray | None = None
    flat_exposure: CalibrationStack | None = None

    @classmethod
    def from_files(
        cls,
        dark_paths: list[Path],
        flat_paths: list[Path],
        dark_exposure: ExposureSettings,
        flat_exposure: ExposureSettings,
        flat_smooth_sigma: float = 64.0,
    ) -> DeveloperPipeline:
        """Build calibration stacks from raw files on disk."""
        dark = None
        if dark_paths:
            frames = [open_frame(p).data for p in dark_paths]
            median, spread = stack(frames)
            dark = CalibrationStack(
                shutter=dark_exposure.shutter,
                iso=dark_exposure.iso,
                aperture=dark_exposure.aperture,
                data=median,
                noise=spread,
                frame_count=len(frames),
            )
        flat = None
        flat_stack = None
        if flat_paths:
            flat = build_flat([open_frame(p).data for p in flat_paths], flat_smooth_sigma)
            flat_stack = CalibrationStack(
                shutter=flat_exposure.shutter,
                iso=flat_exposure.iso,
                aperture=flat_exposure.aperture,
                data=flat,
                frame_count=len(flat_paths),
            )
        return cls(dark=dark, flat=flat, flat_exposure=flat_stack)

    # ------------------------------------------------------------------ develop

    def develop(self, frame: RawFrame, params: DeveloperParams, shutter: float | None = None) -> DeveloperResult:
        """Mono develop of one raw frame — the whole pipeline, end to end.

        The sensor is monochrome: the raw data *is* the image, no interpolation
        step exists between calibration and the tone curve.
        """
        # _calibrate already returns normalised 0..1 data; do not re-normalise.
        calibrated = self._calibrate(frame, shutter)
        image = _positive_from_linear(calibrated, params.positive)
        return self._result(image, params, frame.path)

    # ----------------------------------------------------------------- internals

    def _calibrate(self, frame: RawFrame, shutter: float | None) -> np.ndarray:
        return self._calibrate_array(
            frame.data,
            frame.black_level,
            frame.white_level,
            shutter if shutter is not None else (frame.acquisition.exposure_time or 1.0),
        )

    def _calibrate_array(
        self, data: np.ndarray, black: float, white: float, shutter: float
    ) -> np.ndarray:
        """Dark and flat corrected data, normalised to 0..1."""
        return self._calibrate_above_black(data, black, shutter) / (white - black)

    def _calibrate_above_black(
        self, data: np.ndarray, black: float, shutter: float
    ) -> np.ndarray:
        """Calibrated data with the pedestal removed exactly once.

        Everything happens in raw DN, because every raw frame -- scan, dark and
        flat alike -- carries the same sensor pedestal. The dark subtracts it as
        part of itself, so after this the result is *above black*: subtracting
        ``black`` again afterwards would remove a pedestal that is already gone.

        The flat is dark-subtracted before its mean is taken, or the pedestal
        inflates the gain map and the correction under-corrects vignetting by the
        pedestal/mean ratio.

        Exposure rescaling of the flat is deliberately absent: after dark
        subtraction the flat is a pure photon signal, so dividing by its own mean
        already makes it exposure-invariant, which is exactly what the brief's
        "normalise by exposure time" requirement is for.
        """
        dn = np.asarray(data, dtype=np.float64)
        flat_signal: np.ndarray | None = None
        if self.dark is not None and self.dark.ready:
            # A dark is an ordinary raw frame from the same camera at the same
            # ISO, so it carries the scan's pedestal and subtracting it removes
            # that pedestal exactly once. black_level == 0 therefore means
            # "assume the scan's own", not "there is none".
            pedestal = self.dark.black_level or black
            dn = dn - rescale_dark(
                self.dark.data, self.dark.shutter, shutter, pedestal
            )
            if self.flat is not None and self.flat_exposure is not None:
                flat_signal = np.asarray(self.flat, dtype=np.float64) - rescale_dark(
                    self.dark.data,
                    self.dark.shutter,
                    self.flat_exposure.shutter,
                    pedestal,
                )
        else:
            # No dark to remove the pedestal with, so drop it explicitly -- from
            # the flat too, or the pedestal dilutes the gain map.
            dn = dn - black
            if self.flat is not None and self.flat_exposure is not None:
                flat_signal = np.asarray(self.flat, dtype=np.float64) - black
        if flat_signal is not None:
            gain = normalise_flat(flat_signal)
            # 0.05 floor: where the diffuser blocked light entirely there is no
            # information, and dividing by ~0 would amplify noise without limit.
            dn = dn / np.maximum(gain, 0.05)
        return dn

    def _result(self, image: np.ndarray, params: DeveloperParams, source: Path | None) -> DeveloperResult:
        return DeveloperResult(
            image=image,
            params=params,
            source=source,
            calibration_used=self._calibration_names(),
            fingerprint=params.fingerprint(),
        )

    def _calibration_names(self) -> tuple[str, ...]:
        names: list[str] = []
        if self.dark is not None and self.dark.ready:
            names.append(f"dark@{self.dark.shutter}s x{self.dark.frame_count}")
        if self.flat is not None and self.flat_exposure is not None:
            names.append(f"flat@{self.flat_exposure.shutter}s x{self.flat_exposure.frame_count}")
        return tuple(names)


def _positive_from_linear(linear01: np.ndarray, params: PositiveParams) -> np.ndarray:
    """Base subtraction, inversion, exposure and filmic on 2-D linear data."""
    return params.profile.apply(
        exposure_to_linear(_stage_to_positive(linear01, params), params.exposure_ev)
    )


def _stage_to_positive(linear01: np.ndarray, params: PositiveParams) -> np.ndarray:
    if not params.invert:
        floor = float(np.percentile(linear01, 1.0))
        return np.clip((linear01 - floor) / max(1.0 - floor, 1e-9), 0.0, 1.0)
    base = params.base_level
    if base is None:
        base = estimate_base(linear01, params.base_percentile)
    return subtract_base(linear01, base)


def write_tiff(result: DeveloperResult, destination: str | Path, bits: int = 16) -> Path:
    """Write the developed frame. 16-bit TIFF is the archival target.

    No gamma is applied on write because the tone curve is already baked into
    ``result.image``. Re-transforming an archived 16-bit TIFF is the classic way
    to silently double-encode a scan, so this function deliberately offers no
    option to do it.
    """
    dest = Path(destination)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if bits not in (8, 16):
        raise ValueError("bits must be 8 or 16")
    data = result.as_uint16() if bits == 16 else result.as_uint8()
    if data.ndim == 2:
        data = np.dstack([data] * 3)
    # OpenCV is BGR; channel order matters even for BW because the three stacked
    # planes are identical and any future colour work must not inherit a swap.
    cv2.imwrite(str(dest), np.ascontiguousarray(data[:, :, ::-1]), [cv2.IMWRITE_TIFF_COMPRESSION, 1])
    return dest


def write_jpeg(result: DeveloperResult, destination: str | Path, quality: int = 95) -> Path:
    """Quick-look JPEG. Not the archival artefact."""
    dest = Path(destination)
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = result.as_uint8()
    if data.ndim == 2:
        data = np.dstack([data] * 3)
    cv2.imwrite(str(dest), np.ascontiguousarray(data[:, :, ::-1]), [cv2.IMWRITE_JPEG_QUALITY, quality])
    return dest


def sidecar_for(result: DeveloperResult, processed_at: datetime | None = None) -> dict[str, object]:
    """Processing provenance, written next to every export.

    Enough to regenerate the file bit-for-bit without the originating session:
    parameters, calibration identity, and a fingerprint to compare against.
    """
    return {
        "source": str(result.source) if result.source else None,
        "processed_at": (processed_at or datetime.now().astimezone()).isoformat(),
        "pipeline": [
            "dark", "flat", "base_subtraction", "invert",
            "exposure", "filmic",
        ],
        "calibration": list(result.calibration_used),
        "parameters": result.params.to_dict(),
        "fingerprint": result.fingerprint,
    }
