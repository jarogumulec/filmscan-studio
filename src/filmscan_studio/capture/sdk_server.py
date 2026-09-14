"""x86_64 helper: serves Nikon MAID commands over stdin/stdout JSON lines.

Runs inside ``.venv-x86`` (Rosetta) because ``libNkPTPDriver2.dylib`` has no
arm64 slice; the arm64 GUI talks to it through
:mod:`filmscan_studio.capture.nikon_backend`. Protocol: one JSON object per
line on stdin, one JSON object per line on stdout — ``{"ok": true, ...}`` or
``{"ok": false, "error": "..."}``. Frames travel base64-encoded; the NEF is
written straight to the destination path (shared filesystem), never piped.

Launch (what nikon_backend does):

    .venv-x86/bin/python -m filmscan_studio.capture.sdk_server
"""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path

from filmscan_studio.capture import nikon_sdk as sdk

JPEG_MAGIC = b"\xff\xd8\xff"


class Server:
    def __init__(self) -> None:
        self.mod: sdk.MaidModule | None = None
        self.src: sdk.Source | None = None
        # Decoded packed-string enums, cached at connect(): index -> string.
        self.shutter_strings: list[str] = []
        self.iso_strings: list[str] = []

    # ------------------------------------------------------------------ setup

    def connect(self) -> dict:
        self.mod = sdk.MaidModule()
        self.mod.__enter__()
        # ImageCaptureCore needs the run loop serviced for AddChild events.
        for _ in range(300):
            self.mod.tramp.runloop_tick(0.005)
        devs = sdk.devices(self.mod)
        if not devs:
            raise sdk.MaidError("no device — USB/PTP?", -1)
        self.src = sdk.Source(self.mod, devs[0])
        self.shutter_strings = self._packed_strings(sdk.CAP_SHUTTER_SPEED)
        self.iso_strings = self._packed_strings(sdk.CAP_SENSITIVITY)
        battery: int | None
        try:
            battery = self.mod.get_integer(self.src.obj, sdk.CAP_BATTERY_LEVEL)
        except sdk.MaidError:
            battery = None
        return {
            "model": self.src.describe(),
            "camera_type": self.src.camera_type(),
            "battery": battery,
            "shutter": {"current_index": self._enum_index(sdk.CAP_SHUTTER_SPEED),
                        "strings": self.shutter_strings},
            "iso": {"current_index": self._enum_index(sdk.CAP_SENSITIVITY),
                    "strings": self.iso_strings},
            "has_live_view": self.src.has(sdk.CAP_LIVE_VIEW_STATUS),
            # Which of the exposure controls the body actually grants depends
            # on the physical mode dial: in A it is ISO + ExposureComp only;
            # ShutterSpeed gains SET when the dial moves to S or M. Verified
            # against CapInfo ulOperations on the D750.
            "shutter_settable": self.src.has(sdk.CAP_SHUTTER_SPEED, sdk.OP_SET),
            "iso_settable": self.src.has(sdk.CAP_SENSITIVITY, sdk.OP_SET),
        }

    def _packed_strings(self, cap_id: int) -> list[str]:
        """PackedString enum (wPhysicalBytes == 1, sample
        SetEnumPackedStringCapability): raw bytes are NUL-separated strings
        and ulValue is the *index*, not a character value."""
        _, _, raw = self._enum_raw(cap_id)
        strs: list[str] = []
        i = 0
        while i < len(raw):
            j = raw.index(b"\x00", i)
            strs.append(raw[i:j].decode(errors="replace"))
            i = j + 1
        return strs

    def _enum_raw(self, cap_id: int):
        from filmscan_studio.capture.nikon_sdk import ffi
        st = ffi.new("NkMAIDEnum *")
        r = self.mod.command(self.src.obj, sdk.CMD_CAP_GET, cap_id,
                             sdk.DT_ENUM_PTR, self.mod._ptr(st))
        if r != sdk.R_OK:
            raise sdk.MaidError(f"CapGet enum 0x{cap_id:x}", r)
        count, phys, cur = int(st.ulElements), int(st.wPhysicalBytes), int(st.ulValue)
        if count == 0:
            return cur, phys, b""
        buf = ffi.new(f"char[{count * phys}]")
        st.pData = buf
        r = self.mod.command(self.src.obj, sdk.CMD_CAP_GET_ARRAY, cap_id,
                             sdk.DT_ENUM_PTR, self.mod._ptr(st))
        if r != sdk.R_OK:
            raise sdk.MaidError(f"CapGetArray enum 0x{cap_id:x}", r)
        return cur, phys, bytes(ffi.buffer(buf))

    def _enum_index(self, cap_id: int) -> int:
        cur, _, _ = self._enum_raw(cap_id)
        return cur

    # ------------------------------------------------------------- properties

    def get_settings(self) -> dict:
        # Mode-dial changes flip which caps carry OP_SET — refresh CapInfos
        # so shutter_settable reflects the dial, not connect() time.
        self.src.caps = {c["id"]: c for c in self.mod.cap_infos(self.src.obj)}
        si = self._enum_index(sdk.CAP_SHUTTER_SPEED)
        ii = self._enum_index(sdk.CAP_SENSITIVITY)
        out = {
            "shutter_index": si,
            "shutter_string": self.shutter_strings[si] if si < len(self.shutter_strings) else None,
            "iso_index": ii,
            "iso_string": self.iso_strings[ii] if ii < len(self.iso_strings) else None,
            "shutter_settable": self.src.has(sdk.CAP_SHUTTER_SPEED, sdk.OP_SET),
            "iso_settable": self.src.has(sdk.CAP_SENSITIVITY, sdk.OP_SET),
        }
        try:
            out["exposure_comp"] = self.mod.get_range(
                self.src.obj, sdk.CAP_EXPOSURE_COMP)["value"]
        except sdk.MaidError:
            pass
        return out

    def set_enum(self, which: str, index: int) -> dict:
        cap = {"shutter": sdk.CAP_SHUTTER_SPEED,
               "iso": sdk.CAP_SENSITIVITY}[which]
        strings = {"shutter": self.shutter_strings,
                   "iso": self.iso_strings}[which]
        if not 0 <= index < len(strings):
            raise ValueError(f"{which} index {index} out of range")
        if not self.src.has(cap, sdk.OP_SET):
            raise RuntimeError(
                f"{which} nastavitelné není — režim na těle je A/P "
                "(přepni kolečkem na M nebo S)")
        # set_enum_value tries the Unsigned shortcut then the full EnumPtr
        # struct — ShutterSpeed rejects Unsigned with -127 and needs the
        # struct form.
        self.mod.set_enum_value(self.src.obj, cap, index)
        applied = self._enum_index(cap)
        return {"index": applied, "string": strings[applied]}

    def set_exposure_comp(self, ev: float) -> dict:
        return {"ev": self.mod.set_range(self.src.obj, sdk.CAP_EXPOSURE_COMP,
                                         ev)}

    def exposure_ev(self) -> dict:
        return {"ev": self.src.exposure_status()}

    # -------------------------------------------------------------- live view

    def lv_on(self) -> dict:
        self.src.lv_on()
        return {"status": self.src.lv_status()}

    def lv_off(self) -> dict:
        self.src.lv_off()
        return {"status": self.src.lv_status()}

    def set_zoom(self, rate: int) -> dict:
        self.src.set_zoom(rate)
        cur, _ = self.src.zoom_values()
        return {"zoom": cur}

    def lv_frame(self) -> dict:
        blob = self.src.lv_image()
        off = blob.find(JPEG_MAGIC)
        return {"jpeg_b64": base64.b64encode(blob[off:]).decode("ascii")}

    # ---------------------------------------------------------------- capture

    def capture(self, destination: str, stem: str) -> dict:
        dest = Path(destination)
        started = time.monotonic()
        # The module keeps the card name it reported on the Item object;
        # capture_still writes exactly what the body sent.
        target = dest / f"{stem}.NEF"
        self.src.capture_still(target)
        return {"path": str(target),
                "bytes": target.stat().st_size,
                "elapsed": round(time.monotonic() - started, 2)}

    # ------------------------------------------------------------------- misc

    def close(self) -> dict:
        if self.src is not None:
            try:
                self.src.lv_off()
            except Exception:  # noqa: BLE001 - teardown is best effort
                pass
            self.src = None
        if self.mod is not None:
            self.mod.close()   # unwinds open children first (driver requirement)
            self.mod = None
        return {}


