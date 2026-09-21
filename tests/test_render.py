"""Tests for the density-domain rendering layer."""

from __future__ import annotations

import numpy as np
import pytest

from filmscan_studio.core import render
from filmscan_studio.core.filmic import FilmicProfile


@pytest.fixture
def params() -> render.RenderParams:
    # shadow_band=0: these are axis tests (base -> black, dmax -> white);
    # the default band shifts base off zero on purpose -- see TestShadowBand.
    return render.RenderParams(dmin=0.2, dmax=2.6, exposure_ev=0.0,
                               profile=FilmicProfile.neutral(),
                               shadow_band=0.0)


class TestRenderParams:
    def test_dmax_must_exceed_dmin(self) -> None:
        with pytest.raises(ValueError, match="must exceed"):
            render.RenderParams(dmin=2.0, dmax=1.0)

    def test_fingerprint_changes_with_every_field(self) -> None:
        base = render.RenderParams()
        variants = [
            base,
            render.RenderParams(dmin=0.21),
            render.RenderParams(dmax=2.7),
            render.RenderParams(exposure_ev=0.3),
            render.RenderParams(profile=FilmicProfile(toe=0.5)),
            render.RenderParams(dmax_source="film"),
        ]
        fps = [v.fingerprint() for v in variants]
        assert len(set(fps)) == len(fps)

    def test_dict_round_trip(self) -> None:
        p = render.RenderParams(dmin=0.52, dmax=3.1, exposure_ev=-0.7,
                                profile=FilmicProfile(toe=0.2, gamma=1.3,
                                                      shoulder=0.6),
                                dmax_source="film")
        assert render.RenderParams.from_dict(p.to_dict()) == p


class TestRenderDensity:
    def test_base_maps_black_dmax_white(self, params) -> None:
        # Positive of a negative: clear base (least dye) -> black print;
        # dmax (most dye, brightest scene) -> white print.
        d = np.array([[params.dmin, params.dmax]], dtype=np.float32)
        out = render.render_density(d, params)
        # float32 0.2 is not float64 0.2; the map's knots are pinned exactly,
        # the tolerance is the float32 storage of the test density.
        assert out[0, 0] == pytest.approx(0.0, abs=1e-7)   # base -> black
        assert out[0, 1] == pytest.approx(1.0, abs=1e-7)   # dmax -> white

    def test_nan_passes_through(self, params) -> None:
        d = np.array([[0.5, np.nan]], dtype=np.float32)
        out = render.render_density(d, params)
        assert np.isfinite(out[0, 0])
        assert np.isnan(out[0, 1])

    def test_infinite_density_clips_to_scale_ends(self, params) -> None:
        # Too dense to measure (+inf) -> top of the scale (white); a clipped
        # sensor (-inf) -> bottom (black). Neither is NaN: their direction IS
        # known, only their value isn't.
        d = np.array([[np.inf, -np.inf, np.nan]], dtype=np.float32)
        out = render.render_density(d, params)[0]
        assert out[0] == pytest.approx(1.0)
        assert out[1] == pytest.approx(0.0)
        assert np.isnan(out[2])

    def test_positive_density_order_follows_density(self, params) -> None:
        # A denser (D larger) pixel came from a brighter scene -> brighter
        # positive. (Rendering D inverted is the negative again.)
        d = np.array([[0.4, 1.0, 2.0]], dtype=np.float32)
        out = render.render_density(d, params)[0]
        assert out[0] < out[1] < out[2]

    def test_exposure_positive_brightens(self, params) -> None:
        d = np.full((3, 3), 1.5, dtype=np.float32)
        a = render.render_density(d, params)[1, 1]
        b = render.render_density(d, params.with_exposure(1.0))[1, 1]
        assert b > a

    def test_one_stop_shifts_net_density_by_log10_2(self, params) -> None:
        # +1 ev == moving both scale points down by one stop of density:
        # the same net-density axis, hence an identical render.
        d = np.full((1, 1), 1.0, dtype=np.float32)
        shifted = render.RenderParams(dmin=0.2 - render.D_PER_STOP,
                                      dmax=2.6 - render.D_PER_STOP,
                                      profile=FilmicProfile.neutral(),
                                      shadow_band=0.0)
        assert render.render_density(d, params.with_exposure(1.0))[0, 0] == \
            pytest.approx(render.render_density(d, shifted)[0, 0], abs=1e-12)

    def test_below_base_clips_to_black_above_dmax_to_white(self, params) -> None:
        # Negative net density (lamp drift / fog below measured base) exists
        # in real frames (B1 finding) -- it must clip, not go <0.
        d = np.array([[0.0, 4.0]], dtype=np.float32)
        out = render.render_density(d, params)
        assert out[0, 0] == 0.0 and out[0, 1] == 1.0

    def test_monotone_under_real_profile(self) -> None:
        p = render.RenderParams(dmin=0.2, dmax=2.6,
                                profile=FilmicProfile())  # toe+shoulder+gamma
        d = np.linspace(0.2, 2.6, 512, dtype=np.float32)
        out = render.render_density(d, p)
        assert np.all(np.diff(out) >= -1e-12)  # non-decreasing: D up -> brighter


