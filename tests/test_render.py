"""Tests for the density-domain rendering layer."""

from __future__ import annotations

import numpy as np
import pytest

from filmscan_studio.core import render
from filmscan_studio.core.filmic import FilmicProfile


@pytest.fixture
def params() -> render.RenderParams:
    return render.RenderParams(dmin=0.2, dmax=2.6, exposure_ev=0.0,
                               profile=FilmicProfile.neutral())


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
                                      profile=FilmicProfile.neutral())
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
