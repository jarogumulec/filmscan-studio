"""Tests for the capture layer: camera contract, session workflow, auto exposure.

Everything here runs against :class:`MockCamera`, which is shaped like the
Touptek TS2600MP-G2 — continuous microsecond shutter, permille gain ladder,
binned/ROI stream modes and real 16-bit TIFF output — so snapping behaviour,
the session's read path and the auto-exposure loop are tested against the
constraints the real backend has, not idealised ones.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from filmscan_studio.capture.autoexposure import AutoExposureController, LiveMeter
from filmscan_studio.capture.camera import (
    CameraError,
    CameraInfo,
    CaptureResult,
    LiveFrame,
    NotConnectedError,
)
from filmscan_studio.capture.mock import MOCK_GAIN_RANGE, MOCK_SENSOR, MockCamera
from filmscan_studio.capture.session import (
    CALIBRATION_AVERAGE,
    DARK_TEMPERATURE_TOLERANCE_C,
    CaptureSession,
    SessionPaths,
)
from filmscan_studio.core.exposure import (
    ARCHIVE_GAIN,
    ExposureSettings,
    MeterReading,
    parse_shutter,
)
from filmscan_studio.core.models import (
    AcquisitionMetadata,
    FilmMetadata,
    FilmType,
    FrameKind,
)
from filmscan_studio.core.rawio import open_frame
from filmscan_studio.core.zoom import (
    MIN_ROI_PX,
    NO_BINNING,
    OVERVIEW_BINNING,
    ZOOM_ROI_SIZE,
)


@pytest.fixture
def film() -> FilmMetadata:
    return FilmMetadata(
        film_id="HP5_001",
        film_name="Ilford HP5+",
        camera="Touptek TS2600MP-G2",
        format="aps-c",
        film_type_class=FilmType.BW_NEGATIVE,
        development="ID-11 1+1 13 min",
        operator="JG",
    )


@pytest.fixture
def session(tmp_path: Path, film: FilmMetadata) -> CaptureSession:
    camera = MockCamera()
    camera.connect()
    # No frame_reader override: the mock writes real TIFFs and the session
    # reads them through the production rawio path.
    return CaptureSession(camera, film, SessionPaths.create(tmp_path, film.film_id))


class TestShutterParsing:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("1/60", 1 / 60),
            ("0.0400 s", 0.04),
            ('30"', 30.0),
            ("1/4000", 1 / 4000),
            ("2", 2.0),
        ],
    )
    def test_parses_camera_vocabulary(self, text: str, expected: float) -> None:
        assert parse_shutter(text) == pytest.approx(expected)

    @pytest.mark.parametrize("text", ["Bulb", "Time", "", "n/a"])
    def test_non_numeric_returns_none(self, text: str) -> None:
        assert parse_shutter(text) is None


class TestCameraContract:
    def test_context_manager_connects_and_disconnects(self) -> None:
        with MockCamera() as camera:
            assert camera.info.model.startswith("TS2600MP-G2")
        with pytest.raises(NotConnectedError):
            camera.get_settings()

    def test_shutter_is_continuous(self) -> None:
        """No ladder on a µs-resolution shutter: the camera takes what it is asked."""
        with MockCamera() as camera:
            applied = camera.set_shutter(1 / 61)
            assert applied == pytest.approx(1 / 61)
            assert camera.info.shutter_choices == ()

    def test_gain_snaps_to_percent_value(self) -> None:
        """The SDK ladder is the integer percent Gain Value (100 = 1.00x)."""
        with MockCamera() as camera:
            applied = camera.set_gain(1.2345)
            assert applied == pytest.approx(1.23, abs=1e-9)
            assert camera.get_settings().gain == pytest.approx(1.23)

    def test_gain_clamped_to_range(self) -> None:
        with MockCamera() as camera:
            assert camera.set_gain(999.0) == MOCK_GAIN_RANGE[1]
            assert camera.set_gain(0.1) == MOCK_GAIN_RANGE[0]

    def test_iso_is_not_offered(self) -> None:
        with MockCamera() as camera:
            with pytest.raises(NotImplementedError):
                camera.set_iso(100)
            assert camera.capabilities().iso is False
            assert camera.capabilities().gain is True

    def test_readout_modes_switchable(self) -> None:
        """The mock mirrors the ATR2600M's LCG/HCG + low-noise switches,
        defaulting to the scanner optimum (LCG + low noise)."""
        with MockCamera() as camera:
            modes = camera.get_modes()
            assert modes.hcg is False and modes.low_noise is True
            assert camera.capabilities().conversion_gain is True
            assert camera.capabilities().low_noise is True
            modes = camera.set_conversion_gain(hcg=True)
            assert modes == camera.get_modes()
            assert camera.mode_history[-1].hcg is True

    def test_live_frames_only_while_running(self) -> None:
        with MockCamera(live_fps=200) as camera:
            assert camera.next_live_frame() is None
            camera.start_live_view()
            frame = camera.next_live_frame()
            assert frame is not None
            camera.stop_live_view()
            assert camera.next_live_frame() is None

    def test_live_frame_is_linear_uint16(self) -> None:
        with MockCamera(live_fps=200) as camera:
            camera.start_live_view()
            frame = camera.next_live_frame()
        assert frame.data.dtype == np.uint16 and frame.data.ndim == 2
        assert (frame.width, frame.height) == frame.data.shape[::-1]
        assert frame.black_level == 0.0 and frame.white_level == 65535.0

    def test_overview_is_binned_third_scale(self) -> None:
        with MockCamera(live_fps=200) as camera:
            camera.start_live_view()
            frame = camera.next_live_frame()
        assert frame.width == MOCK_SENSOR.width // 3
        assert frame.height == MOCK_SENSOR.height // 3

    def test_roi_mode_delivers_one_to_one_window(self) -> None:
        with MockCamera(live_fps=200) as camera:
            camera.set_live_view_roi((1000, 1000, ZOOM_ROI_SIZE, ZOOM_ROI_SIZE))
            camera.start_live_view()
            frame = camera.next_live_frame()
        assert (frame.width, frame.height) == (ZOOM_ROI_SIZE, ZOOM_ROI_SIZE)
        assert camera.binning == NO_BINNING

    def test_roi_records_stream_switches(self) -> None:
        with MockCamera() as camera:
            camera.set_live_view_roi((2000, 1500, 1200, 1200))
            camera.set_live_view_roi(None)
        assert camera.stream_history[0][0] == NO_BINNING
        assert camera.stream_history[-1] == (OVERVIEW_BINNING, None)

    def test_tiny_roi_refused(self) -> None:
        with MockCamera() as camera:
            with pytest.raises(ValueError):
                camera.set_live_view_roi((100, 100, MIN_ROI_PX - 2, 1200))

    def test_roi_clamped_into_sensor(self) -> None:
        with MockCamera() as camera:
            camera.set_live_view_roi((6200, 4160, 1200, 1200))
        assert camera.roi.x + camera.roi.width <= MOCK_SENSOR.width

    def test_nearest_helpers_on_empty_choices(self) -> None:
        info = CameraInfo(model="x")
        assert info.nearest_shutter(1 / 60) is None
        assert info.nearest_iso(100) is None


class TestCoolingContract:
    def test_temperature_and_target_reported(self) -> None:
        with MockCamera(temperature_c=22.0) as camera:
            assert camera.capabilities().cooling is True
            assert camera.get_temperature_c() == pytest.approx(22.0, abs=0.6)
            assert camera.get_target_temperature_c() == -5.0

    def test_temperature_moves_toward_target(self) -> None:
        import time
        with MockCamera(temperature_c=22.0) as camera:
            first = camera.get_temperature_c()
            time.sleep(0.3)
            second = camera.get_temperature_c()
        assert second < first  # cooling toward -5 °C

    def test_target_settable(self) -> None:
        with MockCamera() as camera:
            assert camera.set_target_temperature_c(-10.0) == -10.0
            assert camera.target_history == [-10.0]

    def test_uncold_camera_has_no_temperature(self) -> None:
        with MockCamera(cooling=False) as camera:
            assert camera.capabilities().cooling is False
            assert camera.get_temperature_c() is None
            with pytest.raises(CameraError):
                camera.set_target_temperature_c(-10.0)


class TestSessionWorkflow:
    def test_dark_then_flat_then_frames(self, session: CaptureSession) -> None:
        assert session.state.stage == "dark"
        session.capture_dark(1, average=1)
        assert session.state.stage == "flat"
        session.capture_flat(1, average=1)
        assert session.state.stage == "frames"
        assert session.state.has_calibration
        session.capture_scan()
        session.capture_scan()
        state = session.state
        assert (state.dark_count, state.flat_count, state.scan_count) == (1, 1, 2)

    def test_tiff_files_are_written(self, session: CaptureSession) -> None:
        session.capture_dark(1, average=1)
        files = list(session.paths.frames.glob("*.tif"))
        assert len(files) == 1

    def test_written_tiff_reads_back_16bit_mono(self, session: CaptureSession) -> None:
        """The archive promise across the real read path: what write_frame put
        in the file comes out at 6224x4168 with the 16-bit levels."""
        result = session.capture_scan()
        frame = open_frame(result.path)
        assert (frame.width, frame.height) == (MOCK_SENSOR.width, MOCK_SENSOR.height)
        assert frame.data.dtype == np.uint16 and frame.data.ndim == 2
        assert frame.black_level == 0.0 and frame.white_level == 65535.0

    def test_keep_live_view_reaches_the_backend(self, session) -> None:
        session.capture_scan()
        assert session.camera.last_keep_live_view is True
        session.keep_live_view = False
        session.capture_scan()
        assert session.camera.last_keep_live_view is False

    def test_sidecar_written_beside_raw(self, session: CaptureSession) -> None:
        result = session.capture_dark(1, average=1)[0]
        sidecar = session.paths.sidecar(result.path)
        assert sidecar.exists()
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        assert payload["kind"] == "dark"
        assert payload["film"]["film_id"] == "HP5_001"

    def test_never_modifies_the_raw_file(self, session: CaptureSession) -> None:
        """The archive promise: the capture file is byte-for-byte what we wrote."""
        result = session.capture_scan()
        before = result.path.read_bytes()
        session.export_project()
        assert result.path.read_bytes() == before

    def test_sidecar_records_calibration_settings(self, session: CaptureSession) -> None:
        session.camera.set_shutter(8.0)
        session.camera.set_gain(1.5)
        result = session.capture_dark(1, average=1)[0]
        payload = json.loads(session.paths.sidecar(result.path).read_text(encoding="utf-8"))
        assert payload["acquisition"]["exposure_time"] == pytest.approx(result.settings.shutter)
        assert payload["acquisition"]["gain"] == pytest.approx(1.5)

    def test_sidecar_records_readout_modes(self, session: CaptureSession) -> None:
        """LCG/HCG and low-noise change the DN scale — the sidecar must say
        which modes the frame was exposed in (user order 2026-09-20)."""
        session.camera.set_conversion_gain(hcg=True)
        session.camera.set_low_noise(False)
        result = session.capture_scan()
        payload = json.loads(session.paths.sidecar(result.path).read_text(encoding="utf-8"))
        assert payload["acquisition"]["conversion_gain"] == "HCG"
        assert payload["acquisition"]["low_noise"] is False

    def test_sidecar_records_sensor_temperature(self, session: CaptureSession) -> None:
        result = session.capture_dark(1, average=1)[0]
        payload = json.loads(session.paths.sidecar(result.path).read_text(encoding="utf-8"))
        assert payload["sensor_temperature_c"] == pytest.approx(
            result.sensor_temperature_c
        )

    def test_scan_average_stores_one_file_with_count(self, session) -> None:
        """Averaging is the operator's 2026-09-19 ruling: K exposures go in,
        ONE TIFF comes out, and the sidecar tells how many were averaged."""
        result = session.capture_scan(average=4)
        assert session.camera.last_capture_frames == 4
        assert len(list(session.paths.frames.glob("frame*.tif"))) == 1
        payload = json.loads(session.paths.sidecar(result.path).read_text(encoding="utf-8"))
        assert payload["acquisition"]["frames_averaged"] == 4

    def test_scan_average_default_is_single_exposure(self, session) -> None:
        result = session.capture_scan()
        assert session.camera.last_capture_frames == 1
        payload = json.loads(session.paths.sidecar(result.path).read_text(encoding="utf-8"))
        assert payload["acquisition"]["frames_averaged"] == 1

    def test_scan_average_progress_reports(self, session) -> None:
        seen: list[tuple[int, int]] = []
        session.capture_scan(average=3,
                             progress=lambda k, n: seen.append((k, n)))
        assert seen == [(1, 3), (2, 3), (3, 3)]

    def test_calibration_averages_ten_by_default(self, session) -> None:
        """Operator order 2026-09-19: dark/flat/base average CALIBRATION_AVERAGE
        exposures implicitly — one averaged TIFF per set, counted like the old
        single file, sidecar carrying the count."""
        dark = session.capture_dark()[0]
        flat = session.capture_flat()[0]
        assert session.camera.last_capture_frames == CALIBRATION_AVERAGE
        assert len(list(session.paths.frames.glob("dark_*.tif"))) == 1
        assert len(list(session.paths.frames.glob("flat_*.tif"))) == 1
        for result in (dark, flat):
            payload = json.loads(
                session.paths.sidecar(result.path).read_text(encoding="utf-8"))
            assert payload["acquisition"]["frames_averaged"] == CALIBRATION_AVERAGE

    def test_base_averages_by_default(self, session) -> None:
        result, _sample = session.capture_base()
        payload = json.loads(
            session.paths.sidecar(result.path).read_text(encoding="utf-8"))
        assert payload["acquisition"]["frames_averaged"] == CALIBRATION_AVERAGE

    def test_scan_average_value_does_not_touch_calibration(self, session) -> None:
        """The spinbox value is a scan setting passed explicitly per call;
        calibration keeps its own implicit average regardless."""
        scan = session.capture_scan(average=3)
        assert session.camera.last_capture_frames == 3
        dark = session.capture_dark()[0]
        assert session.camera.last_capture_frames == CALIBRATION_AVERAGE
        scan_payload = json.loads(
            session.paths.sidecar(scan.path).read_text(encoding="utf-8"))
        dark_payload = json.loads(
            session.paths.sidecar(dark.path).read_text(encoding="utf-8"))
        assert scan_payload["acquisition"]["frames_averaged"] == 3
        assert dark_payload["acquisition"]["frames_averaged"] == CALIBRATION_AVERAGE

    def test_frame_numbers_follow_film(self, session: CaptureSession) -> None:
        session.capture_scan(12)
        session.capture_scan(13)
        scans = session.catalog.captures("HP5_001", FrameKind.SCAN)
        assert sorted(s.frame_number for s in scans) == [12, 13]

    def test_auto_numbering_continues(self, session: CaptureSession) -> None:
        session.capture_scan(20)
        assert session.capture_scan().path.name in {c.path.name for c in session.camera.captures}
        assert session.state.next_frame_number == 22

    def test_frame_number_validation(self, session: CaptureSession) -> None:
        with pytest.raises(ValueError):
            session.capture_scan(0)

    def test_base_capture_archives_and_measures(self, session: CaptureSession) -> None:
        """A base frame is a full archive capture *plus* the rect measurement.

        Sidecar like a dark's (kind, exposure), and the mean DN over the
        requested sensor-px rect lands in film_base.json with the exposure it
        was taken at — the pair the scaling rule needs later.
        """
        session.camera.set_shutter(4.0)
        rect = (100, 100, 600, 500)
        result, sample = session.capture_base(rect, average=1)
        payload = json.loads(session.paths.sidecar(result.path).read_text(encoding="utf-8"))
        assert payload["kind"] == "base"
        assert payload["acquisition"]["exposure_time"] == pytest.approx(4.0)
        assert session.state.base_count == 1
        stored = session.base_samples()
        assert len(stored) == 1
        assert stored[0].kind == "frame"
        assert stored[0].shutter == pytest.approx(4.0)
        assert stored[0].rect == rect
        assert stored[0].mean_dn == pytest.approx(sample.mean_dn)
        # The measured mean must equal the frame's own region mean — proving
        # the rect was interpreted on the full-size frame, not the stream.
        frame = open_frame(result.path)
        assert sample.mean_dn == pytest.approx(
            float(frame.data[100:500, 100:600].mean()))

    def test_scan_records_crop_rect_in_sidecar(self, session: CaptureSession) -> None:
        """The red frame the operator drew rides into the scan's sidecar so the
        developer GUI can crop by it — stored in the full-size frame's own px,
        which is the grid the frame file is written at."""
        crop = (512, 300, 5800, 3900)
        result = session.capture_scan(1, crop_rect=crop)
        payload = json.loads(session.paths.sidecar(result.path).read_text(encoding="utf-8"))
        assert payload["crop_rect"] == list(crop)
        # And it survives the catalog round-trip the developer GUI reads from.
        scan = session.catalog.captures("HP5_001", FrameKind.SCAN)[0]
        assert scan.crop_rect == crop

    def test_scan_without_crop_rect_is_null(self, session: CaptureSession) -> None:
        """No rect drawn = no crop claim in the sidecar, not a whole-frame
        sentinel a later tool would have to second-guess."""
        result = session.capture_scan(1)
        payload = json.loads(session.paths.sidecar(result.path).read_text(encoding="utf-8"))
        assert payload["crop_rect"] is None

    def test_base_samples_append_in_order(self, session: CaptureSession) -> None:
        from filmscan_studio.core.filmbase import FilmBaseSample

        session.record_base_sample(FilmBaseSample(
            kind="stream", mean_dn=4000.0, black_level=0.0,
            white_level=65535.0, shutter=1.0, gain=1.0))
        session.record_base_sample(FilmBaseSample(
            kind="stream", mean_dn=8000.0, black_level=0.0,
            white_level=65535.0, shutter=2.0, gain=1.0))
        stored = session.base_samples()
        assert [s.mean_dn for s in stored] == [4000.0, 8000.0]

    def test_export_project_writes_json(self, session: CaptureSession) -> None:
        session.capture_dark(1, average=1)
        session.capture_flat(2, average=1)
        session.capture_scan()
        path, unmatched = session.export_project()
        assert unmatched == []   # mock temperature is stable: darks match
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["counts"] == {"scans": 1, "dark": 1, "flat": 2, "base": 0}
        assert payload["film"]["film_id"] == "HP5_001"
        assert payload["operator"] == "JG"

    def test_export_lists_scans_without_temperature_matching_dark(
        self, tmp_path: Path, film: FilmMetadata
    ) -> None:
        """The cooling rule (INSTRUCTIONS §7): a dark taken warm does not
        calibrate a scan taken cold — export must name the frame.

        The mock's TEC is time-accelerated (×20), so switching it off and
        idling a third of a second drifts the sensor degrees off the setpoint
        — the same physical situation the warning exists for.
        """
        camera = MockCamera()
        camera.connect()
        s = CaptureSession(camera, film, SessionPaths.create(tmp_path, film.film_id))
        s.capture_dark(1, average=1)                        # dark at the setpoint
        camera.set_tec_enabled(False)                       # sensor drifts warm
        import time
        time.sleep(0.4)
        scan = s.capture_scan()
        assert abs(scan.sensor_temperature_c - (-5.0)) > DARK_TEMPERATURE_TOLERANCE_C
        _, unmatched = s.export_project()
        assert unmatched == [1]

    def test_capture_without_temperature_is_unmatched(
        self, tmp_path: Path, film: FilmMetadata
    ) -> None:
        """Unknown is not the same as matching: an uncooled capture's scans
        are reported so the operator knows darks were never thermal-logged."""
        camera = MockCamera(cooling=False)
        camera.connect()
        s = CaptureSession(camera, film, SessionPaths.create(tmp_path, film.film_id))
        s.capture_dark(1, average=1)
        s.capture_scan()
        assert s.unmatched_scan_frame_numbers() == [1]

    def test_unreadable_raw_still_records_metadata(
        self, tmp_path: Path, film: FilmMetadata
    ) -> None:
        """A file tifffile cannot parse must not cost us the shot's metadata."""

        def broken(_path: Path):
            raise RuntimeError("tifffile: unreadable tag")

        camera = MockCamera()
        camera.connect()
        s = CaptureSession(
            camera, film, SessionPaths.create(tmp_path, film.film_id),
            frame_reader=broken,
        )
        result = s.capture_scan()
        payload = json.loads(s.paths.sidecar(result.path).read_text(encoding="utf-8"))
        assert payload["acquisition"]["gain"] == pytest.approx(result.settings.gain)
        assert payload["width"] is None
        assert s.state.scan_count == 1

    def test_capture_failure_is_reported_not_swallowed(
        self, tmp_path: Path, film: FilmMetadata
    ) -> None:
        class Failing(MockCamera):
            def capture(self, destination: Path, filename_stem: str,
                        keep_live_view: bool = True, frames: int = 1,
                        progress=None):
                raise RuntimeError("Čtení proudu selhalo")

        camera = Failing()
        camera.connect()
        s = CaptureSession(camera, film, SessionPaths.create(tmp_path, film.film_id))
        with pytest.raises(RuntimeError, match="Čtení proudu"):
            s.capture_scan()
        assert s.state.last_error == "Čtení proudu selhalo"


