"""Tests for the measurement layer: transmittance, density, Dmin, archive.

Synthetic data first (known ground truth), then the fixed points measured on
the real B1 project when its folder happens to be on disk.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from filmscan_studio.core import calibration, density
from filmscan_studio.core.filmbase import FilmBaseSample


class TestTransmittance:
    def test_identity_when_scan_equals_flat(self) -> None:
        flat = np.full((4, 6), 40_000.0)
        t = density.transmittance(flat.copy(), flat)
        assert np.allclose(t, 1.0)

    def test_density_of_known_neutral(self) -> None:
        # A film transmitting exactly 1% should read D = 2.000.
        flat = np.full((8, 8), 50_000.0)
        scan = flat * 0.01
        t = density.transmittance(scan, flat)
        d = density.density(t, density.valid_mask(t, flat))
        assert np.allclose(d, 2.0, atol=1e-5)

    def test_shape_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="shape mismatch"):
            density.transmittance(np.zeros((3, 4)), np.ones((4, 3)))


class TestMasks:
    def test_negative_signal_invalid(self) -> None:
        flat = np.full((4, 4), 40_000.0)
        scan = flat.copy()
        scan[1, 1] = -5.0  # dark over-subtraction
        t = density.transmittance(scan, flat)
        mask = density.valid_mask(t, flat)
        assert not mask[1, 1]
        assert mask.sum() == 15

    def test_border_outside_illumination_invalid(self) -> None:
        flat = np.full((20, 20), 50_000.0)
        flat[:5, :] = 100.0  # unlit frame border
        t = np.full((20, 20), 0.4)
        mask = density.valid_mask(t, flat)
        assert not mask[:5, :].any()
        assert mask[5:, :].all()

    def test_saturated_pixels_invalid(self) -> None:
        flat = np.full((4, 4), 40_000.0)
        scan = flat * 0.5
        scan[0, 0] = 65535  # clipped raw
        t = density.transmittance(scan, flat)
        mask = density.valid_mask(t, flat, raw_scan=scan, white_level=65535.0)
        assert not mask[0, 0]

    def test_invalid_pixels_become_nan(self) -> None:
        flat = np.full((4, 4), 40_000.0)
        scan = flat * 0.1
        scan[2, 2] = -1.0
        t = density.transmittance(scan, flat)
        d = density.density(t, density.illuminated_mask(t, flat))
        assert d.dtype == np.float32
        assert np.isposinf(d[2, 2])      # too dense to measure: direction known
        assert np.isclose(d[0, 0], 1.0)

    def test_unlit_pixels_are_nan(self) -> None:
        # Outside the illuminated area nothing was measured at all -> NaN.
        flat = np.full((20, 20), 50_000.0)
        flat[:5, :] = 100.0
        t = np.full((20, 20), 0.4)
        d = density.density(t, density.illuminated_mask(t, flat))
        assert np.isnan(d[:5, :]).all()
        assert np.isclose(d[5:, :], 0.39794, atol=1e-4).all()

    def test_clipped_pixels_become_minus_inf(self) -> None:
        # Saturated raw: the film was *thinner* than recordable, i.e. below
        # any measurable density -- -inf, clipped to the dark end by the
        # renderer, not painted as unknown.
        flat = np.full((4, 4), 40_000.0)
        scan = flat * 0.5
        scan[0, 0] = 65535
        t = density.transmittance(scan, flat)
        clipped = scan >= 65535
        d = density.density(t, density.illuminated_mask(t, flat),
                            clipped=clipped)
        assert np.isneginf(d[0, 0])
        assert np.isclose(d[1, 1], 0.30103, atol=1e-4)


class TestDmin:
    def test_dmin_from_signals_matches_transmittance_definition(self) -> None:
        # Base passes 30 % of the no-film light -> Dmin = 0.523.
        d = density.dmin_from_signals(
            base_mean_dn=30_000, base_black_level=0, base_shutter=1.0,
            base_gain=None,
            flat_mean_dn=100_000, flat_black_level=0, flat_shutter=1.0,
        )
        assert d == pytest.approx(-np.log10(0.3), abs=1e-9)

    def test_dmin_exposure_invariance(self) -> None:
        """Base measured at 0.5 ms against a flat at 0.3 ms: ratio via scaling."""
        same = density.dmin_from_signals(
            base_mean_dn=15_736, base_black_level=0, base_shutter=0.0003,
            base_gain=None,
            flat_mean_dn=52_445, flat_black_level=0, flat_shutter=0.0003,
        )
        # B1 real numbers: base @0.5ms, flat @0.3ms -> same Dmin within the
        # ±0.02 D the two independent measurements differed by.
        cross = density.dmin_from_signals(
            base_mean_dn=26_222, base_black_level=0, base_shutter=0.0005,
            base_gain=None,
            flat_mean_dn=52_445, flat_black_level=0, flat_shutter=0.0003,
        )
        assert same == pytest.approx(cross, abs=0.02)

    def test_measure_dmin_from_sample(self) -> None:
        flat = np.full((40, 40), 30_000.0)  # above-black, flat @0.3 ms
        sample = FilmBaseSample(
            kind="frame", mean_dn=15_736 * 0.5 / 0.3, black_level=0.0,
            white_level=65535.0, shutter=0.0005, gain=1.0,
            rect=(5, 5, 35, 35), source="synthetic",
        )
        d = density.measure_dmin(sample, flat, flat_shutter=0.0003,
                                 flat_gain=1.0)
        # base sig at flat exposure = 15736*0.5/0.3 * 0.3/0.5 = 15736;
        # ratio = 15736/30000
        assert d == pytest.approx(-np.log10(15736 / 30000), abs=1e-6)

    def test_implausible_transmittance_rejected(self) -> None:
        with pytest.raises(ValueError, match="implausible"):
            density.dmin_from_signals(
                base_mean_dn=200_000, base_black_level=0, base_shutter=1.0,
                base_gain=None,
                flat_mean_dn=10_000, flat_black_level=0, flat_shutter=1.0,
            )


class TestDmax:
    def test_percentile_with_margin(self) -> None:
        d = np.linspace(0.5, 2.5, 1001).astype(np.float32)
        d[0] = np.nan  # NaN ignored
        assert density.estimate_dmax(d) == pytest.approx(
            np.percentile(d[1:], 99.9) + 0.05, abs=1e-4)

    def test_all_nan_returns_margin(self) -> None:
        assert density.estimate_dmax(np.full((4, 4), np.nan)) == 0.05


class TestArchive:
    def test_round_trip_preserves_nan_and_provenance(self, tmp_path) -> None:
        d = np.array([[0.5, 1.5], [np.nan, 3.0]], dtype=np.float32)
        prov = density.DensityProvenance(
            source="frame001.tif", shutter=0.001333, gain=1.0,
            black_level=0.0, white_level=65535.0,
            dark_files=("dark_001.tif",), flat_files=("flat_001.tif",),
            dmin_density=0.516, crop_rect=(10, 20, 30, 40),
        )
        p = density.write_density_tiff(tmp_path / "f001.density.tif", d, prov)
        back, meta = density.read_density_tiff(p)
        assert back.dtype == np.float32
        assert np.isnan(back[1, 0]) and np.isclose(back[0, 1], 1.5)
        assert meta["source"] == "frame001.tif"
        assert meta["dmin_density"] == 0.516
        assert meta["crop_rect"] == [10, 20, 30, 40]

    def test_foreign_tiff_rejected(self, tmp_path) -> None:
        import tifffile
        p = tmp_path / "plain.tif"
        tifffile.imwrite(p, np.zeros((4, 4), dtype=np.uint16))
        with pytest.raises(ValueError, match="not a filmscan density"):
            density.read_density_tiff(p)

    def test_3d_map_rejected(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="2-D"):
            density.write_density_tiff(
                tmp_path / "x.tif",
                np.zeros((4, 4, 3), dtype=np.float32),
                density.DensityProvenance(source="x", shutter=1.0, gain=None,
                                          black_level=0, white_level=65535),
            )


class TestStats:
    def test_stats_valid_only(self) -> None:
        d = np.full((100,), 1.0, dtype=np.float32)
        d[:50] = np.nan
        s = density.density_stats(d)
        assert s["valid_fraction"] == pytest.approx(0.5)
        assert s["d_p50"] == pytest.approx(1.0)

    def test_stats_all_nan(self) -> None:
        s = density.density_stats(np.full((4, 4), np.nan))
        assert s["valid_fraction"] == 0.0
        assert np.isnan(s["d_p50"])


class TestStackDarks:
    """Time-matched dark stacking (the B1 finding: mixed shutter masters)."""

    def test_mixed_shutters_normalise_before_median(self) -> None:
        # Same dark current rate, two shutter times: stacking must not mix
        # the raw levels -- after normalisation both are the same master.
        rate = 220.0  # DN per second above a 1 DN pedestal
        pedestal = 1.0
        short = np.full((8, 8), pedestal + rate * 0.001)
        long_ = np.full((8, 8), pedestal + rate * 0.00117)
        med, spread = calibration.stack_darks([short, long_],
                                              [0.001, 0.00117],
                                              black_level=pedestal)
        # Reference = longest shutter: both map to pedestal + rate*0.00117.
        assert np.allclose(med, pedestal + rate * 0.00117, atol=1e-6)
        assert np.allclose(spread, 0.0)

    def test_same_shutter_matches_plain_stack(self) -> None:
        frames = [np.full((4, 4), float(50 + i)) for i in range(3)]
        a = calibration.stack_darks(list(frames), [0.002] * 3)
        b = calibration.stack(list(frames))
        assert np.allclose(a[0], b[0])

    def test_mismatched_lengths_raise(self) -> None:
        with pytest.raises(ValueError):
            calibration.stack_darks([np.zeros((2, 2))], [])


B1_DIR = Path.home() / "Downloads" / "skeny" / "B1"


@pytest.mark.skipif(not B1_DIR.exists(), reason="B1 sample project absent")
class TestB1FixedPoints:
    """Real-data invariants measured on 2026-09-18 (design doc 01 §5).

    Skipped wherever the folder is not on disk; these are regression pins, not
    the only tests of the layer.
    """

    @pytest.fixture(scope="class")
    def project(self):
        from filmscan_studio.developer.project import DevelopProject
        return DevelopProject.open(B1_DIR)

    def test_dmin_reproducible(self, project) -> None:
        values = project.dmin_candidates()
        assert len(values) >= 2
        # B1: all five base readings land within ±0.005 D of Dmin = 0.431.
        assert max(values) - min(values) < 0.01
        assert 0.40 < np.mean(values) < 0.46

    def test_frame002_median_density(self, project) -> None:
        d, _ = project.build_density("frame002.tif")
        s = density.density_stats(d)
        assert s["d_p50"] == pytest.approx(1.70, abs=0.15)

    def test_empty_frame_detected(self, project) -> None:
        # frame004 holds no film: its median D sits at (or under) the base.
        d, _ = project.build_density("frame004.tif")
        s = density.density_stats(d)
        assert s["d_p50"] < 0.2
