"""Body-vocabulary parsers — importable from the x86_64 helper.

``sdk_server`` runs inside ``.venv-x86``, which deliberately holds only
stdlib + cffi (no numpy, no Qt). It needs the same shutter/ISO string parsing
as the GUI, and importing them through :mod:`~filmscan_studio.capture.
nikon_backend` drags in ``camera`` → ``core.exposure`` → numpy, which killed
the helper at import time — the GUI then reported an unhelpful RPC timeout and
silently fell back to gphoto2. Keeping the parsers here, dependency-free, is
what makes that chain safe.
"""

from __future__ import annotations

import re

_SHUTTER_RE = re.compile(
    r"^\s*(?:(\d+)\s*/\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?))\s*(?:s|\"|sec)?\s*$"
)


def parse_shutter(value: str) -> float | None:
    """'1/60' -> 0.0166…, '2.5'/'0.0400 s'/'30"' -> seconds.

    None for entries that are not numeric speeds ('Bulb', 'Time'). The
    denominator may be fractional because the SDK's own ladder contains
    '1/1.3'-style entries.
    """
    m = _SHUTTER_RE.match(value)
    if not m:
        return None
    num, den, whole = m.groups()
    if num is not None:
        d = float(den)
        return int(num) / d if d else None
    return float(whole)


def parse_iso(value: str) -> int | None:
    """'100' -> 100; None for 'LO-1'/'Hi-2.0' extended ranges (out of the
    auto-exposure controller's world anyway)."""
    m = re.fullmatch(r"(\d+)", value.strip())
    return int(m.group(1)) if m else None
