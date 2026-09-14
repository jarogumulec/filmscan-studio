"""Developer pipeline tests, including a real-raw round trip when a NEF is present.

The synthetic tests prove the maths. The optional real-file test proves the
calibration-then-demosaic path actually runs against LibRaw and a D750 NEF, and it
is skipped rather than failed when no file is available so CI stays green.
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from filmscan_studio.core.calibration import (
    CalibrationStack,
    build_flat,
    normalise_flat,
)
from filmscan_studio.core.exposure import ExposureSettings
from filmscan_studio.core.filmic import FilmicProfile
from filmscan_studio.core.models import AcquisitionMetadata
from filmscan_studio.core.positive import PositiveParams
from filmscan_studio.core.rawio import RawFrame
from filmscan_studio.developer.pipeline import (
    DeveloperParams,
    DeveloperPipeline,
    sidecar_for,
    write_jpeg,
    write_tiff,
)

BLACK, WHITE = 600.0, 16383.0
REAL_NEF = Path("/tmp/fs_probe/bind_shot.NEF")


def make_negative_frame(n: int = 160, seed: int = 0) -> RawFrame:
    rng = np.random.default_rng(seed)
    scene = rng.random((n, n))
    density = 0.2 + 2.0 * scene
    t = 10.0 ** (-density)
    t /= t.max()
    dn = (t * (WHITE - BLACK) + BLACK).astype(np.uint16)
    return RawFrame(
        path=Path("synthetic.nef"),
        data=dn,
        black_level=BLACK,
        white_level=WHITE,
        color_desc="RGGB",
        width=n,
        height=n,
        acquisition=AcquisitionMetadata(exposure_time=1.0, iso=100),
    )


def test_negative_frame_is_a_2d_mosaic() -> None:
    frame = make_negative_frame()
    assert frame.data.ndim == 2


class TestMonochromeDevelop:
    def test_produces_a_positive(self) -> None:
        frame = make_negative_frame()
        result = DeveloperPipeline().develop(frame, DeveloperParams())
        assert result.image.shape == frame.data.shape
        assert 0.0 <= result.image.min() and result.image.max() <= 1.0

    def test_recovers_scene_rank_order(self) -> None:
        frame = make_negative_frame()
        result = DeveloperPipeline().develop(frame, DeveloperParams())
        rng = np.random.default_rng(0)
        scene = rng.random(frame.data.shape)
        ranks_a = np.argsort(np.argsort(scene)).ravel()
        ranks_b = np.argsort(np.argsort(result.image)).ravel()
        assert np.corrcoef(ranks_a, ranks_b)[0, 1] > 0.9

    def test_params_fingerprint_is_stable(self) -> None:
        a = DeveloperParams(PositiveParams(exposure_ev=0.5))
        b = DeveloperParams(PositiveParams(exposure_ev=0.5))
        assert a.fingerprint() == b.fingerprint()

    def test_different_params_differ(self) -> None:
        a = DeveloperParams(PositiveParams(exposure_ev=0.5))
        b = DeveloperParams(PositiveParams(exposure_ev=0.6))
        assert a.fingerprint() != b.fingerprint()

    def test_same_inputs_same_bytes(self) -> None:
        """Reproducibility, checked rather than asserted."""
        frame = make_negative_frame()
        pipeline = DeveloperPipeline()
        params = DeveloperParams(PositiveParams(exposure_ev=0.3))
        first = pipeline.develop(frame, params).as_uint16()
        second = pipeline.develop(frame, params).as_uint16()
        assert np.array_equal(first, second)

    def test_exposure_brightens(self) -> None:
        frame = make_negative_frame()
        pipeline = DeveloperPipeline()
        dark = pipeline.develop(frame, DeveloperParams(PositiveParams(0.0))).image.mean()
        bright = pipeline.develop(frame, DeveloperParams(PositiveParams(1.0))).image.mean()
        assert bright > dark

    def test_profile_change_is_reported_in_fingerprint(self) -> None:
        frame = make_negative_frame()
        params = DeveloperParams(
            PositiveParams(profile=FilmicProfile(toe=0.5, gamma=1.2, shoulder=0.3))
        )
        result = DeveloperPipeline().develop(frame, params)
        assert result.fingerprint == params.fingerprint()


class TestCalibrationInPipeline:
    def test_dark_subtraction_lowers_signal(self) -> None:
        """Dark current must come out of the *linear* data.

        Deliberately not asserted on the developed image: base subtraction is
        automatic, so an offset that rides on the whole frame is divided back out
        by the base measurement. That renormalisation is the feature; the dark
        still has to be gone underneath it.
        """
        frame = make_negative_frame(n=64)
        dark_current = 50.0
        plain = DeveloperPipeline()._calibrate_array(frame.data, BLACK, WHITE, 1.0)
        with_dark = DeveloperPipeline(
            dark=CalibrationStack(
                shutter=frame.acquisition.exposure_time,
                iso=100,
                data=np.full(frame.data.shape, BLACK + dark_current),
                frame_count=1,
            )
        )._calibrate_array(frame.data, BLACK, WHITE, 1.0)
        assert plain.mean() - with_dark.mean() == pytest.approx(
            dark_current / (WHITE - BLACK), rel=1e-9
        )

    def test_dark_normalised_by_shutter(self) -> None:
        """A dark shot at 1/4 of the scan's shutter must be scaled up 4x."""
        frame = make_negative_frame(n=64)
        dark_current = 100.0
        dark_stack = CalibrationStack(
            shutter=0.25,
            iso=100,
            data=np.full(frame.data.shape, BLACK + dark_current),
            frame_count=1,
        )
        # Scan at 1s accumulates 4x the dark current of the 1/4s dark.
        pipeline = DeveloperPipeline(dark=dark_stack)
        out = pipeline._calibrate_array(frame.data, BLACK, WHITE, 1.0)
        plain = (frame.data - BLACK) / (WHITE - BLACK)
        # Difference should be the 4x-scaled dark current, not the 1x.
        removed = plain.mean() - out.mean()
        assert removed == pytest.approx(4 * dark_current / (WHITE - BLACK), rel=0.05)

    def test_flat_flattens_a_vignette(self) -> None:
        """The whole point of the flat: uneven illumination must even out."""
        n = 128
        y, x = np.mgrid[0:n, 0:n]
        vignette = 1.0 - 0.35 * (((x - 64) ** 2 + (y - 64) ** 2) / (64.0**2))
        # An evenly exposed film base, so every remaining non-uniformity is the
        # optical path -- the case a flat exists to fix.
        scan_dn = BLACK + 4000.0 * vignette
        # The flat is the same illumination at 3x the exposure, pedestal included,
        # exactly as a real raw flat arrives.
        flat_dn = BLACK + 12000.0 * vignette
        # sigma 0: a blur that is not identity would itself reshape a vignette this
        # tight, which is a different test's concern (see test_core).
        flat = build_flat([flat_dn], smooth_sigma=0.0)

        plain = DeveloperPipeline()._calibrate_array(scan_dn, BLACK, WHITE, 1.0)
        corrected = DeveloperPipeline(
            flat=flat,
            flat_exposure=CalibrationStack(
                shutter=2.0, iso=100, data=flat, frame_count=1
            ),
        )._calibrate_array(scan_dn, BLACK, WHITE, 1.0)

        before = float(plain.std() / plain.mean())
        after = float(corrected.std() / corrected.mean())
        assert before > 0.08, "the synthetic vignette is not visible"
        assert after < before * 0.02

    def test_dark_is_subtracted_before_the_flat_mean(self) -> None:
        """A pedestal left in the flat dilutes the gain map and under-corrects."""
        n = 64
        y, x = np.mgrid[0:n, 0:n]
        vignette = 1.0 - 0.4 * (((x - 32) ** 2 + (y - 32) ** 2) / 32.0**2)
        scan_dn = BLACK + 2000.0 * vignette
        flat_dn = BLACK + 6000.0 * vignette
        dark = CalibrationStack(
            shutter=1.0, iso=100, data=np.full((n, n), BLACK), frame_count=1
        )

        corrected = DeveloperPipeline(
            dark=dark,
            flat=flat_dn,
            flat_exposure=CalibrationStack(
                shutter=1.0, iso=100, data=flat_dn, frame_count=1
            ),
        )._calibrate_array(scan_dn, BLACK, WHITE, 1.0)
        # Same optical path, so the vignette cancels to flatness.
        assert float(corrected.std() / corrected.mean()) < 1e-9

        # The same flat used raw -- the bug this ordering prevents. The pedestal is
        # the same 600 DN everywhere, so it shrinks the gain map's contrast by
        # 600/(600+6000) and a tenth of the illumination error survives.
        naive = (scan_dn - BLACK) / normalise_flat(flat_dn)
        naive_residual = float(naive.std() / naive.mean())
        assert naive_residual > 0.01

    def test_no_calibration_is_passthrough(self) -> None:
        frame = make_negative_frame(n=64)
        out = DeveloperPipeline()._calibrate_array(frame.data, BLACK, WHITE, 1.0)
        assert out.min() >= 0.0
        assert np.allclose(out, (frame.data - BLACK) / (WHITE - BLACK))