class TestFlatRenderAndQuantise:
    def test_flat_is_linear_in_density(self) -> None:
        p = render.RenderParams(dmin=0.0, dmax=2.0)
        d = np.array([[0.5, 1.0, 1.5]], dtype=np.float32)
        out = render.render_flat(d, p)[0]
        assert np.allclose(np.diff(out), out[1] - out[0])

    def test_quantise_nan_to_black(self) -> None:
        img = np.array([[0.5, np.nan]])
        q = render.quantise16(img)
        assert q[0, 0] == pytest.approx(32768, abs=1)
        assert q[0, 1] == 0

    def test_display_gamma_preserves_nan(self, params) -> None:
        d = np.array([[1.0, np.nan]], dtype=np.float32)
        out = render.render_for_display(d, params)
        assert 0.0 < out[0, 0] < 1.0
        assert np.isnan(out[0, 1])


class TestDisplayTransfer:
    """The last step: gamma_display, shared by preview/export."""

    def test_gamma_display_default_is_2_2(self) -> None:
        assert render.RenderParams().gamma_display == pytest.approx(2.2)

    def test_transfer_formula(self) -> None:
        p = render.RenderParams(gamma_display=2.0, shadow_band=0.0)
        out = render.apply_display(np.array([0.0, 0.25, 1.0]), p)
        assert out[0] == pytest.approx(0.0)
        assert out[1] == pytest.approx(0.5)                        # sqrt
        assert out[2] == pytest.approx(1.0)                        # white pinned

    def test_gamma_one_is_identity(self) -> None:
        p = render.RenderParams(gamma_display=1.0)
        x = np.linspace(0.0, 1.0, 257)
        assert np.allclose(render.apply_display(x, p), x)

    def test_display_transfer_is_monotone(self, params) -> None:
        d = np.linspace(params.dmin, params.dmax, 512, dtype=np.float32)
        out = render.render_for_display(d, params)
        assert np.all(np.diff(out) >= -1e-12)

    def test_fingerprint_covers_display_fields(self) -> None:
        base = render.RenderParams()
        assert (render.RenderParams(gamma_display=2.0).fingerprint()
                != base.fingerprint())
        assert (render.RenderParams(shadow_band=0.0).fingerprint()
                != base.fingerprint())

    def test_legacy_dict_without_display_fields(self) -> None:
        """Settings saved before the display knobs existed must load."""
        legacy = {"dmin": 0.2, "dmax": 2.6, "exposure_ev": 0.0,
                  "name": "neutral", "toe": 0.0, "gamma": 1.0,
                  "shoulder": 0.0}
        p = render.RenderParams.from_dict(legacy)
        assert p.gamma_display == pytest.approx(2.2)
        assert p.shadow_band == pytest.approx(0.01)


