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
