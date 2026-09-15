"""Deterministic mock camera.

Exists so the GUI, session logic and auto-exposure loop can be tested without a
body in hand. It emulates the D750's real constraints rather than being a
convenient stub: shutter and ISO snap to the body's actual choice lists, Live
View frames are produced at a controllable rate, and captures synthesise a
radiometrically plausible frame from the current settings so dark/flat
normalisation can be exercised for real.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from filmscan_studio.capture.camera import (
    CameraBackend,
    CameraCapabilities,
    CameraInfo,
    CaptureResult,
    LiveFrame,
    NotConnectedError,
)
from filmscan_studio.core.exposure import ExposureSettings
from filmscan_studio.core.zoom import ZOOM_ALL, SensorSize

#: What the mock's "NEF" is, mirroring the D750 it emulates.
MOCK_SENSOR = (6016, 4016)

#: The D750's real shutter ladder, in seconds.
D750_SHUTTERS: tuple[float, ...] = (
    1 / 4000, 1 / 2000, 1 / 1000, 1 / 500, 1 / 250, 1 / 200, 1 / 160, 1 / 125,
    1 / 100, 1 / 80, 1 / 60, 1 / 50, 1 / 40, 1 / 30, 1 / 25, 1 / 20, 1 / 15,
    1 / 13, 1 / 10, 1 / 8, 1 / 6, 1 / 5, 1 / 4, 0.3, 0.4, 0.5, 0.6, 0.8,
    1.0, 1.3, 1.6, 2.0, 2.5, 3.2, 4.0, 5.0, 6.0, 8.0, 10.0, 13.0, 15.0, 20.0,
    25.0, 30.0,
)

D750_ISOS: tuple[int, ...] = (
    50, 64, 80, 100, 125, 160, 200, 250, 320, 400, 500, 640, 800, 1000, 1250,
    1600, 2000, 2500, 3200, 4000, 5000, 6400, 8000, 10000, 12800, 25600,
)

MOCK_BLACK = 600.0
MOCK_WHITE = 16383.0


class MockCamera(CameraBackend):
    """A D750-shaped simulator.

    ``scene_level`` is the normalised illumination of the simulated scene, so
    tests can drive the auto-exposure loop and assert it converges.
    """

    def __init__(
        self,
        scene_level: float = 0.25,
        live_fps: float = 40.0,
        capture_seconds: float = 0.0,
        width: int = 640,
        height: int = 424,
        settings: ExposureSettings | None = None,
    ) -> None:
        if not 0.0 <= scene_level <= 1.0:
            raise ValueError("scene_level must be within 0..1")
        self.scene_level = scene_level
        self._live_fps = live_fps
        self._capture_seconds = capture_seconds
        self._width = width
        self._height = height
        self._settings = settings or ExposureSettings(1 / 60, 100)
        self._connected = False
        self._live_view = False
        self._frame_index = 0
        self._captures: list[CaptureResult] = []
        self._rng = np.random.default_rng(1234)
        self.shutter_history: list[float] = []
        self.iso_history: list[int] = []
        #: Body-side LV zoom rate (core.zoom ZOOM_*); the mock reframes its
        #: synthetic pattern when it changes, like the D750 does.
        self.live_view_zoom_rate = ZOOM_ALL
        self.live_view_zoom_history: list[int] = []

    def connect(self) -> CameraInfo:
        self._connected = True
        return CameraInfo(
            model="NIKON D750 (mock)",
            manufacturer="Nikon Corporation (mock)",
            serial="MOCK0000000001",
            battery_percent=92,
            shutter_choices=D750_SHUTTERS,
            iso_choices=D750_ISOS,
            sensor_width=MOCK_SENSOR[0],
            sensor_height=MOCK_SENSOR[1],
        )

    def disconnect(self) -> None:
        self._connected = False
        self._live_view = False

    @property
    def info(self) -> CameraInfo:
        return CameraInfo(
            model="NIKON D750 (mock)",
            manufacturer="Nikon Corporation (mock)",
            serial="MOCK0000000001",
            battery_percent=92,
            shutter_choices=D750_SHUTTERS,
            iso_choices=D750_ISOS,
            sensor_width=MOCK_SENSOR[0],
            sensor_height=MOCK_SENSOR[1],
        )

    def capabilities(self) -> CameraCapabilities:
        # live_view_zoom True so the SDK-only zoomed-detail path is exercised
        # by the GUI tests; the real gphoto2 backend reports False there.
        return CameraCapabilities(aperture=False, live_view_zoom=True)

    def get_settings(self) -> ExposureSettings:
        self._require()
        return self._settings

    def set_shutter(self, seconds: float) -> float:
        self._require()
        applied = min(D750_SHUTTERS, key=lambda s: abs(s - seconds))
        self._settings = self._settings.with_shutter(applied)
        self.shutter_history.append(applied)
        return applied

    def set_iso(self, iso: int) -> int:
        self._require()
        applied = min(D750_ISOS, key=lambda i: abs(i - iso))
        self._settings = self._settings.with_iso(applied)
        self.iso_history.append(applied)
        return applied

    def start_live_view(self) -> None:
        self._require()
        self._live_view = True

    def stop_live_view(self) -> None:
        self._live_view = False

    def next_live_frame(self) -> LiveFrame | None:
        if not self._live_view:
            return None
        self._require()
        if self._live_fps > 0:
            time.sleep(1.0 / self._live_fps)
        self._frame_index += 1
        return LiveFrame(jpeg=self._synth_jpeg(), width=self._width, height=self._height)

    def set_live_view_zoom(self, rate: int) -> None:
        self._require()
        self.live_view_zoom_rate = rate
        self.live_view_zoom_history.append(rate)

    def capture(self, destination: Path, filename_stem: str) -> CaptureResult:
        self._require()
        started = time.monotonic()
        if self._capture_seconds:
            time.sleep(self._capture_seconds)
        destination.mkdir(parents=True, exist_ok=True)
        # A genuine NEF cannot be synthesised without LibRaw write support, so a
        # .npy mosaic is written instead. Session code obtains frame metadata
        # through its injected frame reader, which tests point at np.load; that
        # keeps the production read path (rawpy) unmocked and still exercised.
        target = destination / f"{filename_stem}.npy"
        np.save(target, self.synth_frame())
        result = CaptureResult(
            path=target,
            size_bytes=target.stat().st_size,
            settings=self._settings,
            elapsed=time.monotonic() - started,
            capture_target="card",
        )
        self._captures.append(result)
        return result

    # ------------------------------------------------------------------ internals

    def _require(self) -> None:
        if not self._connected:
            raise NotConnectedError("mock fotoaparát není připojen")

    def sensor_signal(self, shutter: float | None = None, iso: int | None = None) -> float:
        """Mean linear signal in DN for the given settings.

        Linear in shutter and ISO, matching the assumption the calibration code
        relies on, with the black pedestal added.
        """
        s = self._settings.shutter if shutter is None else shutter
        g = self._settings.iso if iso is None else iso
        base = self.scene_level * (MOCK_WHITE - MOCK_BLACK)
        reference = 1 / 60 * 100
        signal = base * (s * g / reference)
        return float(np.clip(signal + MOCK_BLACK, 0, MOCK_WHITE))

    def synth_frame(self, height: int = 256, width: int = 384) -> np.ndarray:
        """A plausible raw mosaic: scene gradient, vignette, noise, dark current."""
        y, x = np.mgrid[0:height, 0:width]
        gradient = 0.75 + 0.5 * (x / max(width - 1, 1))
        vignette = 1.0 - 0.18 * (((x - width / 2) ** 2 + (y - height / 2) ** 2) / ((width / 2) ** 2))
        signal = self.sensor_signal() * np.clip(gradient * vignette, 0, None)
        dark = 12.0 * (self._settings.shutter / (1 / 60))
        noisy = signal + dark + self._rng.normal(0, 4.0, signal.shape)
        return np.clip(noisy, 0, MOCK_WHITE).astype(np.uint16)

    def _synth_jpeg(self) -> bytes:
        """A minimal valid JPEG so the GUI decodes a real image, not a stub.

        The body-side zoom rate is honoured the way the D750 honours it: the
        frame keeps its pixel size but stands for a crop of the scene, so the
        scene's gradient is spread across the frame by 1/crop instead of
        uniform. Tests use the pattern change to prove a zoom request actually
        reached the camera.
        """
        import cv2

        from filmscan_studio.core.zoom import detail_for

        detail = detail_for(self.live_view_zoom_rate, self._width, self._height,
                            SensorSize(*MOCK_SENSOR))
        base = self.sensor_signal() / MOCK_WHITE
        span = min(base * 0.6, 1.0)
        ramp = np.linspace(-0.5, 0.5, self._width) * (1.0 / max(detail.crop_fraction, 1e-6))
        ramp = np.clip(base + span * ramp, 0.0, 1.0)
        level = np.clip((ramp * 255).astype(np.uint8)[None, :, None], 0, 255)
        img = np.repeat(np.repeat(level, self._height, axis=0), 3, axis=2)
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        return buf.tobytes() if ok else b""

    @property
    def captures(self) -> list[CaptureResult]:
        return list(self._captures)
