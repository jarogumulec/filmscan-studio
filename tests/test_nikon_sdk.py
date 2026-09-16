"""Offline tests for the Nikon SDK bindings — no camera, no x86_64 needed.

The real module is x86_64-only and needs a D750; what *is* testable on any
machine is everything layered around it: the cffi struct layout (the ABI
contract — one wrong offset means memory corruption on real hardware), SDK
path discovery, and the C trampoline logic, exercised by compiling the very
same ``_filmscan_trampolines.c`` for the test machine's arch and firing its
callbacks from Python exactly as the Nikon module's worker threads would.
"""

from __future__ import annotations

import ctypes
import io
import json
from pathlib import Path

import pytest

from filmscan_studio.capture import nikon_sdk as sdk

BUNDLE_LAYOUT = Path("Nikon_SDK/S-SDKD750-011BF-ALLIN/Module/Mac/Binary Files/binary15")


# ---------------------------------------------------------------------------
# ABI layout — LP64 (macOS): NKPARAM is u64 and pointers force 8-byte
# alignment where the C headers rely on padding. A mismatch here crashes the
# camera thread on real hardware, so pin every offset the bindings write
# through. (Cross-checked against the real headers by _Static_assert in CI of
# the plan; mirrored here in Python.)
# ---------------------------------------------------------------------------

class TestAbiLayout:
    def test_object_struct(self):
        assert sdk.ffi.sizeof("NkMAIDObject") == 24
        assert sdk.ffi.offsetof("NkMAIDObject", "ulID") == 4
        assert sdk.ffi.offsetof("NkMAIDObject", "refClient") == 8
        assert sdk.ffi.offsetof("NkMAIDObject", "refModule") == 16

    def test_capinfo_struct(self):
        assert sdk.ffi.sizeof("NkMAIDCapInfo") == 272
        assert sdk.ffi.offsetof("NkMAIDCapInfo", "szDescription") == 16

    def test_enum_struct(self):
        # 4x u32, then 2+2 bytes, then a u64 pointer needing 8-alignment —
        # this struct is memcpy'd field-by-field by every enum read.
        assert sdk.ffi.sizeof("NkMAIDEnum") == 32
        assert sdk.ffi.offsetof("NkMAIDEnum", "wPhysicalBytes") == 16
        assert sdk.ffi.offsetof("NkMAIDEnum", "pData") == 24

    def test_array_struct(self):
        assert sdk.ffi.sizeof("NkMAIDArray") == 32
        assert sdk.ffi.offsetof("NkMAIDArray", "pData") == 24

    def test_callback_struct(self):
        assert sdk.ffi.sizeof("NkMAIDCallback") == 16
        assert sdk.ffi.offsetof("NkMAIDCallback", "refProc") == 8

    def test_range_struct(self):
        assert sdk.ffi.sizeof("NkMAIDRange") == 48
        assert sdk.ffi.offsetof("NkMAIDRange", "lfLower") == 24


class TestCapabilityConstants:
    """Transcribed values — a typo here silently drives the *wrong property*
    on the camera instead of failing loudly, so pin them to the headers."""

    def test_generic_ids_from_maid3(self):
        assert sdk.CAP_EVENT_PROC == 3
        assert sdk.CAP_DATA_PROC == 4
        assert sdk.CAP_CAPTURE == 0x11
        assert sdk.CAP_ACQUIRE == 0x14

    def test_d1_live_view_ids(self):
        # VendorBaseD1 = 0x8100; offsets from Maid3d1.h.
        assert sdk.CAP_LIVE_VIEW_STATUS == 0x823E
        assert sdk.CAP_LIVE_VIEW_IMAGE_ZOOM_RATE == 0x823F
        assert sdk.CAP_GET_LIVE_VIEW_IMAGE == 0x8247
        assert sdk.CAP_MF_DRIVE_STEP == 0x8248
        assert sdk.CAP_MF_DRIVE == 0x8249
        assert sdk.CAP_LIVE_VIEW_IMAGE_SIZE == 0x8353

    def test_zoom_enum_ladder(self):
        assert (sdk.ZOOM_ALL, sdk.ZOOM_100, sdk.ZOOM_200) == (0, 5, 6)

    def test_capstart_accepts_release_busy_codes(self):
        # Maid3d1.h: Bulb=164, Silent=165, MovieFrame=166 are "still busy",
        # not errors; treating them as errors aborts long exposures.
        assert set((164, 165, 166)) <= set(sdk.CAPSTART_OK)


