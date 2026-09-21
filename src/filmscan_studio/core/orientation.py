"""Film orientation: the three recorded flags, applied to pixels.

The capture app *records* how the strip sat in the holder
(``mirrored_horizontal`` / ``mirrored_vertical`` / ``rotated_180`` on
``FilmMetadata``) and never touches pixels — post-production flips. Both
consumers of that record (the developer's renders and the annotator's
previews) need the same pixel transform, so it lives here once.

The combination rules are the model's (``FilmMetadata._orientation_not_identity``
rejects all three flags at once); this function just composes what it is
given, in the order rotate → mirror H → mirror V that
:meth:`filmscan_studio.developer.project.DevelopProject.orientation_apply`
has always used.
"""

from __future__ import annotations

import numpy as np


def apply_orientation(image: np.ndarray, *, mirrored_horizontal: bool,
                      mirrored_vertical: bool, rotated_180: bool) -> np.ndarray:
    """Flip/rotate a 2-D map (or HxWxN array) into viewer orientation.

    Returns the input untouched (same object) when no flag is set, so callers
    in the density pipeline never pay for a copy they do not need.
    """
    if not (mirrored_horizontal or mirrored_vertical or rotated_180):
        return image
    a = np.asarray(image)
    if rotated_180:
        a = np.rot90(a, 2)
    if mirrored_horizontal:
        a = a[:, ::-1]
    if mirrored_vertical:
        a = a[::-1, :]
    return np.ascontiguousarray(a)