class TestBrightnessContrast:
    """Viewer-side knobs in DISPLAY space, deliberately after the gamma.

    Pre-gamma brightness would ramp through the density domain -- that is
    exposure_ev's job. These are Photoshop-curve knobs on displayed pixels.
    """

    def test_defaults_neutral(self) -> None:
        p = render.RenderParams()
        assert p.brightness == 0.0 and p.contrast == 1.0

    def test_identity_when_neutral(self) -> None:
        p = render.RenderParams(gamma_display=1.0)
        x = np.linspace(0.0, 1.0, 257)
        assert np.allclose(render.apply_display(x, p), x)

    def test_brightness_shifts_display_not_density(self) -> None:
        p = render.RenderParams(gamma_display=1.0, brightness=0.1)
        out = render.apply_display(np.array([0.0, 0.5, 0.89]), p)
        assert np.allclose(out, [0.1, 0.6, 0.99])
        # clipped at the top -- brightness must not exceed white
        assert render.apply_display(np.array([1.0]), p)[0] == 1.0

    def test_contrast_pivots_at_display_midgrey(self) -> None:
        p = render.RenderParams(gamma_display=1.0, contrast=2.0)
        out = render.apply_display(np.array([0.25, 0.5, 0.75]), p)
        assert np.allclose(out, [0.0, 0.5, 1.0])
        # pivot stays put: more contrast must not move middle grey
        assert out[1] == pytest.approx(0.5)

    def test_nan_survives_knobs(self) -> None:
        p = render.RenderParams(brightness=0.2, contrast=1.5)
        out = render.apply_display(np.array([0.4, np.nan]), p)
        assert np.isfinite(out[0]) and np.isnan(out[1])

    def test_fingerprint_and_roundtrip_cover_them(self) -> None:
        base = render.RenderParams()
        assert (render.RenderParams(brightness=0.1).fingerprint()
                != base.fingerprint())
        assert (render.RenderParams(contrast=1.5).fingerprint()
                != base.fingerprint())
        p = render.RenderParams(brightness=-0.2, contrast=1.35)
        assert render.RenderParams.from_dict(p.to_dict()) == p

    def test_validates_range(self) -> None:
        with pytest.raises(ValueError):
            render.RenderParams(brightness=0.6)
        with pytest.raises(ValueError):
            render.RenderParams(contrast=0.0)


class TestShadowBand:
    """Shadow separation BEFORE the curve: widening the domain under base.

    The rejected alternative -- a paper-black lift AFTER the curve -- could
    not unfuse clipped shadows: every clipped pixel maps to the same value.
    Widening the domain keeps the gradient that physically exists between
    the sensor floor and the measured base.
    """

    def test_default_is_small(self) -> None:
        # The display gamma lifts small values hard; keep the band a
        # darkroom floor, not a milky black.
        assert render.RenderParams().shadow_band == pytest.approx(0.01)

    def test_base_lifts_off_zero(self) -> None:
        p = render.RenderParams(dmin=0.2, dmax=2.6, shadow_band=0.1)
        d = np.array([[0.2]], dtype=np.float64)
        out = render.render_density(d, p)
        assert 0.0 < out[0, 0] < 0.1

    def test_sub_base_gradient_survives(self) -> None:
        # Densities below base (fog, lamp drift): with a band they render as
        # distinct dark tones; with band 0 all of them fuse to one 0.
        d = np.array([[0.0, 0.1, 0.2]], dtype=np.float64)
        fused = render.render_density(d, render.RenderParams(
            dmin=0.2, dmax=2.6, shadow_band=0.0))
        kept = render.render_density(d, render.RenderParams(
            dmin=0.2, dmax=2.6, shadow_band=0.2))
        assert fused[0, 0] == fused[0, 1] == fused[0, 2] == 0.0
        assert kept[0, 0] < kept[0, 1] < kept[0, 2]

    def test_band_preserves_dmax_white(self) -> None:
        p = render.RenderParams(dmin=0.2, dmax=2.6, shadow_band=0.1)
        out = render.render_density(np.array([[2.6]], dtype=np.float64), p)
        assert out[0, 0] == pytest.approx(1.0)
