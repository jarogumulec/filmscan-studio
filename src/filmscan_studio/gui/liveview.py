"""Live View off the UI thread.

Two facts drive this module. The D750 delivers ~43 Live View frames per second
over the python-gphoto2 bindings, and the call that fetches one blocks until a
frame arrives. Neither is compatible with a Qt event loop: at 43 fps a queued
signal per frame would flood it, and a blocking call in the GUI thread freezes
the window.

So the worker runs its own loop, drops frames the GUI has not consumed yet
(always showing the newest, which is what focusing wants anyway), and delivers
decoded frames at most ``max_fps`` times a second.
"""

from __future__ import annotations

import logging
import time

import numpy as np
from PySide6.QtCore import QThread, Signal

from filmscan_studio.capture.autoexposure import LiveMeter
from filmscan_studio.capture.camera import CameraBackend, LiveFrame

log = logging.getLogger(__name__)

#: Cap on delivered frames. The D750 offers ~43; the eye does not need the rest
#: and the tone mapping in the preview path costs real milliseconds.
DELIVERY_FPS = 20.0


class LiveViewWorker(QThread):
    """Polls the camera and emits linear-domain frames plus their metering.

    Both are emitted together and always agree: the histogram and the image the
    operator sees are the same frame, which matters because the brief requires
    the histogram to describe linear sensor data. A histogram computed from a
    later frame than the one on screen would be a subtle lie about exposure.
    """

    #: (linear 0..1 luminance-ready RGB float image, meter reading for that
    #: image, body exposure-meter EV or None when the backend has no meter)
    frameReady = Signal(object, object, object)
    error = Signal(str)
    stopped = Signal()

    #: The body meter is a cheap CapGet, but not free: read it at most this
    #: often so a 20 fps poller does not spend half its USB bandwidth on it.
    BODY_EV_INTERVAL_S = 0.2

    def __init__(
        self,
        camera: CameraBackend,
        meter: LiveMeter | None = None,
        max_fps: float = DELIVERY_FPS,
        body_ev_fn=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._camera = camera
        self._meter = meter or LiveMeter()
        self._body_ev_fn = body_ev_fn
        self._min_interval = 1.0 / max_fps if max_fps > 0 else 0.0
        self._running = False
        #: Last successful body exposure-meter reading (EV, log2 domain).
        #: Delivered with every frame so the histogram can be *predicted* to
        #: the exposure the NEF will actually get (see capture_window).
        self._body_ev: float | None = None
        self._body_ev_updated = 0.0
        #: The newest undelivered frame. Older ones are discarded, not queued:
        #: a stale frame on a focus screen is worse than a dropped one. A lost
        #: race on this single reference can only drop a frame, which is the
        #: designed behaviour, so it needs no lock.
        self._pending: LiveFrame | None = None
        #: Set by the owner before stop() when it will keep metering itself:
        #: Auto Exposure polls frames right after the worker is stopped, and
        #: the Nikon SDK answers GetLiveViewImage with -127 if the teardown
        #: already switched the body's Live View off (observed 2026-09).
        self.leave_live_view = False

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:  # type: ignore[override]
        self._running = True
        super().start()

    def stop(self, wait_ms: int = 3000) -> None:
        self._running = False
        self.wait(wait_ms)

    @property
    def running(self) -> bool:
        return self._running

    # ----------------------------------------------------------------- loop

    def run(self) -> None:  # noqa: D102 - QThread contract
        last_delivery = 0.0
        try:
            self._camera.start_live_view()
        except Exception as exc:  # noqa: BLE001 - reported, not raised across threads
            self.error.emit(str(exc))
            return
        try:
            while self._running:
                try:
                    frame = self._camera.next_live_frame()
                except Exception as exc:  # noqa: BLE001
                    self.error.emit(str(exc))
                    break
                if frame is None:
                    self.msleep(5)
                    continue
                self._pending = frame
                now = time.monotonic()
                if now - last_delivery < self._min_interval:
                    continue
                current, self._pending = self._pending, None
                if current is None:
                    continue
                last_delivery = now
                try:
                    encoded = self._meter.decode_live_frame(current)
                    linear = self._meter.jpeg_to_linear(encoded)
                    reading = self._meter.meter_jpeg(current)
                except Exception as exc:  # noqa: BLE001 - one bad frame is not fatal
                    log.debug("skipping undecodable Live View frame: %s", exc)
                    continue
                if (
                    self._body_ev_fn is not None
                    and now - self._body_ev_updated >= self.BODY_EV_INTERVAL_S
                ):
                    self._body_ev_updated = now
                    try:
                        self._body_ev = self._body_ev_fn()
                    except Exception:  # noqa: BLE001 - a missed read is not fatal
                        log.debug("exposure_ev read failed", exc_info=True)
                if not self._running:
                    break
                self.frameReady.emit(linear, reading, self._body_ev)
        finally:
            if not self.leave_live_view:
                try:
                    self._camera.stop_live_view()
                except Exception:  # noqa: BLE001 - nothing useful to do on the way out
                    log.exception("stop_live_view failed")
            self.stopped.emit()


def luminance(rgb: np.ndarray) -> np.ndarray:
    """Rec. 709 luminance of a float RGB image, for histogram display."""
    return np.asarray(rgb, dtype=np.float64) @ np.array([0.2126, 0.7152, 0.0722])