# ---------------------------------------------------------------------------
# SDK discovery
# ---------------------------------------------------------------------------

class TestSdkPaths:
    def test_finds_repo_layout(self, tmp_path, monkeypatch):
        src = tmp_path / BUNDLE_LAYOUT
        (src / "Type0015 Module.bundle").mkdir(parents=True)
        monkeypatch.delenv("FILMSCAN_NIKON_SDK", raising=False)
        assert sdk.find_sdk_source(src) == src

    def test_env_var_wins(self, tmp_path, monkeypatch):
        src = tmp_path / "sdk"
        (src / "Type0015 Module.bundle").mkdir(parents=True)
        monkeypatch.setenv("FILMSCAN_NIKON_SDK", str(src))
        assert sdk.find_sdk_source() == src

    def test_finds_real_repo_tree(self, monkeypatch):
        """This checkout ships Nikon_SDK/ — discovery must find it without
        any hint (the helper process relies on exactly this walk)."""
        monkeypatch.delenv("FILMSCAN_NIKON_SDK", raising=False)
        found = sdk.find_sdk_source()
        assert (found / "Type0015 Module.bundle").exists()

    def test_not_installed_reported_cleanly(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sdk, "SDK_INSTALL_DIR", tmp_path / "nowhere")
        assert not sdk.sdk_installed()


# ---------------------------------------------------------------------------
# Trampoline C logic, exercised natively: Python plays the role of the Nikon
# module and fires the stubs the way the module's worker threads do.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def tramp():
    dylib = sdk._trampoline_dylib(Path(__file__).parent / ".tmp_tramp",
                                  arch="native")
    return sdk._Tramp(dylib)


def _cfn(addr, restype, argtypes):
    proto = ctypes.CFUNCTYPE(restype, *argtypes)
    return proto(addr)


class TestEventTrampoline:
    def test_roundtrip_and_autoreset(self, tramp):
        fire = _cfn(tramp.event_addr(), None,
                    [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint64])
        tramp.ev_reset()
        assert tramp.read_event() is None
        fire(0x1234, 0x107, 0xDEADBEEF)          # AddPreviewImage
        assert tramp.read_event() == (0x1234, 0x107, 0xDEADBEEF)
        assert tramp.read_event() is None        # consumed by the read


class TestCompletionTrampoline:
    def test_negative_result_decodes(self, tramp):
        fire = _cfn(tramp.completion_addr(), None,
                    [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                     ctypes.c_uint32, ctypes.c_uint64, ctypes.c_void_p,
                     ctypes.c_int32])
        tramp.comp_reset()
        fire(None, 5, 0x8247, 0, 0, None, -127)  # CapStart -> NotSupported
        cmd, result = tramp.read_completion()
        assert (cmd, result) == (5, -127)


class TestDataAccumulation:
    """Nikon delivers a file as (ulStart, ulLength) chunks of an announced
    total; the C side must reassemble them whatever the arrival order."""

    TOTAL = 24
    # out of order on purpose: middle, head, tail
    CHUNKS = [(8, b"SECOND02"), (0, b"FIRST_01"), (16, b"THIRD__3")]

    def _fire_file_chunk(self, tramp, start, payload, total=None):
        class FileInfo(ctypes.Structure):
            _fields_ = [("ulType", ctypes.c_uint32),        # File|Image
                        ("ulFileDataType", ctypes.c_uint32),
                        ("ulTotalLength", ctypes.c_uint32),
                        ("ulStart", ctypes.c_uint32),
                        ("ulLength", ctypes.c_uint32),
                        ("fDiskFile", ctypes.c_char),
                        ("fRemoveObject", ctypes.c_char)]
        info = FileInfo(0x11, 1, total or self.TOTAL, start, len(payload), 0, 0)
        buf = ctypes.create_string_buffer(payload)
        fire = _cfn(tramp.data_addr(), ctypes.c_int32,
                    [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p])
        assert fire(None, ctypes.byref(info), buf) == 0

    def test_out_of_order_reassembly(self, tramp):
        tramp.data_reset()
        for start, payload in self.CHUNKS:
            self._fire_file_chunk(tramp, start, payload)
        assert tramp.data_fired()
        assert tramp.data_total() == self.TOTAL
        blob = tramp.take_blob()
        assert blob == b"FIRST_01SECOND02THIRD__3"
        assert tramp.data_have() == 0            # take_blob consumed it

    def test_reset_clears_state(self, tramp):
        self._fire_file_chunk(tramp, 0, b"ABCD", total=4)
        tramp.data_reset()
        assert tramp.data_have() == 0
        assert tramp.take_blob() == b""


