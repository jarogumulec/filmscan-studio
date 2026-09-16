"""Nikon SDK backend for the D750 — the gphoto2 replacement.

The arm64 GUI cannot dlopen ``libNkPTPDriver2.dylib`` (x86_64-only), so this
class owns a line-JSON-RPC pipe into a Rosetta helper process running
:mod:`filmscan_studio.capture.sdk_server` from ``.venv-x86``. Everything above
this file keeps talking to :class:`CameraBackend` unchanged.

What the SDK adds over gphoto2 (all measured against the D750, see
``sdk_probe_results.json``):

* ``set_zoom()`` — the *body* crops the Live View stream (Whole..200%),
  so focus checking on film grain happens at sensor resolution instead of
  upscaling a 640px whole-frame preview in OpenCV;
* ``exposure_ev()`` — the body's own meter reading (Float cap) usable as
  AE feedback while Live View runs;
* no ``ptpcamera`` fight: the SDK shares the device through ImageCaptureCore.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

from filmscan_studio.capture.camera import (
    CameraBackend,
    CameraCapabilities,
    CameraError,
    CameraInfo,
    CaptureResult,
    LiveFrame,
    NotConnectedError,
)
from filmscan_studio.capture.parsing import parse_iso, parse_shutter  # noqa: F401  (re-export)
from filmscan_studio.core.exposure import ExposureSettings

log = logging.getLogger(__name__)

#: Zoom levels for LiveViewImageZoomRate (eNkMAIDLiveViewImageZoomRate).
ZOOM_ALL, ZOOM_25, ZOOM_33, ZOOM_50, ZOOM_66, ZOOM_100, ZOOM_200 = range(7)


def _helper_python() -> Path | None:
    """Locate the Rosetta venv created by scripts/install_helper.sh:
    next to the repo root (editable install) or beside the running one."""
    here = Path(__file__).resolve()
    for root in [here.parents[3], *Path.cwd().parents]:
        candidate = root / ".venv-x86" / "bin" / "python"
        if candidate.is_file():
            return candidate
    return None


class NikonSdkBackend(CameraBackend):
    """D750 through Nikon's Type0015 module, via an x86_64 helper process."""

    def __init__(self, helper_python: Path | None = None,
                 rpc_timeout: float = 30.0) -> None:
        self._helper_python = helper_python
        self._rpc_timeout = rpc_timeout
        self._proc: subprocess.Popen | None = None
        #: One JSON request must complete (write + its reply read) before the
        #: next starts. Without it the LiveView poller and a UI-thread zoom or
        #: shutter write interleave on the same pipe pair: each thread reads
        #: the other's line and json sees 'Extra data'/'Expecting value'
        #: (observed 2026-09 with the D750 attached).
        self._rpc_lock = threading.Lock()
        self._info = CameraInfo(model="unknown")
        self._shutter_choices: list[tuple[int, float]] = []   # (index, seconds)
        self._iso_choices: list[tuple[int, int]] = []         # (index, iso)
        self._current_shutter: float | None = None
        self._current_iso: int | None = None
        self._live_view = False
        #: Helper's stderr lands here (a pipe would deadlock once the crash
        #: traceback outgrows the 64 KB buffer; a file the child shares with
        #: us cannot). Read on failure to explain *why* it died — the numpy
        #: import crash of 2026-09 hid behind DEVNULL for a whole afternoon.
        self._err_log: tempfile.TemporaryFile | None = None

    # ---------------------------------------------------------------- lifecycle

    def connect(self) -> CameraInfo:
        if self._proc is not None:
            return self._info
        helper = self._helper_python or _helper_python()
        if helper is None:
            raise CameraError(
                "Nikon SDK helper chybí — spusť scripts/install_helper.sh "
                "(vytvoří .venv-x86 pod Rosettou).")
        if not Path("/Library/Application Support/Nikon/Camera Control "
                    "Modules/Type0015 Module.bundle").is_dir():
            raise CameraError(
                "Nikon SDK není nainstalovaný — spusť scripts/install_sdk.sh "
                "(sudo, zkopíruje modul do /Library/Application Support/Nikon).")
        self._err_log = tempfile.TemporaryFile(mode="w+b")
        try:
            self._proc = subprocess.Popen(
                [str(helper), "-m", "filmscan_studio.capture.sdk_server"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=self._err_log, text=True,
                env={**os.environ,
                     "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
            )
        except OSError as exc:            # unspawnable helper (bad path/arch)
            self._close_err_log()
            raise CameraError(f"Nelze spustit Nikon SDK helper: {exc}") from exc
        try:
            reply = self._rpc("connect")
        except CameraError as exc:
            reason = self._helper_error()
            self._kill()
            if isinstance(exc, NotConnectedError):
                # _rpc refused to talk to a process that is already gone —
                # the log tail is the whole story, drop the boilerplate.
                raise CameraError(reason or "Nikon SDK helper spadl") from None
            raise CameraError(f"{exc} — helper: {reason}" if reason
                              else str(exc)) from None
        model = reply["model"]
        parsed_shutter = [(i, v) for i, s in enumerate(reply["shutter"]["strings"])
                          if (v := parse_shutter(s)) is not None]
        parsed_iso = [(i, v) for i, s in enumerate(reply["iso"]["strings"])
                      if (v := parse_iso(s)) is not None]
        self._shutter_choices = parsed_shutter
        self._iso_choices = parsed_iso
        cur_s = reply["shutter"]["current_index"]
        cur_i = reply["iso"]["current_index"]
        self._current_shutter = dict(parsed_shutter).get(cur_s)
        self._current_iso = dict(parsed_iso).get(cur_i)
        self._info = CameraInfo(
            model=model,
            manufacturer="Nikon",
            battery_percent=reply.get("battery"),
            shutter_choices=tuple(v for _, v in parsed_shutter),
            iso_choices=tuple(v for _, v in parsed_iso),
            # D750 NEF geometry; not queried from the body (no cap verified).
            sensor_width=6016,
            sensor_height=4016,
        )
        log.info("SDK backend: %s, %d shutter / %d ISO choices",
                 model, len(parsed_shutter), len(parsed_iso))
        return self._info

    def disconnect(self) -> None:
        if self._proc is None:
            return
        try:
            self._rpc("close", timeout=10.0)
        except CameraError:
            pass
        finally:
            self._kill()
            self._live_view = False

    def _kill(self) -> None:
        if self._proc is not None:
            self._proc.kill()
            self._proc.wait(timeout=5)
            self._proc = None
        self._close_err_log()

    def _close_err_log(self) -> None:
        if self._err_log is not None:
            self._err_log.close()
            self._err_log = None

    def _helper_error(self) -> str:
        """Why an already-exited helper died — the tail of its stderr, or ''
        while it is still alive. Returns '' cheaply so callers can append."""
        if (self._proc is None or self._err_log is None
                or self._proc.poll() is None):
            return ""
        try:
            self._err_log.seek(0)
            data = self._err_log.read()
        except OSError:
            return ""
        lines = [ln for ln in data.decode(errors="replace").splitlines()
                 if ln.strip()]
        if not lines:
            return f"spadl (exit {self._proc.poll()}) bez hlášky"
        return (f"spadl (exit {self._proc.poll()}): "
                + " ┃ ".join(deque(lines, maxlen=4)))

    # ------------------------------------------------------------------ RPC

    def _rpc(self, method: str, timeout: float | None = None, **params):
        if self._proc is None or self._proc.poll() is not None:
            raise NotConnectedError("Nikon SDK helper neběží")
        # One complete request/reply at a time, poller thread and UI thread
        # alike — see the comment on _rpc_lock in __init__.
        with self._rpc_lock:
            if self._proc is None or self._proc.poll() is not None:
                raise NotConnectedError("Nikon SDK helper neběží")
            assert self._proc.stdin is not None and self._proc.stdout is not None
            self._proc.stdin.write(json.dumps({"method": method, **params}) + "\n")
            self._proc.stdin.flush()
            deadline = time.monotonic() + (timeout or self._rpc_timeout)
            # select() would be nicer but stdout of a Popen text-mode pipe is
            # not always select()-safe on macOS; a watchdog thread around
            # readline is.
            result: list[str] = []

            def _read() -> None:
                line = self._proc.stdout.readline()
                result.append(line)

            t = threading.Thread(target=_read, daemon=True)
            t.start()
            t.join(timeout=max(0.05, deadline - time.monotonic()))
            if t.is_alive() or not result or not result[0]:
                self._kill()
                raise CameraError(f"Nikon SDK helper neodpověděl na {method!r}")
            reply = json.loads(result[0])
        if not reply.get("ok"):
            raise CameraError(f"Nikon SDK ({method}): {reply.get('error')}")
        return reply

    # ------------------------------------------------------------ capabilities

    def capabilities(self) -> CameraCapabilities:
        return CameraCapabilities(
            live_view=True, shutter=True, iso=True,
            aperture=False,
            # MfDrive is absent on the manual AI/AI-s lens (verified by the
            # hardware probe); even on electronic glass focus stacking wants
            # the macro rail, not the lens motor.
            focus_drive=False,
            live_view_zoom=True,
            notes=("Nikon SDK:Live View zoom (Whole..200%) na straně těla; "
                   "clona se na manuálním skle nenastavuje (z EXIF)."),
        )

    def get_settings(self) -> ExposureSettings:
        if self._proc is None:
            raise NotConnectedError("Nikon SDK helper neběží")
        s = self._rpc("get_settings")
        sv = dict(self._shutter_choices).get(s["shutter_index"])
        iv = dict(self._iso_choices).get(s["iso_index"])
        if sv is not None:
            self._current_shutter = sv
        if iv is not None:
            self._current_iso = iv
        return ExposureSettings(shutter=sv or 1.0, iso=iv or 100)

    def set_shutter(self, seconds: float) -> float:
        if seconds <= 0:
            raise ValueError("shutter must be positive")
        if not self._shutter_choices:
            raise CameraError("fotoaparát nenabízí žádné expoziční časy")
        index, applied = min(self._shutter_choices,
                             key=lambda iv: abs(iv[1] - seconds))
        reply = self._rpc("set_enum", which="shutter", index=index)
        got = parse_shutter(reply["string"]) or applied
        self._current_shutter = got
        return got

    def set_iso(self, iso: int) -> int:
        if iso <= 0:
            raise ValueError("iso must be positive")
        if not self._iso_choices:
            raise CameraError("fotoaparát nenabízí žádné hodnoty ISO")
        index, applied = min(self._iso_choices, key=lambda iv: abs(iv[1] - iso))
        reply = self._rpc("set_enum", which="iso", index=index)
        got = parse_iso(reply["string"]) or applied
        self._current_iso = got
        return got

    # --------------------------------------------------------------- live view

    def start_live_view(self) -> None:
        self._rpc("lv_on", timeout=20.0)
        self._live_view = True

    def stop_live_view(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            self._live_view = False
            return
        try:
            self._rpc("lv_off", timeout=15.0)
        finally:
            self._live_view = False

    def next_live_frame(self) -> LiveFrame | None:
        if self._proc is None:
            raise NotConnectedError("Nikon SDK helper neběží")
        try:
            reply = self._rpc("lv_frame", timeout=15.0)
        except CameraError as exc:
            # -127 on GetLiveViewImage means the body has left Live View while
            # we still thought it ran (a cancelled capture does this). One
            # lv_on + retry turns a GUI-visible error into one blank moment.
            if "-127" not in str(exc) or not self._live_view:
                raise
            log.info("LV odpovídá -127 (tělo mimo Live View) — restartuji LV")
            self.start_live_view()
            reply = self._rpc("lv_frame", timeout=15.0)
        return LiveFrame(jpeg=base64.b64decode(reply["jpeg_b64"]))

    # SDK-only extras (the reason for the whole swap) -------------------------

    def set_live_view_zoom(self, rate: int) -> None:
        """Whole-frame .. 200% camera-side LV crop (core.zoom ZOOM_* rates)."""
        self._rpc("set_zoom", rate=rate)

    #: Backwards-compatible alias for probe scripts and older call sites.
    set_zoom = set_live_view_zoom

    def exposure_ev(self) -> float:
        """Body's meter reading in EV; the AE loop drives this to 0."""
        return float(self._rpc("exposure_ev")["ev"])

    def set_exposure_ev(self, ev: float) -> float:
        """ExposureComp (Range cap) — a third actuator beside shutter and ISO."""
        return float(self._rpc("set_exposure_comp", ev=ev)["ev"])

    # ---------------------------------------------------------------- capture

    def capture(self, destination: Path, filename_stem: str) -> CaptureResult:
        if self._proc is None:
            raise NotConnectedError("Nikon SDK helper neběží")
        destination.mkdir(parents=True, exist_ok=True)
        settings = self.get_settings() if self._current_shutter is None else \
            ExposureSettings(shutter=self._current_shutter or 1.0,
                             iso=self._current_iso or 100)
        reply = self._rpc("capture", timeout=90.0,
                          destination=str(destination), stem=filename_stem)
        target = Path(reply["path"])
        if reply.get("file_format") == "jpeg":
            target = target.with_suffix(".jpg")
            Path(reply["path"]).rename(target)
            log.warning("tělo poslalo JPEG místo RAW (Compression Level "
                        "není RAW) — uloženo poctivě jako %s", target.name)
        return CaptureResult(
            path=target,
            size_bytes=reply["bytes"],
            settings=settings,
            elapsed=reply["elapsed"],
            capture_target="card",
            file_format=reply.get("file_format", "nef"),
        )

    # ------------------------------------------------------------- media caps

    def media_settings(self) -> dict:
        """Compression Level + Image Size as the body reports them now."""
        return self._rpc("media_settings")

    def set_media(self, compression: int | None = None,
                  size: int | None = None) -> dict:
        """Set RAW/size and return the read-back state (verified write)."""
        return self._rpc("set_media", compression=compression, size=size)

    @property
    def info(self) -> CameraInfo:
        return self._info
