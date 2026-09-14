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
import re
import subprocess
import threading
import time
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
from filmscan_studio.core.exposure import ExposureSettings

log = logging.getLogger(__name__)

#: Zoom levels for LiveViewImageZoomRate (eNkMAIDLiveViewImageZoomRate).
ZOOM_ALL, ZOOM_25, ZOOM_33, ZOOM_50, ZOOM_66, ZOOM_100, ZOOM_200 = range(7)

_SHUTTER_RE = re.compile(r"^(?:(\d+)\s*/\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?))$")


def parse_shutter(value: str) -> float | None:
    """'1/60' -> 0.0166…, '2.5' -> 2.5; None for 'Bulb'/'Time'-style entries."""
    m = _SHUTTER_RE.match(value.strip())
    if not m:
        return None
    num, den, whole = m.groups()
    if num is not None:
        d = float(den)
        return int(num) / d if d else None
    return float(whole)


def parse_iso(value: str) -> int | None:
    """'100' -> 100; None for 'LO-1'/'Hi-2.0' extended ranges (out of the
    auto-exposure controller's world anyway)."""
    m = re.fullmatch(r"(\d+)", value.strip())
    return int(m.group(1)) if m else None


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
        self._info = CameraInfo(model="unknown")
        self._shutter_choices: list[tuple[int, float]] = []   # (index, seconds)
        self._iso_choices: list[tuple[int, int]] = []         # (index, iso)
        self._current_shutter: float | None = None
        self._current_iso: int | None = None
        self._live_view = False

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
        self._proc = subprocess.Popen(
            [str(helper), "-m", "filmscan_studio.capture.sdk_server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
            env={**os.environ,
                 "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
        )
        try:
            reply = self._rpc("connect")
        except CameraError:
            self._kill()
            raise
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

    # ------------------------------------------------------------------ RPC

    def _rpc(self, method: str, timeout: float | None = None, **params):
        if self._proc is None or self._proc.poll() is not None:
            raise NotConnectedError("Nikon SDK helper neběží")
        assert self._proc.stdin is not None and self._proc.stdout is not None
        self._proc.stdin.write(json.dumps({"method": method, **params}) + "\n")
        self._proc.stdin.flush()
        deadline = time.monotonic() + (timeout or self._rpc_timeout)
        # select() would be nicer but stdout of a Popen text-mode pipe is not
        # always select()-safe on macOS; a watchdog thread around readline is.
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
        reply = self._rpc("lv_frame", timeout=15.0)
        return LiveFrame(jpeg=base64.b64decode(reply["jpeg_b64"]))

    # SDK-only extras (the reason for the whole swap) -------------------------

    def set_zoom(self, rate: int) -> None:
        """Whole-frame .. 200% camera-side LV crop (ZOOM_* constants)."""
        self._rpc("set_zoom", rate=rate)

    def exposure_ev(self) -> float:
        """Body's meter reading in EV; the AE loop drives this to 0."""
        return float(self._rpc("exposure_ev")["ev"])

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
        return CaptureResult(
            path=target,
            size_bytes=reply["bytes"],
            settings=settings,
            elapsed=reply["elapsed"],
            capture_target="card",
        )

    @property
    def info(self) -> CameraInfo:
        return self._info
