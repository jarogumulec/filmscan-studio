"""Nikon MAID3 SDK bindings (cffi ABI, no compiler) — x86_64 helper process only.

Binds Nikon's official ``Type0015 Module`` (S-SDKD750) for the D750. The reason
it exists next to the working gphoto2 backend: PTP ``capturePreview`` always
delivers the *whole* downscaled frame, while the module exposes
``LiveViewStatus`` / ``LiveViewImageZoomRate`` / ``GetLiveViewImage`` as first
class capabilities — the body itself sends a zoomed (grain-level) crop, and MF
drive / contrast AF exist for electronic lenses.

Three constraints shape this file:

* **x86_64-only binary.** ``libNkPTPDriver2.dylib`` is a thin Mach-O; the GUI
  (arm64) cannot load it. Everything here runs under Rosetta 2 inside the
  helper venv — and must stay import-light (stdlib + cffi only, no Qt).
* **Absolute-path driver linkage.** The bundle links
  ``/Library/Application Support/Nikon/Camera Control Modules/libNkPTPDriver2``
  ``.dylib`` by install name, so the three binaries must be rsynced there
  once (``scripts/install_sdk.sh``). Verified: ``Type0015 Module`` exports
  ``_MAIDEntryPoint`` directly, so plain ``dlopen``/``dlsym`` replaces the
  sample's CoreFoundation ``CFBundleGetFunctionPointerForName``.
* **Callbacks cannot be Python.** The module invokes callbacks from its own
  worker threads; re-entering libffi there deadlocks against Python's own
  calls into the module. ``_filmscan_trampolines.c`` provides tiny C stubs
  that only record arguments (and accumulate data deliveries) into static
  buffers, which Python polls *between* module calls.

The command/callback protocol itself mirrors ``Type0015_CtrlSample_Mac``:
everything is synchronous — pass ``pfnComplete = NULL``, call
``kNkMAIDCommand_Async`` in a loop until state settles (sample ``IdleLoop``).
"""

from __future__ import annotations

import ctypes
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

import cffi

# ---------------------------------------------------------------------------
# cffi ABI declarations — structs from Maid3.h / Maid3d1.h. macOS LP64:
# ULONG=u32 SLONG=i32 NKPARAM=u64 NKREF=void* BOOL=char DOUB_P=double.
# Only what the helper actually touches is declared.
# ---------------------------------------------------------------------------

_CDEF = r"""
typedef uint32_t ULONG;
typedef int32_t  SLONG;
typedef uint64_t NKPARAM;
typedef void*    NKREF;
typedef void*    LPNKFUNC;

typedef struct {
    LPNKFUNC pProc;
    NKREF    refProc;
} NkMAIDCallback;

typedef struct {
    ULONG  ulType;      /* eNkMAIDObjectType */
    ULONG  ulID;
    NKREF  refClient;
    NKREF  refModule;
} NkMAIDObject;

typedef struct {
    ULONG ulID;
    ULONG ulType;         /* eNkMAIDCapType */
    ULONG ulVisibility;
    ULONG ulOperations;   /* eNkMAIDCapOperations bits */
    char  szDescription[256];
} NkMAIDCapInfo;

typedef struct {
    ULONG   ulType;           /* eNkMAIDArrayType */
    ULONG   ulElements;
    ULONG   ulValue;
    ULONG   ulDefault;
    int16_t wPhysicalBytes;
    char    _pad[2];
    void*   pData;            /* caller-allocated before CapGetArray */
} NkMAIDEnum;

typedef struct {
    ULONG    ulType;
    ULONG    ulElements;
    ULONG    ulDimSize1;
    ULONG    ulDimSize2;
    ULONG    ulDimSize3;
    uint16_t wPhysicalBytes;
    uint16_t wLogicalBits;
    void*    pData;           /* caller-allocated before CapGetArray */
} NkMAIDArray;

typedef struct {
    double lfValue;
    double lfDefault;
    ULONG  ulValueIndex;
    ULONG  ulDefaultIndex;
    double lfLower;
    double lfUpper;
    ULONG  ulSteps;
} NkMAIDRange;

typedef struct { SLONG x; SLONG y; } NkMAIDPoint;
typedef struct { ULONG w; ULONG h; } NkMAIDSize;
typedef struct { SLONG x; SLONG y; ULONG w; ULONG h; } NkMAIDRect;
"""

ffi = cffi.FFI()
ffi.cdef(_CDEF)

# ---------------------------------------------------------------------------
# Constants — values transcribed from Maid3.h / Maid3d1.h.
# ---------------------------------------------------------------------------

# eNkMAIDCommand
CMD_ASYNC = 0
CMD_OPEN = 1
CMD_CLOSE = 2
CMD_GET_CAP_COUNT = 3
CMD_GET_CAP_INFO = 4
CMD_CAP_START = 5
CMD_CAP_SET = 6
CMD_CAP_GET = 7
CMD_CAP_GET_DEFAULT = 8
CMD_CAP_GET_ARRAY = 9
CMD_MARK = 10
CMD_ABORT_TO_MARK = 11
CMD_ABORT = 12
CMD_ENUM_CHILDREN = 13
CMD_GET_PARENT = 14

# eNkMAIDDataType
DT_NULL = 0
DT_BOOLEAN = 1
DT_INTEGER = 2
DT_UNSIGNED = 3
DT_INTEGER_PTR = 5
DT_UNSIGNED_PTR = 6
DT_FLOAT_PTR = 7
DT_POINT_PTR = 8
DT_STRING_PTR = 11
DT_CALLBACK_PTR = 13
DT_RANGE_PTR = 14
DT_ARRAY_PTR = 15
DT_ENUM_PTR = 16
DT_OBJECT_PTR = 17
DT_CAPINFO_PTR = 18
DT_GENERIC_PTR = 19

