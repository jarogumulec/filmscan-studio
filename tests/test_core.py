"""Tests for the core maths: exposure, filmic, positive, histogram, calibration.

These are pure-function tests with synthetic data, so they run anywhere and pin
down the invariants the rest of the application silently depends on.
"""

from __future__ import annotations

import numpy as np
import pytest

from filmscan_studio.core import calibration, exposure, filmic, histogram, positive


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    """Rank correlation.

    The Working Positive passes through a deliberately nonlinear tone curve, so
    Pearson against the ground-truth scene understates correctness. Rank order is
    the honest invariant: the pipeline must not invert or scramble tonal order.
    """
    ra = np.argsort(np.argsort(a)).astype(np.float64)
    rb = np.argsort(np.argsort(b)).astype(np.float64)
    return float(np.corrcoef(ra, rb)[0, 1])


def synthetic_negative(n: int = 200, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Simulate a negative: scene -> density -> transmittance -> sensor DN.

    Returns (true_scene, sensor_dn). The scene is the ground truth the Working
    Positive must correlate with after inversion.
    """
    rng = np.random.default_rng(seed)
    scene = rng.random((n, n))
    density = 0.2 + 2.0 * scene
    transmittance = 10.0 ** (-density)
    transmittance /= transmittance.max()
    dn = transmittance * (16383 - 600) + 600
    return scene, dn


class TestExposureSettings:
    def test_shutter_string_formats_fractions(self) -> None:
        assert exposure.ExposureSettings(1 / 125, 100).shutter_string() == "1/125"
        assert exposure.ExposureSettings(2.0, 100).shutter_string() == "2"
        assert exposure.ExposureSettings(0.5, 100).shutter_string() == "1/2"

    def test_doubles_shutter_is_one_stop(self) -> None:
        a = exposure.ExposureSettings(1 / 120, 100)
        b = exposure.ExposureSettings(1 / 60, 100)
        assert a.stops_between(b) == pytest.approx(1.0)

    def test_iso_double_is_one_stop(self) -> None:
        a = exposure.ExposureSettings(1 / 60, 100)
        b = exposure.ExposureSettings(1 / 60, 200)
        assert a.stops_between(b) == pytest.approx(-1.0)

    def test_rejects_nonsense(self) -> None:
        with pytest.raises(ValueError):
            exposure.ExposureSettings(0, 100)
        with pytest.raises(ValueError):
            exposure.ExposureSettings(1, 0)
        with pytest.raises(ValueError):
            # NaN slipped past ``<=`` (every NaN comparison is False) and the
            # combo then displayed "1/nan" — 2026-09 round-trip sweep.
            exposure.ExposureSettings(float("nan"), 100)

    def test_shutter_string_round_trips(self) -> None:
        """The combo echoes the applied shutter back; parse(shutter_string())
        must land within a two-percent tick for every speed the rig shoots."""
        for seconds in (3e-4, 0.001, 1 / 125, 1 / 60, 0.1, 1 / 3, 0.9999,
                        1.0, 2.5, 15.0, 52.0, 1800.0):
            text = exposure.ExposureSettings(seconds, 100).shutter_string()
            back = exposure.parse_shutter(text)
            assert back is not None, text
            assert abs(back - seconds) / seconds < 0.02, f"{seconds} -> {text}"
        # A denominator of zero is nonsense, not a speed:
        assert exposure.parse_shutter("1/0") is None


class TestMetering:
    def test_measure_reports_percentile_and_clipping(self) -> None:
        data = np.linspace(600, 16383, 10_000)
        reading = exposure.measure(data, 600, 16383)
        assert reading.signal_p999 < 16383
        assert reading.clipped_fraction > 0  # top of linspace hits the rail

    def test_empty_array_rejected(self) -> None:
        with pytest.raises(ValueError):
            exposure.measure(np.array([]), 0, 1)

    def test_no_clipping_when_below_rail(self) -> None:
        reading = exposure.measure(np.full((10, 10), 16000.0), 600, 16383)
        assert not reading.clipped


class TestAutoExposure:
    def test_target_keeps_headroom_below_clip(self) -> None:
        target = exposure.signal_at_target(0.4, 600, 16383)
        util = (target - 600) / (16383 - 600)
        assert util == pytest.approx(2.0**-0.4)

    def test_delta_moves_toward_target(self) -> None:
        # Signal peaking at half scale needs ~+0.7 EV to reach a 0.4 EV headroom.
        data = np.linspace(600, 8000, 4096)
        reading = exposure.measure(data, 600, 16383)
        delta = exposure.required_ev_change(reading, headroom_ev=0.4)
        assert delta > 0

    def test_round_trip_reaches_target(self) -> None:
        rng = np.random.default_rng(3)
        data = 600 + rng.random((400, 400)) * 6000
        reading = exposure.measure(data, 600, 16383)
        delta = exposure.required_ev_change(reading, headroom_ev=0.4)
        shifted = (data - 600) * 2.0**delta + 600
        after = exposure.measure(shifted, 600, 16383)
        assert after.signal_p999 == pytest.approx(
            exposure.signal_at_target(0.4, 600, 16383), rel=0.05
        )

    def test_metering_uses_linear_not_display(self) -> None:
        """A gamma-encoded frame must not meter the same as its linear source.

        This is the brief's central rule encoded as a test: if applying display
        gamma did not move the meter, the meter would be insensitive to the very
        thing it is supposed to measure.
        """
        rng = np.random.default_rng(4)
        linear = 600 + rng.random((300, 300)) * 9000
        # Same frame, encoded for display: linear utilisation u becomes u**(1/2.2).
        display = 600 + ((linear - 600) / 15783.0) ** (1 / 2.2) * 15783.0
        a = exposure.measure(linear, 600, 16383)
        b = exposure.measure(display, 600, 16383)
        # Gamma-encoded data reads brighter, which is exactly why metering it
        # would mislead: it would under-report how close highlights are to clipping.
        assert abs(a.highlight_utilisation - b.highlight_utilisation) > 0.1

    def test_chooses_closest_shutter(self) -> None:
        cur = exposure.ExposureSettings(1 / 60, 100)
        cands = [1 / 30, 1 / 40, 1 / 50, 1 / 60, 1 / 80, 1 / 100]
        assert exposure.choose_shutter(cur, 0.0, cands) == 1 / 60
        assert exposure.choose_shutter(cur, 1.0, cands) == 1 / 30

    def test_clamped_into_range_when_ideal_unreachable(self) -> None:
        cur = exposure.ExposureSettings(1 / 8000, 100)
        cands = [1 / 8000, 1 / 4000]
        assert exposure.choose_shutter(cur, 10.0, cands) == 1 / 4000


class TestFilmic:
    def test_endpoints_fixed(self) -> None:
        p = filmic.FilmicProfile()
        out = p.apply(np.linspace(0, 1, 4096))
        assert out[0] == pytest.approx(0.0)
        assert out[-1] == pytest.approx(1.0)

    def test_monotonic_over_full_range(self) -> None:
        for p in (
            filmic.FilmicProfile(),
            filmic.FilmicProfile(toe=1.0, shoulder=1.0),
            filmic.FilmicProfile(gamma=2.5),
            filmic.FilmicProfile.neutral(),
        ):
            out = p.apply(np.linspace(0, 1, 4096))
            assert np.all(np.diff(out) >= -1e-9), f"not monotonic: {p}"

    def test_neutral_is_identity(self) -> None:
        x = np.linspace(0, 1, 257)
        assert np.allclose(filmic.FilmicProfile.neutral().apply(x), x)

    def test_toe_compresses_shadows(self) -> None:
        """Shadow compression = a narrower output band near black.

        Not "lifts shadows". A toe that raised shadows would *expand* the low
        band and increase shadow contrast, which is the opposite control.
        """
        x = np.array([0.0, 0.10])
        with_toe = filmic.FilmicProfile(toe=0.8, gamma=1.0, shoulder=0.0).apply(x)
        without = filmic.FilmicProfile(toe=0.0, gamma=1.0, shoulder=0.0).apply(x)
        assert (with_toe[1] - with_toe[0]) < (without[1] - without[0])
        assert with_toe[1] < without[1]

    def test_shoulder_compresses_highlights(self) -> None:
        """Compression means a narrower output interval, not brighter highlights."""
        x = np.array([0.90, 1.0])
        with_shoulder = filmic.FilmicProfile(toe=0.0, gamma=1.0, shoulder=0.8).apply(x)
        without = filmic.FilmicProfile(toe=0.0, gamma=1.0, shoulder=0.0).apply(x)
        assert (with_shoulder[1] - with_shoulder[0]) < (without[1] - without[0])
        assert with_shoulder[0] > without[0]

    def test_knees_do_not_move_endpoints(self) -> None:
        """Compression must never clip: black stays black, white stays white."""
        for p in (
            filmic.FilmicProfile(toe=1.0, gamma=1.0, shoulder=0.0),
            filmic.FilmicProfile(toe=0.0, gamma=1.0, shoulder=1.0),
        ):
            out = p.apply(np.array([0.0, 1.0]))
            assert out[0] == pytest.approx(0.0)
            assert out[1] == pytest.approx(1.0)

    def test_gamma_is_contrast_at_the_pivot(self) -> None:
        """gamma is defined as the output slope at the pivot, so measure it."""
        for g in (1.0, 1.5, 2.0):
            p = filmic.FilmicProfile(toe=0.0, gamma=g, shoulder=0.0)
            x = np.array([0.499, 0.501])
            out = p.apply(x)
            assert (out[1] - out[0]) / (x[1] - x[0]) == pytest.approx(g, rel=0.02)

    def test_gamma_is_exposure_neutral_at_pivot(self) -> None:
        p = filmic.FilmicProfile(toe=0.35, gamma=1.10, shoulder=0.40).apply(np.array([0.5]))
        q = filmic.FilmicProfile(toe=0.35, gamma=2.00, shoulder=0.40).apply(np.array([0.5]))
        assert p[0] == pytest.approx(q[0], abs=0.02)

    def test_knees_shift_the_mid_in_expected_directions(self) -> None:
        """A toe darkens mid, a shoulder brightens it -- the H-D S-curve.

        The gamma slope is defined against the curve's own pivot, so combining
        the controls is not expected to leave the input-space slope untouched;
        this pins the direction of each knee's effect instead.
        """
        ref = filmic.FilmicProfile(toe=0.0, gamma=1.0, shoulder=0.0).apply(np.array([0.5]))[0]
        toe = filmic.FilmicProfile(toe=0.8, gamma=1.0, shoulder=0.0).apply(np.array([0.5]))[0]
        shoulder = filmic.FilmicProfile(toe=0.0, gamma=1.0, shoulder=0.8).apply(np.array([0.5]))[0]
        assert toe < ref < shoulder

    def test_extreme_combination_stays_valid(self) -> None:
        p = filmic.FilmicProfile(toe=1.0, gamma=2.5, shoulder=1.0)
        out = p.apply(np.linspace(0, 1, 4096))
        assert np.all(np.diff(out) >= -1e-9)
        assert out[0] == pytest.approx(0.0)
        assert out[-1] == pytest.approx(1.0)

    def test_validates_range(self) -> None:
        with pytest.raises(ValueError):
            filmic.FilmicProfile(toe=1.5)
        with pytest.raises(ValueError):
            filmic.FilmicProfile(gamma=0)

    def test_json_roundtrip(self) -> None:
        p = filmic.FilmicProfile(toe=0.35, gamma=1.10, shoulder=0.40, name="HP5 ID11 1+1")
        assert filmic.FilmicProfile.from_dict(p.to_dict()) == p


class TestPositive:
    def test_working_positive_recovers_scene_order(self) -> None:
        scene, dn = synthetic_negative()
        pos = positive.to_working_positive(dn, 600, 16383)
        # Not 1.0: the H-D curve is nonlinear by design. But rank order holds.
        assert spearman(scene.ravel(), pos.ravel()) > 0.9

    def test_raw_view_stays_negative(self) -> None:
        scene, dn = synthetic_negative()
        raw = positive.to_raw_view(dn, 600, 16383)
        assert np.corrcoef(scene.ravel(), raw.ravel())[0, 1] < -0.9

    def test_slide_mode_is_direct(self) -> None:
        scene, _ = synthetic_negative()
        dn = scene * (16383 - 600) + 600
        out = positive.to_working_positive(
            dn, 600, 16383, positive.PositiveParams(invert=False)
        )
        assert spearman(scene.ravel(), out.ravel()) > 0.99

    def test_inversion_uses_high_base_not_low(self) -> None:
        """The base sits at the top of the sensor range on a negative."""
        x = np.linspace(0, 1, 101)
        out = positive.subtract_base(x, 0.9)
        assert out[0] == pytest.approx(1.0)  # zero signal -> brightest
        assert out[-1] == pytest.approx(0.0)  # base -> black

    def test_full_range_survives_base_subtraction(self) -> None:
        scene, dn = synthetic_negative()
        pos = positive.to_working_positive(dn, 600, 16383)
        assert pos.max() > 0.9, "base subtraction collapsed the tonal range"
        assert pos.min() < 0.1

    def test_base_override(self) -> None:
        _, dn = synthetic_negative()
        out = positive.to_working_positive(
            dn, 600, 16383, positive.PositiveParams(base_level=0.98)
        )
        assert out.max() > 0.9

    def test_exposure_shift_moves_mean(self) -> None:
        _, dn = synthetic_negative()
        base = positive.to_working_positive(dn, 600, 16383, positive.PositiveParams(0.0))
        up = positive.to_working_positive(dn, 600, 16383, positive.PositiveParams(1.0))
        assert up.mean() > base.mean()

    def test_does_not_mutate_input(self) -> None:
        _, dn = synthetic_negative(n=50)
        before = dn.copy()
        positive.to_working_positive(dn, 600, 16383)
        assert np.array_equal(dn, before)

    def test_rejects_bad_base(self) -> None:
        with pytest.raises(ValueError):
            positive.subtract_base(np.zeros((2, 2)), 0.0)

    def test_normalise_requires_ordered_levels(self) -> None:
        with pytest.raises(ValueError):
            positive.normalise(np.zeros((2, 2)), 1000, 500)


class TestHistogram:
    def test_is_linear_so_bins_are_stop_equal(self) -> None:
        """A uniform-in-linear source must spread uniformly across bins."""
        linear = np.linspace(0, 16383, 65536).astype(np.float64)
        h = histogram.compute(linear, 0, 16383, bins=16)
        assert h.counts.std() / h.counts.mean() < 0.05

    def test_gamma_source_bunches_in_shadows(self) -> None:
        """Confirms the histogram is in linear space, not display space."""
        linear = np.linspace(0, 1, 65536) ** 2.2 * 16383
        h = histogram.compute(linear, 0, 16383, bins=16)
        assert h.counts[:4].sum() > h.counts[-4:].sum()

    def test_clipping_detected(self) -> None:
        h = histogram.compute(np.full((100, 100), 16383.0), 600, 16383)
        assert h.clipped_high == 10_000
        assert h.clipping_warning

    def test_no_false_clipping(self) -> None:
        h = histogram.compute(np.linspace(600, 16000, 10_000), 600, 16383)
        assert not h.clipping_warning

    def test_normalised_peaks_at_one(self) -> None:
        h = histogram.compute(np.random.default_rng(0).random((50, 50)), 0, 1)
        assert h.normalised().max() == pytest.approx(1.0)

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValueError):
            histogram.compute(np.array([]), 0, 1)


class TestCalibration:
    def test_stack_median_rejects_outlier_frame(self) -> None:
        good = np.full((8, 8), 1000.0)
        bad = good.copy()
        bad[3, 3] = 99_999
        median, spread = calibration.stack([good, good, bad])
        assert median[3, 3] == pytest.approx(1000.0)
        assert spread.max() >= 0

    def test_dark_scales_with_shutter_only(self) -> None:
        dark = np.full((4, 4), 100.0)
        assert calibration.rescale_dark(dark, 1.0, 4.0)[0, 0] == pytest.approx(400.0)
        assert calibration.rescale_dark(dark, 4.0, 1.0)[0, 0] == pytest.approx(25.0)

    def test_dark_subtraction_recovers_signal(self) -> None:
        true = np.full((8, 8), 500.0)
        dark_current = np.full((8, 8), 40.0)
        scan_shutter, dark_shutter = 4.0, 1.0
        scan = true + dark_current * (scan_shutter / dark_shutter)
        dark = calibration.CalibrationStack(
            shutter=dark_shutter, iso=100, data=dark_current, frame_count=1
        )
        assert np.allclose(calibration.correct_dark(scan, dark, scan_shutter), true)

    def test_flat_removes_vignetting(self) -> None:
        y, x = np.mgrid[0:64, 0:64]
        vignette = 1.0 - 0.3 * ((x - 32) ** 2 + (y - 32) ** 2) / (32**2 * 2)
        flat = 10_000 * vignette
        scan = 10_000 * vignette  # uniform scene through the same vignette
        out = calibration.correct_flat(scan, flat, 1.0, 1.0)
        assert out.std() / out.mean() < 0.02

    def test_flat_smoothing_removes_content(self) -> None:
        """A flat containing a hard edge must not etch that edge into scans."""
        frame = np.full((64, 64), 10_000.0)
        frame[:, :32] = 9_000.0
        flat = calibration.build_flat([frame], smooth_sigma=32.0)
        centre = flat[32, 30:34]
        assert centre.std() / centre.mean() < 0.02, "content leaked into the flat"

    def test_flat_guard_against_black_regions(self) -> None:
        flat = np.zeros((8, 8))
        flat[4:, :] = 10_000
        scan = np.full((8, 8), 5_000.0)
        out = calibration.correct_flat(scan, flat, 1.0, 1.0)
        assert np.all(np.isfinite(out))
        assert out.max() < 100 * scan.mean()

    def test_empty_stack_rejected(self) -> None:
        with pytest.raises(ValueError):
            calibration.stack([])

    def test_uncalibrated_path_is_passthrough(self) -> None:
        scan = np.arange(16.0).reshape(4, 4)
        assert np.allclose(calibration.apply_calibration(scan, None, None, 1.0), scan)
