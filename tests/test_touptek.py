"""The Touptek backend against a scripted fake of the SDK handle.

No camera is on hand, so the *contract with the SDK* is what is pinned here:
which options are written in what order, that BINNING/ROI only ever change on
a stopped stream, that frames are pulled at 16 bits into the sized buffer,
that the permille/µs/0.1 °C unit exchanges are right, and that capture ends
in a readable full-sensor TIFF. Every fake assertion is an INSTRUCTIONS §11
hardware-checklist item in waiting: when the real camera contradicts one, the
matching test must fail, not silently pass.
"""

from __future__ import annotations


import queue

import numpy as np
import pytest

from filmscan_studio.capture import touptek
from filmscan_studio.capture._toupcam import toupcam as sdk
from filmscan_studio.capture.camera import CameraError, NotConnectedError
from filmscan_studio.capture.touptek import (
    COLOR_ONLY_OPTIONS,
    EXPO_TIME_RANGE_US,
    MONO_ONLY_OPTIONS,
    RAW_OPTIONS,
    TouptekCamera,
    apply_raw_contract,
    disable_camera_autoexposure,
    even_roi,
    gain_to_value,
    options_for_flags,
    parse_gain_range,
    sensor_from_device,
    shutter_to_us,
)
from filmscan_studio.core.rawio import open_frame
from filmscan_studio.core.zoom import (
    NO_BINNING,
    OVERVIEW_BINNING,
    Roi,
    SensorSize,
)

SENSOR = SensorSize(6224, 4168)


def _const(name: str) -> int:
    return getattr(sdk, f"TOUPCAM_OPTION_{name}")