def _wrap(dn: np.ndarray) -> RawFrame:
    return RawFrame(
        path=Path("vignetted.nef"),
        data=dn,
        black_level=BLACK,
        white_level=WHITE,
        color_desc="RGGB",
        width=dn.shape[1],
        height=dn.shape[0],
        acquisition=AcquisitionMetadata(exposure_time=1.0, iso=100),
    )


def _spatial_gradient_energy(img: np.ndarray) -> float:
    gx = np.diff(img, axis=1)
    gy = np.diff(img, axis=0)
    return float(np.mean(gx**2) + np.mean(gy**2))


class TestExport:
    def test_16bit_tiff_round_trip(self, tmp_path: Path) -> None:
        frame = make_negative_frame(n=96)
        result = DeveloperPipeline().develop(frame, DeveloperParams())
        path = write_tiff(result, tmp_path / "out.tif")
        assert path.exists()
        read = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        assert read.dtype == np.uint16
        # OpenCV reads BGR; a BW frame has three identical planes, so any order works.
        assert np.array_equal(read[:, :, 0], result.as_uint16())

    def test_no_double_gamma_on_write(self, tmp_path: Path) -> None:
        """The archived TIFF must be the curve output, unmodified.

        Verified by checking that the mean survives the write intact rather than
        being brightened by a second display transform.
        """
        frame = make_negative_frame(n=96)
        result = DeveloperPipeline().develop(frame, DeveloperParams())
        path = write_tiff(result, tmp_path / "out.tif")
        read = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)[:, :, 0].astype(np.float64) / 65535.0
        assert abs(read.mean() - result.image.mean()) < 0.002

    def test_8bit_tiff(self, tmp_path: Path) -> None:
        frame = make_negative_frame(n=64)
        result = DeveloperPipeline().develop(frame, DeveloperParams())
        path = write_tiff(result, tmp_path / "out8.tif", bits=8)
        assert cv2.imread(str(path), cv2.IMREAD_UNCHANGED).dtype == np.uint8

    def test_rejects_bad_bit_depth(self, tmp_path: Path) -> None:
        frame = make_negative_frame(n=32)
        result = DeveloperPipeline().develop(frame, DeveloperParams())
        with pytest.raises(ValueError):
            write_tiff(result, tmp_path / "x.tif", bits=12)

    def test_jpeg_quicklook(self, tmp_path: Path) -> None:
        frame = make_negative_frame(n=64)
        result = DeveloperPipeline().develop(frame, DeveloperParams())
        path = write_jpeg(result, tmp_path / "q.jpg")
        assert cv2.imread(str(path)) is not None

    def test_sidecar_records_provenance(self) -> None:
        frame = make_negative_frame(n=32)
        params = DeveloperParams(PositiveParams(exposure_ev=0.5))
        result = DeveloperPipeline().develop(frame, params)
        payload = sidecar_for(result)
        assert payload["parameters"]["exposure_ev"] == 0.5
        assert payload["fingerprint"] == result.fingerprint
        assert "filmic" in payload["pipeline"]

    def test_sidecar_is_json_serialisable(self) -> None:
        frame = make_negative_frame(n=32)
        result = DeveloperPipeline().develop(frame, DeveloperParams())
        assert json.loads(json.dumps(sidecar_for(result)))["calibration"] == []


