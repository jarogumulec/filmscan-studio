"""The sensor-pixel zoom model for a binned-overview + hardware-ROI stream.

These numbers are the promises the UI makes to the operator: "100 %" means
one screen pixel per *sensor* pixel whichever stream delivers it, the 3x3
overview is honest up to 3x display, and past that the sensor itself must
reframe (a 1:1 ROI) rather than have the widget invent detail.
"""

from __future__ import annotations

import pytest

from filmscan_studio.core.zoom import (
    FIT,
    MIN_ROI_PX,
    OVERVIEW_SCALE,
    ZOOM_LEVELS,
    ZOOM_ROI_SIZE,
    Roi,
    SensorSize,
    display_scale,
    overview_detail,
    overview_to_sensor,
    roi_detail,
    roi_for_center,
    stream_plan,
)

IMX571 = SensorSize(6224, 4168)


class TestSensorSize:
    def test_fit_is_the_smaller_ratio(self):
        # 2000x1000 widget, 6224x4168 sensor -> height binds.
        assert IMX571.fit_scale(2000, 1000) == pytest.approx(1000 / 4168)

    def test_rejects_garbage(self):
        with pytest.raises(ValueError):
            SensorSize(0, 100)


class TestRoi:
    def test_rejects_bad_geometry(self):
        with pytest.raises(ValueError):
            Roi(0, 0, 0, 10)
        with pytest.raises(ValueError):
            Roi(-1, 0, 10, 10)

    def test_center(self):
        assert Roi(100, 200, 1200, 1200).center == (700, 800)

    def test_clamped_slides_inside(self):
        # Far-edge overhang slides back; near-edge origin clamp happens in
        # roi_for_center before construction (see its test).
        roi = Roi(6000, 4000, 1200, 1200).clamped(IMX571)
        assert roi.x == IMX571.width - 1200
        assert roi.y == IMX571.height - 1200

    def test_clamped_shrinks_only_if_sensor_smaller(self):
        roi = Roi(0, 0, 9000, 9000).clamped(IMX571)
        assert (roi.width, roi.height) == (IMX571.width, IMX571.height)


class TestRoiForCenter:
    def test_centre_of_sensor(self):
        roi = roi_for_center(3112, 2084, IMX571)
        assert (roi.width, roi.height) == (ZOOM_ROI_SIZE, ZOOM_ROI_SIZE)
        assert abs(roi.center[0] - 3112) <= 1
        assert abs(roi.center[1] - 2084) <= 1

    def test_top_left_aim_does_not_crash(self):
        # Regression (smoke test 2026-09): the view's centre starts at the
        # frame's top-left on a fresh image; a naive sensor_x - half went
        # negative and tripped the Roi guard.
        roi = roi_for_center(30, 20, IMX571)
        assert (roi.x, roi.y) == (0, 0)

    def test_far_edge_slides_inside(self):
        roi = roi_for_center(IMX571.width - 10, IMX571.height - 10, IMX571)
        assert roi.x + roi.width <= IMX571.width
        assert roi.y + roi.height <= IMX571.height
        assert (roi.width, roi.height) == (ZOOM_ROI_SIZE, ZOOM_ROI_SIZE)


class TestStreamPlan:
    def test_fit_and_low_zooms_keep_the_binned_overview(self):
        # 1x/2x display of sensor px: the 3x3 overview already delivers one
        # real sensor px per 3 screen px max here — cropping a ROI would only
        # cut away frame the overview shows honestly.
        assert stream_plan(FIT, IMX571) is None
        assert stream_plan(1.0, IMX571) is None
        assert stream_plan(2.0, IMX571) is None

    def test_beyond_the_overview_scale_asks_for_a_roi(self):
        detail = stream_plan(4.0, IMX571)
        assert detail is not None and not detail.binned
        assert detail.sensor_px_per_lv_px == 1.0
        assert detail.roi.width == ZOOM_ROI_SIZE

    def test_roi_aims_at_the_requested_center(self):
        detail = stream_plan(4.0, IMX571, center_uv=(1000, 500))
        # Overview px -> sensor px is the OVERVIEW_SCALE multiply.
        assert abs(detail.roi.center[0] - 1000 * OVERVIEW_SCALE) <= 1
        assert abs(detail.roi.center[1] - 500 * OVERVIEW_SCALE) <= 1

    def test_default_center_is_the_frame_centre(self):
        detail = stream_plan(4.0, IMX571)
        cx, cy = detail.roi.center
        assert abs(cx - IMX571.width / 2) <= OVERVIEW_SCALE
        assert abs(cy - IMX571.height / 2) <= OVERVIEW_SCALE

    def test_near_edge_center_clamps_instead_of_crashing(self):
        detail = stream_plan(4.0, IMX571, center_uv=(0.0, 0.0))
        assert (detail.roi.x, detail.roi.y) == (0, 0)


class TestOverviewMapping:
    def test_overview_detail(self):
        detail = overview_detail()
        assert detail.is_overview and detail.roi is None
        assert detail.sensor_px_per_lv_px == OVERVIEW_SCALE

    def test_overview_to_sensor_is_the_linear_scale(self):
        assert overview_to_sensor(100, 200) == (300, 600)

    def test_roi_detail_is_one_to_one(self):
        roi = Roi(1000, 1000, 1200, 1200)
        detail = roi_detail(roi)
        assert not detail.is_overview and detail.roi == roi
        assert detail.sensor_px_per_lv_px == 1.0


class TestDisplayScale:
    def test_named_zooms_are_per_sensor_pixel(self):
        # The combobox label promise: 200 % = two screen px per sensor px,
        # whatever the stream.
        assert display_scale(2.0, IMX571, 1200, 800) == 2.0

    def test_fit_resolves_to_widget(self):
        assert display_scale(FIT, IMX571, 2000, 1000) == pytest.approx(
            IMX571.fit_scale(2000, 1000)
        )


def test_summary_mentions_binning_or_roi():
    assert "3x3 binnig" in overview_detail().summary()
    text = roi_detail(Roi(1000, 1000, 1200, 1200)).summary()
    assert "1200" in text and "1000" in text and "1:1" in text


def test_constants_agree_with_the_sdk_contract():
    # Even-pixel ROI geometry: the SDK refuses odd sizes and sub-64 windows.
    assert ZOOM_ROI_SIZE % 2 == 0 and ZOOM_ROI_SIZE >= MIN_ROI_PX
    assert OVERVIEW_SCALE == 3.0
    assert FIT in ZOOM_LEVELS and 1.0 in ZOOM_LEVELS