class TestLiveMeter:
    def test_metering_is_raw_dn_no_gamma(self) -> None:
        """The guard for the brief's rule: a linear frame meters as itself.

        There is no JPEG gamma to undo any more — if someone re-introduces a
        display transform in the meter path, this fails.
        """
        meter = LiveMeter()
        half = 32767.5
        reading = meter.meter_frame(
            LiveFrame(data=np.full((8, 8), 32768, np.uint16), width=8, height=8)
        )
        assert reading.signal_p999 == pytest.approx(half, rel=1e-3)
        assert reading.highlight_utilisation == pytest.approx(0.5, abs=1e-3)

    def test_decode_reads_a_real_frame(self) -> None:
        with MockCamera(live_fps=500) as camera:
            camera.start_live_view()
            frame = camera.next_live_frame()
        img = LiveMeter.decode_live_frame(frame)
        assert img.ndim == 2 and img.dtype == np.float64

    def test_normalized_frame_divides_by_the_frames_own_levels(self) -> None:
        meter = LiveMeter()
        frame = LiveFrame(
            data=np.array([[0, 1000], [500, 1000]], np.uint16),
            width=2, height=2, black_level=0.0, white_level=1000.0,
        )
        data, reading = meter.normalized_frame(frame)
        assert data.max() == pytest.approx(1.0)
        assert reading.white_level == 1.0
        assert reading.signal_p999 <= 1.0