# ---------------------------------------------------------------------------
# Probe CLI contract — no camera: only the surface, never the USB path.
# ---------------------------------------------------------------------------

class TestProbeCli:
    def test_fails_gracefully_without_camera_stack(self, tmp_path, monkeypatch):
        """Any host that cannot load the module must exit non-zero *and*
        write the JSON report — the probe reports, it never raises."""
        monkeypatch.setattr(sdk, "SDK_INSTALL_DIR", tmp_path / "not-installed")
        from filmscan_studio.capture.sdk_probe import main
        rc = main(["--out", str(tmp_path)])
        assert rc != 0
        report = (tmp_path / "sdk_probe_results.json").read_text()
        assert "machine" in report

    def test_main_rejects_unknown_module(self):
        """The module choices guard the CLI; sdk-probe must be accepted and
        a typo rejected, or the helper entry point is unusable."""
        import filmscan_studio.__main__ as m
        with pytest.raises(SystemExit) as exc:
            m.main(["bogus-module"])
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# Backend (arm64 side) — parsers and RPC framing against a fake helper.
# ---------------------------------------------------------------------------

class TestBackendParsers:
    def test_shutter_fractions_and_wholes(self):
        from filmscan_studio.capture.nikon_backend import parse_shutter
        assert parse_shutter("1/60") == pytest.approx(1 / 60)
        assert parse_shutter("2.5") == pytest.approx(2.5)
        assert parse_shutter("30") == pytest.approx(30)
        assert parse_shutter("1/1.3") == pytest.approx(1 / 1.3)
        assert parse_shutter("Bulb") is None
        assert parse_shutter("Time") is None

    def test_iso_numeric_only(self):
        """LO-/Hi- extended values are deliberately None — they leave the
        auto-exposure controller's sane range."""
        from filmscan_studio.capture.nikon_backend import parse_iso
        assert parse_iso("100") == 100
        assert parse_iso("12800") == 12800
        assert parse_iso("LO-1") is None
        assert parse_iso("Hi-2.0") is None


