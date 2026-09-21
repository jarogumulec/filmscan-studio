"""Deterministic mock camera shaped like the Touptek TS2600MP-G2.

Exists so the GUI, session logic and auto-exposure loop can be tested without
a sensor in hand. It emulates the *real* constraints rather than being a
convenient stub:

* shutter snaps to a continuous-µs backend (any value is accepted), gain
  snaps to the SDK's permille ladder,
* Live View honours the two stream modes for real — 3x3-binned overview or a
  1:1 hardware ROI window — so zoom tests prove the request reached the camera,
* captures write the same 16-bit mono TIFF the Touptek backend writes, so the
  production read path (``rawio``) is exercised, not mocked,
* the TEC tracks a setpoint with a first-order approach, so cooling UI and the
  dark/scan temperature-matching validation can be tested for real.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from filmscan_studio.capture.camera import (
    CameraBackend,
    CameraCapabilities,
    CameraError,
    CameraInfo,
    CaptureResult,
    LiveFrame,
    NotConnectedError,
    SensorModes,
)
from filmscan_studio.core.exposure import ARCHIVE_GAIN, ExposureSettings
from filmscan_studio.core.models import AcquisitionMetadata
from filmscan_studio.core.rawio import write_frame
from filmscan_studio.core.zoom import (
    MIN_ROI_PX,
    NO_BINNING,
    OVERVIEW_BINNING,
    OVERVIEW_SCALE,
    Roi,
    SensorSize,
)

#: What the mock is, mirroring the TS2600MP-G2 it emulates.
MOCK_SENSOR = SensorSize(6224, 4168)
MOCK_BLACK = 0.0
MOCK_WHITE = 65535.0
#: The SDK's analog gain range in linear multipliers. The real ATR2600M
#: reports get_ExpoAGainRange() = (100, 10000, 100) *percent* Gain Values =
#: 1x..100x (hardware-checked 2026-09-20; the old (0.1, 10.0) came from
#: reading those percent values as permille). Unity is the floor and the
# archival setting (ARCHIVE_GAIN = 1.00x).
MOCK_GAIN_RANGE = (1.0, 100.0)
#: Simulated dark-current DN per second of exposure at the archive gain.
MOCK_DARK_DN_PER_S = 6.0
#: Sensor temperature physics: ambient the sensor drifts toward, °C per minute
#: of pull per watt-equivalent of TEC effort.
_MOCK_AMBIENT_C = 22.0
_MOCK_COOL_RATE_C_PER_MIN = 42.0


class MockCamera(CameraBackend):
    """A TS2600MP-G2-shaped simulator.

    ``scene_level`` is the normalised illumination of the simulated scene, so
    tests can drive the auto-exposure loop and assert it converges. Temperature
    starts settled at the default setpoint; pass ``temperature_c`` to start
    warm and watch the cooling loop converge.
    """

    def __init__(
        self,
        scene_level: float = 0.25,
        live_fps: float = 30.0,
        capture_seconds: float = 0.0,
        settings: ExposureSettings | None = None,
        temperature_c: float | None = None,
        target_temperature_c: float = -5.0,
        cooling: bool = True,
    ) -> None:
        if not 0.0 <= scene_level <= 1.0:
            raise ValueError("scene_level must be within 0..1")
        self.scene_level = scene_level
        self._live_fps = live_fps
        self._capture_seconds = capture_seconds
        self._settings = settings or ExposureSettings(1.0, iso=None,
                                                      gain=ARCHIVE_GAIN)
        self._connected = False
        self._live_view = False
        self._frame_index = 0
        self._captures: list[CaptureResult] = []
        self._rng = np.random.default_rng(1234)
        self.shutter_history: list[float] = []
        self.gain_history: list[float] = []
        # ------------------------------------------------------------ stream mode
        #: Current stream: binned overview until an ROI is requested.
        self.binning = OVERVIEW_BINNING
        self.roi: Roi | None = None
        self.stream_history: list[tuple[int, Roi | None]] = []
        # ---------------------------------------------------------------- cooling
        self._cooling = cooling
        self._tec_enabled = cooling
        self._target_temp = target_temperature_c
        self._temp_c = (target_temperature_c if temperature_c is None
                        else temperature_c)
        self._temp_clock = time.monotonic()
        self.target_history: list[float] = []
        # ----------------------------------------------------------- readout modes
        # Scanner default = the ATR2600M optimum the app applies at connect:
        # LCG + low noise. HCG multiplies DN ~2.8x (measured); the mock keeps
        # LCG+LN as its reference DN scale so scene maths stays in plain DN.
        self._hcg = False
        self._low_noise = True
        self.mode_history: list[SensorModes] = []
        #: What the last capture() was asked about Live View (GUI contract).
        self.last_keep_live_view: bool | None = None
        #: How many exposures the last capture() averaged (GUI contract).
        self.last_capture_frames: int | None = None

    # ------------------------------------------------------------------ identity

    def connect(self) -> CameraInfo:
        self._connected = True
        return self.info

    def disconnect(self) -> None:
        self._connected = False
        self._live_view = False

    @property
    def info(self) -> CameraInfo:
        return CameraInfo(
            model="TS2600MP-G2 (mock)",
            manufacturer="Touptek (mock)",
            serial="MOCK0000000001",
            gain_range=MOCK_GAIN_RANGE,
            sensor_width=MOCK_SENSOR.width,
            sensor_height=MOCK_SENSOR.height,
        )

    def capabilities(self) -> CameraCapabilities:
        return CameraCapabilities(
            shutter=True, gain=True, live_view_zoom=True, cooling=self._cooling,
            conversion_gain=True, low_noise=True,
        )

    # ------------------------------------------------------------- readout modes

    def get_modes(self) -> SensorModes:
        return SensorModes(hcg=self._hcg, low_noise=self._low_noise)

    def set_conversion_gain(self, hcg: bool) -> SensorModes:
        self._require()
        self._hcg = bool(hcg)
        modes = self.get_modes()
        self.mode_history.append(modes)
        return modes

    def set_low_noise(self, enabled: bool) -> SensorModes:
        self._require()
        self._low_noise = bool(enabled)
        modes = self.get_modes()
        self.mode_history.append(modes)
        return modes

    def _mode_dn_factor(self) -> float:
        """DN scale vs. the LCG reference (hardware ratios, camera_tests).

        HCG reads ~2.8× the DN of the same exposure (measured). Low-noise
        readout is DN-neutral in *stills* (measured 0.996×); its ~0.83×
        shift was a live-stream-only effect, so the mock — which exists for
        exposure-maths tests — keeps it out.
        """
        return 2.81 if self._hcg else 1.0

    # ----------------------------------------------------------------- exposure

    def get_settings(self) -> ExposureSettings:
        self._require()
        return self._settings

    def set_shutter(self, seconds: float) -> float:
        self._require()
        if seconds <= 0:
            raise ValueError("shutter must be positive")
        self._settings = self._settings.with_shutter(seconds)
        self.shutter_history.append(seconds)
        return seconds

    def set_gain(self, gain: float) -> float:
        self._require()
        low, high = MOCK_GAIN_RANGE
        # The SDK takes an integer percent Gain Value; snap like the real thing.
        applied = round(max(low, min(high, gain)) * 100) / 100
        self._settings = self._settings.with_gain(applied)
        self.gain_history.append(applied)
        return applied

    # -------------------------------------------------------------- live view

    def start_live_view(self) -> None:
        self._require()
        self._live_view = True

    def stop_live_view(self) -> None:
        self._live_view = False

    def set_live_view_roi(self, roi: tuple[int, int, int, int] | None) -> None:
        self._require()
        if roi is None:
            self.binning = OVERVIEW_BINNING
            self.roi = None
        else:
            window = Roi(*roi)
            if window.width < MIN_ROI_PX or window.height < MIN_ROI_PX:
                raise ValueError(f"ROI pod {MIN_ROI_PX} px SDK nepřijme")
            self.binning = NO_BINNING
            self.roi = window.clamped(MOCK_SENSOR)
        self.stream_history.append((self.binning, self.roi))

    def next_live_frame(self) -> LiveFrame | None:
        if not self._live_view:
            return None
        self._require()
        if self._live_fps > 0:
            time.sleep(1.0 / self._live_fps)
        self._frame_index += 1
        self._advance_temperature()
        if self.roi is not None:
            height, width = self.roi.height, self.roi.width
        else:
            width, height = (MOCK_SENSOR.width // int(OVERVIEW_SCALE),
                             MOCK_SENSOR.height // int(OVERVIEW_SCALE))
        data = self.synth_frame(height=height, width=width,
                                binned=self.roi is None,
                                offset=self.roi)
        return LiveFrame(data=data, width=width, height=height,
                         black_level=MOCK_BLACK, white_level=MOCK_WHITE)

    # ------------------------------------------------------------------ capture

    def capture(self, destination: Path, filename_stem: str,
                keep_live_view: bool = True, frames: int = 1,
                progress=None) -> CaptureResult:
        self._require()
        started = time.monotonic()
        frames = max(1, int(frames))
        self.last_keep_live_view = keep_live_view
        self.last_capture_frames = frames
        notes: list[str] = []
        accumulated: np.ndarray | None = None
        for taken in range(frames):
            if self._capture_seconds:
                time.sleep(self._capture_seconds)
            self._advance_temperature()
            frame = self.synth_frame(height=MOCK_SENSOR.height,
                                     width=MOCK_SENSOR.width, binned=False)
            accumulated = (frame.astype(np.float32) if accumulated is None
                           else accumulated + frame)
            if progress is not None:
                progress(taken + 1, frames)
        average = np.rint(accumulated / frames).astype(np.uint16)
        destination.mkdir(parents=True, exist_ok=True)
        # The same full-res mono TIFF the Touptek backend writes — the session
        # and developer read it through the production rawio path unchanged.
        target = destination / f"{filename_stem}.tif"
        modes = self.get_modes()
        write_frame(target, average,
                    acquisition=AcquisitionMetadata(
                        camera="Mock TS2600MP-G2", camera_serial="MOCK",
                        iso=None, gain=self._settings.gain,
                        exposure_time=self._settings.shutter,
                        frames_averaged=frames,
                        conversion_gain="HCG" if modes.hcg else "LCG",
                        low_noise=modes.low_noise),
                    black_level=MOCK_BLACK, white_level=MOCK_WHITE)
        if frames > 1:
            notes.append(f"average: uložen průměr z {frames} expozic")
        result = CaptureResult(
            path=target,
            size_bytes=target.stat().st_size,
            settings=self._settings,
            elapsed=time.monotonic() - started,
            # An uncooled camera logs no temperature — "unknown" is what the
            # export validation must see, not a made-up number.
            sensor_temperature_c=(self._temp_c if self._cooling else None),
            bit_depth=16,
            conversion_gain="HCG" if modes.hcg else "LCG",
            low_noise=modes.low_noise,
            notes=tuple(notes),
        )
        self._captures.append(result)
        return result

    # ------------------------------------------------------------------ cooling

    def get_temperature_c(self) -> float | None:
        if not self._cooling:
            return None
        self._advance_temperature()
        return round(self._temp_c, 1)

    def get_target_temperature_c(self) -> float | None:
        return self._target_temp if self._cooling else None

    def set_target_temperature_c(self, temperature_c: float) -> float:
        if not self._cooling:
            raise CameraError("mock kamera nemá chlazení")
        self._target_temp = float(temperature_c)
        self.target_history.append(self._target_temp)
        return self._target_temp

    def set_tec_enabled(self, enabled: bool) -> None:
        if not self._cooling:
            raise CameraError("mock kamera nemá chlazení")
        self._tec_enabled = enabled

    @property
    def tec_enabled(self) -> bool:
        return self._tec_enabled

    # ------------------------------------------------------------------ internals

    def _require(self) -> None:
        if not self._connected:
            raise NotConnectedError("mock kamera není připojena")

    def _advance_temperature(self) -> None:
        """First-order approach to the setpoint, minutes-per-step realistic."""
        now = time.monotonic()
        minutes = (now - self._temp_clock) / 60.0
        self._temp_clock = now
        if minutes <= 0 or not self._cooling:
            return
        goal = self._target_temp if self._tec_enabled else _MOCK_AMBIENT_C
        # Exponential approach with a ~2-minute time constant, clamped so a
        # test that sleeps 100 ms sees motion without waiting out real TECs.
        rate = _MOCK_COOL_RATE_C_PER_MIN / 42.0     # ~1 °C/min scale
        step = rate * (goal - self._temp_c) * minutes * 20.0
        max_step = abs(goal - self._temp_c)
        self._temp_c += max(-max_step, min(max_step, step))

    def sensor_signal(self, shutter: float | None = None,
                      gain: float | None = None) -> float:
        """Mean linear signal in DN for the given settings.

        Linear in shutter *and* gain — the assumption both the calibration
        normalisation and the AE loop rely on — plus the dark pedestal, which
        scales with shutter only (dark current does not ride on analog gain).
        """
        s = self._settings.shutter if shutter is None else shutter
        g = self._settings.gain if gain is None else (gain or 1.0)
        base = self.scene_level * (MOCK_WHITE - MOCK_BLACK)
        reference = 1.0 * ARCHIVE_GAIN
        signal = base * (s * g / reference)
        dark = MOCK_DARK_DN_PER_S * s
        return float(np.clip((signal + dark + MOCK_BLACK) * self._mode_dn_factor(),
                             0, MOCK_WHITE))

    def synth_frame(self, height: int, width: int, *, binned: bool = False,
                    offset: Roi | None = None) -> np.ndarray:
        """A plausible linear mono frame: gradient, vignette, noise, dark.

        Stream pixels are mapped back to *sensor* coordinates, so all three
        stream modes see one consistent scene: an ROI window shows the slice
        it really would (moving it produces a different frame — what the zoom
        tests assert against), and binned overview pixels stand for 3x3 sensor
        px with the averaging's sqrt(9) noise benefit.
        """
        scale = OVERVIEW_SCALE if binned else 1.0
        oy, ox = np.mgrid[0:height, 0:width]
        scene_x = (offset.x if offset is not None else 0) + ox * scale
        scene_y = (offset.y if offset is not None else 0) + oy * scale
        gradient = 0.75 + 0.5 * (scene_x / max(MOCK_SENSOR.width - 1, 1))
        vignette = 1.0 - 0.18 * (
            ((scene_x - MOCK_SENSOR.width / 2) ** 2
             + (scene_y - MOCK_SENSOR.height / 2) ** 2)
            / ((MOCK_SENSOR.width / 2) ** 2))
        signal = self.sensor_signal() * np.clip(gradient * vignette, 0, None)
        noise_sigma = 4.0 / scale
        dark = MOCK_DARK_DN_PER_S * self._settings.shutter
        noisy = signal + dark + self._rng.normal(0, noise_sigma, signal.shape)
        return np.clip(noisy, 0, MOCK_WHITE).astype(np.uint16)

    @property
    def captures(self) -> list[CaptureResult]:
        return list(self._captures)
