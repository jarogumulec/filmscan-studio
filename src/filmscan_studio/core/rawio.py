"""Raw file access: 16-bit monochrome TIFF frames plus embedded acquisition JSON.

The capture camera is mono (IMX571): there is no Bayer mosaic, no demosaic and
no LibRaw anywhere. An archive frame is a single-channel 16-bit TIFF written by
:func:`write_frame`, which embeds the acquisition metadata (camera, serial,
shutter, gain, sensor temperature, black/white levels, timestamp) as JSON in
the ImageDescription tag — so a file is self-describing even without the
session sidecar, and any external tool still reads it as a plain TIFF.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import tifffile

from filmscan_studio.core.models import AcquisitionMetadata

#: Extension of archive frames.
RAW_SUFFIX = ".tif"

_MAGIC = "filmscan"


@dataclass(frozen=True)
class RawFrame:
    """A raw sensor frame plus the radiometric constants needed to normalise it."""

    path: Path | None
    #: Mono sensor data, uncorrected, as read from the file.
    data: np.ndarray
    black_level: float
    white_level: float
    #: Always 'mono' — kept for the geometry contract dark/flat frames share.
    color_desc: str
    width: int
    height: int
    acquisition: AcquisitionMetadata

    @property
    def span(self) -> float:
        return self.white_level - self.black_level


def write_frame(
    path: str | Path,
    data: np.ndarray,
    *,
    acquisition: AcquisitionMetadata | None = None,
    black_level: float = 0.0,
    white_level: float = 65535.0,
) -> Path:
    """Write one uint16 mono archive frame with embedded acquisition JSON."""
    p = Path(path)
    if data.ndim != 2:
        raise ValueError(f"mono frame must be 2-D, got shape {data.shape}")
    payload = {
        "magic": _MAGIC,
        "black_level": black_level,
        "white_level": white_level,
        "acquisition": (acquisition.model_dump(mode="json")
                        if acquisition is not None else None),
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    tifffile.imwrite(
        p,
        np.ascontiguousarray(data, dtype=np.uint16),
        description=json.dumps(payload, ensure_ascii=False),
        datetime=datetime.now().astimezone(),
    )
    return p


def open_frame(path: str | Path) -> RawFrame:
    """Read an archive frame's data and radiometric constants."""
    p = Path(path)
    data = tifffile.imread(p)
    if data.ndim == 3 and data.shape[-1] == 1:
        data = data[..., 0]
    if data.ndim != 2:
        raise ValueError(f"{p.name} isn't a mono frame (shape {data.shape})")
    meta = _read_description(p)
    acquisition = _acquisition_from_meta(meta)
    black = float(meta.get("black_level", 0.0)) if meta else 0.0
    white = float(meta.get("white_level", 65535.0)) if meta else 65535.0
    h, w = data.shape
    return RawFrame(
        path=p,
        data=data,
        black_level=black,
        white_level=white,
        color_desc="mono",
        width=w,
        height=h,
        acquisition=acquisition,
    )


def frame_levels(path: str | Path) -> tuple[float, float]:
    """Black/white levels of an archive frame without loading its pixels."""
    meta = _read_description(path)
    if meta is None:
        return (0.0, 65535.0)
    return (float(meta.get("black_level", 0.0)),
            float(meta.get("white_level", 65535.0)))


def _read_description(path: str | Path) -> dict | None:
    """The embedded JSON payload, or None for a foreign/plain TIFF."""
    try:
        with tifffile.TiffFile(path) as tf:
            page = tf.pages[0]
            raw = page.description
    except Exception:  # noqa: BLE001 - unreadable metadata is not fatal here
        return None
    if not raw:
        return None
    try:
        meta = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return meta if isinstance(meta, dict) and meta.get("magic") == _MAGIC else None


def _acquisition_from_meta(meta: dict | None) -> AcquisitionMetadata:
    if not meta or not meta.get("acquisition"):
        return AcquisitionMetadata()
    try:
        return AcquisitionMetadata.model_validate(meta["acquisition"])
    except Exception:  # noqa: BLE001 - a slightly odd header never blocks a read
        return AcquisitionMetadata()