class TestColourDevelop:
    """Colour path: calibration in the mosaic, LibRaw doing the interpolation."""

    def _colour_source(self) -> RawFrame:
        frame = make_negative_frame(n=256, seed=2)
        frame.data.astype(np.uint16)
        return frame

    def test_real_nef_when_available(self) -> None:
        if not REAL_NEF.exists():
            pytest.skip("no test NEF present")
        result = DeveloperPipeline().develop_colour(REAL_NEF, DeveloperParams(half_size=True))
        assert result.image.ndim == 3 and result.image.shape[2] == 3
        assert result.image.min() >= 0.0 and result.image.max() <= 1.0

    def test_calibration_changes_the_colour_output(self) -> None:
        """A dark really does reach LibRaw's input on the colour path.

        The direction is *upwards*: removing dark current lowers the linear
        signal, and a negative is inverted, so lowering what is already a
        transmittance raises the positive. Asserting the naive "dark subtracts,
        therefore darker" would encode the wrong physics.
        """
        if not REAL_NEF.exists():
            pytest.skip("no test NEF present")
        params = DeveloperParams(half_size=True)
        plain = DeveloperPipeline().develop_colour(REAL_NEF, params).image
        dark_data = np.full((4032, 6032), 800.0)
        calibrated = DeveloperPipeline(
            dark=CalibrationStack(shutter=1 / 15, iso=100, data=dark_data, frame_count=1)
        ).develop_colour(REAL_NEF, params).image
        assert not np.array_equal(calibrated, plain)
        assert calibrated.mean() > plain.mean()

    def test_deterministic(self) -> None:
        if not REAL_NEF.exists():
            pytest.skip("no test NEF present")
        pipeline = DeveloperPipeline()
        params = DeveloperParams(half_size=True)
        a = pipeline.develop_colour(REAL_NEF, params).as_uint16()
        b = pipeline.develop_colour(REAL_NEF, params).as_uint16()
        assert np.array_equal(a, b)

    def test_source_nef_is_never_modified(self) -> None:
        if not REAL_NEF.exists():
            pytest.skip("no test NEF present")
        before = REAL_NEF.read_bytes()
        DeveloperPipeline().develop_colour(REAL_NEF, DeveloperParams(half_size=True))
        assert REAL_NEF.read_bytes() == before

    def test_base_level_measured_once_for_all_channels(self) -> None:
        """A per-channel base would shift colour as the base is raised."""
        rgb = np.dstack([np.full((64, 64), 0.9)] * 3)
        from filmscan_studio.developer.pipeline import _positive_rgb

        out = _positive_rgb(rgb, PositiveParams(base_level=0.9))
        # All channels identical in, identical out: no cast introduced.
        assert np.allclose(out[:, :, 0], out[:, :, 1]) and np.allclose(
            out[:, :, 1], out[:, :, 2]
        )


class TestSlidePath:
    def test_invert_false_treats_input_as_positive(self) -> None:
        rng = np.random.default_rng(7)
        scene = rng.random((64, 64))
        dn = (scene * (WHITE - BLACK) + BLACK).astype(np.uint16)
        frame = _wrap(dn)
        out = DeveloperPipeline().develop(
            frame, DeveloperParams(PositiveParams(invert=False))
        ).image
        ra = np.argsort(np.argsort(scene)).ravel()
        rb = np.argsort(np.argsort(out)).ravel()
        assert np.corrcoef(ra, rb)[0, 1] > 0.99
