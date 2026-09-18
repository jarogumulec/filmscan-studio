"""Post-capture audit (capture.quality) and the fast LUT preview.

The audit is tested against real TIFFs written by ``write_frame`` — the same
16-bit mono archive files the backend writes — so the read path
(``rawio.open_frame``) is exercised, not mocked, while the small grids keep
the suite quick.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from filmscan_studio.capture.quality import AuditResult, audit_frame, render_preview_jpeg
from filmscan_studio.core.exposure import ExposureSettings, signal_at_target
from filmscan_studio.core.positive import (
    FastPositivePreview,
    PositiveParams,
    apply_wb,
    estimate_wb_gains,
    to_working_positive,
)
from filmscan_studio.core.rawio import write_frame

TARGET_DN = signal_at_target(0.4, 0.0, 65535.0)


def _tiff(tmp_path: Path, data: np.ndarray, name: str = "f.tif") -> Path:
    path = tmp_path / name
    write_frame(path, data.astype(np.uint16), black_level=0.0,
                white_level=65535.0)
    return path


def _settings(shutter: float = 1.0, gain: float = 1.0) -> ExposureSettings:
    return ExposureSettings(shutter=shutter, iso=None, gain=gain)


LADDER = [1 / 8, 1 / 4, 1 / 2, 1.0, 2.0, 4.0]


class TestAudit:
    def test_well_exposed_passes(self, tmp_path):
        # Half the frame at the target (0.4 EV headroom) puts p99.9 on it.
        data = np.full((200, 300), 3000.0)
        data[:100, :] = TARGET_DN
        result = audit_frame(_tiff(tmp_path, data), None, (640, 424),
                             _settings(), shutter_ladder=LADDER)
        assert result.verdict == "ok"
        assert not result.needs_action

    def test_clipped_frame_is_over_with_negative_ev(self, tmp_path):
        fine = [2.0 ** (-i / 3) for i in range(4, 13)]
        data = np.full((300, 300), 64000.0)
        data[0, :10] = 65535.0              # 0.01% blown — over the tolerance
        result = audit_frame(_tiff(tmp_path, data), None, (640, 424),
                             _settings(shutter=1.0), shutter_ladder=fine)
        assert result.verdict == "over"
        assert result.ev_change < 0
        assert result.suggested_shutter is not None
        assert result.suggested_shutter < 1.0
        assert result.suggested_shutter in fine

    def test_dark_frame_is_under_with_longer_shutter(self, tmp_path):
        data = np.full((200, 300), 3000.0)
        result = audit_frame(_tiff(tmp_path, data), None, (640, 424),
                             _settings(), shutter_ladder=LADDER)
        assert result.verdict == "under"
        assert result.suggested_shutter is not None
        assert result.suggested_shutter > 1.0

    def test_continuous_backend_gets_no_ladder_suggestion(self, tmp_path):
        """No ladder = no rung to suggest: the audit still says by how many EV
        the next frame must move (the GUI turns that into a µs shutter), but
        never invents a shutter on a ladder the camera does not have."""
        data = np.full((200, 300), 3000.0)
        result = audit_frame(_tiff(tmp_path, data), None, (640, 424),
                             _settings(), shutter_ladder=None)
        assert result.verdict == "under"
        assert result.ev_change > 0.25
        assert result.suggested_shutter is None

    def test_ae_rect_meters_only_the_rect(self, tmp_path):
        """The rect must drive the verdict — and sit where it says it does.

        Frame at 640x424 so stream→frame mapping is 1:1. A blown patch at
        rows 120:240, cols 160:320 is inside rect (160,120)-(320,240) and
        outside the control rect (0,0,100,100). Same file: blown verdict from
        the first rect, ok verdict from the second — proving both that the
        rect is honoured and that it landed on the right pixels.
        """
        data = np.full((424, 640), TARGET_DN)
        data[120:240, 160:320] = 65535.0
        path = _tiff(tmp_path, data)
        in_patch = audit_frame(path, (160, 120, 320, 240), (640, 424),
                               _settings(), shutter_ladder=LADDER)
        assert in_patch.verdict == "over"
        outside = audit_frame(path, (0, 0, 100, 100), (640, 424),
                              _settings(), shutter_ladder=LADDER)
        assert outside.verdict == "ok"

    def test_rect_maps_through_the_binned_overview(self, tmp_path):
        """Stream coordinates are overview px: a rect at the frame's right
        half must land on the frame's right half after the scale by 3."""
        data = np.full((200, 600), TARGET_DN)
        data[:, 300:] = 65535.0             # right half blown
        path = _tiff(tmp_path, data)
        # Overview of a 600-px-wide sensor is 200 px; the blown right half is
        # overview cols 100..200.
        right = audit_frame(path, (100, 0, 200, 133), (200, 133),
                            _settings(), shutter_ladder=LADDER)
        left = audit_frame(path, (0, 0, 90, 133), (200, 133),
                           _settings(), shutter_ladder=LADDER)
        assert right.verdict == "over"
        assert left.verdict == "ok"

    def test_tiny_rect_degenerates_to_whole_frame(self, tmp_path):
        # A rect rounding to zero sensor pixels must meter the frame rather
        # than crash on an empty array.
        data = np.full((400, 600), 3000.0)
        result = audit_frame(_tiff(tmp_path, data), (0, 0, 1, 1), (6400, 4240),
                             _settings(), shutter_ladder=LADDER)
        assert result.verdict == "under"

    def test_message_names_the_scope(self, tmp_path):
        data = np.full((200, 300), TARGET_DN)
        result = audit_frame(_tiff(tmp_path, data), (0, 0, 200, 200),
                             (300, 200), _settings())
        assert isinstance(result, AuditResult)
        assert "AE výřez" in result.message


class TestPreviewJpeg:
    def test_renders_from_raw_with_the_preview_chain(self, tmp_path):
        import cv2

        # Mono negative: mostly dense (dark) emulsion with a bright base
        # corner — the luminance structure of a film frame.
        data = np.full((200, 300), 0.10 * 65535.0)
        data[:40, :40] = 0.9 * 65535.0
        out = tmp_path / "f.jpg"
        written = render_preview_jpeg(_tiff(tmp_path, data), out,
                                      PositiveParams())
        assert written == out and out.exists()
        got = cv2.imread(str(out))
        assert got is not None and got.shape[1] <= 1600
        # Inverted: the bright base corner became the *dark* corner.
        assert got[:20, :20].mean() < got[100:180, 100:280].mean()

    def test_downsamples_wide_frames(self, tmp_path):
        import cv2

        data = np.full((100, 4000), 20000.0)
        out = tmp_path / "wide.jpg"
        render_preview_jpeg(_tiff(tmp_path, data, "wide.tif"), out,
                            PositiveParams(), max_width=1600)
        got = cv2.imread(str(out))
        assert got.shape[1] == 1600 and got.shape[0] == 40


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

    def test_grey_input_renders_repeated_rgb(self):
        """Mono stream frames are their own one-channel picture."""
        grey = np.full((16, 16), 0.6)
        got = FastPositivePreview(PositiveParams(base_level=0.1)).render(grey)
        assert got.ndim == 3 and got.shape[2] == 3
        assert np.allclose(got[:, :, 0], got[:, :, 1])
        assert np.allclose(got[:, :, 1], got[:, :, 2])

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
    """WB survives as a colour-film preview tool (the mono stream itself has
    no channels — these test the positive chain, not the capture path)."""

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
