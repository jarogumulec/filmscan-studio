"""Raw file access: reading mosaic data, black/white levels and EXIF.

The project does not implement its own demosaicing. LibRaw (via rawpy) is used,
both because it is excellent and because the value added here is the
reproducible workflow, not the interpolation. See the project brief.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Iterator

import exifread
import numpy as np
import rawpy

from filmscan_studio.core.models import AcquisitionMetadata


@dataclass(frozen=True)
class RawFrame:
    """A raw sensor frame plus the radiometric constants needed to normalise it."""

    path: Path | None
    #: Mosaic sensor data, uncorrected, as read from the file.
    data: np.ndarray
    black_level: float
    white_level: float
    #: e.g. 'RGGB'. Matters because dark/flat must share this geometry.
    color_desc: str
    width: int
    height: int
    acquisition: AcquisitionMetadata

    @property
    def span(self) -> float:
        return self.white_level - self.black_level


def open_frame(path: str | Path) -> RawFrame:
    """Read a raw file's mosaic data and radiometric constants.

    The camera black level is used as-is rather than a fixed pedestal: LibRaw
    reports the per-channel value from the file, and on the D750 that is ~600 DN
    at base ISO.
    """
    p = Path(path)
    with rawpy.imread(str(p)) as raw:
        data = raw.raw_image_visible.copy()
        black = float(np.mean(raw.black_level_per_channel))
        white = float(raw.white_level)
        desc = raw.color_desc.decode() if isinstance(raw.color_desc, bytes) else str(raw.color_desc)
        other = raw.other
        h, w = data.shape
    return RawFrame(
        path=p,
        data=data,
        black_level=black,
        white_level=white,
        color_desc=desc,
        width=w,
        height=h,
        acquisition=read_exif(p) or _acquisition_from_rawpy(other),
    )


def open_preview_frame(path: str | Path, half_size: bool = True) -> RawFrame:
    """Half-size demosaiced frame for fast preview: linear, undisplayed, 0..1.

    LibRaw's ``postprocess`` already subtracts black and scales by white, so the
    frame's radiometry is declared as black=0 / white=1. ``normalise()`` is then a
    no-op and downstream code is identical to the mosaic path.
    """
    p = Path(path)
    with rawpy.imread(str(p)) as raw:
        img = raw.postprocess(
            use_camera_wb=True,
            no_auto_bright=True,
            half_size=half_size,
            output_bps=16,
            gamma=(1.0, 1.0),
        )
        desc = raw.color_desc.decode() if isinstance(raw.color_desc, bytes) else str(raw.color_desc)
        other = raw.other
    data = _to_unit_linear(img)
    h, w = data.shape[:2]
    return RawFrame(
        path=p,
        data=data,
        black_level=0.0,
        white_level=1.0,
        color_desc=desc,
        width=w,
        height=h,
        acquisition=read_exif(p) or _acquisition_from_rawpy(other),
    )


def jpeg_dimensions(path: str | Path) -> tuple[int, int] | None:
    """(width, height) from a JPEG's SOF marker — no decode, no dependency.

    Exists because the D750 can be configured to answer a still capture with
    a JPEG wearing a .NEF extension (Compression Level != RAW); feeding such
    a file to rawpy yields LibRaw's cryptic b'Input/output error', and the
    session needs geometry for the sidecar either way.
    """
    with open(path, "rb") as fh:
        if fh.read(2) != b"\xff\xd8":
            return None
        while True:
            marker = fh.read(2)
            if len(marker) < 2 or marker[0] != 0xFF:
                return None
            code = marker[1]
            # SOF0..SOF15 minus DHT(C4)/JPG(C8)/DAC(CC); SOF1 is progressive.
            if 0xC0 <= code <= 0xCF and code not in (0xC4, 0xC8, 0xCC):
                seg = fh.read(7)
                if len(seg) < 7:
                    return None
                h = int.from_bytes(seg[3:5], "big")
                w = int.from_bytes(seg[5:7], "big")
                return (w, h) if w and h else None
            if code in (0xD8, 0x01) or 0xD0 <= code <= 0xD7:
                continue                      # standalone markers, no length
            length = fh.read(2)
            if len(length) < 2:
                return None
            fh.seek(int.from_bytes(length, "big") - 2, 1)


def _acquisition_from_rawpy(other: rawpy.Other) -> AcquisitionMetadata:
    """Fall back to LibRaw's summary when EXIF parsing finds nothing."""
    from datetime import datetime

    ts = getattr(other, "timestamp", None)
    capture_date = None
    if ts:
        try:
            capture_date = datetime.fromtimestamp(int(ts))
        except (ValueError, OSError):
            capture_date = None
    return AcquisitionMetadata(
        lens=_fmt_lens(other.aperture),
        iso=int(other.iso_speed) if other.iso_speed else None,
        exposure_time=float(other.shutter_speed) if other.shutter_speed else None,
        f_number=float(other.aperture) if other.aperture else None,
        capture_date=capture_date,
    )


