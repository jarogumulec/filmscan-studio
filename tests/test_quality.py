"""Post-capture audit (capture.quality), fast LUT preview and locked WB.

The audit is tested against synthetic "NEFs": a monkeypatched frame reader
keeps the suite free of real rawpy reads while still exercising the rect
mapping, the verdict branches and the ISO-100-only shutter correction — the
logic the operator trusts after every scan.
"""

from __future__ import annotations

import numpy as np
import pytest

from filmscan_studio.capture import quality
from filmscan_studio.capture.camera import CaptureResult
from filmscan_studio.capture.quality import audit_nef, render_preview_jpeg
from filmscan_studio.core.exposure import ExposureSettings
from filmscan_studio.core.positive import (
    FastPositivePreview,
    PositiveParams,
    apply_wb,
    estimate_wb_gains,
    to_working_positive,
)
from filmscan_studio.core.rawio import RawFrame
from filmscan_studio.core.zoom import ZOOM_13, ZOOM_17, detail_for


def _fake_frame(monkeypatch, data: np.ndarray,
                black: float = 600.0, white: float = 16383.0) -> None:
    frame = RawFrame(path=None, data=data, black_level=black, white_level=white,
                     color_desc="RGGB", width=data.shape[1], height=data.shape[0],
                     acquisition=None)  # type: ignore[arg-type]
    monkeypatch.setattr(quality, "open_frame", lambda _p: frame)


def _settings(iso: int = 100, shutter: float = 1.0) -> ExposureSettings:
    return ExposureSettings(shutter=shutter, iso=iso, aperture=8.0)


LADDER = [1 / 8, 1 / 4, 1 / 2, 1.0, 2.0, 4.0]


class TestAudit:
    def test_well_exposed_passes(self, monkeypatch, tmp_path):
        # Half the frame at the target (0.4 EV headroom) puts p99.9 on it —
        # a 0.1% patch would not: percentile interpolation would still read
        # the background.
        from filmscan_studio.core.exposure import signal_at_target
        data = np.full((200, 300), 5000.0)
        data[:100, :] = signal_at_target(0.4, 600.0, 16383.0)
        _fake_frame(monkeypatch, data)
        result = audit_nef(tmp_path / "f.NEF", None, (640, 424), _settings(),
                           shutter_ladder=LADDER)
        assert result.verdict == "ok"
        assert not result.needs_action

    def test_clipped_frame_is_over_with_negative_suggestion(
        self, monkeypatch, tmp_path
    ):
        # 1/3-stop ladder: a whole-stop ladder would snap the small corrective
        # move back onto the current shutter and prove nothing.
        fine = [2.0 ** (-i / 3) for i in range(4, 13)]
        data = np.full((300, 300), 16000.0)
        data[0, :10] = 16383.0            # 0.01% blown — over the tolerance
        _fake_frame(monkeypatch, data)
        result = audit_nef(tmp_path / "f.NEF", None, (640, 424),
                           _settings(shutter=1.0), shutter_ladder=fine)
        assert result.verdict == "over"
        assert result.ev_change < 0
        # Shorter exposure: a suggestion exists, on the ladder, below current.
        assert result.suggested_shutter is not None
        assert result.suggested_shutter < 1.0
        assert result.suggested_shutter in fine

    def test_dark_frame_is_under_with_longer_shutter(self, monkeypatch, tmp_path):
        data = np.full((200, 300), 1200.0)
        _fake_frame(monkeypatch, data)
        result = audit_nef(tmp_path / "f.NEF", None, (640, 424), _settings(),
                           shutter_ladder=LADDER)
        assert result.verdict == "under"
        assert result.suggested_shutter is not None
        assert result.suggested_shutter > 1.0

    def test_no_suggestion_at_foreign_iso(self, monkeypatch, tmp_path):
        """The shutter suggestion is only valid at the archival ISO — at ISO
        400 the settings the number was computed against are meaningless."""
        data = np.full((200, 300), 1200.0)
        _fake_frame(monkeypatch, data)
        result = audit_nef(tmp_path / "f.NEF", None, (640, 424),
                           _settings(iso=400), shutter_ladder=LADDER)
        assert result.verdict == "under"
        assert result.suggested_shutter is None

    def test_ae_rect_meters_only_the_rect(self, monkeypatch, tmp_path):
        """The rect must drive the verdict — and sit where it says it does.

        Fake NEF at 640x424 so stream→sensor mapping is 1:1. A blown patch at
        rows 120:240, cols 160:320 is inside rect (160,120)-(320,240) and
        outside the control rect (0,0,100,100). Same file: blown verdict from
        the first rect, ok verdict from the second — proving both that the
        rect is honoured and that it landed on the right pixels.
        """
        from filmscan_studio.core.exposure import signal_at_target
        data = np.full((424, 640), signal_at_target(0.4, 600.0, 16383.0))
        data[120:240, 160:320] = 16383.0
        _fake_frame(monkeypatch, data)
        in_patch = audit_nef(tmp_path / "f.NEF", (160, 120, 320, 240),
                             (640, 424), _settings(), shutter_ladder=LADDER)
        assert in_patch.verdict == "over"
        outside = audit_nef(tmp_path / "f.NEF", (0, 0, 100, 100),
                            (640, 424), _settings(), shutter_ladder=LADDER)
        assert outside.verdict == "ok"

    def test_tiny_rect_degenerates_to_whole_frame(self, monkeypatch, tmp_path):
        # A rect rounding to zero sensor pixels must meter the frame rather
        # than crash on an empty array.
        data = np.full((400, 600), 1200.0)
        _fake_frame(monkeypatch, data)
        result = audit_nef(tmp_path / "f.NEF", (0, 0, 1, 1), (640, 424),
                           _settings(), shutter_ladder=LADDER)
        assert result.verdict == "under"