class FakeHcam:
    """Records every call; answers plausibly; enforces stream-state rules."""

    #: Options the REAL ATR2600M accepts on a running stream (hardware-checked
    #: 2026-09-20: CG and LOW_NOISE writes land during a live stream; only
    #: geometry (BINNING/ROI) is E_WRONG_THREAD there).
    STREAM_SAFE_OPTIONS = frozenset({_const("CG"), _const("LOW_NOISE")})

    def __init__(self, *, options: dict[int, int] | None = None,
                 size: tuple[int, int] = (6224, 4168),
                 gain_range=(100, 10000, 100), refuse_options=(),
                 autoexpo: int = 0) -> None:
        # size is what get_Size answers — hardware: always the sensor
        # resolution, binning/ROI notwithstanding. Delivered frames come
        # from delivered_size().
        # gain_range mirrors the real ATR2600M: percent Gain Values,
        # (100, 10000, 100) = 1x..100x (hardware-checked get_ExpoAGainRange).
        self.calls: list[tuple] = []
        self.options = dict(options or {})
        self.refused = set(refuse_options)
        self._size = size
        self._gain_range = gain_range
        self._expo_us = 1_000_000
        #: Hardware-measured 2026-09-18: the real ATR2600M reports
        #: info.v3.expotime = 0 on every live frame *and* still — it never
        #: stamps its exposure. The fake defaults to that; a test that wants
        #: the SDK-documented stamping opts in explicitly.
        self._stamp_expotime = False
        self._gain_value = 100         # percent: 100 = 1.00x
        self._temperature = -52      # -5.2 °C in 0.1 units
        self._autoexpo = autoexpo    # camera-side AE state, persists in flash
        # A mono camera has no colour pipeline: reads of colour options fail
        # the way they fail on the real ATR2600M.
        self.unreadable_options = {getattr(sdk, f"TOUPCAM_OPTION_{name}")
                                   for name, _, _ in COLOR_ONLY_OPTIONS}
        self.stream_running = False
        self.roi: tuple[int, int, int, int] | None = None
        self.pulls: list[tuple] = []
        self.waited: list[tuple] = []
        #: Per-pull still fills (averaging tests); empty = constant 999.
        self.still_fills: list[int] = []
        self.still_pulls = 0

    # -- options ----------------------------------------------------------
    def put_Option(self, opt: int, value: int) -> None:
        if opt in self.refused:
            raise sdk.HRESULTException(0x80004005)
        self.calls.append(("put_Option", opt, value))
        self.options[opt] = value
        if opt not in self.STREAM_SAFE_OPTIONS:
            self._check_not_running(opt)

    def get_Option(self, opt: int) -> int:
        if opt in self.unreadable_options or opt not in self.options:
            raise sdk.HRESULTException(0x8000FFFF)
        return self.options[opt]

    # -- camera-side auto exposure ------------------------------------------
    def put_AutoExpoEnable(self, mode: int) -> None:
        self.calls.append(("put_AutoExpoEnable", mode))
        self._autoexpo = mode

    def get_AutoExpoEnable(self) -> int:
        return self._autoexpo

    # -- exposure -----------------------------------------------------------
    def put_ExpoTime(self, us: int) -> None:
        self.calls.append(("put_ExpoTime", us))
        self._expo_us = us

    def get_ExpoTime(self) -> int:
        return self._expo_us

    def put_ExpoAGain(self, value: int) -> None:
        self.calls.append(("put_ExpoAGain", value))
        self._gain_value = value

    def get_ExpoAGain(self) -> int:
        return self._gain_value

    def get_ExpoAGainRange(self):
        return self._gain_range

    # -- geometry -----------------------------------------------------------
    def put_Roi(self, x: int, y: int, w: int, h: int) -> None:
        self.calls.append(("put_Roi", x, y, w, h))
        # put_Roi over the whole sensor IS "ROI off" — the hardware idiom
        # _configure_stream uses — and binning then applies again.
        self.roi = None if (x, y) == (0, 0) and (w, h) == self._size \
            else (x, y, w, h)
        self._check_not_running("put_Roi")

    def put_Size(self, w: int, h: int) -> None:
        self.calls.append(("put_Size", w, h))
        self._size = (w, h)

    def get_Size(self):
        return self._size

    # -- stream --------------------------------------------------------------
    def StartPullModeWithCallback(self, fun, ctx) -> None:
        self.calls.append(("Start",))
        self.stream_running = True
        self._callback = fun

    def Stop(self) -> None:
        self.calls.append(("Stop",))
        self.stream_running = False

    # -- image pulls -----------------------------------------------------------
    def PullImageV4(self, buf, width, bits, pitch, info) -> None:
        # Hardware contract on the ATR2600M: the image argument is a char
        # buffer (c_char_p argtypes — arrays/POINTERs raise TypeError) and
        # *the frame's own info record* carries its true size, because
        # get_Size keeps naming the sensor resolution whatever binning or
        # ROI is active. A backend sizing by get_Size shows a mosaic; the
        # fake reproduces the mismatch so such a regression fails here.
        w, h = self.delivered_size()
        self.pulls.append(((h, w), bits, width, pitch))
        np.frombuffer(buf, dtype=np.uint16, count=w * h).reshape(h, w)[:] = 4242
        if info is not None:
            info.v3.width, info.v3.height = w, h
            # The real ATR2600M answers 0 (no stamp) — see __init__. The
            # backend maps 0 -> None and every metering falls back to the
            # requested settings; the stamp-propagation path is kept alive
            # by the opt-in test below.
            info.v3.expotime = self._expo_us if self._stamp_expotime else 0

    def PullStillImageV2(self, buf, bits, info) -> None:
        w, h = self._size
        self.pulls.append(((h, w), bits))
        # Averaging tests need per-exposure values: a test sets still_fills
        # to a list of constants and every pull delivers the next one.
        fill = 999
        if self.still_fills:
            fill = self.still_fills[min(self.still_pulls, len(self.still_fills) - 1)]
            self.still_pulls += 1
        np.frombuffer(buf, dtype=np.uint16, count=w * h).reshape(h, w)[:] = fill

    def delivered_size(self) -> tuple[int, int]:
        """What the stream actually sends: get_Size is *not* this."""
        if self.roi is not None:
            return (self.roi[2], self.roi[3])
        binning = self.options.get(_const("BINNING"), OVERVIEW_BINNING)
        if binning == OVERVIEW_BINNING:
            return (self._size[0] // 3, self._size[1] // 3)
        return self._size

    def Snap(self, flag: int) -> None:
        self.calls.append(("Snap", flag))
        # Hardware fires TOUPCAM_EVENT_STILLIMAGE when the exposed still is
        # ready (~exposure + readout); the fake delivers it straight away —
        # capture waits on the event, so the ordering is all that matters.
        self._callback(sdk.TOUPCAM_EVENT_STILLIMAGE, None)

    def fire_frame(self) -> None:
        """Simulate one SDK-thread TOUPCAM_EVENT_IMAGE callback."""
        self._callback(sdk.TOUPCAM_EVENT_IMAGE, None)

    # -- misc ------------------------------------------------------------------
    def get_Temperature(self) -> int:
        return self._temperature

    def Close(self) -> None:
        self.calls.append(("Close",))

    # -- invariants -----------------------------------------------------------
    def _check_not_running(self, what: object) -> None:
        # The SDK refuses geometry changes on a running stream; the backend
        # must never attempt one (E_WRONG_THREAD on real hardware).
        if self.stream_running:
            raise AssertionError(
                f"volání {what} na běžícím proudu — "
                "SDK by vrátil E_WRONG_THREAD")


class FakeDevice:
    """EnumV2 record shape: dev.model.{name,res,still,preview,flag}, dev.id."""

    class _Res:
        def __init__(self, width, height):
            self.width, self.height = width, height

    class _Model:
        def __init__(self, name, res, still, preview, flag):
            self.name, self.res = name, res
            self.still, self.preview, self.flag = still, preview, flag

    def __init__(self, dev_id="usb:1", name=b"ATR2600M",
                 flag=(sdk.TOUPCAM_FLAG_TEC | sdk.TOUPCAM_FLAG_MONO
                       | sdk.TOUPCAM_FLAG_CG
                       | sdk.TOUPCAM_FLAG_LOW_NOISE)):
        self.id = dev_id
        self.displayname = name
        self.model = FakeDevice._Model(
            name,
            [FakeDevice._Res(6224, 4168), FakeDevice._Res(3112, 2084)],
            still=2, preview=0, flag=flag,
        )


@pytest.fixture
def fake(monkeypatch):
    """Patch EnumV2/Open so TouptekCamera() talks to a FakeHcam.

    TECTARGET starts readable: the backend probes cooling by asking for it,
    so a handle without it would (correctly) report an uncooled camera.
    """
    hcam = FakeHcam(options={_const("TECTARGET"): -50})
    monkeypatch.setattr(sdk.Toupcam, "EnumV2",
                        staticmethod(lambda: [FakeDevice()]))
    monkeypatch.setattr(sdk.Toupcam, "Open", staticmethod(lambda _id: hcam))
    return hcam


@pytest.fixture
def camera(fake):
    cam = TouptekCamera()
    cam.connect()
    return cam


class TestConnect:
    def test_raw_options_all_written_and_verified(self, fake, camera) -> None:
        written = {c[1]: c[2] for c in fake.calls if c[0] == "put_Option"}
        for name, wanted, _label in RAW_OPTIONS + MONO_ONLY_OPTIONS:
            assert written[_const(name)] == wanted, name

    def test_mono_camera_never_asked_for_colour_options(self, fake) -> None:
        """Hardware-measured: the mono ATR2600M refuses CURVE (E_INVALIDARG)
        and has no colour pipeline at all — writing that wall of options was
        the source of the connect-time warning spam."""
        written = {c[1] for c in fake.calls if c[0] == "put_Option"}
        for name, _wanted, _label in COLOR_ONLY_OPTIONS:
            assert _const(name) not in written

    def test_options_for_flags_splits_mono_from_colour(self) -> None:
        mono = options_for_flags(sdk.TOUPCAM_FLAG_MONO)
        assert ("RGB", 4, "16bit Grey (mono)") in mono
        assert not any(name.startswith(("COLORMATIX", "WBGAIN", "DEMOSAIC"))
                       for name, _, _ in mono)
        colour = options_for_flags(0)
        assert not any(name == "RGB" for name, _, _ in colour)
        assert any(name == "COLORMATIX" for name, _, _ in colour)

    def test_camera_autoexposure_is_killed_at_connect(self, fake, camera) -> None:
        """A camera left in hardware AE by any other program overwrites every
        put_ExpoTime — the manual shutter then visibly does nothing."""
        assert ("put_AutoExpoEnable", 0) in fake.calls
        assert fake._autoexpo == 0

    def test_persisted_camera_autoexposure_is_reported(self, monkeypatch) -> None:
        hcam = FakeHcam(autoexpo=1)
        # The fake honours the write; make it *stubborn* instead: put says 0,
        # get still answers 1 — a camera that must be named, not quietly fought.
        hcam.put_AutoExpoEnable = lambda mode: None
        monkeypatch.setattr(sdk.Toupcam, "EnumV2",
                            staticmethod(lambda: [FakeDevice()]))
        monkeypatch.setattr(sdk.Toupcam, "Open", staticmethod(lambda _id: hcam))
        notes = disable_camera_autoexposure(hcam)
        assert notes and "kamera měnit sama" in notes[0]

    def test_refused_option_reported_not_fatal(self, monkeypatch) -> None:
        hcam = FakeHcam(refuse_options={_const("LINEAR")})
        monkeypatch.setattr(sdk.Toupcam, "EnumV2",
                            staticmethod(lambda: [FakeDevice()]))
        monkeypatch.setattr(sdk.Toupcam, "Open", staticmethod(lambda _id: hcam))
        cam = TouptekCamera()
        info = cam.connect()               # must not raise
        assert info.model == "ATR2600M"

    def test_info_fields(self, camera) -> None:
        info = camera.info
        assert info.manufacturer == "Touptek"
        assert info.shutter_choices == ()             # continuous
        assert info.gain_range == (1.0, 100.0)        # percent GV / 100
        assert (info.sensor_width, info.sensor_height) == (6224, 4168)

    def test_no_camera_raises_with_power_hint(self, monkeypatch) -> None:
        monkeypatch.setattr(sdk.Toupcam, "EnumV2", staticmethod(lambda: []))
        monkeypatch.setattr(sdk.Toupcam, "Open", staticmethod(lambda _id: None))
        with pytest.raises(CameraError, match="11"):
            TouptekCamera().connect()

    def test_enumerate_reports_cooling_flag(self, monkeypatch) -> None:
        monkeypatch.setattr(
            sdk.Toupcam, "EnumV2",
            staticmethod(lambda: [FakeDevice(),
                                  FakeDevice("usb:2", b"GM1", flag=0)]))
        found = TouptekCamera.enumerate()
        assert found[0]["cooling"] is True and found[1]["cooling"] is False

    def test_sensor_from_enumeration_picks_largest_still(self) -> None:
        assert sensor_from_device(FakeDevice()) == SENSOR

    def test_sensor_fallback_on_empty_res_list(self) -> None:
        dev = FakeDevice()
        dev.model.res = []
        assert sensor_from_device(dev) == touptek.DEFAULT_SENSOR


class TestExposureUnits:
    def test_shutter_exchanged_in_microseconds(self, fake, camera) -> None:
        applied = camera.set_shutter(2.5)
        assert ("put_ExpoTime", 2_500_000) in fake.calls
        assert applied == pytest.approx(2.5)
        assert camera.get_settings().shutter == pytest.approx(2.5)

    def test_shutter_clamped_to_backend_range(self, fake, camera) -> None:
        assert camera.set_shutter(1e-9) == EXPO_TIME_RANGE_US[0] / 1e6
        assert camera.set_shutter(1e9) == EXPO_TIME_RANGE_US[1] / 1e6

    def test_shutter_floor_is_the_hardware_floor(self) -> None:
        """Anchor: 300 µs was only an unprobed guess; the hardware floor is
        100 µs (camera_tests/probe_min_exposure.py, 2026-09-19 — below it
        put_ExpoTime raises E_INVALIDARG). Do not raise this again."""
        assert EXPO_TIME_RANGE_US[0] == 100

    def test_shutter_reports_what_firmware_accepted(self, fake, camera) -> None:
        """First light 2026-09: the firmware has its own exposure ceiling —
        the GUI must show the accepted time, not the requested one (the
        silent 52 s → 50 s combo jump)."""
        cap_us = 50_000_000
        original_put = fake.put_ExpoTime

        def capping_put(us: int) -> None:
            original_put(min(us, cap_us))

        fake.put_ExpoTime = capping_put
        applied = camera.set_shutter(52.0)
        assert applied == pytest.approx(50.0)
        assert camera.get_settings().shutter == pytest.approx(50.0)

    def test_gain_exchanged_in_percent_value(self, fake, camera) -> None:
        """The SDK's ExpoAGain is a percent Gain Value (100 = 1.00x), not
        permille — the historical /1000 divisor made the app call GV 1000
        "1.00x" when it is physically 10x (toupcam.h "percent", hardware
        readback range (100, 10000, 100), both checked 2026-09-20)."""
        applied = camera.set_gain(2.5)
        assert ("put_ExpoAGain", 250) in fake.calls
        assert applied == pytest.approx(2.5)

    def test_gain_clamped_to_reported_range(self, fake, camera) -> None:
        assert camera.set_gain(0.5) == pytest.approx(1.0)
        assert camera.set_gain(999.0) == pytest.approx(100.0)

    def test_gain_helpers(self) -> None:
        assert gain_to_value(1.0) == 100
        assert gain_to_value(1.234) == 123            # SDK ladder is integer
        with pytest.raises(ValueError):
            gain_to_value(0.0)
        assert parse_gain_range((100, 10000, 100)) == (1.0, 100.0)
        assert parse_gain_range((0, 0, 0)) == (1.0, 1.0)   # broken read
        assert shutter_to_us(0.5) == 500_000


class TestStreamModes:
    def test_overview_writes_binning_then_full_roi(self, fake, camera) -> None:
        """The ROI-off idiom is put_Roi over the full sensor, and the buffer
        is sized from get_Size *after* — hardware item §11: verify the camera
        reports the binned size there."""
        fake.calls.clear()               # connect wrote the RAW options first
        camera.start_live_view()
        seq = [c for c in fake.calls
               if c[0] in ("put_Option", "put_Roi", "Start")]
        assert seq[0] == ("put_Option", _const("BINNING"), OVERVIEW_BINNING)
        assert ("put_Roi", 0, 0, 6224, 4168) in fake.calls
        assert seq[-1] == ("Start",)

    def test_roi_mode_uses_no_binning_and_even_window(self, fake, camera) -> None:
        """A pending ROI written before the stream starts reaches the SDK only
        when the stream comes up — the backend configures geometry at start."""
        camera.set_live_view_roi((1001, 999, 1201, 1201))  # odd -> rounded
        camera.start_live_view()
        assert fake.options[_const("BINNING")] == NO_BINNING
        assert fake.roi == (1000, 998, 1200, 1200)

    def test_mode_change_stops_before_reconfigures(self, fake, camera) -> None:
        camera.start_live_view()
        fake.calls.clear()
        camera.set_live_view_roi((2000, 1500, 1200, 1200))
        kinds = [c[0] for c in fake.calls]
        assert kinds.index("Stop") < kinds.index("put_Option")
        assert kinds.index("put_Roi") < kinds.index("Start")
        # And the invariant checker inside FakeHcam proved nothing ran on a
        # live stream (it would have raised mid-call).

    def test_frames_pulled_at_16_bits_into_sized_buffer(self, fake, camera) -> None:
        camera.start_live_view()
        fake.fire_frame()
        frame = camera.next_live_frame()
        assert fake.pulls and fake.pulls[-1][1] == 16
        pull_shape, _bits, width, _pitch = fake.pulls[-1]
        assert pull_shape == (frame.height, frame.width)
        assert width == 0                        # 0 = full buffer width
        # The frame arrives at the *delivered* (binned) size — get_Size
        # still says the sensor resolution, trusting it would reshape a
        # 2074x1388 overview as nine full-size frames (the mosaic).
        assert (frame.width, frame.height) == fake.delivered_size()
        assert (frame.width, frame.height) == (2074, 1389)
        assert frame.data.dtype == np.uint16
        assert frame.white_level == 65535.0

    def test_stale_frames_dropped_newest_kept(self, fake, camera) -> None:
        camera.start_live_view()
        fake.fire_frame()
        fake.fire_frame()
        _data, size, _expo = camera._frames.get_nowait()   # queue maxsize=1
        assert size == fake.delivered_size()

    def test_frame_carries_reported_expotime(self, fake, camera) -> None:
        """The 0 -> None / stamp-forward mapping, with the fake opting in.

        Hardware-measured 2026-09-18: the real ATR2600M answers expotime=0
        on every frame (live *and* still) — see TestNoStampBelow; the fake
        reproduces that by default and this test turns the stamping on to
        keep the forwarding path covered.
        """
        fake._stamp_expotime = True
        camera.set_shutter(2.5)
        camera.start_live_view()
        fake.fire_frame()
        frame = camera.next_live_frame()
        assert frame.expotime_us == 2_500_000

    def test_real_camera_stamp_is_none(self, fake, camera) -> None:
        """Default fake = real ATR2600M: no stamp -> LiveFrame.expotime_us None.

        This is the contract the whole metering chain runs on in practice:
        fresh_live_frame and the film-base stream reading fall back to the
        requested settings because the camera never stamps a frame.
        """
        camera.set_shutter(2.5)
        camera.start_live_view()
        fake.fire_frame()
        frame = camera.next_live_frame()
        assert frame.expotime_us is None

    def test_silent_stream_times_out_as_camera_error(self, camera, monkeypatch) -> None:
        monkeypatch.setattr(touptek, "FRAME_TIMEOUT_S", 0.01)
        monkeypatch.setattr(touptek, "FRAME_TIMEOUT_SLACK_S", 0.01)
        camera.start_live_view()
        with pytest.raises(CameraError, match="přestal"):
            camera.next_live_frame()

    def test_long_exposure_widens_the_frame_timeout(self, camera, monkeypatch) -> None:
        # A 10 s shutter means the stream is legitimately silent for 10 s; the
        # poll must wait the whole shutter+slack out, not declare the stream
        # dead after one slice (which is how a 3× AE killed Live View on
        # hardware). The wait is now sliced so Stop() is honoured mid-exposure,
        # so the assertion is on the CUMULATIVE budget, not a single get().
        waits: list[float] = []

        def spy_get(timeout=None):
            waits.append(timeout)
            raise queue.Empty

        monkeypatch.setattr(camera._frames, "get", spy_get)
        monkeypatch.setattr(touptek, "FRAME_TIMEOUT_S", 5.0)
        monkeypatch.setattr(touptek, "FRAME_POLL_SLICE_S", 1.0)
        camera.set_shutter(10.0)
        camera._live_view = True
        with pytest.raises(CameraError, match="přestal"):
            camera.next_live_frame()
        # Every slice is short (so a Stop is seen fast) but they sum to the
        # full shutter+slack budget the long exposure is owed.
        assert max(waits) <= 1.0
        assert sum(waits) == pytest.approx(12.0)   # shutter + slack

    def test_stopped_stream_returns_promptly_not_after_full_exposure(
        self, camera, monkeypatch
    ) -> None:
        # The freeze's second half: at a 52 s shutter the old single blocking
        # get() sat out the whole exposure even after Stop() had drained the
        # queue, so the previous Live View worker's teardown raced the next
        # Snap. Stop() now flips the flag out from under a waiting poll and it
        # returns None within a slice, never the whole shutter.
        import threading

        monkeypatch.setattr(touptek, "FRAME_TIMEOUT_S", 30.0)
        monkeypatch.setattr(touptek, "FRAME_TIMEOUT_SLACK_S", 30.0)
        monkeypatch.setattr(touptek, "FRAME_POLL_SLICE_S", 0.02)
        camera.set_shutter(52.0)
        camera._live_view = True
        out: list = []
        poll = threading.Thread(target=lambda: out.append(camera.next_live_frame()))
        poll.start()
        camera.stop_live_view()      # clears the flag + drains
        poll.join(timeout=2.0)
        assert not poll.is_alive(), "poll outlived its slice budget after Stop"
        assert out == [None]

    def test_non_image_events_ignored(self, fake, camera) -> None:
        camera.start_live_view()
        fake._callback(0x0008, None)     # not TOUPCAM_EVENT_IMAGE
        assert fake.pulls == []


class TestCapture:
    def test_capture_writes_full_sensor_tiff(self, fake, camera, tmp_path) -> None:
        fake._size = (6224, 4168)        # get_Size after put_Size on hardware
        camera.set_shutter(4.0)
        camera.set_gain(1.0)
        result = camera.capture(tmp_path, "frame001")
        assert result.path == tmp_path / "frame001.tif"
        frame = open_frame(result.path)
        assert (frame.width, frame.height) == (6224, 4168)
        assert frame.data.dtype == np.uint16
        assert int(frame.data.max()) == 999   # what PullStillImageV2 filled
        assert result.bit_depth == 16
        assert result.sensor_temperature_c == pytest.approx(-5.2)

    def test_capture_reconfigures_to_full_1to1(self, fake, camera, tmp_path) -> None:
        camera.start_live_view()
        camera.set_live_view_roi((1000, 1000, 1200, 1200))
        fake.calls.clear()
        camera.capture(tmp_path, "f")
        kinds = [c[0] for c in fake.calls]
        assert kinds[0] == "Stop"                       # stream down first
        assert ("put_Option", _const("BINNING"), NO_BINNING) in fake.calls
        assert ("put_Roi", 0, 0, 6224, 4168) in fake.calls
        assert ("put_Size", 6224, 4168) in fake.calls
        assert ("Snap", 0xFFFFFFFF) in fake.calls
        # The still comes via TOUPCAM_EVENT_STILLIMAGE + PullStillImageV2 —
        # never WaitImageV4 (hardware: a Snap'd still never arrives there).
        assert fake.waited == []
        assert fake.pulls and fake.pulls[-1][1] == 16   # 16-bit pull

    def test_capture_frames_averages_into_one_tiff(self, fake, camera, tmp_path) -> None:
        """frames=4: four Snaps in ONE still session, the archived TIFF is
        their mean, the sidecar metadata says how many (operator ruling
        2026-09-19 — the individual exposures are not kept)."""
        fake._size = (6224, 4168)
        fake.still_fills = [100, 200, 300, 400]
        seen: list[tuple[int, int]] = []
        result = camera.capture(tmp_path, "avg", frames=4,
                                progress=lambda k, n: seen.append((k, n)))
        snaps = [c for c in fake.calls if c[0] == "Snap"]
        assert fake.pulls and len(snaps) == 4
        frame = open_frame(result.path)
        assert int(frame.data[0, 0]) == 250        # (100+200+300+400)/4
        assert frame.acquisition.frames_averaged == 4
        assert any("average" in n for n in result.notes)
        assert seen == [(1, 4), (2, 4), (3, 4), (4, 4)]
        # One reconfiguration only: a single Stop->Start still session.
        assert len([c for c in fake.calls if c[0] == "Start"]) == 1

    def test_capture_frames_default_unchanged(self, fake, camera, tmp_path) -> None:
        """frames=1 keeps the old record: no average note, frames_averaged 1,
        exactly one Snap."""
        fake._size = (6224, 4168)
        result = camera.capture(tmp_path, "single")
        assert not any("average" in n for n in result.notes)
        frame = open_frame(result.path)
        assert frame.acquisition.frames_averaged == 1
        assert len([c for c in fake.calls if c[0] == "Snap"]) == 1

    def test_capture_average_keeps_sub_dn_precision(self, fake, camera, tmp_path) -> None:
        """The mean runs in float: (100+102+103)/3 = 101.67 rounds to 102.
        An integer mean that truncated between steps would land on 101 —
        a systematic half-DN bias the noise maths must not inherit."""
        fake._size = (6224, 4168)
        fake.still_fills = [100, 102, 103]
        result = camera.capture(tmp_path, "frac", frames=3)
        frame = open_frame(result.path)
        assert int(frame.data[0, 0]) == 102

    def test_capture_restores_live_view_mode(self, fake, camera, tmp_path) -> None:
        camera.start_live_view()
        camera.set_live_view_roi((2000, 1500, 1200, 1200))
        camera.capture(tmp_path, "f", keep_live_view=True)
        assert camera._binning == NO_BINNING
        assert camera._roi == Roi(2000, 1500, 1200, 1200)
        assert fake.stream_running is True
        assert camera._live_view is True

    def test_capture_without_keep_leaves_stream_stopped(
        self, fake, camera, tmp_path
    ) -> None:
        camera.start_live_view()
        camera.capture(tmp_path, "f", keep_live_view=False)
        assert fake.stream_running is False

    def test_capture_restores_binning_when_stream_was_stopped_first(
        self, fake, camera, tmp_path
    ) -> None:
        # THE freeze. The GUI stops the stream (_pause_live_view) *before* it
        # queues a capture, so capture() sees was_live=False. The old restore
        # was guarded by `if keep_live_view and was_live`, so it never ran on
        # this — the normal — path and left _binning at NO_BINNING: the next
        # start_live_view then streamed the whole 26 MP sensor forever (the
        # "full-res view never switches off" freeze). The mode state must be
        # restored regardless of whether the stream happened to be up.
        camera.start_live_view()                    # overview (3x3 binned)
        camera.stop_live_view()                     # GUI pause before capture
        assert camera._live_view is False
        camera.capture(tmp_path, "f", keep_live_view=False)
        assert camera._binning == OVERVIEW_BINNING  # not stranded at NO_BINNING
        assert camera._roi is None

    def test_capture_error_still_restores_binning(
        self, fake, camera, tmp_path, monkeypatch
    ) -> None:
        # A dead cable mid-exposure raised out of the old finally *before* the
        # Stop, skipping the restore entirely. Even on failure the binning must
        # not be left at full-sensor NO_BINNING.
        monkeypatch.setattr(touptek, "STILL_WAIT_HEADROOM_S", 0.05)
        fake.Snap = lambda flag: fake.calls.append(("Snap", flag))  # never fires
        camera.start_live_view()
        with pytest.raises(CameraError, match="STILLIMAGE"):
            camera.capture(tmp_path, "f", keep_live_view=True)
        assert camera._binning == OVERVIEW_BINNING

    def test_pull_refusal_surfaces_as_camera_error(
        self, fake, camera, tmp_path
    ) -> None:
        def refuse(*_args):
            raise sdk.HRESULTException(0x80040000)
        fake.PullStillImageV2 = refuse
        camera.start_live_view()
        with pytest.raises(CameraError, match="exposice"):
            camera.capture(tmp_path, "f")

    def test_missing_still_event_times_out_as_camera_error(
        self, fake, camera, tmp_path, monkeypatch
    ) -> None:
        # Snap that never fires STILLIMAGE (dead cable mid-exposure) must
        # fail as CameraError, not hang.
        monkeypatch.setattr(touptek, "STILL_WAIT_HEADROOM_S", 0.05)
        fake.Snap = lambda flag: fake.calls.append(("Snap", flag))
        camera.start_live_view()
        with pytest.raises(CameraError, match="STILLIMAGE"):
            camera.capture(tmp_path, "f")

    def test_settings_embedded_in_acquisition(self, fake, camera, tmp_path) -> None:
        camera.set_shutter(8.0)
        fake._size = (8, 8)
        camera._sensor = SensorSize(8, 8)
        result = camera.capture(tmp_path, "f")
        assert result.settings.shutter == pytest.approx(8.0)


class TestCoolingUnits:
    def test_temperature_is_decidegrees(self, camera) -> None:
        assert camera.get_temperature_c() == pytest.approx(-5.2)

    def test_target_exchanged_in_tenths(self, fake, camera) -> None:
        fake.options[_const("TECTARGET")] = -50
        assert camera.get_target_temperature_c() == pytest.approx(-5.0)
        assert camera.set_target_temperature_c(-12.3) == pytest.approx(-12.3)
        assert ("put_Option", _const("TECTARGET"), -123) in fake.calls

    def test_tec_toggle(self, fake, camera) -> None:
        camera.set_tec_enabled(False)
        assert ("put_Option", _const("TEC"), 0) in fake.calls
        camera.set_tec_enabled(True)
        assert ("put_Option", _const("TEC"), 1) in fake.calls

    def test_camera_without_tec_option_reports_no_cooling(self, monkeypatch) -> None:
        hcam = FakeHcam()                        # TECTARGET absent -> get raises
        monkeypatch.setattr(sdk.Toupcam, "EnumV2",
                            staticmethod(lambda: [FakeDevice()]))
        monkeypatch.setattr(sdk.Toupcam, "Open", staticmethod(lambda _id: hcam))
        cam = TouptekCamera()
        cam.connect()
        assert cam.capabilities().cooling is False
        assert cam.get_temperature_c() is None
        with pytest.raises(CameraError):
            cam.set_tec_enabled(True)


class TestHelpers:
    def test_even_roi_rounds_down_and_clamps(self) -> None:
        roi = even_roi(Roi(101, 203, 1203, 1203), SENSOR)
        assert (roi.x, roi.y) == (100, 202)
        assert (roi.width, roi.height) == (1202, 1202)
        assert roi.x + roi.width <= SENSOR.width

    def test_even_roi_floors_at_min(self) -> None:
        roi = even_roi(Roi(0, 0, 4, 4), SENSOR)
        assert roi.width >= 64 and roi.width % 2 == 0

    def test_disconnect_closes_and_refuses(self, fake) -> None:
        cam = TouptekCamera()
        cam.connect()
        cam.disconnect()
        assert ("Close",) in fake.calls
        with pytest.raises(NotConnectedError):
            cam.get_settings()

    def test_apply_raw_contract_survives_refusals(self) -> None:
        hcam = FakeHcam(refuse_options={_const("RGB")})
        notes = apply_raw_contract(hcam, RAW_OPTIONS + MONO_ONLY_OPTIONS)
        assert any("Grey" in n and "odmítnuto" in n for n in notes)
        # The refused option is not re-audited into a second, duplicate note.
        assert sum("Grey" in n for n in notes) == 1

    def test_apply_raw_contract_names_a_lying_camera(self) -> None:
        class LyingHcam(FakeHcam):
            def get_Option(self, opt: int) -> int:
                value = super().get_Option(opt)
                return value + 1 if opt == _const("LINEAR") else value

        notes = apply_raw_contract(LyingHcam(), RAW_OPTIONS)
        assert any("lineární" in n and "hlásí" in n for n in notes)


class TestReadoutModes:
    """LCG/HCG + low-noise switches (user order 2026-09-20)."""

    def test_connect_applies_scanner_defaults(self, fake, camera) -> None:
        """The ATR2600M persists in HCG (hardware: CG reads 1 at connect) —
        the raw contract must put it into LCG + low noise unless told
        otherwise, and the modes must be readable back."""
        assert ("put_Option", _const("CG"), 0) in fake.calls       # LCG
        assert ("put_Option", _const("LOW_NOISE"), 1) in fake.calls
        modes = camera.get_modes()
        assert modes.hcg is False and modes.low_noise is True

    def test_connect_honours_operator_defaults_off(self, monkeypatch) -> None:
        hcam = FakeHcam()
        monkeypatch.setattr(sdk.Toupcam, "EnumV2",
                            staticmethod(lambda: [FakeDevice()]))
        monkeypatch.setattr(sdk.Toupcam, "Open", staticmethod(lambda _id: hcam))
        cam = TouptekCamera(default_lcg=False, default_low_noise=False)
        cam.connect()
        assert ("put_Option", _const("CG"), 1) in hcam.calls        # HCG
        assert ("put_Option", _const("LOW_NOISE"), 0) in hcam.calls
        modes = cam.get_modes()
        assert modes.hcg is True and modes.low_noise is False

    def test_switch_conversion_gain_live(self, camera) -> None:
        modes = camera.set_conversion_gain(hcg=True)
        assert modes.hcg is True and modes.low_noise is True   # LN untouched
        assert camera.set_conversion_gain(hcg=False).hcg is False

    def test_switch_low_noise_live(self, camera) -> None:
        modes = camera.set_low_noise(False)
        assert modes.low_noise is False and modes.hcg is False  # LCG kept
        assert camera.set_low_noise(True).low_noise is True

    def test_mode_switch_while_streaming_is_allowed(self, camera) -> None:
        """Hardware-checked 2026-09-20: the real camera takes CG/LOW_NOISE
        writes on a running stream (unlike BINNING/ROI)."""
        camera.start_live_view()
        assert camera.set_conversion_gain(hcg=True).hcg is True
        assert camera.set_low_noise(False).low_noise is False
        camera.stop_live_view()

    def test_camera_without_flags_refuses_and_is_not_asked(self,
                                                           monkeypatch) -> None:
        hcam = FakeHcam()
        plain = FakeDevice(flag=sdk.TOUPCAM_FLAG_TEC | sdk.TOUPCAM_FLAG_MONO)
        monkeypatch.setattr(sdk.Toupcam, "EnumV2",
                            staticmethod(lambda: [plain]))
        monkeypatch.setattr(sdk.Toupcam, "Open", staticmethod(lambda _id: hcam))
        cam = TouptekCamera()
        cam.connect()
        written = {c[1] for c in hcam.calls if c[0] == "put_Option"}
        assert _const("CG") not in written       # never asked what it lacks
        assert _const("LOW_NOISE") not in written
        caps = cam.capabilities()
        assert caps.conversion_gain is False and caps.low_noise is False
        modes = cam.get_modes()
        assert modes.hcg is None and modes.low_noise is None
        with pytest.raises(CameraError):
            cam.set_conversion_gain(True)
        with pytest.raises(CameraError):
            cam.set_low_noise(True)

    def test_capture_records_modes_in_metadata(self, fake, camera,
                                               tmp_path) -> None:
        fake._size = (6224, 4168)
        camera.set_conversion_gain(hcg=False)
        camera.set_low_noise(True)
        result = camera.capture(tmp_path, "mode_frame")
        assert result.conversion_gain == "LCG"
        assert result.low_noise is True
        from filmscan_studio.core.rawio import open_frame
        acq = open_frame(result.path).acquisition
        assert acq.conversion_gain == "LCG" and acq.low_noise is True