# eNkMAIDObjectType
OBJ_MODULE = 1
OBJ_SOURCE = 2
OBJ_ITEM = 3
OBJ_DATAOBJ = 4

# eNkMAIDCapType
CAPT_PROCESS = 0
CAPT_UNSIGNED = 3
CAPT_STRING = 8
CAPT_ARRAY = 11
CAPT_ENUM = 12
CAPT_RANGE = 13

# eNkMAIDCapOperations
OP_START = 0x0001
OP_GET = 0x0002
OP_SET = 0x0004
OP_GET_ARRAY = 0x0008
OP_GET_DEFAULT = 0x0010

# eNkMAIDCapVisibility
VIS_INVALID = 0x20

# eNkMAIDResult — success codes for CapStart per sample Command_CapStart:
# NoError, Pending, and the three ReleaseBusy codes (Bulb=164, Silent=165,
# MovieFrame=166 from Maid3d1.h).
R_OK = 0
R_PENDING = 1
R_BUFFER_SIZE = -124   # count raced between GetCapCount/GetCapInfo — retry
# Vendor result codes (Maid3d1.h eNkMAIDResult + VendorBaseD1):
R_DEVICE_BUSY = 152    # body still owns the PTP session — wait and retry
R_CPX_PLAYBACK = 158   # camera sits in playback mode
R_NOT_LIVE_VIEW = 159  # LV frame requested but mirror already dropped
R_MF_DRIVE_END = 160   # MfDrive hit the focus-ring end stop
CAPSTART_OK = (0, 1, 164, 165, 166)
#: Codes meaning "the camera is momentarily busy", worth retrying (observed
#: on the D750 right after USB attach: every CapSet answers 152 for a while).
RETRYABLE = (R_DEVICE_BUSY,)

#: eNkMAIDLiveViewProhibit bit meanings (Maid3d1.h), most significant first.
LV_PROHIBIT_NAMES = {
    0x80000000: "ExpModeScene", 0x4000000: "RecordingImage",
    0x1000000: "Retractable (zoom ring extended)", 0x400000: "DuringMirrorup",
    0x200000: "BulbWarning", 0x100000: "CardUnformat", 0x80000: "CardError",
    0x40000: "CardProtect", 0x20000: "TempRise", 0x10000: "EffectMode",
    0x8000: "Capture", 0x4000: "NoCardLock", 0x2000: "MirrorMode",
    0x1000: "SdramImg", 0x800: "NonCPU lens", 0x400: "ApertureRing",
    0x200: "TTL", 0x100: "Battery", 0x80: "Mirrorup", 0x40: "Bulb",
    0x20: "FEE", 0x10: "Button", 0x4: "Sequence", 0x1: "CF",
}


def lv_prohibit_reasons(bits: int) -> list[str]:
    return [name for bit, name in LV_PROHIBIT_NAMES.items() if bits & bit]

# eNkMAIDEvent
EV_ADD_CHILD = 0
EV_REMOVE_CHILD = 1
EV_CAP_CHANGE = 4
EV_CAP_CHANGE_VALUE_ONLY = 6
EV_ADD_PREVIEW_IMAGE = 0x107          # D1Origin + 1
EV_CAPTURE_COMPLETE = 0x108           # D1Origin + 2

# Generic object capabilities (Maid3.h enum eNkMAIDCapability)
CAP_PROGRESS_PROC = 0x02
CAP_EVENT_PROC = 0x03
CAP_DATA_PROC = 0x04
CAP_UI_REQUEST_PROC = 0x05
CAP_IS_ALIVE = 0x06
CAP_CHILDREN = 0x07
CAP_NAME = 0x09
CAP_DESCRIPTION = 0x0A
CAP_CAPTURE = 0x11
CAP_ACQUIRE = 0x14
CAP_FIRMWARE = 0x2E
CAP_BATTERY_LEVEL = 0x31

# Vendor D1 (Maid3d1.h). D1 base = 0x8000 + 0x100 = 0x8100.
CAP_MODULE_MODE = 0x8101
CAP_PRE_CAPTURE = 0x8104
CAP_LOCK_FOCUS = 0x8105
CAP_LOCK_EXPOSURE = 0x8106
CAP_FILE_TYPE = 0x810F
CAP_COMPRESSION_LEVEL = 0x8110
CAP_EXPOSURE_MODE = 0x8111
CAP_SHUTTER_SPEED = 0x8112
CAP_APERTURE = 0x8113
CAP_EXPOSURE_COMP = 0x8115
CAP_METERING_MODE = 0x8116
CAP_SENSITIVITY = 0x8117
CAP_IMAGE_SIZE = 0x8157
CAP_EXPOSURE_STATUS = 0x810B          # Array, eNkMAIDExposureStatus per element
CAP_SHOOTING_MODE = 0x818D            # Enum; Live View needs P/S/A/M
CAP_CAMERA_TYPE = 0x81D7
CAP_LIVE_VIEW_MODE = 0x823C
CAP_LIVE_VIEW_DRIVE_MODE = 0x823D
CAP_LIVE_VIEW_STATUS = 0x823E
CAP_LIVE_VIEW_IMAGE_ZOOM_RATE = 0x823F
CAP_CONTRAST_AF = 0x8240
CAP_DELETE_DRAM_IMAGE = 0x8243
CAP_GET_PREVIEW_IMAGE_LOW = 0x8245
CAP_GET_PREVIEW_IMAGE_NORMAL = 0x8246
CAP_GET_LIVE_VIEW_IMAGE = 0x8247
CAP_MF_DRIVE_STEP = 0x8248
CAP_MF_DRIVE = 0x8249
CAP_CONTRAST_AF_AREA = 0x824A
CAP_LIVE_VIEW_PROHIBIT = 0x825E
CAP_AF_AREA_POINT = 0x8254
CAP_CENTER_BUTTON_ON_LIVE_VIEW = 0x8280
CAP_ZOOM_RATE_ON_LIVE_VIEW = 0x8281
CAP_LIVE_VIEW_AF = 0x8275
CAP_TERMINATE_CAPTURE = 0x8318        # verified against header + live D750
# 0x8328 is WBPreset Protect3 in Maid3d1.h — an earlier draft labelled it
# "silent capture"; it is *not* and must not be used as one (never verified on
# the D750, and the probe capability list does not offer it).
CAP_LIVE_VIEW_EXPOSURE_PREVIEW = 0x8333
CAP_LIVE_VIEW_SELECTOR = 0x8334
CAP_LIVE_VIEW_IMAGE_SIZE = 0x8353     # 0x8100 + 0x253