class TestPreviewJpeg:
    def test_renders_from_raw_with_the_preview_chain(self, monkeypatch, tmp_path):
        import cv2

        # Linear RGB "demosaic": mostly dark (dense dyes) with a bright base
        # corner — the luminance structure of a negative.
        rgb = np.zeros((200, 300, 3))
        rgb[:] = (0.1, 0.08, 0.06)
        rgb[:40, :40] = 0.9
        monkeypatch.setattr(quality, "demosaic_linear",
                            lambda p, half_size=True, use_camera_wb=True: rgb)
        out = tmp_path / "f.jpg"
        written = render_preview_jpeg(tmp_path / "f.NEF", out, PositiveParams())
        assert written == out and out.exists()
        got = cv2.imread(str(out))
        assert got is not None and got.shape[1] <= 1600
        # Inverted: the bright base corner became the *dark* corner.
        assert got[:20, :20].mean() < got[100:180, 100:280].mean()


class TestFastPreview:
    def test_lut_matches_exact_chain_at_a_shared_base(self):
        """Same base in, quantisation-only difference out.

        The fast path measures the base on *luminance* (the deliberate design
        — WB-corrected luminance is the domain the LUT subtracts in), while
        the exact chain percentiles all channels; with base_level pinned to the
        value the fast path found, both evaluate the identical per-channel
        map, and only LUT quantisation may separate them.
        """
        from dataclasses import replace

        rng = np.random.default_rng(3)
        rgb = np.clip(rng.random((64, 96, 3)) * 0.9, 0, 1)
        params = PositiveParams(exposure_ev=0.5, base_percentile=97.0)
        probe = FastPositivePreview(params)
        probe.render(rgb)                    # measures the base
        pinned = replace(params, base_level=probe.base_level)
        got = FastPositivePreview(pinned).render(rgb)
        want = to_working_positive(rgb.astype(np.float64), 0.0, 1.0, pinned)
        # One LUT step of input error (1/4096), amplified by the steepest
        # local slope of exposure x filmic (measured ~2.6 at these params).
        assert np.abs(got - want).max() < 4.0 / 4096

    def test_wb_gains_change_the_lut(self):
        rgb = np.full((16, 16, 3), 0.5)
        plain = FastPositivePreview(PositiveParams()).render(rgb)
        warm = FastPositivePreview(
            PositiveParams(wb_gains=(0.8, 1.0, 1.2))).render(rgb)
        assert not np.allclose(plain, warm)

    def test_base_holds_between_refreshes(self):
        params = PositiveParams()
        fast = FastPositivePreview(params, base_refresh=4)
        rgb = np.full((16, 16, 3), 0.6)
        fast.render(rgb)
        first = fast.base_level
        brighter = np.full((16, 16, 3), 0.9)
        for _ in range(3):
            fast.render(brighter)
        assert fast.base_level == first      # held, no breathing
        fast.render(brighter)                # 4th frame re-measures
        assert fast.base_level != first


class TestWhiteBalance:
    def test_gains_neutralise_an_orange_cast(self):
        # Orange light on a white base: R > G > B.
        rgb = np.full((32, 32, 3), (0.9, 0.6, 0.4))
        gains = estimate_wb_gains(rgb)
        corrected = apply_wb(rgb, gains)
        # Green-referenced gains equalise the channels.
        assert abs(corrected[0, 0, 0] - corrected[0, 0, 1]) < 1e-9
        assert abs(corrected[0, 0, 1] - corrected[0, 0, 2]) < 1e-9
        assert gains[1] == 1.0

    def test_gains_centre_on_green_no_brightness_jump(self):
        rgb = np.full((16, 16, 3), 0.5)
        gains = estimate_wb_gains(rgb)
        assert gains == (1.0, 1.0, 1.0)      # already neutral input


class TestZoomEnumExtension:
    """SDK enum values 7 (13 %) and 8 (17 %) exist on the D750; the table
    reads them shallower than 25 %, between Whole and 25 % magnification."""

    def test_new_rates_are_shallower_than_25(self):
        d13 = detail_for(ZOOM_13, 640, 424)
        d17 = detail_for(ZOOM_17, 640, 424)
        whole = detail_for(0, 640, 424)
        assert whole.sensor_px_per_lv_px > d13.sensor_px_per_lv_px
        assert d13.sensor_px_per_lv_px > d17.sensor_px_per_lv_px
        assert d17.sensor_px_per_lv_px > detail_for(1, 640, 424).sensor_px_per_lv_px
        assert d13.crop_fraction < 1.0

    def test_capture_result_flags_lv_cycle(self):
        # The mirror-up contract: the flag exists and defaults to "kept".
        r = CaptureResult(path=None, size_bytes=0, settings=_settings())  # type: ignore[arg-type]
        assert r.lv_cycled is False
