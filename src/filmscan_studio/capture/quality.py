"""Post-capture exposure audit straight from the NEF.

Auto Exposure on the Live View stream is a *prediction* — the body's meter is
global and its JPEG is auto-brightness. The delivered NEF is the only honest
word about what the sensor actually got. This module reads that word after
every scan: it meters the same area the operator framed with the red rect (or
the whole frame when no rect is set), compares against the project's exposure
target (the 99.9 percentile ``DEFAULT_HEADROOM_EV`` below clipping), and says
``ok`` / ``over`` / ``under`` with the shutter the *next* frame needs.

The correction is applied as a shutter move only, and only at the archival
ISO — the audit deliberately never touches ISO, mirroring the GUI rule that
the whole film is one measurement at ISO 100. The already-exposed NEF cannot
be rescued; the verdict exists so the operator re-shoots that frame and the
next one lands right.

Mapping the red rect from stream to sensor pixels is only trustworthy at the
body-side ``Whole`` zoom rate: a zoomed LV crop is positionally unreported by
the D750 (no LiveViewPosition measurement exists), so a rect drawn on a
zoomed stream is refused rather than silently metered at the wrong place.
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
from filmscan_studio.core.rawio import demosaic_linear, open_frame

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
    #: True when the suggestion is actionable (ISO 100 + shutter move on ladder).
    auto_applied: bool = False

    @property
    def needs_action(self) -> bool:
        return self.verdict != "ok"


def audit_nef(
    path: Path,
    ae_rect: tuple[int, int, int, int] | None,
    lv_size: tuple[int, int],
    settings: ExposureSettings,
    headroom_ev: float = DEFAULT_HEADROOM_EV,
    archive_iso: int = 100,
    shutter_ladder: list[float] | None = None,
) -> AuditResult:
    """Meter one captured NEF and judge it.

    ``ae_rect`` is the red rectangle in Live View *stream* pixels as drawn;
    ``lv_size`` the stream size those coordinates belong to. Rect→sensor
    mapping is linear from the whole-frame stream; at other rates the rect
    refers to pixels whose sensor position is unknown.
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
        # Even edges keep the Bayer phase that `measure` sees across the whole
        # frame rather than one row shifted. A rect rounding to zero sensor
        # pixels falls back to the whole frame instead of metering nothing.
        rw, rh = (rx1 - rx0) & ~1, (ry1 - ry0) & ~1
        if rw > 0 and rh > 0:
            region = data[ry0:ry0 + rh, rx0:rx0 + rw]
            mapped = True
    # Mosaiced BW/D750 frames: one channel value per pixel is exactly what
    # `measure` wants (per-site DN); no demosaic needed for a rank statistic.
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
            ev, reading, settings, shutter_ladder, archive_iso,
        )
    try:
        ev = required_ev_change(reading, headroom_ev)
    except ValueError:
        ev = headroom_ev + 4.0     # signal at black: badly under
        return _decision(
            "under", f"PODEXPOZICE v {scope}: signál na černé ({path.name})",
            ev, reading, settings, shutter_ladder, archive_iso,
        )
    if ev > EV_TOLERANCE:
        return _decision(
            "under",
            f"PODEXPOZICOVÁNO v {scope}: o {ev:.2f} EV níž, než cílí "
            f"({path.name})",
            ev, reading, settings, shutter_ladder, archive_iso,
        )
    if ev < -EV_TOLERANCE:
        return _decision(
            "over",
            f"Zbytečně světlé {scope}: o {-ev:.2f} EV nad cíl ({path.name})",
            ev, reading, settings, shutter_ladder, archive_iso,
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
    archive_iso: int,
) -> AuditResult:
    """Pair a verdict with the shutter that would land the target — if we may."""
    suggested: float | None = None
    # Shutter is the only correction lever, and only valid at the archival ISO:
    # a suggestion computed against ISO 400 settings would be meaningless.
    if shutter_ladder and settings.iso == archive_iso and abs(ev) > EV_TOLERANCE:
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
    nef_path: Path,
    jpg_path: Path,
    positive_params,
    max_width: int = 1600,
    quality: int = 92,
) -> Path:
    """Positive JPEG next to the NEF: WB → base → invert → S-curve.

    Rendered from the RAW, never from the Live View JPEG — the body's stream
    is auto-brightness and would bake the display's lies into the archive's
    quick look. Half-size LibRaw demosaic + 8-bit output is the deliberate
    shortcut: this is a look-over-shoulder preview, the archival render stays
    the developer's 16-bit TIFF job.
    """
    import cv2

    from filmscan_studio.core.positive import to_working_positive

    # When the operator locked WB gains (Auto WB), LibRaw's camera WB must
    # stay off or the two corrections stack into a double cast. Without a
    # lock, the camera's own guess is the best there is for a quick look.
    locked = getattr(positive_params, "wb_gains", None) is not None
    rgb = demosaic_linear(nef_path, half_size=True, use_camera_wb=not locked)
    display = to_working_positive(rgb, 0.0, 1.0, positive_params)
    data = np.clip(display * 255.0, 0, 255).astype(np.uint8)
    h, w = data.shape[:2]
    if w > max_width:
        scale = max_width / w
        data = cv2.resize(data, (max_width, max(1, int(h * scale))),
                          interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(jpg_path), data[:, :, ::-1], [cv2.IMWRITE_JPEG_QUALITY, quality])
    return jpg_path