class TestAutoExposure:
    """The loop must converge on a target that is a headroom below clipping."""

    @staticmethod
    def _reading(utilisation: float, clipped: bool = False) -> MeterReading:
        """Fake reading expressed as a fraction of the available range."""
        span = 1.0
        p999 = utilisation * span
        return MeterReading(
            black_level=0.0,
            white_level=1.0,
            signal_min=0.0,
            signal_median=0.1,
            signal_p999=p999,
            signal_max=1.0 if clipped else p999 * 1.01,
            clipped_fraction=0.01 if clipped else 0.0,
            near_black_fraction=0.0,
        )

    def test_adds_light_when_under(self) -> None:
        camera = MockCamera(settings=ExposureSettings(0.1, iso=None, gain=1.0))
        camera.connect()
        controller = AutoExposureController(camera, LiveMeter())
        result = controller.run(read=lambda: self._reading(0.05))
        assert result.achieved_ev_change > 0
        assert camera.shutter_history

    def test_removes_light_when_over(self) -> None:
        camera = MockCamera(settings=ExposureSettings(4.0, iso=None, gain=1.0))
        camera.connect()
        controller = AutoExposureController(camera, LiveMeter())
        result = controller.run(read=lambda: self._reading(0.95))
        assert result.achieved_ev_change < 0

    def test_no_change_when_already_at_target(self) -> None:
        camera = MockCamera(settings=ExposureSettings(1.0, iso=None, gain=1.0))
        camera.connect()
        target = 2.0 ** (-0.4)
        controller = AutoExposureController(camera, LiveMeter())
        result = controller.run(read=lambda: self._reading(target))
        assert result.achieved_ev_change == pytest.approx(0.0)
        assert camera.shutter_history == []
        assert result.converged

    def test_converges_with_a_responsive_camera(self) -> None:
        """With a camera whose signal tracks shutter*gain, the loop lands on target."""
        camera = MockCamera(scene_level=0.08,
                            settings=ExposureSettings(0.05, iso=None, gain=1.0))
        camera.connect()
        meter = LiveMeter()
        controller = AutoExposureController(camera, meter, max_iterations=8)
        result = controller.run(read=responsive_read(camera))
        assert result.reading.highlight_utilisation == pytest.approx(2.0**-0.4, abs=0.06)

    def test_gain_lock_solves_with_shutter_alone(self) -> None:
        """Archival rule: locked, a dark scene must never raise the gain."""
        camera = MockCamera(scene_level=0.02,
                            settings=ExposureSettings(0.1, iso=None, gain=1.0))
        camera.connect()
        controller = AutoExposureController(camera, LiveMeter(), gain_lock=True,
                                            max_iterations=6)
        result = controller.run(read=responsive_read(camera))
        assert camera.gain_history == []
        assert result.settings.gain == pytest.approx(ARCHIVE_GAIN)

    def test_unlocked_solves_clipped_scene_with_shutter_first(self) -> None:
        """Unlocked loop on a clipped frame: the shutter (continuous down to
        microseconds) absorbs it and gain stays untouched — gain is only the
        residual the shutter range cannot reach."""
        camera = MockCamera(settings=ExposureSettings(1.0, iso=None, gain=1.0))
        camera.connect()
        controller = AutoExposureController(camera, LiveMeter(), gain_lock=False)
        result = controller.run(read=lambda: self._reading(0.999, clipped=True))
        assert camera.gain_history == []
        assert not result.limited_by_gain
        assert camera.shutter_history and camera.shutter_history[-1] < 1.0

    def test_pinned_at_slowest_with_lock_reports_lighting_not_a_bug(self) -> None:
        """Even 1800 s too dark + lock: name the extreme and the residual.

        utilisation 4e-4 (not 1e-5): a p999 below one ten-thousandth of range
        is *at black* — required_ev_change rightly refuses to meter it and
        the controller enters its blind big-step path instead.
        """
        camera = MockCamera(settings=ExposureSettings(1800.0, iso=None, gain=1.0))
        camera.connect()
        controller = AutoExposureController(camera, LiveMeter(), gain_lock=True)
        result = controller.run(read=lambda: self._reading(4e-4))
        assert not result.converged
        assert result.limited_by_lens
        assert "1800" in result.limit_note and "tmavší" in result.limit_note
        assert "gain uzamčen" in result.limit_note

    def test_on_step_previews_proposed_settings(self) -> None:
        camera = MockCamera(settings=ExposureSettings(0.25, iso=None, gain=1.0))
        camera.connect()
        seen: list[ExposureSettings] = []
        controller = AutoExposureController(camera, LiveMeter(), max_iterations=2)
        controller.run(read=lambda: self._reading(0.02), on_step=seen.append)
        assert seen and all(s.gain == ARCHIVE_GAIN for s in seen)

    def test_gain_is_never_touched_with_the_archival_lock(self) -> None:
        """Shutter is the default actuator; gain costs noise on a studio scan."""
        camera = MockCamera(settings=ExposureSettings(0.001, iso=None, gain=4.0))
        camera.connect()
        controller = AutoExposureController(camera, LiveMeter(), gain_lock=True)
        controller.run(read=lambda: self._reading(0.01))
        assert camera.gain_history == []

    def test_meters_only_frames_exposed_with_the_current_shutter(self) -> None:
        """The hardware-caused non-convergence: a frame pulled right after
        set_shutter was still exposed with the *old* shutter (it was in
        flight over USB). The controller must swap past it via the frame's
        own expotime stamp, or every reading lags one setting behind and the
        loop oscillates instead of converging."""
        from filmscan_studio.capture.autoexposure import fresh_live_frame
        from filmscan_studio.capture.camera import LiveFrame

        class StampingCamera(MockCamera):
            """Mock that stamps each frame with the shutter at *shot* time
            and delays a frame's exposure update by one frame, like USB."""

            def next_live_frame(self):
                frame = super().next_live_frame()
                if frame is not None:
                    frame = LiveFrame(
                        data=frame.data, width=frame.width, height=frame.height,
                        black_level=frame.black_level,
                        white_level=frame.white_level,
                        expotime_us=int(self._shot_shutter * 1e6))
                    self._shot_shutter = self.get_settings().shutter
                return frame

        cam = StampingCamera(settings=ExposureSettings(1.0, iso=None, gain=1.0))
        cam._shot_shutter = 1.0          # frames so far shot at 1 s
        cam.connect()
        cam.start_live_view()
        cam.set_shutter(4.0)             # in-flight frames still carry 1 s
        frame = fresh_live_frame(cam)
        assert frame.expotime_us == 4_000_000

    def test_fresh_frame_helper_passes_through_unstamped_backends(self) -> None:
        from filmscan_studio.capture.autoexposure import fresh_live_frame

        cam = MockCamera()
        cam.connect()
        cam.start_live_view()
        assert fresh_live_frame(cam).expotime_us is None

    def test_headroom_is_configurable(self) -> None:
        camera = MockCamera(settings=ExposureSettings(1.0, iso=None, gain=1.0))
        camera.connect()
        other = MockCamera(settings=ExposureSettings(1.0, iso=None, gain=1.0))
        other.connect()
        tight = AutoExposureController(camera, LiveMeter(), headroom_ev=0.05)
        loose = AutoExposureController(other, LiveMeter(), headroom_ev=1.5)
        a = tight.run(read=lambda: self._reading(0.5))
        b = loose.run(read=lambda: self._reading(0.5))
        assert a.achieved_ev_change > b.achieved_ev_change

    def test_clipping_is_surfaced(self) -> None:
        camera = MockCamera(settings=ExposureSettings(1.0, iso=None, gain=1.0))
        camera.connect()
        controller = AutoExposureController(camera, LiveMeter())
        result = controller.run(read=lambda: self._reading(0.999, clipped=True))
        assert result.clipped

    def test_clip_step_is_bigger_than_the_blind_residual(self) -> None:
        """The meter cannot see past the rail: a clipped frame must move by
        more than the 0.4 EV blind reading implies, or escaping a 5-stop
        overexposure would take 12 iterations."""
        camera = MockCamera(settings=ExposureSettings(60.0, iso=None, gain=1.0))
        camera.connect()
        controller = AutoExposureController(camera, LiveMeter(), max_iterations=1)
        controller.run(read=lambda: self._reading(1.0, clipped=True))
        applied = camera.shutter_history[0]
        assert applied <= 60.0 / 2.5   # at least ~1.3 stops in one step


