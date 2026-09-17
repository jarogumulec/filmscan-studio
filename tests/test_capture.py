"""Tests for the capture layer: camera contract, session workflow, auto exposure.

Everything here runs against :class:`MockCamera`, which enforces the D750's real
shutter and ISO ladders so that snapping behaviour and the auto-exposure loop are
tested against actual constraints rather than idealised ones.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from filmscan_studio.capture.autoexposure import AutoExposureController, LiveMeter
from filmscan_studio.capture.camera import CameraInfo, LiveFrame, NotConnectedError
from filmscan_studio.capture.gphoto2 import parse_iso, parse_shutter
from filmscan_studio.capture.mock import D750_ISOS, D750_SHUTTERS, MockCamera
from filmscan_studio.capture.session import CaptureSession, SessionPaths
from filmscan_studio.core.exposure import ExposureSettings, MeterReading
from filmscan_studio.core.models import AcquisitionMetadata, FilmMetadata, FilmType, FrameKind
from filmscan_studio.core.rawio import RawFrame


@pytest.fixture
def film() -> FilmMetadata:
    return FilmMetadata(
        film_id="HP5_001",
        film_name="Ilford HP5+",
        camera="Nikon D750",
        format="35mm",
        film_type_class=FilmType.BW_NEGATIVE,
        development="ID-11 1+1 13 min",
        operator="JG",
    )


def npy_frame_reader(path: Path) -> RawFrame:
    """Stand-in for rawpy.open_frame against the mock's .npy output."""
    data = np.load(path)
    return RawFrame(
        path=path,
        data=data,
        black_level=600.0,
        white_level=16383.0,
        color_desc="RGGB",
        width=data.shape[1],
        height=data.shape[0],
        acquisition=AcquisitionMetadata(camera="NIKON D750", lens="Micro Nikkor 60/2.8"),
    )


@pytest.fixture
def session(tmp_path: Path, film: FilmMetadata) -> CaptureSession:
    camera = MockCamera()
    camera.connect()
    return CaptureSession(
        camera, film, SessionPaths.create(tmp_path, film.film_id), frame_reader=npy_frame_reader
    )


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

    def test_iso_parse(self) -> None:
        assert parse_iso("100") == 100
        assert parse_iso("Hi 25600") == 25600


class TestCameraContract:
    def test_context_manager_connects_and_disconnects(self) -> None:
        with MockCamera() as camera:
            assert camera.info.model.startswith("NIKON D750")
        with pytest.raises(NotConnectedError):
            camera.get_settings()

    def test_shutter_snaps_to_body_ladder(self) -> None:
        with MockCamera() as camera:
            applied = camera.set_shutter(1 / 61)
            assert applied == pytest.approx(1 / 60)

    def test_iso_snaps(self) -> None:
        with MockCamera() as camera:
            assert camera.set_iso(110) in D750_ISOS

    def test_aperture_is_not_offered(self) -> None:
        with MockCamera() as camera:
            assert camera.capabilities().aperture is False

    def test_live_frames_only_while_running(self) -> None:
        with MockCamera(live_fps=200) as camera:
            assert camera.next_live_frame() is None
            camera.start_live_view()
            assert camera.next_live_frame() is not None
            camera.stop_live_view()
            assert camera.next_live_frame() is None

    def test_nearest_helpers_on_empty_choices(self) -> None:
        info = CameraInfo(model="x")
        assert info.nearest_shutter(1 / 60) is None
        assert info.nearest_iso(100) is None