def _dispatch(server: Server, req: dict) -> dict:
    method = req["method"]
    if method == "connect":
        return server.connect()
    if method == "get_settings":
        return server.get_settings()
    if method == "set_enum":
        return server.set_enum(req["which"], req["index"])
    if method == "exposure_ev":
        return server.exposure_ev()
    if method == "set_zoom":
        return server.set_zoom(req["rate"])
    if method == "set_exposure_comp":
        return server.set_exposure_comp(req["ev"])
    if method == "lv_on":
        return server.lv_on()
    if method == "lv_off":
        return server.lv_off()
    if method == "lv_frame":
        return server.lv_frame()
    if method == "capture":
        return server.capture(req["destination"], req["stem"])
    if method == "close":
        result = server.close()
        result["bye"] = True
        return result
    raise KeyError(f"unknown method {method!r}")


def main() -> int:
    # stdout is the RPC channel; keep stray prints off it.
    sys.stdout = sys.stderr
    real_out = sys.__stdout__
    server = Server()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            reply = _dispatch(server, req)
            bye = reply.pop("bye", False)
            reply["ok"] = True
        except Exception as exc:  # noqa: BLE001 - report, keep serving
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            bye = False
        real_out.write(json.dumps(reply) + "\n")
        real_out.flush()
        if bye:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
