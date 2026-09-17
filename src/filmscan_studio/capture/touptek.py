"""Touptek TS2600MP-G2 / ATR2600M backend (Sony IMX571, 6224x4168 mono, TEC).

The SDK story is deliberately small compared to the old Nikon MAID3 stack:
one native universal ``libtoupcam.dylib`` vendored beside the official
ctypes wrapper (``capture/_toupcam/``), loaded in-process — no helper
subprocess, no Rosetta, no PTP.

What this module adds on top of the raw SDK:

* **Raw honesty.** Every option that could put a tone curve between the
  sensor and the file is switched off at connect (RAW mode, builtin linear
  and curve tone mapping off) and *verified* — :func:`audit_options` returns
  what the camera actually accepted, because option writes are advisory on
  some models. The IMX571 is mono, so ``RGB=4`` selects 16-bit Grey.
* **Stream modes** (see :mod:`filmscan_studio.core.zoom`): overview is
  3x3 *average* binning (``0x83`` — depth-preserving, so the stream stays
  linear and meters honestly); zoom is no-binning plus a hardware ROI. The
  SDK refuses ``BINNING``/``ROI`` changes from the callback context, so a
  mode change is always Stop -> configure -> Start, and every call runs on
  the caller's thread (the GUI's CameraWorker), never in the callback.
* **Capture** reconfigures the stream to full-sensor 1:1, waits one frame
  (``WaitImageV4`` with the SDK's exposure-aware default timeout — the frame
  that arrives *is* the exposure), writes the mono TIFF through
  :func:`filmscan_studio.core.rawio.write_frame`, and restores the previous
  Live View mode when asked.
* **Cooling**: TEC on/off, setpoint exchanged in the SDK's 0.1 °C units,
  current sensor temperature read at exposure into ``CaptureResult``.

Thread contract (INSTRUCTIONS §8): the SDK calls the frame callback from its
own thread; it only ever *copies + queues*. ``next_live_frame`` and every
``put_*`` run on the caller's thread.
"""

from __future__ import annotations

import logging
import queue
import time
from pathlib import Path

import numpy as np

from filmscan_studio.capture._toupcam import toupcam as sdk
from filmscan_studio.capture.camera import (
    CameraBackend,
    CameraCapabilities,
    CameraError,
    CameraInfo,
    CaptureResult,
    LiveFrame,
    NotConnectedError,
)
from filmscan_studio.core.exposure import (
    ARCHIVE_GAIN,
    ExposureSettings,
    WHITE_LEVEL_16BIT,
)
from filmscan_studio.core.models import AcquisitionMetadata
from filmscan_studio.core.rawio import write_frame
from filmscan_studio.core.zoom import (
    MIN_ROI_PX,
    NO_BINNING,
    OVERVIEW_BINNING,
    Roi,
    SensorSize,
)

log = logging.getLogger(__name__)

#: Fallback identity when the camera reports no usable resolution list.
DEFAULT_SENSOR = SensorSize(6224, 4168)
#: The SDK speaks microseconds; this is the clamp the shutter setter uses
#: (0.3 ms to 30 minutes) until a narrower range is probed on hardware.
EXPO_TIME_RANGE_US = (300, 1_800_000_000)
#: Permille divisor: the SDK's ExpoAGain 1000 means 1.0x.
GAIN_UNIT = 1000.0
#: How long the live-view poll tolerates a silent stream before erroring.
FRAME_TIMEOUT_S = 5.0

#: Option writes the raw contract consists of, as (option, wanted, label).
#: Verified after connect (and before every capture) by :func:`audit_options`;
#: a mismatch is a warning in ``CaptureResult.notes``, never a hard failure —
#: the operator still gets frames, and the quality check sees the damage if
#: the camera lied.
RAW_OPTIONS: tuple[tuple[str, int, str], ...] = (
    ("RAW", 1, "RAW mód (bez ISP)"),
    ("BITDEPTH", 1, "16bitová hloubka"),
    ("RGB", 4, "16bit Grey (mono)"),
    ("LINEAR", 0, "vestavěný lineární tone-mapping vypnutý"),
    ("CURVE", 0, "vestavěný křivkový tone-mapping vypnutý"),
    ("COLORMATIX", 0, "barevná matice vypnutá"),
    ("WBGAIN", 0, "white-balance gain vypnutý"),
    ("DEMOSAIC_VIDEO", 0, "žádný demosaic proudu"),
    ("DEMOSAIC_STILL", 0, "žádný demosaic stillu"),
)