class TestSessionWorkflow:
    def test_dark_then_flat_then_frames(self, session: CaptureSession) -> None:
        assert session.state.stage == "dark"
        session.capture_dark(1)
        assert session.state.stage == "flat"
        session.capture_flat(3)
        assert session.state.stage == "frames"
        assert session.state.has_calibration
        session.capture_scan()
        session.capture_scan()
        state = session.state
        assert (state.dark_count, state.flat_count, state.scan_count) == (1, 3, 2)

    def test_raw_files_are_written(self, session: CaptureSession) -> None:
        session.capture_dark(1)
        files = list(session.paths.frames.glob("*.npy"))
        assert len(files) == 1

    def test_keep_live_view_reaches_the_backend(self, session, film) -> None:
        """Mirror-up capture: the session passes the flag through, and flipping
        it off (after a body refused) reaches the next capture too."""
        session.capture_scan()
        assert session.camera.last_keep_live_view is True
        session.keep_live_view = False
        session.capture_scan()
        assert session.camera.last_keep_live_view is False

    def test_sidecar_written_beside_raw(self, session: CaptureSession) -> None:
        result = session.capture_dark(1)[0]
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
        session.camera.set_shutter(1 / 8)
        session.camera.set_iso(200)
        result = session.capture_dark(1)[0]
        payload = json.loads(session.paths.sidecar(result.path).read_text(encoding="utf-8"))
        assert payload["acquisition"]["exposure_time"] == pytest.approx(result.settings.shutter)
        assert payload["acquisition"]["iso"] == result.settings.iso

    def test_sidecar_carries_lens_from_exif(self, session: CaptureSession) -> None:
        result = session.capture_scan()
        payload = json.loads(session.paths.sidecar(result.path).read_text(encoding="utf-8"))
        assert payload["acquisition"]["lens"] == "Micro Nikkor 60/2.8"

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

    def test_export_project_writes_json(self, session: CaptureSession) -> None:
        session.capture_dark(1)
        session.capture_flat(2)
        session.capture_scan()
        path = session.export_project()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["counts"] == {"scans": 1, "dark": 1, "flat": 2}
        assert payload["film"]["film_id"] == "HP5_001"
        assert payload["operator"] == "JG"

    def test_catalog_export_is_readable(self, session: CaptureSession) -> None:
        session.capture_dark(1)
        session.export_project()
        dump = json.loads((session.paths.root / "project_export.json").read_text(encoding="utf-8"))
        assert len(dump["captures"]) == 1
        assert dump["films"][0]["film_id"] == "HP5_001"

    def test_unreadable_raw_still_records_metadata(
        self, tmp_path: Path, film: FilmMetadata
    ) -> None:
        """A file LibRaw cannot parse must not cost us the shot's metadata."""

        def broken(_path: Path) -> RawFrame:
            raise RuntimeError("LibRaw: unsupported format")

        camera = MockCamera()
        camera.connect()
        s = CaptureSession(
            camera,
            film,
            SessionPaths.create(tmp_path, film.film_id),
            frame_reader=broken,
        )
        result = s.capture_scan()
        payload = json.loads(s.paths.sidecar(result.path).read_text(encoding="utf-8"))
        assert payload["acquisition"]["iso"] == result.settings.iso
        assert payload["width"] is None
        assert s.state.scan_count == 1

    def test_capture_failure_is_reported_not_swallowed(
        self, tmp_path: Path, film: FilmMetadata
    ) -> None:
        class Failing(MockCamera):
            def capture(self, destination: Path, filename_stem: str,
                        keep_live_view: bool = True):
                raise RuntimeError("Závěrka se nespustila")

        camera = Failing()
        camera.connect()
        s = CaptureSession(
            camera, film, SessionPaths.create(tmp_path, film.film_id), frame_reader=npy_frame_reader
        )
        with pytest.raises(RuntimeError, match="Závěrka"):
            s.capture_scan()
        assert s.state.last_error == "Závěrka se nespustila"

    def test_jpeg_disguised_as_nef_is_recorded_honestly(
        self, tmp_path: Path, film: FilmMetadata
    ) -> None:
        """Body at Compression Level != RAW delivers JPEG at the .NEF path.
        The sidecar must say jpeg with SOF geometry — and rawpy must never be
        called (that call is the b'Input/output error' in the 2026-09 log)."""
        import cv2

        jpeg = cv2.imencode(".jpg", np.zeros((2008, 3008, 3), np.uint8))[1].tobytes()

        class JpegBody(MockCamera):
            def capture(self, destination: Path, filename_stem: str,
                        keep_live_view: bool = True):
                target = destination / f"{filename_stem}.NEF"
                target.write_bytes(jpeg)
                from filmscan_studio.capture.camera import CaptureResult
                return CaptureResult(
                    path=target, size_bytes=len(jpeg), settings=self._settings,
                    elapsed=0.1, capture_target="card", file_format="jpeg")

        def never_reader(_path: Path) -> RawFrame:
            raise AssertionError("rawpy musí na JPEG nikdy sahnout")

        camera = JpegBody()
        camera.connect()
        s = CaptureSession(camera, film, SessionPaths.create(tmp_path, film.film_id),
                           frame_reader=never_reader)
        result = s.capture_scan()
        payload = json.loads(s.paths.sidecar(result.path).read_text(encoding="utf-8"))
        assert payload["file_format"] == "jpeg"
        assert payload["width"] == 3008 and payload["height"] == 2008
        assert payload["acquisition"]["iso"] == result.settings.iso
        assert s.state.scan_count == 1


