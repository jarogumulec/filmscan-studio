"""Post-capture exposure audit straight from the delivered 16-bit frame.

Auto Exposure on the Live View stream is a *prediction* — it meters a binned
overview, not the full-resolution exposure. The delivered frame is the only
honest word about what the sensor actually got. This module reads that word
after every scan: it meters the same area the operator framed with the red
rect (or the whole frame when no rect is set), compares against the project's
exposure target (the 99.9 percentile ``DEFAULT_HEADROOM_EV`` below clipping),
and says ``ok`` / ``over`` / ``under`` with the shutter the *next* frame needs.

The correction is applied as a shutter move only — the audit deliberately
never touches gain, mirroring the GUI rule that the whole film is one
measurement at the noise-floor gain. The already-exposed frame cannot be
rescued; the verdict exists so the operator re-shoots that frame and the next
one lands right.

The sensor is mono, so a rect maps linearly from either stream mode: the
binned overview scales by a fixed 3, an ROI is offset-only — unlike the old
D750 body zoom whose crop position was unreported.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from filmscan_studio.core.exposure import (
    DEFAULT_HEADROOM_EV,
    ExposureSettings,
    choose_shutter,
    measure,
    required_ev_change,
)
from filmscan_studio.core.rawio import open_frame

log = logging.getLogger(__name__)

#: Above this clipped-pixel fraction the frame is over. One ten-thousandth is
#: the same bar MeterReading.clipped uses — enough that dust/hot pixels do not
#: trip it, low enough that real blown base does.
CLIP_TOLERANCE = 0.0001
#: Residuals under this are ladder granularity, not an exposure mistake.
EV_TOLERANCE = 0.25


@dataclass(frozen=True)
class AuditResult:
    """The verdict, the numbers behind it, and the suggested fix."""

    verdict: str                       # "ok" | "over" | "under"
    message: str                       # human sentence for the GUI/log
    ev_change: float                   # stops to ADD for the next frame (0 = none)
    suggested_shutter: float | None    # shutter that would land the target
    clipped_fraction: float
    #: True when the suggestion is actionable (a shutter move exists on ladder).
    auto_applied: bool = False

    @property
    def needs_action(self) -> bool:
        return self.verdict != "ok"


def audit_frame(
    path: Path,
    ae_rect: tuple[int, int, int, int] | None,
    lv_size: tuple[int, int],
    settings: ExposureSettings,
    headroom_ev: float = DEFAULT_HEADROOM_EV,
    shutter_ladder: list[float] | None = None,
) -> AuditResult:
    """Meter one captured frame and judge it.

    ``ae_rect`` is the red rectangle in Live View *stream* pixels as drawn;
    ``lv_size`` the stream size those coordinates belong to. The map to sensor
    pixels is the plain scale between the two — true for the binned overview
    (uniform 3:1) and for a hardware ROI (the GUI keeps the rect in stream
    coordinates of the current stream, whatever it is).
    """
    frame = open_frame(path)
    data = frame.data
    region = data
    mapped = False
    if ae_rect is not None:
        lv_w, lv_h = lv_size
        x0, y0, x1, y1 = ae_rect
        sx, sy = frame.width / max(lv_w, 1), frame.height / max(lv_h, 1)
        rx0 = max(int(x0 * sx), 0)
        ry0 = max(int(y0 * sy), 0)
        rx1 = min(int(x1 * sx), frame.width)
        ry1 = min(int(y1 * sy), frame.height)
        if rx1 > rx0 and ry1 > ry0:
            region = data[ry0:ry1, rx0:rx1]
            mapped = True
    reading = measure(region.astype(np.float64), frame.black_level,
                      frame.white_level)
    clipped = reading.clipped_fraction
    scope = "AE výřez" if mapped else "celý snímek"

    if clipped > CLIP_TOLERANCE:
        ev = required_ev_change(reading, headroom_ev)
        return _decision(
            "over",
            f"PŘEPAL v {scope}: {clipped:.3%} pixelů na bílé ({path.name}) — "
            f"snímek zopakuj, další o {ev:+.2f} EV",
            ev, reading, settings, shutter_ladder,
        )
    try:
        ev = required_ev_change(reading, headroom_ev)
    except ValueError:
        ev = headroom_ev + 4.0     # signal at black: badly under
        return _decision(
            "under", f"PODEXPOZICE v {scope}: signál na černé ({path.name})",
            ev, reading, settings, shutter_ladder,
        )
    if ev > EV_TOLERANCE:
        return _decision(
            "under",
            f"PODEXPOZICOVÁNO v {scope}: o {ev:.2f} EV níž, než cílí "
            f"({path.name})",
            ev, reading, settings, shutter_ladder,
        )
    if ev < -EV_TOLERANCE:
        return _decision(
            "over",
            f"Zbytečně světlé {scope}: o {-ev:.2f} EV nad cíl ({path.name})",
            ev, reading, settings, shutter_ladder,
        )
    return AuditResult(
        verdict="ok",
        message=f"Expozice v cíli ({scope}, {path.name})",
        ev_change=0.0, suggested_shutter=None, clipped_fraction=clipped,
    )


def _decision(
    verdict: str,
    message: str,
    ev: float,
    reading,
    settings: ExposureSettings,
    shutter_ladder: list[float] | None,
) -> AuditResult:
    """Pair a verdict with the shutter that would land the target — if we may.

    Shutter is the only correction lever: gain is the measurement's identity
    and the audit never moves it.
    """
    suggested: float | None = None
    if shutter_ladder and abs(ev) > EV_TOLERANCE:
        try:
            suggested = choose_shutter(settings, ev, list(shutter_ladder))
        except ValueError:
            suggested = None
    return AuditResult(
        verdict=verdict, message=message, ev_change=ev,
        suggested_shutter=suggested,
        clipped_fraction=reading.clipped_fraction,
    )


# --------------------------------------------------------------------- preview

def render_preview_jpeg(
    raw_path: Path,
    jpg_path: Path,
    positive_params,
    max_width: int = 1600,
    quality: int = 92,
) -> Path:
    """Positive JPEG next to the frame: base → invert → S-curve.

    Rendered from the delivered 16-bit data, never from the Live View stream —
    a half-size downsample of the real pixels is a look-over-shoulder preview;
    the archival render stays the developer's 16-bit TIFF job.
    """
    import cv2

    from filmscan_studio.core.positive import to_working_positive

    frame = open_frame(raw_path)
    # Downsample first (radiometry-preserving area average), then the positive
    # chain in float; a mono frame is already its own one-channel "RGB".
    data = frame.data.astype(np.float64)
    h, w = data.shape
    if w > max_width:
        scale = max_width / w
        data = cv2.resize(data, (max_width, max(1, int(h * scale))),
                          interpolation=cv2.INTER_AREA)
    display = to_working_positive(data, frame.black_level, frame.white_level,
                                  positive_params)
    out = np.clip(display * 255.0, 0, 255).astype(np.uint8)
    if out.ndim == 2:
        out = np.repeat(out[:, :, None], 3, axis=2)
    cv2.imwrite(str(jpg_path), out[:, :, ::-1], [cv2.IMWRITE_JPEG_QUALITY, quality])
    return jpg_path