def responsive_read(camera: MockCamera):
    """Metering callback wired to the mock's own radiometry."""
    def read() -> MeterReading:
        return _reading_of_dn(camera.sensor_signal())
    return read


def _reading_of_dn(dn: float) -> MeterReading:
    util = dn / 65535.0
    return MeterReading(
        black_level=0.0, white_level=1.0,
        signal_min=util * 0.5, signal_median=util * 0.8,
        signal_p999=util, signal_max=min(util * 1.01, 1.0),
        clipped_fraction=util >= 1.0 and 0.01 or 0.0,
        near_black_fraction=0.0,
    )


class TestFilmOrientation:
    """2026-09 (uživatel): volně kombinovatelné atributy orientace filmu.

    Zrcadlo H + zrcadlo V JE rotace 180° — proto všechen najednou zakazuje
    model (vyrušilo by se to v identitu). Dva najednou zůstávají platné.
    """

    def test_defaults_are_all_off(self):
        film = FilmMetadata(film_id="O1")
        assert not film.mirrored_horizontal
        assert not film.mirrored_vertical
        assert not film.rotated_180

    def test_each_flag_alone_is_valid(self):
        for flag in ("mirrored_horizontal", "mirrored_vertical", "rotated_180"):
            film = FilmMetadata(film_id="O1", **{flag: True})
            assert getattr(film, flag) is True

    def test_any_two_flags_combine(self):
        # H+V = rotace 180 vyjádřená přes zrcadla — platná kombinace.
        FilmMetadata(film_id="O1", mirrored_horizontal=True,
                     mirrored_vertical=True)
        FilmMetadata(film_id="O1", mirrored_horizontal=True, rotated_180=True)
        FilmMetadata(film_id="O1", mirrored_vertical=True, rotated_180=True)

    def test_all_three_rejected_as_identity(self):
        # H+V je 180°; přidat rot_180 navzáď = nic (identita).
        with pytest.raises(Exception, match="vyru"):
            FilmMetadata(film_id="O1", mirrored_horizontal=True,
                         mirrored_vertical=True, rotated_180=True)

    def test_roundtrip_and_retired_mirrored_still_stripped(self):
        film = FilmMetadata(film_id="O1", mirrored_horizontal=True)
        again = FilmMetadata.model_validate_json(film.model_dump_json())
        assert again == film and again.mirrored_horizontal
        # Staré jednopříznakové „mirrored" se stále maže při načtení —
        # nové atributy mají jiná jména a zůstávají.
        old = {"film_id": "OLD", "mirrored": True,
               "mirrored_horizontal": True}
        loaded = FilmMetadata.model_validate(old)
        assert not hasattr(loaded, "mirrored")
        assert loaded.mirrored_horizontal