class TestJpegDimensions:
    def test_reads_sof_without_decoding(self, tmp_path: Path) -> None:
        import cv2

        from filmscan_studio.core.rawio import jpeg_dimensions

        buf = np.zeros((64, 96, 3), np.uint8)
        path = tmp_path / "a.jpg"
        path.write_bytes(cv2.imencode(".jpg", buf)[1].tobytes())
        assert jpeg_dimensions(path) == (96, 64)

    def test_returns_none_for_non_jpeg(self, tmp_path: Path) -> None:
        from filmscan_studio.core.rawio import jpeg_dimensions

        path = tmp_path / "b.nef"
        path.write_bytes(b"II*\x00never a jpeg")
        assert jpeg_dimensions(path) is None


class TestLiveMeter:
    def test_jpeg_is_linearised_before_metering(self) -> None:
        """A mid-grey Live View JPEG must meter as its linear value, not 0.5.

        This is the guard for the brief's rule that metering happens on linear
        data: if someone removes the gamma undo, this test fails.
        """
        meter = LiveMeter()
        encoded = np.full((8, 8, 3), 0.5)
        reading = meter.meter_raw_signal(meter.jpeg_to_linear(encoded), 0.0, 1.0)
        assert reading.signal_p999 == pytest.approx(0.5**2.2)

    def test_decode_rejects_garbage(self) -> None:
        with pytest.raises(ValueError):
            LiveMeter.decode_live_frame(LiveFrame(jpeg=b"not a jpeg"))

    def test_decode_reads_a_real_frame(self) -> None:
        with MockCamera(live_fps=500) as camera:
            camera.start_live_view()
            frame = camera.next_live_frame()
        img = LiveMeter.decode_live_frame(frame)
        assert img.ndim == 3 and img.max() <= 1.0


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
        camera = MockCamera(settings=ExposureSettings(1 / 250, 100))
        camera.connect()
        camera.start_live_view()
        # Scene so dark the meter asks for more than two stops.
        controller = AutoExposureController(camera, LiveMeter())
        result = controller.run(read=lambda: self._reading(0.05))
        assert result.achieved_ev_change > 0
        assert camera.shutter_history

    def test_removes_light_when_over(self) -> None:
        camera = MockCamera(settings=ExposureSettings(1 / 30, 100))
        camera.connect()
        controller = AutoExposureController(camera, LiveMeter())
        result = controller.run(read=lambda: self._reading(0.95))
        assert result.achieved_ev_change < 0

    def test_no_change_when_already_at_target(self) -> None:
        camera = MockCamera(settings=ExposureSettings(1 / 60, 100))
        camera.connect()
        target = 2.0 ** (-0.4)
        controller = AutoExposureController(camera, LiveMeter())
        result = controller.run(read=lambda: self._reading(target))
        assert result.achieved_ev_change == pytest.approx(0.0)
        assert camera.shutter_history == []

    def test_converges_with_a_responsive_camera(self) -> None:
        """With a camera whose signal tracks shutter, the loop must land on target."""
        camera = MockCamera(scene_level=0.08, settings=ExposureSettings(1 / 1000, 100))
        camera.connect()
        meter = LiveMeter()

        state = {"last": None}

        def read() -> MeterReading:
            dn = camera.sensor_signal()
            reading = meter.meter_raw_signal(np.array([[dn]]), 600.0, 16383.0)
            state["last"] = reading
            return reading

        controller = AutoExposureController(camera, meter, max_iterations=8)
        result = controller.run(read=read)
        assert result.reading.highlight_utilisation == pytest.approx(2.0**-0.4, abs=0.06)

    def test_reports_lens_limit_not_a_bug(self) -> None:
        """When even the slowest shutter is too fast, say so explicitly."""
        camera = MockCamera(settings=ExposureSettings(min(D750_SHUTTERS), 100))
        camera.connect()
        controller = AutoExposureController(camera, LiveMeter())
        result = controller.run(read=lambda: self._reading(0.001))
        assert result.limited_by_lens
        assert not result.converged

    def test_on_step_previews_proposed_settings(self) -> None:
        camera = MockCamera(settings=ExposureSettings(1 / 250, 100))
        camera.connect()
        seen: list[ExposureSettings] = []
        controller = AutoExposureController(camera, LiveMeter(), max_iterations=2)
        controller.run(read=lambda: self._reading(0.02), on_step=seen.append)
        assert seen and all(s.iso == 100 for s in seen)

    def test_iso_is_never_touched(self) -> None:
        """Shutter is the default actuator; ISO costs noise on a studio scan."""
        camera = MockCamera(settings=ExposureSettings(1 / 1000, 400))
        camera.connect()
        controller = AutoExposureController(camera, LiveMeter())
        controller.run(read=lambda: self._reading(0.01))
        assert camera.iso_history == []

    def test_headroom_is_configurable(self) -> None:
        camera = MockCamera(settings=ExposureSettings(1 / 60, 100))
        camera.connect()
        tight = AutoExposureController(camera, LiveMeter(), headroom_ev=0.05)
        loose = AutoExposureController(MockCamera(settings=ExposureSettings(1 / 60, 100)), LiveMeter(), headroom_ev=1.5)
        loose.camera.connect()
        a = tight.run(read=lambda: self._reading(0.5))
        b = loose.run(read=lambda: self._reading(0.5))
        assert a.achieved_ev_change > b.achieved_ev_change

    def test_clipping_is_surfaced(self) -> None:
        camera = MockCamera(settings=ExposureSettings(1 / 60, 100))
        camera.connect()
        controller = AutoExposureController(camera, LiveMeter())
        result = controller.run(read=lambda: self._reading(0.999, clipped=True))
        assert result.clipped