class TestBackendRpc:
    @pytest.fixture()
    def backend(self, monkeypatch):
        import subprocess
        from filmscan_studio.capture import nikon_backend as nb

        state = {"replies": {}, "requests": [],
                 "default": {"ok": True}}
        CONNECT_REPLY = {"ok": True,
                         "model": "D750", "camera_type": 59, "battery": 80,
                         "shutter": {"current_index": 1,
                                     "strings": ["30", "1/60", "1/125"]},
                         "iso": {"current_index": 0, "strings": ["100", "200"]},
                         "has_live_view": True,
                         "shutter_settable": True, "iso_settable": True}

        class FakePopen:
            class WritePipe:
                def __init__(self, owner): self.owner = owner
                def write(self, s): self.owner._pending = s
                def flush(self): pass

            class ReadPipe:
                def __init__(self, owner): self.owner = owner
                def readline(self):
                    import json as _json
                    req = _json.loads(self.owner._pending)
                    state["requests"].append(req)
                    reply = state["replies"].pop(req["method"],
                                                 state["default"])
                    return _json.dumps(reply) + "\n"

            def __init__(self, *a, **k):
                self._pending = None
                self.stdin = FakePopen.WritePipe(self)
                self.stdout = FakePopen.ReadPipe(self)

            def poll(self): return None
            def kill(self): pass
            def wait(self, timeout=None): return 0

        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        # connect() stat-checks the installed module bundle; offline there is
        # none — pretend the install scripts have run.
        monkeypatch.setattr(Path, "is_dir", lambda self: True)
        b = nb.NikonSdkBackend(helper_python=Path("/fake/python"))
        state["default"] = CONNECT_REPLY
        return b, state

    def test_connect_parses_choices(self, backend):
        b, state = backend
        info = b.connect()
        assert info.model == "D750"
        assert info.battery_percent == 80
        assert info.shutter_choices == (30.0, 1 / 60, 1 / 125)
        assert info.iso_choices == (100, 200)
        assert state["requests"][0]["method"] == "connect"

    def test_set_shutter_sends_picked_index(self, backend):
        b, state = backend
        b.connect()
        state["replies"]["set_enum"] = {"ok": True, "index": 2,
                                        "string": "1/125"}
        state["default"] = {"ok": True, "index": 2, "string": "1/125"}
        got = b.set_shutter(1 / 125)
        assert got == pytest.approx(1 / 125)
        req = state["requests"][-1]
        assert req["method"] == "set_enum" and req["which"] == "shutter"
        assert req["index"] == 2

    def test_next_live_frame_decodes_base64(self, backend):
        import base64
        b, state = backend
        b.connect()
        jpeg = b"\xff\xd8\xff" + b"x" * 100
        state["replies"]["lv_frame"] = {
            "ok": True, "jpeg_b64": base64.b64encode(jpeg).decode()}
        frame = b.next_live_frame()
        assert frame.jpeg == jpeg
        assert state["requests"][-1]["method"] == "lv_frame"

    def test_error_reply_becomes_camera_error(self, backend):
        from filmscan_studio.capture.camera import CameraError
        b, state = backend
        b.connect()
        state["replies"]["exposure_ev"] = {"ok": False, "error": "boom"}
        with pytest.raises(CameraError, match="boom"):
            b.exposure_ev()

    def test_concurrent_rpcs_never_interleave(self, backend):
        """The 2026-09 crash: the LiveView poller called lv_frame while a UI
        zoom/shutter write was mid-flight; both read the other's JSON line
        (json 'Extra data' / 'Expecting value'). With _rpc_lock held across
        write+reply each reply must answer its own request."""
        import threading as th

        b, state = backend
        b.connect()

        class Repeated:
            """The fixture's fake pops presets — this answers every call."""
            TABLE = {"lv_frame": {"ok": True, "jpeg_b64": "eHk="},
                     "set_zoom": {"ok": True, "zoom": 5}}

            @staticmethod
            def pop(key, default=None):
                return dict(Repeated.TABLE[key])

        state["replies"] = Repeated

        errors: list[Exception] = []
        start = th.Barrier(2)

        def poller():
            try:
                start.wait()
                for _ in range(30):
                    assert b.next_live_frame() is not None
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def setter():
            try:
                start.wait()
                for _ in range(30):
                    b.set_live_view_zoom(5)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [th.Thread(target=poller), th.Thread(target=setter)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not errors, errors
        # Each request line was answered exactly once (no stolen replies).
        kinds = [r["method"] for r in state["requests"]]
        assert kinds.count("lv_frame") == 30 and kinds.count("set_zoom") == 30

    def test_not_connected_raises(self):
        from filmscan_studio.capture.camera import NotConnectedError
        from filmscan_studio.capture.nikon_backend import NikonSdkBackend
        b = NikonSdkBackend()
        with pytest.raises(NotConnectedError):
            b.next_live_frame()

    def test_capture_sniffs_jpeg_masked_as_nef(self, backend, tmp_path):
        """Compression Level != RAW makes the body deliver a JPEG at the .NEF
        path (measured 2026-09: JPEG Basic, S size, ~1 MB). The helper must
        report the truth so the backend renames and the sidecar stops lying;
        LibRaw reading that file is the b'Input/output error' from the log."""
        import cv2
        import numpy as np
        from filmscan_studio.capture import sdk_server

        jpeg = cv2.imencode(".jpg", np.zeros((8, 8, 3), np.uint8))[1].tobytes()
        assert jpeg[:3] == sdk_server.JPEG_MAGIC

        class FakeSrc:
            def capture_still(self, target):
                Path(target).write_bytes(jpeg)

        server = sdk_server.Server()
        server.src = FakeSrc()
        reply = server.capture(str(tmp_path), "frame001")
        assert reply["file_format"] == "jpeg"
        assert reply["path"].endswith("frame001.NEF")

        class FakeNefSrc(FakeSrc):
            def capture_still(self, target):
                Path(target).write_bytes(b"II*\x00" + b"\x00" * 64)   # TIFF magic

        server.src = FakeNefSrc()
        assert server.capture(str(tmp_path), "frame002")["file_format"] == "nef"

    def test_backend_renames_jpeg_to_jpg(self, backend, tmp_path):
        b, state = backend
        b.connect()
        state["replies"]["capture"] = {
            "ok": True, "path": str(tmp_path / "frame001.NEF"),
            "bytes": 1024, "elapsed": 1.0, "file_format": "jpeg"}
        (tmp_path / "frame001.NEF").write_bytes(b"\xff\xd8\xff\xe0fake")
        result = b.capture(tmp_path, "frame001")
        assert result.file_format == "jpeg"
        assert result.path.suffix == ".jpg"
        assert not (tmp_path / "frame001.NEF").exists()
        assert (tmp_path / "frame001.jpg").read_bytes().startswith(b"\xff\xd8")

    def test_missing_helper_reports_install_hint(self, monkeypatch):
        from filmscan_studio.capture.camera import CameraError
        from filmscan_studio.capture import nikon_backend as nb
        monkeypatch.setattr(nb, "_helper_python", lambda: None)
        b = nb.NikonSdkBackend()
        with pytest.raises(CameraError, match="install_helper"):
            b.connect()

    def test_crashed_helper_fails_fast_and_names_the_reason(self, monkeypatch):
        """The 2026-09 regression: sdk_server imported numpy, died at import
        inside the stdlib-only .venv-x86, and the GUI sat on the readline for
        the full 30 s RPC timeout, reporting an opaque 'helper neodpověděl'.
        A dead helper must raise immediately, quoting its stderr."""
        import subprocess
        from filmscan_studio.capture import nikon_backend as nb
        from filmscan_studio.capture.camera import CameraError

        class DeadHelper:
            class Pipe:
                @staticmethod
                def write(s): pass
                @staticmethod
                def flush(): pass
                @staticmethod
                def readline(): return ""      # EOF — process is gone

            def __init__(self, *a, **k):
                self.stdin = DeadHelper.Pipe()
                self.stdout = DeadHelper.Pipe()
                self._stderr = io.StringIO(
                    "ModuleNotFoundError: No module named 'numpy'\n")

            def poll(self): return 1           # exited
            def kill(self): pass
            def wait(self, timeout=None): return 1

        monkeypatch.setattr(subprocess, "Popen", DeadHelper)
        b = nb.NikonSdkBackend(helper_python=Path("/fake/python"),
                               rpc_timeout=30.0)
        with pytest.raises(CameraError, match="spadl"):
            b.connect()


class TestHelperImports:
    """The x86_64 helper venv holds stdlib + cffi only. If sdk_server ever
    pulls numpy/Qt/gphoto2 back into its import graph, the helper dies at
    startup and the GUI silently degrades to gphoto2 — catch it offline."""

    def test_sdk_server_imports_only_stdlib_and_cffi(self):
        import subprocess as sp
        import filmscan_studio.capture.nikon_backend as nb
        helper = nb._helper_python()
        if helper is None:
            pytest.skip("Rosetta helper venv not installed")
        src = ("import sys, json, filmscan_studio.capture.sdk_server;"
               "mods = {m.split('.')[0] for m in sys.modules};"
               "print(json.dumps(sorted(mods - sys.stdlib_module_names)))")
        out = sp.run([str(helper), "-c", src], capture_output=True, text=True,
                     env={"PYTHONPATH": str(Path(nb.__file__).parent.parent.parent),
                          "PATH": "/usr/bin:/bin"})
        assert out.returncode == 0, out.stderr
        # The helper venv is stdlib + cffi (+ its pycparser dep) — nothing else.
        # __main__ = the -c script; _virtualenv = venv bootstrap .pth.
        allowed = {"filmscan_studio", "cffi", "pycparser", "_cffi_backend",
                   "__main__", "_virtualenv"}
        extra = set(json.loads(out.stdout)) - allowed
        assert not extra, f"sdk_server pulls in what .venv-x86 lacks: {extra}"