@contextmanager
def _open_exif(path: Path) -> Iterator[dict]:
    with open(path, "rb") as fh:
        yield exifread.process_file(fh, details=False, strict=False)


def read_exif(path: str | Path) -> AcquisitionMetadata | None:
    """Extract acquisition metadata from a raw file.

    exifread returns rationals as strings; everything is coerced to the plain
    numbers the rest of the application expects, and unparseable values become
    None rather than raising, so a slightly odd file never blocks a session.
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        with _open_exif(p) as tags:
            exposure = _to_float(tags.get("EXIF ExposureTime"))
            fnum = _to_float(tags.get("EXIF FNumber"))
            iso = _to_int(
                tags.get("EXIF ISOSpeedRatings") or tags.get("EXIF PhotographicSensitivity")
            )
            lens = str(tags["EXIF LensModel"]) if "EXIF LensModel" in tags else None
            camera = str(tags["Image Model"]) if "Image Model" in tags else None
            when = _to_datetime(tags.get("EXIF DateTimeOriginal"))
    except Exception:
        return None

    if not any((camera, lens, iso, exposure, fnum, when)):
        return None
    return AcquisitionMetadata(
        camera=camera,
        lens=lens,
        iso=iso,
        exposure_time=exposure,
        f_number=fnum,
        capture_date=when,
    )


def _to_float(tag) -> float | None:
    if tag is None:
        return None
    try:
        return float(Fraction(str(tag).split(" ")[0]))
    except (ValueError, ZeroDivisionError):
        return None


def _to_int(tag) -> int | None:
    v = _to_float(tag)
    return int(round(v)) if v is not None else None


def _to_datetime(tag):
    from datetime import datetime

    if tag is None:
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(tag).strip(), fmt)
        except ValueError:
            continue
    return None

def demosaic_linear(path: str | Path, half_size: bool = True, use_camera_wb: bool = True) -> np.ndarray:
    """Demosaic a raw file to linear RGB in 0..1 via LibRaw.

    No gamma and no output colour space are applied, so the result is genuinely
    linear and downstream base subtraction and filmic maths are valid. LibRaw does
    the interpolation deliberately: the added value of this project is the
    reproducible workflow, not a custom demosaicer.
    """
    with rawpy.imread(str(path)) as raw:
        img = raw.postprocess(
            use_camera_wb=use_camera_wb,
            no_auto_bright=True,
            half_size=half_size,
            output_bps=16,
            gamma=(1.0, 1.0),
        )
    return _to_unit_linear(img)


def _to_unit_linear(img: np.ndarray) -> np.ndarray:
    """Scale LibRaw's 16-bit linear output to 0..1.

    With ``gamma=(1.0, 1.0)`` and no auto-brightness, LibRaw emits
    ``(dn - black) * 65535 / (white - black)``. Dividing by 65535 therefore
    reproduces exactly what :func:`filmscan_studio.core.positive.normalise`
    computes from mosaic DN, so the preview and the mosaic pipeline agree.
    """
    arr = img.astype(np.float64)
    if img.dtype == np.uint16:
        arr /= 65535.0
    elif img.dtype == np.uint8:
        arr /= 255.0
    return np.clip(arr, 0.0, 1.0)
