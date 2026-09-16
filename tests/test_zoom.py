"""The sensor-pixel zoom model.

These numbers are the promises the UI makes to the operator, so they are
pinned here rather than eyeballed: "100 %" must mean one screen pixel per NEF
pixel, and every honest-detail claim must follow the delivered stream, not the
widget size.
"""

from __future__ import annotations

import pytest

from filmscan_studio.core.zoom import (
    FIT,
    ZOOM_100,
    ZOOM_200,
    ZOOM_50,
    ZOOM_ALL,
    SensorSize,
    StreamDetail,
    choose_body_rate,
    detail_for,
    display_scale,
)

D750 = SensorSize(6016, 4016)


class TestSensorSize:
    def test_fit_is_the_smaller_ratio(self):
        # 2000x1000 widget, 6016x4016 sensor -> height binds.
        assert D750.fit_scale(2000, 1000) == pytest.approx(1000 / 4016)

    def test_rejects_garbage(self):
        with pytest.raises(ValueError):
            SensorSize(0, 100)


class TestStreamDetail:
    def test_whole_frame_maps_lv_px_to_sensor_px(self):
        detail = detail_for(ZOOM_ALL, 640, 424, D750)
        # 6016/640 ≈ 9.4 sensor pixels per delivered pixel — the number the
        # old UI lied about when it called the fit view "100%".
        assert detail.sensor_px_per_lv_px == pytest.approx(6016 / 640, rel=1e-3)
        assert detail.crop_fraction == 1.0

    def test_body_zoom_100_is_sensor_one_to_one(self):
        detail = detail_for(ZOOM_100, 640, 480, D750)
        assert detail.sensor_px_per_lv_px == 1.0
        assert detail.crop_fraction < 0.15  # a 640px window on a 6016px frame

    def test_body_zoom_200_supersamples(self):
        assert detail_for(ZOOM_200, 640, 480, D750).sensor_px_per_lv_px == 0.5

    def test_interpolation_is_display_scale_times_detail(self):
        detail = detail_for(ZOOM_ALL, 640, 424, D750)
        # At 25% display of a whole-frame stream: 0.25 screen px per sensor px
        # while one source px is worth 9.4 sensor px -> source px lands on
        # ~2.3 screen px: mild upscaling, which the badge must disclose.
        assert detail.interpolation_at(0.25) == pytest.approx(9.4 * 0.25, rel=0.01)
        # Even Fit stretches the 640px stream on any widget wider than ~640px
        # (a 2000px-wide widget: 640 source px spread over 2000 screen px);
        # only a widget narrower than the stream downsamples it.
        assert detail.interpolation_at(2000 / 6016) == pytest.approx(
            2000 / 640, rel=0.01
        )
        assert detail.interpolation_at(600 / 6016) < 1.0

    def test_unknown_rate_treated_as_whole_frame(self):
        detail = detail_for(99, 640, 424, D750)
        assert detail.sensor_px_per_lv_px == pytest.approx(6016 / 640, rel=1e-3)


class TestBodyRateChoice:
    def test_fit_never_crops(self):
        assert choose_body_rate(FIT, 1200, 800, 640, 424, D750) == ZOOM_ALL

    def test_low_percentages_keep_whole_frame(self):
        # 12.5% of sensor: 640 px stream covers the whole frame at ~1.2x
        # upscale — cropping away 94% of the frame would not buy detail worth
        # its cost, so the body stays at Whole.
        assert choose_body_rate(0.125, 1200, 800, 640, 424, D750) == ZOOM_ALL

    def test_half_scale_still_shows_whole_frame(self):
        """2026-09 rule ('nedělej ořez'): below 1:1 nobody judges grain —
        the body's shallowest crop is a ~43%x48% window (measured: zoomed
        rates deliver 640x480), and cutting the frame off reads as a bug.
        Interpolating an overview is the honest trade; crop only at 1:1+."""
        assert choose_body_rate(0.5, 1200, 800, 640, 424, D750) == ZOOM_ALL

    def test_full_1to1_picks_the_native_rate(self):
        # Display 1.0 wants delivered pixels that land one-per-screen-pixel:
        # body 100% (1 sensor px per source px) is exactly native; 200% would
        # downsample, 50% would upscale — both discard or invent what 100% has.
        assert choose_body_rate(1.0, 1200, 800, 640, 424, D750) == ZOOM_100

    def test_200pct_display_uses_the_deepest_crop(self):
        assert choose_body_rate(2.0, 1200, 800, 640, 424, D750) == ZOOM_200

    def test_cropped_rate_still_native_at_1to1(self):
        # The crop selection itself is unchanged at >= 1:1: body 100% is
        # native, and a shallower crop that served the scale natively would
        # win if one existed (it does not at 1.0 display).
        assert choose_body_rate(1.0, 1200, 800, 640, 424, D750,
                                available_rates=(ZOOM_ALL, ZOOM_50, ZOOM_100)) \
            == ZOOM_100

    def test_respects_available_rates(self):
        rate = choose_body_rate(1.0, 1200, 800, 640, 424, D750,
                                available_rates=(ZOOM_ALL, ZOOM_100))
        assert rate == ZOOM_100  # best available, even if beyond the limit


class TestDisplayScale:
    def test_named_scales_pass_through(self):
        assert display_scale(0.25, D750, 1200, 800) == 0.25

    def test_fit_resolves_to_widget(self):
        # Height binds on a 2008x803 widget showing a 3:2 frame.
        assert display_scale(FIT, D750, 2008, 803) == pytest.approx(803 / 4016)


def test_detail_summary_mentions_sizes_and_crop():
    detail: StreamDetail = detail_for(ZOOM_100, 640, 480, D750)
    text = detail.summary()
    assert "640×480" in text
    assert "1.00 px senzoru" in text
    assert "výřez" in text
