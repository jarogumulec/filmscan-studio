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
# The body's shutter/ISO vocabulary ('1/60', '100') parsed by the shared,
# dependency-free module — importing the GUI-side parsers through
# nikon_backend would pull numpy into this venv and kill the helper at import.
from filmscan_studio.capture.parsing import parse_iso, parse_shutter

JPEG_MAGIC = b"\xff\xd8\xff"


class Server:
    def __init__(self) -> None:
        self.mod: sdk.MaidModule | None = None
        self.src: sdk.Source | None = None
        # Decoded packed-string enums, cached at connect(): index -> string.
        # For PackedString caps ulValue is this *index* (measured: Sensitivity
        # element 3 is the string '100'), so index is also what CapSet takes.
        self.shutter_strings: list[str] = []
        self.iso_strings: list[str] = []
        # Parsed numeric forms, index -> seconds / ISO, so set_enum can answer
        # with the value the GUI works in instead of a bare index.
        self.shutter_seconds: list[float | None] = []
        self.iso_values: list[int | None] = []

    # ------------------------------------------------------------------ setup

    def _discover(self) -> list:
        # ImageCaptureCore needs the run loop serviced for AddChild events.
        for _ in range(300):
            self.mod.tramp.runloop_tick(0.005)
        return sdk.devices(self.mod)

    def connect(self) -> dict:
        self.mod = sdk.MaidModule()
        self.mod.__enter__()
        devs = self._discover()
        if not devs:
            # After an unplug/replug the first AddChild sweep can come up
            # empty; one more serviced round is ~1.5 s and usually finds it.
            # Deliberately only one retry — the arm64 side falls back to
            # gphoto2 and needs a fast, honest "no device" otherwise.
            devs = self._discover()
        if not devs:
            raise sdk.MaidError("no device — USB/PTP?", -1)
        self.src = sdk.Source(self.mod, devs[0])
        self.shutter_strings = self._packed_strings(sdk.CAP_SHUTTER_SPEED)
        self.iso_strings = self._packed_strings(sdk.CAP_SENSITIVITY)
        self.shutter_seconds = [parse_shutter(s) for s in self.shutter_strings]
        self.iso_values = [parse_iso(s) for s in self.iso_strings]
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
        numeric = {"shutter": self.shutter_seconds,
                   "iso": self.iso_values}[which]
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
        return {"index": applied, "string": strings[applied],
                "value": numeric[applied] if applied < len(numeric) else None}

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

    def set_lv_size(self, element: int) -> dict:
        """LiveViewImageSize (0x8353) — enumerated 1..2 on the D750, which
        element is which resolution is NOT yet measured; the probe should
        decode a frame per element and record pixel dimensions."""
        self.mod.set_enum_value(self.src.obj, sdk.CAP_LIVE_VIEW_IMAGE_SIZE,
                                element)
        cur, vals = self.src.lv_image_size_values()
        return {"size": cur, "available": vals}

    def lv_capabilities(self) -> dict:
        """What the LV controls report right now — zoom ladder, size ladder,
        and whether exposure preview is offered at all (on the D750 CapInfo
        says 0x8333 is GET-only: the body previews LV exposure itself and
        refuses to be told; verified via sdk_probe_results.json)."""
        out: dict = {}
        try:
            cur, vals = self.src.zoom_values()
            out["zoom"] = {"current": cur, "values": vals}
        except sdk.MaidError as exc:
            out["zoom"] = f"error: {exc}"
        try:
            cur, vals = self.src.lv_image_size_values()
            out["size"] = {"current": cur, "values": vals}
        except sdk.MaidError as exc:
            out["size"] = f"error: {exc}"
        out["exposure_preview_settable"] = self.src.has(
            sdk.CAP_LIVE_VIEW_EXPOSURE_PREVIEW, sdk.OP_SET)
        return out

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
        # The extension is a promise the body does not always keep: with
        # Compression Level != RAW the very same transfer lands as a JPEG
        # (measured 2026-09: JPEG Basic at S 3008x2008, ~1 MB, saved as
        # .NEF). Sniff the magic so callers can stop lying in sidecars and
        # rawpy never sees a JPEG wearing a NEF mask (LibRaw answers the
        # misleading b'Input/output error').
        with open(target, "rb") as fh:
            magic = fh.read(4)
        if magic.startswith(JPEG_MAGIC[:3]):
            file_format = "jpeg"
        elif magic[:4] in (b"II*\x00", b"MM\x00*"):
            file_format = "nef"
        else:
            file_format = "unknown"
        return {"path": str(target),
                "bytes": target.stat().st_size,
                "elapsed": round(time.monotonic() - started, 2),
                "file_format": file_format}

    # ------------------------------------------------------------- media caps

    #: Media-quality caps measured live on the D750 (2026-09-15):
    #: 0x8110 Compression Level = JPEG Basic|JPEG Normal|JPEG Fine|RAW|
    #:        RAW + JPEG Basic|...  (element 3 = RAW)
    #: 0x8157 Image Size = L(6016*4016)|M(4512*3008)|S(3008*2008) (element 0 = L)
    CAP_COMPRESSION_LEVEL = 0x8110
    CAP_IMAGE_SIZE = 0x8157

    def media_settings(self) -> dict:
        out: dict = {}
        for name, cap in (("compression", self.CAP_COMPRESSION_LEVEL),
                          ("size", self.CAP_IMAGE_SIZE)):
            try:
                cur, _, raw = self._enum_raw(cap)
                out[name] = {"current": cur,
                             "strings": [s.decode(errors="replace")
                                         for s in raw.split(b"\x00")[:-1]],
                             "settable": self.src.has(cap, sdk.OP_SET)}
            except sdk.MaidError as exc:
                out[name] = {"error": str(exc)}
        return out

    def set_media(self, compression: int | None = None,
                  size: int | None = None) -> dict:
        for cap, val in ((self.CAP_COMPRESSION_LEVEL, compression),
                         (self.CAP_IMAGE_SIZE, size)):
            if val is None:
                continue
            if not self.src.has(cap, sdk.OP_SET):
                raise RuntimeError(
                    f"cap 0x{cap:04x} tělo nastavit nedovolí (režim voličem?)")
            self.mod.set_enum_value(self.src.obj, cap, val)
        return self.media_settings()

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
    if method == "set_lv_size":
        return server.set_lv_size(req["element"])
    if method == "lv_capabilities":
        return server.lv_capabilities()
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
    if method == "media_settings":
        return server.media_settings()
    if method == "set_media":
        return server.set_media(req.get("compression"), req.get("size"))
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