# eNkMAIDLiveViewImageZoomRate
ZOOM_ALL, ZOOM_25, ZOOM_33, ZOOM_50, ZOOM_66, ZOOM_100, ZOOM_200 = range(7)

# eNkMAIDModuleMode
MODULE_MODE_CONTROLLER = 1

# eNkMAIDDataObjType
DO_IMAGE = 0x01
DO_THUMBNAIL = 0x08
DO_FILE = 0x10

#: Nikon's PTP driver must live here — the bundle links it by absolute
#: install name (otool: /Library/Application Support/Nikon/Camera Control
#: Modules/libNkPTPDriver2.dylib).
SDK_INSTALL_DIR = Path("/Library/Application Support/Nikon/Camera Control Modules")
_BUNDLE = "Type0015 Module.bundle"
_DRIVER = "libNkPTPDriver2.dylib"
_FRAMEWORK = "Royalmile.framework"

_TRAMPOLINE_SRC = Path(__file__).with_name("_filmscan_trampolines.c")


class MaidError(RuntimeError):
    """A MAID command returned a non-success result code."""

    def __init__(self, what: str, code: int):
        super().__init__(f"{what}: MAID result {code}")
        self.what = what
        self.code = code


# ---------------------------------------------------------------------------
# SDK location / installation
# ---------------------------------------------------------------------------

def find_sdk_source(root: Path | None = None) -> Path:
    """Locate the ``binary15`` folder of the extracted S-SDKD750 archive.

    Order: explicit *root*, ``FILMSCAN_NIKON_SDK`` env, then ``Nikon_SDK/`` in
    any ancestor of this file (repo layout).
    """
    candidates: list[Path] = []
    if root is not None:
        candidates.append(Path(root))
    env = os.environ.get("FILMSCAN_NIKON_SDK")
    if env:
        candidates.append(Path(env))
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidates.append(parent / "Nikon_SDK" / "S-SDKD750-011BF-ALLIN"
                          / "Module" / "Mac" / "Binary Files" / "binary15")
    for cand in candidates:
        if (cand / _BUNDLE).exists():
            return cand
    raise FileNotFoundError(
        "Nikon SDK binaries not found. Looked in: "
        + ", ".join(str(c) for c in candidates)
        + ". Set FILMSCAN_NIKON_SDK to the binary15 directory."
    )


def sdk_installed() -> bool:
    return ((SDK_INSTALL_DIR / _BUNDLE).is_dir()
            and (SDK_INSTALL_DIR / _DRIVER).exists()
            and (SDK_INSTALL_DIR / _FRAMEWORK).is_dir())


def install_sdk(source: Path | None = None) -> None:
    """Rsync bundle/dylib/framework to SDK_INSTALL_DIR (needs sudo once)."""
    src = find_sdk_source(source)
    dest = SDK_INSTALL_DIR
    script = (
        f"mkdir -p '{dest}' && "
        f"rsync -a --delete '{src/_BUNDLE}/' '{dest/_BUNDLE}/' && "
        f"rsync -a '{src/_DRIVER}' '{dest}/' && "
        f"rsync -a --delete '{src/_FRAMEWORK}/' '{dest/_FRAMEWORK}/'"
    )
    proc = subprocess.run(["sudo", "bash", "-c", script],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"sdk install failed: {proc.stderr.strip()}")
    if not sdk_installed():
        raise RuntimeError("sdk install did not produce expected layout")


# ---------------------------------------------------------------------------
# Trampoline dylib (C stubs for callbacks) — compiled on first use.
# ---------------------------------------------------------------------------

def _trampoline_dylib(workdir: Path, arch: str = "x86_64") -> Path:
    """Compile (cached) the C stubs for *arch*.

    The helper always asks for x86_64 (matching the Nikon binary); the test
    suite builds the native arch to exercise the C logic without a camera —
    the arch is part of the filename so the two caches never collide.
    """
    import platform
    if arch == "native":
        arch = platform.machine()
    dylib = workdir / f"libfilmscan_trampolines_{arch}.dylib"
    try:
        fresh = dylib.exists() and (
            dylib.stat().st_mtime >= _TRAMPOLINE_SRC.stat().st_mtime)
    except OSError:
        fresh = False
    if fresh:
        return dylib
    workdir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["cc", "-arch", arch, "-mmacosx-version-min=11.0",
         "-dynamiclib", "-Wno-deprecated-declarations",
         "-framework", "CoreFoundation",
         "-o", str(dylib), str(_TRAMPOLINE_SRC)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"trampoline compile failed (xcrun cc):\n{proc.stderr}")
    return dylib


_V = ctypes.c_void_p
_I = ctypes.c_int
_U64 = ctypes.c_uint64


