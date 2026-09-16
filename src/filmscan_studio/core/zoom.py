"""Zoom in *sensor pixel* terms, plus an honest account of what Live View shows.

100 % here means one screen pixel per sensor pixel of a full-resolution frame —
the D750's NEF is 6016x4016 — and **not** one pixel per pixel of the small Live
View JPEG the body sends over USB. That distinction is the entire module. The
previous ladder fitted the 640 px preview to the window and called the result
100 %, which made "Fit" and "100 %" meaningless next to the real frame.

The body cannot send more pixels than it does (640x424 whole-frame, measured),
but ``LiveViewImageZoomRate`` reframes the stream: at a zoomed rate the same 640
pixels stand for a crop of the sensor, so each of them is worth fewer sensor
pixels and the detail on screen genuinely improves. What this module computes is
therefore two separate numbers that the UI must never conflate:

``sensor_px_per_lv_px``
    real detail — how many sensor pixels one delivered pixel represents.
    Set by the body-side zoom rate, never by the app.

``interpolation``
    how much the app has to upscale to honour the requested display scale.
    Zero is honest, anything above zero is stated rather than hidden.

``FIT`` displays the whole frame whatever its pixel count, so its scale depends
on the widget size and is resolved by the view, not here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: Sentinel zoom meaning "scale to fit the widget".
FIT = 0.0

#: Display scales offered in the UI, in screen pixels per *sensor* pixel.
#: 1.0 is a true 1:1 readout of the 6016x4016 frame.
ZOOM_LEVELS: tuple[float, ...] = (FIT, 0.125, 0.25, 0.5, 1.0, 2.0)

#: Labels for :data:`ZOOM_LEVELS`. Percentages are of the sensor's pixel grid,
#: which is what the operator is actually judging focus against.
ZOOM_LABELS: dict[float, str] = {
    FIT: "Fit (celý frame)",
    0.125: "12,5 % sensoru",
    0.25: "25 % sensoru",
    0.5: "50 % sensoru",
    1.0: "100 % — 1:1 pixel NEFu",
    2.0: "200 %",
}

#: ``LiveViewImageZoomRate`` element values (eNkMAIDLiveViewImageZoomRate).
ZOOM_ALL, ZOOM_25, ZOOM_33, ZOOM_50, ZOOM_66, ZOOM_100, ZOOM_200 = range(7)

#: Sensor pixels represented by one delivered Live View pixel, per rate.
#:
#: ``None`` marks Whole-frame, where the factor is the frame width ratio
#: (6016 / 640 ≈ 9.4) and is computed from the actual sizes instead.
#:
#: The numbers encode Nikon's documented reading of the percentages as
#: magnification relative to the sensor's own pixel grid (100 % = 1:1). On the
#: D750 the LV JPEG stays 640 px wide at every rate (measured — see
#: ``sdk_probe_results.json``), so this table *is* the crop model. The deepest
#: rates have not been verified against a ruler on hardware: run
#: ``scripts/probe.sh`` and compare its ``crop_factor`` measurements, then
#: correct this table. Until then the UI prints the assumed figure.
BODY_ZOOM_SENSOR_PX_PER_LV_PX: dict[int, float | None] = {
    ZOOM_ALL: None,
    ZOOM_25: 4.0,
    ZOOM_33: 3.0,
    ZOOM_50: 2.0,
    ZOOM_66: 1.5,
    ZOOM_100: 1.0,
    ZOOM_200: 0.5,
}

#: Above this interpolation the requested scale is better served by a real
#: capture than by upscaling Live View; the UI says so instead of pretending.
HONEST_INTERPOLATION_LIMIT = 2.0

#: Below this display scale a body-side crop is never requested. Measured
#: 2026-09-15 on the D750: every zoomed rate delivers 640x480 — at the
#: shallowest crop the visible area is ~43 % x 48 % of the frame, a crop the
#: operator read as "výřez je čtverec a je oříznutý — nedělej ořez". Body
#: crops buy real detail only where the operator inspects pixels (>= 1:1);
#: an overview must show the whole frame even if that means interpolating.
MIN_ZOOM_FOR_BODY_CROP = 1.0


@dataclass(frozen=True)
class SensorSize:
    """Full-resolution frame geometry, i.e. the NEF, not the Live View stream."""

    width: int = 6016
    height: int = 4016

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("sensor dimensions must be positive")

    def fit_scale(self, widget_width: int, widget_height: int) -> float:
        """Screen pixels per sensor pixel that fits the whole frame."""
        if widget_width <= 0 or widget_height <= 0:
            return 1.0
        return min(widget_width / self.width, widget_height / self.height)


@dataclass(frozen=True)
class StreamDetail:
    """What the delivered stream is actually worth at one body-side zoom rate."""

    rate: int
    lv_width: int
    lv_height: int
    sensor_px_per_lv_px: float
    #: Fraction of the frame width the body is currently showing (1.0 = whole).
    crop_fraction: float

    def sensor_span(self) -> int:
        """Sensor pixels covered horizontally by the delivered frame."""
        return int(round(self.lv_width * self.sensor_px_per_lv_px))

    def interpolation_at(self, screen_px_per_sensor_px: float) -> float:
        """How many screen pixels one delivered pixel is stretched onto.

        ``1.0`` means the delivered pixels land one per screen pixel: no
        invention of detail. Below 1 the stream is downsampled (Fit), which is
        lossy but never fabricates grain.
        """
        if screen_px_per_sensor_px <= 0:
            return 0.0
        return screen_px_per_sensor_px * self.sensor_px_per_lv_px

    def summary(self) -> str:
        interp = self.sensor_px_per_lv_px
        crop = f" · výřez {self.crop_fraction:.0%} šířky" if self.crop_fraction < 0.99 else ""
        return (
            f"stream {self.lv_width}×{self.lv_height} px · "
            f"1 pixel proudu = {interp:.2f} px senzoru{crop}"
        )


def detail_for(
    rate: int,
    lv_width: int,
    lv_height: int,
    sensor: SensorSize = SensorSize(),
) -> StreamDetail:
    """Real detail of the stream at one body-side zoom rate.

    An unknown rate is treated as whole-frame rather than guessed at, because
    over-claiming detail is the failure mode that would mislead focusing.
    """
    whole = sensor.width / max(lv_width, 1)
    factor = BODY_ZOOM_SENSOR_PX_PER_LV_PX.get(rate, None)
    if factor is None:
        return StreamDetail(rate, lv_width, lv_height, whole, 1.0)
    return StreamDetail(
        rate=rate,
        lv_width=lv_width,
        lv_height=lv_height,
        sensor_px_per_lv_px=factor,
        crop_fraction=min(1.0, factor / whole) if whole else 1.0,
    )


def choose_body_rate(
    zoom: float,
    widget_width: int,
    widget_height: int,
    lv_width: int,
    lv_height: int,
    sensor: SensorSize = SensorSize(),
    available_rates: tuple[int, ...] | None = None,
) -> int:
    """Which body-side zoom rate serves ``zoom`` with the least crop.

    Small display scales (Fit and the low percentages) want the whole frame —
    cropping away 75 % of it to sharpen a fit-to-window overview would be
    worse, not better. Once the requested scale implies more than
    :data:`HONEST_INTERPOLATION_LIMIT` upscaling from the whole-frame stream,
    the crop is what buys the detail and is worth taking: pick the shallowest
    rate that keeps interpolation at or under that limit, so the visible crop
    stays as large as possible.
    """
    rates = available_rates or tuple(BODY_ZOOM_SENSOR_PX_PER_LV_PX)
    if zoom == FIT:
        return ZOOM_ALL
    # Operator-visible rule ("nedělej ořez"): below 1:1 nobody is judging
    # grain, they are judging the frame — and the body's zoomed stream is a
    # 43%x48% window at best. Interpolating an overview is honest; cutting off
    # the picture is not.
    if zoom < MIN_ZOOM_FOR_BODY_CROP:
        return ZOOM_ALL if ZOOM_ALL in rates else max(rates)
    whole = detail_for(ZOOM_ALL, lv_width, lv_height, sensor)
    # If the whole-frame stream already serves the request without inventing
    # more than the limit, never crop: overview scales want to see the frame.
    if whole.interpolation_at(zoom) <= HONEST_INTERPOLATION_LIMIT:
        return ZOOM_ALL if ZOOM_ALL in rates else max(rates)
    candidates = [
        r for r in rates
        if detail_for(r, lv_width, lv_height, sensor).interpolation_at(zoom)
        <= HONEST_INTERPOLATION_LIMIT
    ]
    if not candidates:
        # Nothing serves the request: give the deepest crop available, which is
        # the closest to honest, and let the UI flag the invention.
        return max(rates)
    # Best == delivered pixels landing nearest one-per-screen-pixel (native,
    # neither stretched nor averaged away), i.e. min |log2(interpolation)|.
    # At an exact tie the larger visible crop (shallower rate) wins.
    return min(
        candidates,
        key=lambda r: (
            abs(math.log2(detail_for(r, lv_width, lv_height, sensor)
                                .interpolation_at(zoom) or 1e-9)),
            -detail_for(r, lv_width, lv_height, sensor).crop_fraction,
        ),
    )


def display_scale(
    zoom: float, sensor: SensorSize, widget_width: int, widget_height: int
) -> float:
    """Resolve :data:`FIT` into a screen-pixels-per-sensor-pixel number."""
    if zoom != FIT:
        return zoom
    return sensor.fit_scale(widget_width, widget_height)


def is_honest(zoom: float, detail: StreamDetail,
              sensor: SensorSize = SensorSize(),
              widget_width: int = 0, widget_height: int = 0) -> bool:
    """True when the requested scale is met without inventing pixels."""
    return detail.interpolation_at(
        display_scale(zoom, sensor, widget_width, widget_height)
    ) <= HONEST_INTERPOLATION_LIMIT
