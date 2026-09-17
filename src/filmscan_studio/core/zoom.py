"""Zoom and framing on a sensor with hardware binning and ROI.

The IMX571 (6224x4168) reframes its own stream, so there is no body-side zoom
rate table to query and no crop-factor guessing:

* **Overview** — 3x3 average binning of the whole sensor
  (``6224/3 x 4168/3 = 2074x1389``). Continuous, low USB load, and *linear*
  (binning averages DN, it does not auto-brighten), so it meters honestly.
* **Zoom** — no binning plus a hardware ``ROI`` window: the sensor transmits
  only real pixels 1:1 around the point of interest, so fps stays high because
  the window is small, not because detail was thrown away.

Mapping between the two is exact linear arithmetic: an overview pixel
``(u, v)`` is sensor pixel ``(3u, 3v)`` (offsets omitted — the overview always
starts at the sensor origin), and a zoom ROI at ``(x0, y0)`` shows overview
coordinates ``(x0/3, y0/3)`` at its own origin.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Sentinel zoom meaning "fit the whole frame to the widget".
FIT = 0.0

#: Display zoom steps offered by the zoom combobox, labelled in *sensor* px:
#: 100 % = one screen pixel per sensor pixel, whichever stream delivers it
#: (×3 nearest on the binned overview, ×1 on a ROI). FIT first, then
#: pixel-snapped magnifications.
ZOOM_LEVELS = (FIT, 1.0, 2.0, 4.0)

ZOOM_LABELS = {
    FIT: "Vejít se",
    1.0: "100 % — 1:1 px senzoru",
    2.0: "200 %",
    4.0: "400 %",
}

#: The overview binning value for TOUPCAM_OPTION_BINNING: 0x83 = 3x3 average,
#: bit depth unchanged. 0x01 = no binning (zoom mode).
OVERVIEW_BINNING = 0x83
NO_BINNING = 0x01
#: Linear scale between overview and sensor coordinates.
OVERVIEW_SCALE = 3.0

#: Default zoom window (sensor px) when the camera switches to ROI mode. A
#: ~1.2 Mpx window keeps high fps at 16-bit on USB3 while leaving room to pan.
ZOOM_ROI_SIZE = 1200
#: Hard floor for an ROI window the SDK accepts on any axis.
MIN_ROI_PX = 64


@dataclass(frozen=True)
class Roi:
    """A sensor-pixel window: origin (x, y) and size, full-res coordinates."""

    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("ROI size must be positive")
        if self.x < 0 or self.y < 0:
            raise ValueError("ROI origin must be non-negative")

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.width // 2, self.y + self.height // 2)

    def clamped(self, sensor: "SensorSize") -> "Roi":
        """Slide the window fully inside the sensor (shrinking only if needed)."""
        width = min(self.width, sensor.width)
        height = min(self.height, sensor.height)
        x = max(0, min(self.x, sensor.width - width))
        y = max(0, min(self.y, sensor.height - height))
        return Roi(x, y, width, height)


@dataclass(frozen=True)
class SensorSize:
    """Full-resolution sensor geometry — what 1:1 zoom measures against."""

    width: int = 6224
    height: int = 4168

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("sensor size must be positive")

    def fit_scale(self, widget_w: int, widget_h: int) -> float:
        if widget_w <= 0 or widget_h <= 0:
            return 1.0
        return min(widget_w / self.width, widget_h / self.height)


@dataclass(frozen=True)
class StreamDetail:
    """What the Live View stream currently delivers, in sensor terms."""

    binned: bool
    roi: Roi | None
    #: Sensor pixels one delivered stream pixel stands for (3.0 binned,
    #: 1.0 ROI). Display magnification beyond this interpolates.
    sensor_px_per_lv_px: float

    @property
    def is_overview(self) -> bool:
        return self.binned

    def summary(self) -> str:
        if self.binned:
            return f"overview 3x3 binnig {OVERVIEW_SCALE:g}:1 px proudu"
        assert self.roi is not None
        return (f"ROI {self.roi.width}x{self.roi.height} px "
                f"na [{self.roi.x}, {self.roi.y}] — 1:1 bez binningu")


def overview_detail() -> StreamDetail:
    return StreamDetail(binned=True, roi=None,
                        sensor_px_per_lv_px=OVERVIEW_SCALE)


def roi_detail(roi: Roi) -> StreamDetail:
    return StreamDetail(binned=False, roi=roi, sensor_px_per_lv_px=1.0)


def overview_to_sensor(u: float, v: float) -> tuple[float, float]:
    """Overview stream coordinates -> full-resolution sensor coordinates."""
    return (u * OVERVIEW_SCALE, v * OVERVIEW_SCALE)


def roi_for_center(sensor_x: int, sensor_y: int, sensor: SensorSize,
                   size: int = ZOOM_ROI_SIZE) -> Roi:
    """ROI window of ``size`` px centred on a sensor point, clamped inside."""
    half = size // 2
    # The origin clamp happens *before* constructing the Roi: aiming near the
    # top-left edge puts the nominal corner negative, which the Roi guard
    # would (rightly) refuse — here the aim point is an intention, not a
    # promise; clamped() then slides the window off the far edge.
    return Roi(max(0, sensor_x - half), max(0, sensor_y - half),
               size, size).clamped(sensor)


def stream_plan(zoom: float,
                sensor: SensorSize,
                center_uv: tuple[float, float] | None = None,
                roi_size: int = ZOOM_ROI_SIZE) -> StreamDetail | None:
    """What the stream should be for this view: ``None`` = binned overview,
    otherwise a concrete hardware ROI.

    Rule: keep the binned overview while it is honest on screen — one
    overview pixel stands for 3x3 sensor pixels, so displaying it at up to
    ``OVERVIEW_SCALE``x still shows one real sensor pixel per screen pixel.
    Past that the only honest detail is 1:1 sensor data, i.e. a hardware ROI.
    ``center_uv`` is the current view center in *overview* coordinates (what
    the widget knows); it maps linearly onto the sensor.
    """
    if zoom == FIT or zoom < OVERVIEW_SCALE:
        return None
    if center_uv is None:
        center_uv = (sensor.width / OVERVIEW_SCALE / 2,
                     sensor.height / OVERVIEW_SCALE / 2)
    sx, sy = overview_to_sensor(*center_uv)
    return roi_detail(roi_for_center(int(sx), int(sy), sensor, size=roi_size))


def display_scale(zoom: float, sensor: SensorSize,
                  widget_w: int, widget_h: int) -> float:
    """Effective screen px per *sensor* px for the requested display zoom.

    A labelled zoom is already per sensor px (that is what the combobox
    promises); only Fit resolves against the widget.
    """
    if zoom == FIT:
        return sensor.fit_scale(widget_w, widget_h)
    return zoom