class _Tramp:
    """ctypes view onto the C stubs + the cffi bridge pointers they record."""

    def __init__(self, dylib_path: Path):
        self.lib = ctypes.CDLL(str(dylib_path))
        def f(name, restype, argtypes):
            fn = getattr(self.lib, name)
            fn.restype, fn.argtypes = restype, argtypes
            return fn
        self.set_entry = f("fsk_set_entry", None, [_U64])
        self.call = f("fsk_call", ctypes.c_int32,
                      [_U64, ctypes.c_uint32, ctypes.c_uint32,
                       ctypes.c_uint32, _U64, _U64, _U64])
        self.event_addr = f("fsk_event_trampoline_addr", _V, [])
        self.ui_addr = f("fsk_ui_trampoline_addr", _V, [])
        self.completion_addr = f("fsk_completion_trampoline_addr", _V, [])
        self.data_addr = f("fsk_data_trampoline_addr", _V, [])
        self.ev_fired = f("fsk_event_fired", _I, [])
        self.ev_reset = f("fsk_event_reset", None, [])
        self.ev_data = f("fsk_event_data_ptr", _V, [])
        self.comp_fired = f("fsk_completion_fired", _I, [])
        self.comp_reset = f("fsk_completion_reset", None, [])
        self.comp_data = f("fsk_completion_data_ptr", _V, [])
        self.data_fired = f("fsk_data_fired", _I, [])
        self.data_reset = f("fsk_data_reset", None, [])
        self.data_have = f("fsk_data_have", _U64, [])
        self.data_total = f("fsk_data_total", _U64, [])
        self.data_kind = f("fsk_data_kind", ctypes.c_uint32, [])
        self.data_copy = f("fsk_data_copy", _I, [ctypes.c_char_p, _I])
        self.runloop_tick = f("fsk_runloop_tick", None, [ctypes.c_double])

    def read_event(self) -> tuple[int, int, int] | None:
        if not self.ev_fired():
            return None
        p = self.ev_data()
        rc, ev, data = struct.unpack("<qqq", bytes(ctypes.string_at(p, 24)))
        self.ev_reset()
        return rc, ev, data

    def read_completion(self) -> tuple[int, int] | None:
        """(cmd, result) of the newest completion, or None."""
        if not self.comp_fired():
            return None
        p = self.comp_data()
        w0, w1 = struct.unpack("<QQ", bytes(ctypes.string_at(p, 16)))
        cmd, result = w0 >> 32, int(w1 & 0xFFFFFFFF)
        if result >= 0x80000000:            # int32 negative result codes
            result -= 0x100000000
        self.comp_reset()
        return cmd, result

    def take_blob(self) -> bytes:
        have = self.data_have()
        if not have:
            return b""
        total = self.data_total()
        n = total if 0 < total <= have else have
        buf = ctypes.create_string_buffer(n)
        got = self.data_copy(buf, n)
        self.data_reset()
        return buf.raw[:got]


def _require_x86() -> None:
    import platform
    if platform.machine() != "x86_64":
        raise RuntimeError(
            f"Nikon SDK is x86_64-only; this interpreter is "
            f"{platform.machine()}. Run under .venv-x86 (scripts/install_helper.sh).")


# ---------------------------------------------------------------------------
# Module / object handles
# ---------------------------------------------------------------------------

