"""Live View off the UI thread.

Two facts drive this module. The Touptek streams up to ~30 overview frames per
second over USB3, and the call that fetches one blocks until a frame arrives
(``queue.get`` behind the SDK callback). Neither is compatible with a Qt event
loop: at full rate a queued signal per frame would flood it, and a blocking
call in the GUI thread freezes the window.

So the worker runs its own loop, drops frames the GUI has not consumed yet
(always showing the newest, which is what focusing wants anyway), and delivers
*normalised linear data* plus its metering — both from the same frame, so the
histogram and the picture always agree. The stream is already linear sensor
data; the only transform here is the divide by the frame's own white level.
"""

from __future__ import annotations

import logging
import time

import numpy as np
from PySide6.QtCore import QThread, Signal

from filmscan_studio.capture.autoexposure import LiveMeter
from filmscan_studio.capture.camera import CameraBackend, LiveFrame

log = logging.getLogger(__name__)

#: Cap on delivered frames. The overview stream offers ~15-30 fps; the eye
#: does not need the rest and the preview transform costs real milliseconds.
DELIVERY_FPS = 15.0


class LiveViewWorker(QThread):
    """Polls the camera and emits normalised linear frames plus their metering.

    Both are emitted together and always agree: the histogram and the image
    the operator sees are the same frame, which matters because the brief
    requires the histogram to describe linear sensor data. Unlike the D750
    there is no body meter to predict against — the stream *is* the capture's
    radiometry, so one honest path replaced two disagreeing ones.
    """

    #: (normalised 0..1 float mono image, meter reading for that image)
    frameReady = Signal(object, object)
    error = Signal(str)
    stopped = Signal()

    def __init__(
        self,
        camera: CameraBackend,
        meter: LiveMeter | None = None,
        max_fps: float = DELIVERY_FPS,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._camera = camera
        self._meter = meter or LiveMeter()
        self._min_interval = 1.0 / max_fps if max_fps > 0 else 0.0
        self._running = False
        #: The newest undelivered frame. Older ones are discarded, not queued:
        #: a stale frame on a focus screen is worse than a dropped one. A lost
        #: race on this single reference can only drop a frame, which is the
        #: designed behaviour, so it needs no lock.
        self._pending: LiveFrame | None = None
        #: Set by the owner before stop() when it will keep metering itself:
        #: Auto Exposure polls frames right after the worker is stopped, and
        #: the camera must therefore stay streaming through the teardown.
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
                    normalised, reading = self._meter.normalized_frame(current)
                except Exception as exc:  # noqa: BLE001 - one bad frame is not fatal
                    log.debug("skipping undecodable Live View frame: %s", exc)
                    continue
                if not self._running:
                    break
                self.frameReady.emit(normalised, reading)
        finally:
            if not self.leave_live_view:
                try:
                    self._camera.stop_live_view()
                except Exception:  # noqa: BLE001 - nothing useful to do on the way out
                    log.exception("stop_live_view failed")
            self.stopped.emit()


def luminance(image: np.ndarray) -> np.ndarray:
    """Rec. 709 luminance; a no-op passthrough for the mono stream's 2-D data.

    Kept so histogram code reads the same whether a source is colour or the
    TS2600MP-G2's single channel — the mono path must not pretend to be RGB.
    """
    a = np.asarray(image, dtype=np.float64)
    if a.ndim == 2:
        return a
    return a @ np.array([0.2126, 0.7152, 0.0722])