class TestFilmMetadataV2:
    """Schema v2: name+developer collapsed, rig fields added, v1 readable."""

    def test_v1_sidecar_migrates_on_load(self):
        v1 = {
            "schema_version": 1,
            "film_id": "HP5_001",
            "manufacturer": "Ilford",
            "film_type": "HP5+",
            "developer": "ID-11",
            "developer_dilution": "1+1",
            "development_time": "13 min",
        }
        film = FilmMetadata.model_validate(v1)
        assert film.film_name == "Ilford HP5+"
        assert film.development == "ID-11 1+1 13 min"
        assert film.film_id == "HP5_001"

    def test_v1_migration_keeps_explicit_v2_fields(self):
        v1 = {
            "film_id": "X",
            "manufacturer": "Foma",
            "film_type": "100 Classic",
            "film_name": "KEEP ME",
        }
        assert FilmMetadata.model_validate(v1).film_name == "KEEP ME"

    def test_unknown_fields_still_rejected(self):
        with pytest.raises(Exception):
            FilmMetadata.model_validate({"film_id": "X", "typo_field": 1})

    def test_label_prefers_film_name(self):
        assert FilmMetadata(film_id="F1", film_name="Fomapan 100").label() == "Fomapan 100"
        assert FilmMetadata(film_id="F1").label() == "F1"

    def test_rig_defaults_subset(self):
        film = FilmMetadata(
            film_id="F1", film_name="Fomapan 100", camera="TS2600MP-G2",
            digitising_light="CRS LED", development="R09 8 min",
            content="hrady",
        )
        rig = film.rig_defaults()
        assert rig["camera"] == "TS2600MP-G2"
        assert rig["digitising_light"] == "CRS LED"
        # The film's own identity and its development log never carry over.
        assert "film_name" not in rig
        assert "development" not in rig
        assert "content" not in rig

    def test_all_optional_fields_may_stay_empty(self):
        film = FilmMetadata(film_id="F1")
        assert film.film_name is None
        assert film.digitisation_date is None

    def test_roundtrip_json(self):
        film = FilmMetadata(film_id="F2", film_name="Astia 100",
                            film_type_class=FilmType.COLOR_NEGATIVE,
                            development_start="asi 12/25")
        again = FilmMetadata.model_validate_json(film.model_dump_json())
        assert again == film

    def test_retired_dslr_keys_migrate_on_load(self, tmp_path):
        """Archived sidecars still carry the fields the mono Touptek made
        meaningless. extra="forbid" would reject them and lock the operator out
        of old projects, so they are stripped on load — but only *those* keys;
        a genuine typo must still fail loudly."""
        old_film = {"film_id": "OLD_1", "mirrored": True,
                    "manufacturer": "Foma", "film_type": "400 Classic"}
        film = FilmMetadata.model_validate(old_film)
        assert not hasattr(film, "mirrored")
        assert film.film_name == "Foma 400 Classic"    # v1 merge still works

        old_acq = {"camera": "D750", "lens": "Nikon 50/1.8", "lens_serial": "123",
                   "f_number": 8.0, "focus_distance_m": 0.5,
                   "white_balance": "daylight", "raw_developer": "Camera Neutral",
                   "exposure_time": 4.0}
        acq = AcquisitionMetadata.model_validate(old_acq)
        assert acq.exposure_time == 4.0
        # Sidecars written before averaging (2026-09-19) carry no
        # frames_averaged key — the default is the truth for them.
        assert acq.frames_averaged == 1
        for gone in ("lens", "lens_serial", "f_number", "focus_distance_m",
                     "white_balance", "raw_developer"):
            assert not hasattr(acq, gone)

        # The guard that makes the migration safe: an unknown key is an error.
        with pytest.raises(Exception, match="typo"):
            AcquisitionMetadata.model_validate({"typo_field": 1})