class BodyMeterCamera(MockCamera):
    """MockCamera plus the SDK backend's ``exposure_ev`` behaviour.

    The D750's ExposureStatus cap reports ``log2(applied / correctly_exposed)``
    and answered *exactly* in stops on the measured body (2026-09-15): -3.00
    for three stops of shutter, +4.00 for four of ISO. Correct exposure here
    means shutter*iso == ``reference`` — the same convention as
    ``MockCamera.sensor_signal``'s reference term.
    """

    def __init__(self, reference: float = 1 / 60 * 100, **kwargs) -> None:
        super().__init__(**kwargs)
        self._reference = reference

    def exposure_ev(self) -> float:
        s = self.get_settings()
        return math.log2(s.shutter * s.iso / self._reference)


class TestAutoExposureBodyMeter:
    """The 2026-09 rewrite: the SDK's Live View stream is auto-brightness, so
    the controller closes the loop on the body's own exposure meter instead —
    and must therefore converge in ONE press, not one stop per click."""

    def test_without_body_meter_falls_back_to_lv_path(self) -> None:
        camera = MockCamera(settings=ExposureSettings(1 / 60, 100))
        camera.connect()
        # MockCamera has no exposure_ev: run() without `read` must use LV
        # metering and report no body_ev.
        controller = AutoExposureController(camera, LiveMeter())
        result = controller.run(read=lambda: TestAutoExposure._reading(0.05))
        assert result.body_ev is None

    def test_one_press_solves_a_three_stop_move(self) -> None:
        # 1/500 ISO 100 is 2.97 stops under correct -> must land on target in
        # a single actuator write, shutter first, ISO untouched.
        camera = BodyMeterCamera(settings=ExposureSettings(1 / 500, 100))
        camera.connect()
        controller = AutoExposureController(camera, LiveMeter())
        result = controller.run()
        assert result.converged
        assert result.iterations == 1
        assert abs(result.body_ev) <= 0.06
        assert result.limited_by_lens is False and result.limit_note is None
        # Quality policy: shutter ladder buys the light, ISO stays at floor.
        assert camera.iso_history == [] or result.settings.iso == 100
        assert result.settings.shutter > 1 / 500

    def test_shutter_bottomed_out_raises_iso_instead_of_lying(self) -> None:
        """The exact complaint of 2026-09: 'nejmenší dostupný čas' at 1/50
        while the real fix was ISO. With the shutter already at the slowest
        rung the controller must climb ISO and converge, no limit report."""
        # Scene needs 3 stops more than 30 s ISO 100 delivers.
        camera = BodyMeterCamera(reference=2 ** 3 * 30 * 100,
                                 settings=ExposureSettings(
                                     max(D750_SHUTTERS), 100))
        camera.connect()
        controller = AutoExposureController(camera, LiveMeter())
        result = controller.run()
        assert result.converged, result.limit_note
        assert result.settings.iso > 100
        assert result.settings.shutter == max(D750_SHUTTERS)

    def test_darkening_lowers_iso_before_shortening(self) -> None:
        # 4 stops over: ISO 1600 -> 100 is exactly four stops, free quality.
        camera = BodyMeterCamera(settings=ExposureSettings(1 / 60, 1600))
        camera.connect()
        controller = AutoExposureController(camera, LiveMeter())
        result = controller.run()
        assert result.converged, result.limit_note
        assert result.settings.iso == 100
        assert result.settings.shutter == pytest.approx(1 / 60)

    def test_move_beyond_both_ladders_reports_real_extremes(self) -> None:
        # Scene 19 stops dark; the ladders' full travel is 30 s (+9) at
        # ISO 25600 (+8) = 17 EV — two EV short, and the note must say so.
        camera = BodyMeterCamera(reference=2 ** 19 / 60 * 100,
                                 settings=ExposureSettings(1 / 60, 100))
        camera.connect()
        controller = AutoExposureController(camera, LiveMeter())
        result = controller.run()
        assert not result.converged
        assert result.limited_by_lens and result.limited_by_iso
        assert "30" in result.limit_note and "25600" in result.limit_note
        assert "tmavší" in result.limit_note

    def test_straddle_converges_with_residual_note(self) -> None:
        """Target between two neighbouring rungs: no finer step exists, so it
        is honest convergence — the note reports the residue, no fake limit."""
        # 1/60 ISO 100 correct; move +0.3 EV: no ladder pair lands within eps.
        camera = BodyMeterCamera(reference=2 ** 3.0 / 60 * 100 * 2 ** 0.3,
                                 settings=ExposureSettings(1 / 60, 100))
        camera.connect()
        controller = AutoExposureController(camera, LiveMeter())
        result = controller.run()
        # Either it landed exactly (some ladder pair hits it) or it converged
        # as a straddle: never a limit report, always a note when imperfect.
        assert result.converged, result.limit_note
        assert not result.limited_by_lens and not result.limited_by_iso
        if abs(result.body_ev or 0.0) > 0.06:
            assert "jemnější krok" in (result.limit_note or "")

    def test_stops_on_no_move_available_without_claiming_limits(self) -> None:
        """Iteration-cap/dither residue must not be reported as a hardware
        limit — the old bug was exactly this confident lie."""
        camera = BodyMeterCamera(settings=ExposureSettings(1 / 60, 100))
        camera.connect()
        evs = iter([-8.0, 8.0, -7.0, 7.0])     # never converges: oscillates
        controller = AutoExposureController(camera, LiveMeter(),
                                            max_iterations=2)
        controller._body_meter = lambda: (lambda: next(evs))
        result = controller.run()
        assert not result.converged
        assert not result.limited_by_lens and not result.limited_by_iso
        assert "zkus Auto Exposure znovu" in result.limit_note


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
            film_id="F1", film_name="Fomapan 100", camera="D750",
            digitising_light="CRS LED", mirrored=True, development="R09 8 min",
            content="hrady",
        )
        rig = film.rig_defaults()
        assert rig["camera"] == "D750"
        assert rig["digitising_light"] == "CRS LED"
        assert rig["mirrored"] is True
        # The film's own identity and its development log never carry over.
        assert "film_name" not in rig
        assert "development" not in rig
        assert "content" not in rig

    def test_all_optional_fields_may_stay_empty(self):
        film = FilmMetadata(film_id="F1")
        assert film.film_name is None and not film.mirrored
        assert film.digitisation_date is None

    def test_roundtrip_json(self):
        film = FilmMetadata(film_id="F2", film_name="Astia 100",
                            film_type_class=FilmType.COLOR_NEGATIVE,
                            development_start="asi 12/25", mirrored=True)
        again = FilmMetadata.model_validate_json(film.model_dump_json())
        assert again == film