class MaidModule:
    """An open ``Type0015 Module`` session with a synchronous command loop."""

    def __init__(self, sdk_dir: Path | None = None):
        _require_x86()
        sdk_dir = Path(sdk_dir) if sdk_dir else SDK_INSTALL_DIR
        bundle_bin = (sdk_dir / _BUNDLE / "Contents" / "MacOS" / "Type0015 Module")
        if not bundle_bin.exists():
            raise FileNotFoundError(
                f"{bundle_bin} missing — run scripts/install_sdk.sh")
        # RTLD_GLOBAL: the bundle's sibling libs load by absolute install
        # name; global scope matches what CFBundle provided in the sample.
        self._bundle = ctypes.CDLL(str(bundle_bin), mode=ctypes.RTLD_GLOBAL)
        entry = ctypes.cast(self._bundle.MAIDEntryPoint, _V).value or 0
        self.tramp = _Tramp(_trampoline_dylib(
            sdk_dir if os.access(sdk_dir, os.W_OK)
            else Path.home() / ".cache" / "filmscan"))
        self.tramp.set_entry(entry)
        self._keepalive: list = []
        #: Currently open child objects, innermost last. The module segfaults
        #: on Module-Close while children are still open, so close() unwinds
        #: this stack first (sample RemoveChild order).
        self._open_stack: list = []
        self.module_obj = self._new_object(OBJ_MODULE, 1)
        # Module object: parent is NULL, child pointer travels in `data`
        # (sample Command_Open — same protocol as any child).
        r = self.command(None, CMD_OPEN, param=1,
                         dtype=DT_OBJECT_PTR, data=self._ptr(self.module_obj))
        if r != R_OK:
            raise MaidError("Open(Module)", r)
        self._module_caps = self.cap_infos(self.module_obj)
        # Controller mode enables capture commands (sample main.cpp).
        if self._has_cap(self.module_obj, CAP_MODULE_MODE, OP_SET):
            self.command(self.module_obj, CMD_CAP_SET, CAP_MODULE_MODE,
                         DT_UNSIGNED, 1)
        self._install_callbacks(self.module_obj, self._module_caps)

    # ---------------------------------------------------------------- plumbing

    def _new_object(self, obj_type: int, obj_id: int):
        obj = ffi.new("NkMAIDObject *")
        obj.ulType = obj_type
        obj.ulID = obj_id
        self._keepalive.append(obj)      # module stores the pointer on Open
        return obj

    def _ptr(self, x) -> int:
        if x is None or x == ffi.NULL:
            return 0
        return int(ffi.cast("NKPARAM", ffi.cast("void *", x)))

    def command(self, obj, cmd: int, param: int = 0, dtype: int = DT_NULL,
                data: int = 0, done=0, ref: int = 0) -> int:
        """One entry-point call. done=None keeps a completion trampoline set."""
        if done is None:
            self.tramp.comp_reset()
            done = self.tramp.completion_addr()
        return self.tramp.call(self._ptr(obj), cmd, param, dtype, data,
                               done if done else 0, ref)

    def pump(self, obj, timeout: float = 2.0, tick: float = 0.005) -> None:
        """Pump ``Async`` on *obj* until it stops changing things (IdleLoop)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.command(obj, CMD_ASYNC)
            time.sleep(tick)

    def wait_for(self, obj, predicate, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            self.command(obj, CMD_ASYNC)
            time.sleep(0.005)
        return predicate()

    def _install_callbacks(self, obj, caps: list[dict]) -> None:
        for cap in caps:
            if not cap["ops"] & OP_SET:
                continue
            if cap["id"] == CAP_EVENT_PROC:
                self._set_callback(obj, CAP_EVENT_PROC, self.tramp.event_addr())
            elif cap["id"] == CAP_UI_REQUEST_PROC:
                self._set_callback(obj, CAP_UI_REQUEST_PROC, self.tramp.ui_addr())

    def _set_callback(self, obj, cap_id: int, fn_addr: int) -> None:
        cb = ffi.new("NkMAIDCallback *")
        cb.pProc = ffi.cast("LPNKFUNC", fn_addr)
        cb.refProc = ffi.NULL
        self._keepalive.append(cb)
        r = self.command(obj, CMD_CAP_SET, cap_id, DT_CALLBACK_PTR, self._ptr(cb))
        if r != R_OK:
            raise MaidError(f"set callback cap 0x{cap_id:x}", r)

    def _has_cap(self, obj, cap_id: int, op: int = 0) -> bool:
        try:
            caps = self.cap_infos(obj)
        except MaidError:
            return False
        c = next((c for c in caps if c["id"] == cap_id), None)
        return c is not None and not (c["visibility"] & VIS_INVALID) and (
            not op or c["ops"] & op)

    # ------------------------------------------------------------- structure

    def _sync_command(self, obj, cmd: int, param: int = 0,
                      dtype: int = DT_NULL, data: int = 0,
                      timeout: float = 5.0) -> int:
        """Issue *cmd*, pump Async until the completion trampoline carries the
        result (sample IdleLoop/CompletionProc pattern). Returns the result;
        BufferSize (−124) passes through so callers can retry per spec."""
        self.tramp.comp_reset()
        r = self.command(obj, cmd, param, dtype, data, done=None)
        if r not in CAPSTART_OK:
            raise MaidError(f"command {cmd}", r)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.command(obj, CMD_ASYNC)
            c = self.tramp.read_completion()
            if c is not None:
                _, result = c
                if result not in CAPSTART_OK and result != R_BUFFER_SIZE:
                    raise MaidError(f"command {cmd} completion", result)
                return result
            time.sleep(0.002)
        raise MaidError(f"command {cmd}: timeout", -1)

    def cap_infos(self, obj) -> list[dict]:
        """Capability table — per sample EnumCapabilities: GetCapCount then
        GetCapInfo(param = count); BufferSize means the count raced, so redo
        the whole pair."""
        while True:
            count_p = ffi.new("ULONG *")
            self._sync_command(obj, CMD_GET_CAP_COUNT, dtype=DT_UNSIGNED_PTR,
                               data=self._ptr(count_p))
            count = int(count_p[0])
            if count == 0:
                return []
            infos = ffi.new(f"NkMAIDCapInfo[{count}]")
            r = self._sync_command(obj, CMD_GET_CAP_INFO, param=count,
                                   dtype=DT_CAPINFO_PTR, data=self._ptr(infos))
            if r != R_BUFFER_SIZE:
                break
        return [{
            "id": infos[i].ulID,
            "type": infos[i].ulType,
            "visibility": infos[i].ulVisibility,
            "ops": infos[i].ulOperations,
            "name": ffi.string(infos[i].szDescription).decode(errors="replace"),
        } for i in range(count)]

    def open_child(self, parent, child_id: int, obj_type: int):
        """Open a child. Sample Command_Open: param = child ID, data =
        pointer to the *caller-allocated* child NkMAIDObject (the module
        fills refModule), dtype = ObjectPtr."""
        obj = self._new_object(obj_type, child_id)
        r = self.command(parent, CMD_OPEN, param=child_id,
                         dtype=DT_OBJECT_PTR, data=self._ptr(obj))
        if r != R_OK:
            raise MaidError(f"Open(child 0x{child_id:x})", r)
        # Opened children get their own event callback (sample SetProc).
        caps = self.cap_infos(obj)
        if any(c["id"] == CAP_EVENT_PROC and c["ops"] & OP_SET for c in caps):
            self._set_callback(obj, CAP_EVENT_PROC, self.tramp.event_addr())
        self._open_stack.append(obj)
        return obj

    def close_object(self, obj) -> None:
        self.command(obj, CMD_CLOSE)
        try:
            self._open_stack.remove(obj)
        except ValueError:
            pass

    def enum_children(self, obj, timeout: float = 5.0) -> list[int]:
        """Send EnumChildren and harvest *unique* AddChild ids from the pump.

        The module re-announces a child on every Async pump until the client
        opens it (the sample's ModEventProc dedupes against its child list
        the same way), so only first sightings count and only they reset the
        quiescence timer."""
        self.tramp.ev_reset()
        r = self.command(obj, CMD_ENUM_CHILDREN)
        if r != R_OK:
            raise MaidError("EnumChildren", r)
        seen: list[int] = []
        known: set[int] = set()
        deadline = time.monotonic() + timeout
        last_new = time.monotonic()
        # Quiescence rule: keep pumping while *new* children still appear;
        # stop 0.5 s after the last one or at the hard timeout.
        while time.monotonic() < deadline:
            ev = self.tramp.read_event()
            if ev is not None:
                _, event, data = ev
                if event == EV_ADD_CHILD:
                    child_id = data & 0xFFFFFFFF
                    if child_id not in known:
                        known.add(child_id)
                        seen.append(child_id)
                        last_new = time.monotonic()
            elif time.monotonic() - last_new >= 0.5:
                break
            self.command(obj, CMD_ASYNC)
            # Device discovery goes through ImageCaptureCore; its callbacks
            # (and the AddChild events they trigger) only run when the main
            # run loop is serviced.
            self.tramp.runloop_tick(0.005)
        return seen

    # ------------------------------------------------------------- properties

    def get_unsigned(self, obj, cap_id: int) -> int:
        out = ffi.new("ULONG *")
        r = self.command(obj, CMD_CAP_GET, cap_id, DT_UNSIGNED_PTR, self._ptr(out))
        if r != R_OK:
            raise MaidError(f"CapGet 0x{cap_id:x}", r)
        return int(out[0])

    def get_integer(self, obj, cap_id: int) -> int:
        """Signed 32-bit CapGet (IntegerPtr, eNkMAIDDataType 5) — BatteryLevel
        etc. (UnsignedPtr answers -126 for these)."""
        out = ffi.new("int32_t *")
        r = self.command(obj, CMD_CAP_GET, cap_id, DT_INTEGER_PTR, self._ptr(out))
        if r != R_OK:
            raise MaidError(f"CapGet int 0x{cap_id:x}", r)
        return int(out[0])

    def set_unsigned(self, obj, cap_id: int, value: int,
                     retries: int = 0, retry_wait: float = 1.0) -> None:
        for attempt in range(retries + 1):
            r = self.command(obj, CMD_CAP_SET, cap_id, DT_UNSIGNED, value)
            if r == R_OK:
                return
            if r in RETRYABLE and attempt < retries:
                # e.g. 152 DeviceBusy right after USB attach / LV teardown.
                self.pump(obj, timeout=retry_wait)
                continue
            raise MaidError(f"CapSet 0x{cap_id:x}={value}", r)

    def get_array_retry(self, obj, cap_id: int, retries: int = 3,
                        retry_wait: float = 0.25) -> bytes:
        """get_array, retrying transient 'camera still busy' results —
        GetLiveViewImage answers 159/152 while the mirror has not dropped yet
        or the previous frame is still in flight."""
        for attempt in range(retries + 1):
            try:
                return self.get_array(obj, cap_id)
            except MaidError as exc:
                if exc.code in (R_NOT_LIVE_VIEW, R_DEVICE_BUSY, R_CPX_PLAYBACK) \
                        and attempt < retries:
                    self.pump(obj, timeout=retry_wait)
                    continue
                raise
        raise AssertionError  # unreachable

    def get_enum_values(self, obj, cap_id: int) -> tuple[int, list[int]]:
        """(current element value, list of element values)."""
        st = ffi.new("NkMAIDEnum *")
        r = self.command(obj, CMD_CAP_GET, cap_id, DT_ENUM_PTR, self._ptr(st))
        if r != R_OK:
            raise MaidError(f"CapGet enum 0x{cap_id:x}", r)
        count, phys = st.ulElements, st.wPhysicalBytes
        cur = int(st.ulValue)
        if count == 0:
            return cur, []
        buf = ffi.new(f"char[{count * phys}]")
        st.pData = buf
        r = self.command(obj, CMD_CAP_GET_ARRAY, cap_id, DT_ENUM_PTR, self._ptr(st))
        if r != R_OK:
            raise MaidError(f"CapGetArray enum 0x{cap_id:x}", r)
        if phys == 4:
            vals = [int(ffi.cast("ULONG *", buf)[i]) for i in range(count)]
        elif phys == 2:
            vals = [int(ffi.cast("uint16_t *", buf)[i]) for i in range(count)]
        elif phys == 1:
            vals = [int(ffi.cast("uint8_t *", buf)[i]) for i in range(count)]
        else:
            vals = []
        return cur, vals

    def set_enum_value(self, obj, cap_id: int, element: int,
                       retries: int = 3, retry_wait: float = 0.5) -> None:
        """Set an Enum cap to an element *value*.

        The Unsigned shortcut is tried first — that is what the D750 actually
        accepts (the full NkMAIDEnum struct gets -125 InvalidData); the struct
        form is the fallback. Transient DeviceBusy (a zoom change while a LV
        frame is in flight) is retried."""
        deadline = time.monotonic() + retries * retry_wait
        last = None
        while True:
            r = self.command(obj, CMD_CAP_SET, cap_id, DT_UNSIGNED, element)
            if r == R_OK:
                return
            last = r
            if r not in RETRYABLE or time.monotonic() >= deadline:
                st = ffi.new("NkMAIDEnum *")
                r = self.command(obj, CMD_CAP_GET, cap_id, DT_ENUM_PTR,
                                 self._ptr(st))
                if r == R_OK:
                    st.ulValue = element
                    r = self.command(obj, CMD_CAP_SET, cap_id, DT_ENUM_PTR,
                                     self._ptr(st))
                    if r == R_OK:
                        return
                    last = r
                raise MaidError(f"CapSet enum 0x{cap_id:x}={element}", last)
            self.pump(obj, timeout=retry_wait)

    def get_range(self, obj, cap_id: int) -> dict:
        st = ffi.new("NkMAIDRange *")
        r = self.command(obj, CMD_CAP_GET, cap_id, DT_RANGE_PTR, self._ptr(st))
        if r != R_OK:
            raise MaidError(f"CapGet range 0x{cap_id:x}", r)
        return {"value": st.lfValue, "default": st.lfDefault,
                "lower": st.lfLower, "upper": st.lfUpper, "steps": st.ulSteps}

    def set_range(self, obj, cap_id: int, value: float) -> float:
        """Set a Range cap (sample SetRangeCapability): re-read the struct,
        write either lfValue (ulSteps == 0) or the index it maps onto, send
        back via RangePtr. Returns the value the body now reports."""
        st = ffi.new("NkMAIDRange *")
        r = self.command(obj, CMD_CAP_GET, cap_id, DT_RANGE_PTR, self._ptr(st))
        if r != R_OK:
            raise MaidError(f"CapGet range(set) 0x{cap_id:x}", r)
        if st.ulSteps == 0:
            st.lfValue = value
        else:
            idx = round((value - st.lfLower) * (st.ulSteps - 1)
                        / (st.lfUpper - st.lfLower))
            st.ulValueIndex = min(max(idx, 0), int(st.ulSteps) - 1)
        r = self.command(obj, CMD_CAP_SET, cap_id, DT_RANGE_PTR, self._ptr(st))
        if r != R_OK:
            raise MaidError(f"CapSet range 0x{cap_id:x}={value}", r)
        out = ffi.new("NkMAIDRange *")
        r = self.command(obj, CMD_CAP_GET, cap_id, DT_RANGE_PTR, self._ptr(out))
        if r != R_OK:
            raise MaidError(f"CapGet range(verify) 0x{cap_id:x}", r)
        if out.ulSteps == 0:
            return float(out.lfValue)
        return float(out.lfLower + int(out.ulValueIndex)
                     * (out.lfUpper - out.lfLower) / (out.ulSteps - 1))

    def get_array(self, obj, cap_id: int) -> bytes:
        """Array cap via CapGet(size) + CapGetArray(into caller buffer)."""
        arr = ffi.new("NkMAIDArray *")
        r = self.command(obj, CMD_CAP_GET, cap_id, DT_ARRAY_PTR, self._ptr(arr))
        if r != R_OK:
            raise MaidError(f"CapGet array 0x{cap_id:x}", r)
        n = int(arr.ulElements) * int(arr.wPhysicalBytes)
        if n == 0:
            return b""
        buf = ffi.new(f"char[{n}]")
        arr.pData = buf
        r = self.command(obj, CMD_CAP_GET_ARRAY, cap_id, DT_ARRAY_PTR, self._ptr(arr))
        if r != R_OK:
            raise MaidError(f"CapGetArray 0x{cap_id:x}", r)
        return bytes(ffi.buffer(buf, n))

    def set_array(self, obj, cap_id: int, payload: bytes) -> None:
        arr = ffi.new("NkMAIDArray *")
        r = self.command(obj, CMD_CAP_GET, cap_id, DT_ARRAY_PTR, self._ptr(arr))
        if r != R_OK:
            raise MaidError(f"CapGet array(set) 0x{cap_id:x}", r)
        arr.ulElements = len(payload)
        arr.wPhysicalBytes = 1
        buf = ffi.from_buffer(payload)
        arr.pData = buf
        self._keepalive.append(buf)
        r = self.command(obj, CMD_CAP_SET, cap_id, DT_ARRAY_PTR, self._ptr(arr))
        self._keepalive.remove(buf)
        if r != R_OK:
            raise MaidError(f"CapSetArray 0x{cap_id:x}", r)

    # ---------------------------------------------------------------- capture

    def cap_start(self, obj, cap_id: int, dtype: int = DT_NULL, data: int = 0) -> None:
        """Start a Process capability and pump until it finishes.

        The completion trampoline carries the final result code; async must
        keep flowing on *obj* until it arrives (sample IssueProcess).
        """
        self.tramp.comp_reset()
        self.tramp.data_reset()
        r = self.command(obj, CMD_CAP_START, cap_id, dtype, data, done=None)
        if r not in CAPSTART_OK:
            raise MaidError(f"CapStart 0x{cap_id:x}", r)
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            self.command(obj, CMD_ASYNC)
            c = self.tramp.read_completion()
            if c is not None:
                _, result = c
                if result not in CAPSTART_OK:
                    raise MaidError(f"CapStart 0x{cap_id:x} completion", result)
                return
            time.sleep(0.005)
        raise MaidError(f"CapStart 0x{cap_id:x}: timeout", -1)

    def close(self) -> None:
        if self.module_obj is None:
            return
        # Close children innermost-first while the module is still open —
        # closing the module with an open Source segfaults the PTP driver.
        while self._open_stack:
            obj = self._open_stack.pop()
            try:
                self.command(obj, CMD_CLOSE)
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
            self.pump(self.module_obj, timeout=0.05)
        self.command(self.module_obj, CMD_CLOSE)
        self.module_obj = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Source:
    """A connected camera. Mirrors the sample's Source-object usage."""

    def __init__(self, mod: MaidModule, device_id: int):
        self._mod = mod
        self.device_id = device_id
        self.obj = mod.open_child(mod.module_obj, device_id, OBJ_SOURCE)
        self.caps = {c["id"]: c for c in mod.cap_infos(self.obj)}

    # -- introspection -------------------------------------------------------

    def has(self, cap_id: int, op: int | None = None) -> bool:
        c = self.caps.get(cap_id)
        if c is None or (c["visibility"] & VIS_INVALID):
            return False
        return op is None or bool(c["ops"] & op)

    def describe(self) -> str:
        out = ffi.new("char[256]")
        r = self._mod.command(self.obj, CMD_CAP_GET, CAP_NAME, DT_STRING_PTR,
                              self._mod._ptr(out))
        return ffi.string(out).decode(errors="replace") if r == R_OK else ""

    def camera_type(self) -> int:
        return self._mod.get_unsigned(self.obj, CAP_CAMERA_TYPE)

    def battery(self) -> int:
        return self._mod.get_unsigned(self.obj, CAP_BATTERY_LEVEL)

    # -- live view -----------------------------------------------------------

    def lv_status(self) -> int:
        return self._mod.get_unsigned(self.obj, CAP_LIVE_VIEW_STATUS)

    def lv_prohibit(self) -> int:
        return self._mod.get_unsigned(self.obj, CAP_LIVE_VIEW_PROHIBIT)

    def lv_on(self, wait: float = 5.0) -> None:
        """Start Live View. Spec: shooting mode must be P/S/A/M and
        LiveViewProhibit must be 0 — read lv_prohibit()/lv_prohibit_reasons().
        The body answers 152 DeviceBusy while it settles after USB attach,
        hence the retries; then blocks until the mirror is actually down."""
        # A previous session may have left LV running (the body keeps it on
        # after USB detach). Setting 1 while already 1 answers 152 DeviceBusy,
        # so cycle through 0 first.
        for _ in range(3):
            try:
                if self.lv_status() == 1:
                    self.lv_off()
                break
            except MaidError:
                time.sleep(0.3)
        self._mod.set_unsigned(self.obj, CAP_LIVE_VIEW_STATUS, 1,
                               retries=int(wait), retry_wait=1.0)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            self._mod.pump(self.obj, timeout=0.1)
            try:
                if self.lv_status() == 1 and self.lv_prohibit() == 0:
                    return
            except MaidError:
                pass
        # Not fatal: status may be 1 with the mirror still settling.

    def lv_off(self, wait: float = 3.0) -> None:
        self._mod.set_unsigned(self.obj, CAP_LIVE_VIEW_STATUS, 0,
                               retries=2, retry_wait=1.0)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            self._mod.pump(self.obj, timeout=0.1)
            try:
                if self.lv_status() == 0:
                    return
            except MaidError:
                return

    def lv_image(self, retries: int = 3) -> bytes:
        """One GetLiveViewImage array. Measured on the D750: a 384-byte
        header (all zero) precedes the JPEG even though the sample only sets
        it for the D810 — strip blob[blob.find(JPEG_MAGIC):]. Frames are
        ~7 fps fresh from the sensor; back-to-back grabs faster than that
        repeat the cached DRAM frame. 159 NotLiveView right after lv_on
        (mirror still dropping) and 152 while the previous frame is in
        flight are retried transparently."""
        return self._mod.get_array_retry(self.obj, CAP_GET_LIVE_VIEW_IMAGE,
                                         retries=retries)

    def zoom_values(self) -> tuple[int, list[int]]:
        return self._mod.get_enum_values(self.obj, CAP_LIVE_VIEW_IMAGE_ZOOM_RATE)

    def set_zoom(self, rate: int) -> None:
        self._mod.set_enum_value(self.obj, CAP_LIVE_VIEW_IMAGE_ZOOM_RATE, rate)

    def lv_image_size_values(self) -> tuple[int, list[int]]:
        return self._mod.get_enum_values(self.obj, CAP_LIVE_VIEW_IMAGE_SIZE)

    # -- properties ----------------------------------------------------------

    def shutter_values(self) -> tuple[int, list[int]]:
        return self._mod.get_enum_values(self.obj, CAP_SHUTTER_SPEED)

    def iso_values(self) -> tuple[int, list[int]]:
        return self._mod.get_enum_values(self.obj, CAP_SENSITIVITY)

    def set_shutter(self, element: int) -> None:
        self._mod.set_enum_value(self.obj, CAP_SHUTTER_SPEED, element)

    def set_iso(self, element: int) -> None:
        self._mod.set_enum_value(self.obj, CAP_SENSITIVITY, element)

    def exposure_status(self) -> float:
        """Metered EV offset (sample SetFloatCapability: CapGet with
        kNkMAIDDataType_FloatPtr on the Float-type cap; Range/Array forms
        answer -126 UnexpectedDataType). Readable during Live View; the
        autoexposure loop drives it to 0 by stepping ShutterSpeed/Sensitivity/
        ExposureComp — all of which stay settable while LV runs."""
        out = ffi.new("double *")
        r = self._mod.command(self.obj, CMD_CAP_GET, CAP_EXPOSURE_STATUS,
                              DT_FLOAT_PTR, self._mod._ptr(out))
        if r != R_OK:
            raise MaidError("CapGet ExposureStatus float", r)
        return float(out[0])

    # -- MF drive ------------------------------------------------------------

    def mf_step_range(self) -> dict:
        return self._mod.get_range(self.obj, CAP_MF_DRIVE_STEP)

    def mf_drive(self, direction_element: int) -> None:
        """Start MfDrive with an enum/unsigned element (probe reads the enum
        first; direction sign is per the enum list)."""
        self._mod.cap_start(self.obj, CAP_MF_DRIVE, DT_UNSIGNED,
                            direction_element)

    # -- still capture -------------------------------------------------------

    def capture_still(self, target_path: Path) -> Path:
        """Release the shutter and pull the raw down via item-object Acquire.

        Mirrors the sample: CapStart(Capture) on the source → the body writes
        the card and announces an Item object → open its Image DataObj, set
        DataProc, CapStart(Acquire); chunks accumulate in C and are written
        out whole. The card copy stays (in-camera backup, parity with the
        gphoto2 backend).
        """
        mod = self._mod
        mod.cap_start(self.obj, CAP_CAPTURE)
        items = mod.enum_children(self.obj)
        if not items:
            raise MaidError("capture: no item object appeared", -1)
        item = mod.open_child(self.obj, max(items), OBJ_ITEM)
        try:
            children = mod.enum_children(item)
            chosen = next((d for d in children if d & (DO_IMAGE | DO_FILE)),
                          children[0] if children else DO_IMAGE)
            data_obj = mod.open_child(item, chosen, OBJ_DATAOBJ)
            try:
                caps = {c["id"]: c for c in mod.cap_infos(data_obj)}
                if caps.get(CAP_DATA_PROC, {}).get("ops", 0) & OP_SET:
                    mod._set_callback(data_obj, CAP_DATA_PROC, mod.tramp.data_addr())
                mod.tramp.data_reset()
                mod.cap_start(data_obj, CAP_ACQUIRE)
                blob = mod.tramp.take_blob()
                if not blob:
                    raise MaidError("capture: empty data delivery", -1)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_bytes(blob)
            finally:
                mod.close_object(data_obj)
        finally:
            mod.close_object(item)
        return target_path


def devices(mod: MaidModule) -> list[int]:
    """Connected camera Source IDs."""
    return mod.enum_children(mod.module_obj)
