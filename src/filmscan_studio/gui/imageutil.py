"""numpy <-> QImage conversion, and the preview pipeline the two modes share.

The GUI is deliberately the thinnest possible layer over the core: both preview
modes call the same functions the developer calls on export, so what the operator
sees while framing is produced by the code that will produce the file.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtGui import QImage

from filmscan_studio.core.exposure import D750_WHITE_LEVEL
from filmscan_studio.core.positive import (
    PositiveParams,
    to_raw_view,
    to_working_positive,
)


def to_qimage(array: np.ndarray) -> QImage:
    """0..1 float grey or RGB, through 8-bit RGBA, to a QImage that owns its data.

    A copy is mandatory: ``array`` is a temporary in most call sites and QImage
    does not take ownership without one.
    """
    a = np.asarray(array, dtype=np.float64)
    a = np.clip(a, 0.0, 1.0)
    if a.ndim == 2:
        rgb = np.repeat((a * 255.0 + 0.5).astype(np.uint8)[:, :, None], 3, axis=2)
    elif a.ndim == 3 and a.shape[2] == 3:
        rgb = (a * 255.0 + 0.5).astype(np.uint8)
    else:
        raise ValueError(f"cannot convert array of shape {a.shape} to QImage")
    h, w, _ = rgb.shape
    rgba = np.dstack([rgb, np.full((h, w), 255, dtype=np.uint8)])
    # QImage keeps a raw pointer; copy makes the buffer outlive this frame.
    contiguous = np.ascontiguousarray(rgba)
    return QImage(contiguous.data, w, h, 4 * w, QImage.Format.Format_RGBA8888).copy()


def preview(
    linear: np.ndarray,
    params: PositiveParams,
    raw_view: bool,
    black_level: float = 0.0,
    white_level: float = D750_WHITE_LEVEL,
) -> np.ndarray:
    """Display-referred image for one of the two modes.

    ``raw_view`` skips inversion and the filmic curve entirely -- that is the
    definition of the mode, not a quality setting.
    """
    if raw_view:
        return to_raw_view(linear, black_level, white_level)
    return to_working_positive(linear, black_level, white_level, params)