def _option_const(name: str) -> int:
    return getattr(sdk, f"TOUPCAM_OPTION_{name}")


def configure_raw_stream(hcam) -> list[str]:
    """Apply :data:`RAW_OPTIONS` to an open handle; returns notes on writes."""
    notes: list[str] = []
    for name, value, label in RAW_OPTIONS:
        try:
            hcam.put_Option(_option_const(name), value)
        except sdk.HRESULTException as exc:
            notes.append(f"{label}: odmítnuto (hr=0x{exc.hr & 0xffffffff:x})")
    return notes


def audit_options(hcam) -> list[str]:
    """Read back the raw contract; returns human-readable problems, [] = pure."""
    problems: list[str] = []
    for name, wanted, label in RAW_OPTIONS:
        try:
            got = hcam.get_Option(_option_const(name))
        except sdk.HRESULTException:
            problems.append(f"{label}: nejde ověřit")
            continue
        if got != wanted:
            problems.append(f"{label}: kamera hlásí {got}, žádáno {wanted}")
    return problems


def _decode_name(raw: object) -> str:
    """EnumV2 names are bytes on macOS/Linux (c_char_p), str on Windows."""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw or "")


def parse_gain_range(range_permille: tuple[int, int, int]) -> tuple[float, float]:
    """``get_ExpoAGainRange`` (min, max, default) in permille -> multipliers.

    A camera reporting a nonsensical range (all-zero on a broken read) is
    reported as a fixed 1.0x rather than a range that would divide by zero.
    """
    low, high, _default = range_permille
    if low <= 0 or high < low:
        return (ARCHIVE_GAIN, ARCHIVE_GAIN)
    return (low / GAIN_UNIT, high / GAIN_UNIT)


def gain_to_permille(gain: float) -> int:
    """Linear multiplier -> the integer permille the SDK takes."""
    if gain <= 0:
        raise ValueError("gain must be a positive multiplier")
    return max(1, round(gain * GAIN_UNIT))


def shutter_to_us(seconds: float) -> int:
    if seconds <= 0:
        raise ValueError("shutter must be positive")
    return max(1, round(seconds * 1e6))


def sensor_from_device(dev) -> SensorSize:
    """Full sensor size from an EnumV2 device record.

    The largest reported still (or preview) resolution *is* the sensor —
    the enumeration table lists every mode including downsampled previews,
    and the IMX571's native 6224x4168 is what ROI coordinates and 1:1 zoom
    are measured against.
    """
    res = list(getattr(dev.model, "res", []) or [])
    still, preview = getattr(dev.model, "still", 0), getattr(dev.model, "preview", 0)
    # `res` lists stills first, then previews (model.still + model.preview).
    stills = res[:still] if still else res
    best = max(((r.width, r.height) for r in stills), default=(0, 0))
    if best[0] * best[1] == 0:
        best = max(((r.width, r.height) for r in res), default=(0, 0))
    if best[0] * best[1] == 0:
        log.warning("%s: žádné rozlišení v enumeraci, používám výchozí senzor",
                    _decode_name(getattr(dev.model, "name", "?")))
        return DEFAULT_SENSOR
    return SensorSize(*best)


def even_roi(roi: Roi, sensor: SensorSize) -> Roi:
    """Round an ROI to what ``put_Roi`` accepts: even origin/size, inside.

    The SDK requires even offsets and sizes (minimum 8 px); ``MIN_ROI_PX``
    is the project's own practical floor.
    """
    clamped = Roi(roi.x, roi.y,
                  max(MIN_ROI_PX, roi.width), max(MIN_ROI_PX, roi.height)
                  ).clamped(sensor)
    x = clamped.x - clamped.x % 2
    y = clamped.y - clamped.y % 2
    w = max(MIN_ROI_PX, clamped.width - clamped.width % 2)
    h = max(MIN_ROI_PX, clamped.height - clamped.height % 2)
    return Roi(x, y, w, h).clamped(sensor)


class TouptekCamera(CameraBackend):
    """The TS2600MP-G2 as a :class:`CameraBackend`.

    One instance owns one open camera handle and must be driven from a
    single thread (the GUI's CameraWorker); the only thread-crossing is the
    SDK callback queue, which is ``queue.Queue``-safe by construction.

    ``cam_id`` is an enumeration id from :meth:`enumerate`; ``None`` opens
    the first camera found. Ids change between sessions — pass a
    ``"sn:<serial>"`` string for a stable identity.
    """

    def __init__(self, cam_id: str | None = None) -> None:
        self._cam_id = cam_id
        self._hcam = None
        self._info: CameraInfo | None = None
        self._sensor = DEFAULT_SENSOR
        self._settings = ExposureSettings(1.0, iso=None, gain=ARCHIVE_GAIN)
        self._live_view = False
        self._cooling = False
        self._frames: queue.Queue = queue.Queue(maxsize=1)
        # Stream mode the pull loop is configured for right now:
        self._binning = OVERVIEW_BINNING
        self._roi: Roi | None = None
        self._buf: np.ndarray | None = None
        self._stream_size = (0, 0)

    # ------------------------------------------------------------------ identity

    @staticmethod
    def enumerate() -> list[dict]:
        """Connected Touptek cameras: ``[{"id", "name", "cooling"}, ...]``.

        An empty list usually means the camera has no 11-14 V supply — on
        this model USB enumeration is not guaranteed without it (hardware
        checklist item, INSTRUCTIONS §11.4). The connect dialog says so.
        """
        found = []
        for dev in sdk.Toupcam.EnumV2():
            found.append({
                "id": dev.id,
                "name": _decode_name(getattr(dev, "displayname", None)
                                     or getattr(dev.model, "name", "")),
                "cooling": bool(getattr(dev.model, "flag", 0)
                                & sdk.TOUPCAM_FLAG_TEC),
            })
        return found

    def connect(self) -> CameraInfo:
        devices = sdk.Toupcam.EnumV2()
        hcam = sdk.Toupcam.Open(self._cam_id)
        if hcam is None:
            raise CameraError(
                "Touptek nenalezen — zkontroluj USB a napájení 11–14 V")
        self._hcam = hcam
        try:
            # Open(None)/OpenByIndex opens the first enumerated camera, so
            # "first device" is the right record when no id was asked for.
            dev = next((d for d in devices if d.id == self._cam_id),
                       devices[0] if (devices and self._cam_id is None) else None)
            sensor = sensor_from_device(dev) if dev is not None else DEFAULT_SENSOR
            notes = configure_raw_stream(hcam)
            problems = audit_options(hcam)
            gain_range = parse_gain_range(hcam.get_ExpoAGainRange())
            try:
                hcam.get_Option(_option_const("TECTARGET"))
                self._cooling = True
            except sdk.HRESULTException:
                self._cooling = False
            model = _decode_name(dev.model.name) if dev is not None \
                else "Touptek camera"
        except Exception:
            hcam.Close()
            self._hcam = None
            raise
        self._sensor = sensor
        self._settings = ExposureSettings(
            shutter=hcam.get_ExpoTime() / 1e6, iso=None,
            gain=hcam.get_ExpoAGain() / GAIN_UNIT,
        )
        serial = (self._cam_id.split(":", 1)[1]
                  if self._cam_id and self._cam_id.startswith("sn:") else None)
        self._info = CameraInfo(
            model=model,
            manufacturer="Touptek",
            serial=serial,
            shutter_choices=(),               # continuous: put_ExpoTime us
            gain_range=gain_range,
            sensor_width=sensor.width,
            sensor_height=sensor.height,
        )
        for note in notes + problems:
            log.warning("Touptek: %s", note)
        return self._info

    def disconnect(self) -> None:
        if self._hcam is None:
            return
        try:
            self.stop_live_view()
        finally:
            self._hcam.Close()
            self._hcam = None

    @property
    def info(self) -> CameraInfo:
        if self._info is None:
            raise NotConnectedError("kamera není připojena")
        return self._info

    def capabilities(self) -> CameraCapabilities:
        return CameraCapabilities(
            shutter=True, gain=True, live_view_zoom=True, cooling=self._cooling,
        )

    # ----------------------------------------------------------------- exposure

    def get_settings(self) -> ExposureSettings:
        self._require()
        return self._settings

    def set_shutter(self, seconds: float) -> float:
        self._require()
        lo_us, hi_us = EXPO_TIME_RANGE_US
        us = min(hi_us, max(lo_us, shutter_to_us(seconds)))
        self._hcam.put_ExpoTime(us)
        self._settings = self._settings.with_shutter(us / 1e6)
        return us / 1e6

    def set_gain(self, gain: float) -> float:
        self._require()
        lo, hi = self._info.gain_range or (ARCHIVE_GAIN, ARCHIVE_GAIN)
        applied = max(lo, min(hi, gain))
        self._hcam.put_ExpoAGain(gain_to_permille(applied))
        # Read the accepted permille back — the ladder is integer-coarse.
        accepted = self._hcam.get_ExpoAGain() / GAIN_UNIT
        self._settings = self._settings.with_gain(accepted)
        return accepted

    # --------------------------------------------------------------- live view

    def start_live_view(self) -> None:
        self._require()
        if self._live_view:
            return
        self._start_stream()
        self._live_view = True

    def stop_live_view(self) -> None:
        if self._hcam is not None and self._live_view:
            self._hcam.Stop()
        self._live_view = False
        self._drain_frames()

    def set_live_view_roi(self, roi: tuple[int, int, int, int] | None) -> None:
        """Switch the stream mode: binned overview (None) or 1:1 ROI window.

        The SDK honours BINNING/ROI only on a stopped stream, so this is a
        brief Stop -> reconfigure -> Start. The interruption (tens of ms,
        no mechanics to shake) is by design — INSTRUCTIONS §3.
        """
        self._require()
        self._binning, self._roi = (OVERVIEW_BINNING, None) if roi is None \
            else (NO_BINNING, even_roi(Roi(*roi), self._sensor))
        if self._live_view:
            self._hcam.Stop()
            self._live_view = False
            self._drain_frames()
            self._start_stream()
            self._live_view = True

    def _configure_stream(self) -> None:
        """Options for the current mode. Caller must have stopped the stream:
        BINNING and ROI are exactly the writes the SDK forbids on a running
        stream/callback context (E_WRONG_THREAD)."""
        assert self._hcam is not None
        self._hcam.put_Option(_option_const("BINNING"), self._binning)
        if self._roi is None:
            # "ROI off" is put_Roi over the full sensor; the *effective*
            # stream size after binning is read back from get_Size, which
            # is what the buffer is sized from — no geometry guessing.
            self._hcam.put_Roi(0, 0, self._sensor.width, self._sensor.height)
        else:
            self._hcam.put_Roi(self._roi.x, self._roi.y,
                               self._roi.width, self._roi.height)

    def _start_stream(self) -> None:
        self._configure_stream()
        width, height = self._hcam.get_Size()
        self._stream_size = (width, height)
        self._buf = np.zeros((height, width), dtype=np.uint16)
        # StartPullModeWithCallback pins `self` as the ctypes ctx and keeps
        # the trampoline referenced on the handle — no dangling callback.
        self._hcam.StartPullModeWithCallback(self._on_event, None)

    def _on_event(self, event: int, _ctx: object) -> None:
        """SDK-thread callback: copy, queue, nothing else — never options.

        ``TOUPCAM_EVENT_IMAGE`` means one frame is ready for PullImageV4
        into the sized buffer. A full queue drops the *oldest* frame: live
        view wants the newest frame, never a backlog (the D750 worker had
        the same freshness rule).
        """
        if event != sdk.TOUPCAM_EVENT_IMAGE or self._hcam is None:
            return
        buf = self._buf
        if buf is None:
            return
        try:
            self._hcam.PullImageV4(buf, 0, 16, 0, None)
        except sdk.HRESULTException as exc:
            log.warning("PullImageV4 selhalo (hr=0x%x)", exc.hr & 0xffffffff)
            return
        frame = (buf.copy(), self._stream_size)
        try:
            self._frames.put_nowait(frame)
        except queue.Full:
            try:
                self._frames.get_nowait()
            except queue.Empty:
                pass
            self._frames.put_nowait(frame)

    def next_live_frame(self) -> LiveFrame | None:
        if not self._live_view:
            return None
        self._require()
        try:
            data, (width, height) = self._frames.get(timeout=FRAME_TIMEOUT_S)
        except queue.Empty:
            raise CameraError("proud kamery přestal dodávat snímky")
        return LiveFrame(data=data, width=width, height=height,
                         black_level=0.0, white_level=WHITE_LEVEL_16BIT)

    def _drain_frames(self) -> None:
        while True:
            try:
                self._frames.get_nowait()
            except queue.Empty:
                return

    # ------------------------------------------------------------------ capture

    def capture(self, destination: Path, filename_stem: str,
                keep_live_view: bool = True) -> CaptureResult:
        """One full-sensor 1:1 frame written as a mono TIFF.

        Never the streamed frame — the archive must not inherit a binned or
        cropped preview (``CameraBackend.capture`` contract). Stop the live
        stream, reconfigure to full sensor without binning, ``Snap`` the
        still, ``WaitImageV4`` with the SDK's exposure-aware default timeout
        (``waitMS=0`` → exposure × 1.02 + 4 s; it cannot time out on a long
        exposure), then restore the previous Live View mode when asked.
        """
        self._require()
        started = time.monotonic()
        notes: list[str] = []
        was_live = self._live_view
        restore = (self._binning, self._roi)      # Live View mode to come back to
        if was_live:
            self._hcam.Stop()
            self._live_view = False
            self._drain_frames()
        try:
            notes += configure_raw_stream(self._hcam)
            notes += audit_options(self._hcam)
            self._binning, self._roi = NO_BINNING, None
            self._configure_stream()
            self._hcam.put_Size(self._sensor.width, self._sensor.height)
            width, height = self._hcam.get_Size()
            if (width, height) != (self._sensor.width, self._sensor.height):
                notes.append(f"still size {width}x{height} místo "
                             f"{self._sensor.width}x{self._sensor.height}")
            buf = np.zeros((height, width), dtype=np.uint16)
            self._hcam.StartPullModeWithCallback(self._on_event, None)
            try:
                self._hcam.Snap(0xFFFFFFFF)     # 0xffffffff = current preview res
            except sdk.HRESULTException as exc:
                notes.append(f"Snap odmítnut (hr=0x{exc.hr & 0xffffffff:x})")
            info = sdk.ToupcamFrameInfoV4()
            try:
                # waitMS=0: the documented default timeout already scales
                # with the exposure time — right for 30-minute dithers too.
                self._hcam.WaitImageV4(0, buf, 0, 16, 0, info)
            except sdk.HRESULTException as exc:
                raise CameraError(
                    f"exposice nedodala snímek (hr=0x{exc.hr & 0xffffffff:x})")
            temp = self.get_temperature_c()
            acquisition = AcquisitionMetadata(
                camera=self._info.model,
                camera_serial=self._info.serial,
                iso=None,
                gain=self._settings.gain,
                exposure_time=self._settings.shutter,
            )
            destination.mkdir(parents=True, exist_ok=True)
            target = destination / f"{filename_stem}.tif"
            write_frame(target, buf, acquisition=acquisition,
                        black_level=0.0, white_level=WHITE_LEVEL_16BIT)
            # V4 wraps the V3 record; expotime lives on the inner struct.
            if info.v3.expotime:
                notes.append(f"expotime hlášen {info.v3.expotime} us")
        finally:
            try:
                self._hcam.Stop()
            finally:
                if keep_live_view and was_live:
                    self._binning, self._roi = restore
                    self._start_stream()
                    self._live_view = True
        return CaptureResult(
            path=target,
            size_bytes=target.stat().st_size,
            settings=self._settings,
            elapsed=time.monotonic() - started,
            sensor_temperature_c=temp,
            bit_depth=16,
            notes=tuple(notes),
        )

    # ------------------------------------------------------------------ cooling

    def get_temperature_c(self) -> float | None:
        if not self._cooling:
            return None
        self._require()
        try:
            return self._hcam.get_Temperature() / 10.0
        except sdk.HRESULTException as exc:
            log.warning("get_Temperature selhalo (hr=0x%x)", exc.hr & 0xffffffff)
            return None

    def get_target_temperature_c(self) -> float | None:
        if not self._cooling:
            return None
        self._require()
        try:
            return self._hcam.get_Option(_option_const("TECTARGET")) / 10.0
        except sdk.HRESULTException:
            return None

    def set_target_temperature_c(self, temperature_c: float) -> float:
        if not self._cooling:
            raise CameraError("kamera nehlásí chlazení")
        self._require()
        tenths = round(temperature_c * 10.0)
        self._hcam.put_Option(_option_const("TECTARGET"), tenths)
        return tenths / 10.0

    def set_tec_enabled(self, enabled: bool) -> None:
        if not self._cooling:
            raise CameraError("kamera nehlásí chlazení")
        self._require()
        self._hcam.put_Option(_option_const("TEC"), 1 if enabled else 0)

    # ------------------------------------------------------------------ internals

    def _require(self) -> None:
        if self._hcam is None:
            raise NotConnectedError("kamera není připojena")
