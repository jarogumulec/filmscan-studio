"""gphoto2 backend for the Nikon D750.

Implementation notes, all of them learned against real hardware rather than from
documentation:

* **The bindings are used, not the CLI.** Measured on a D750, shelling out to
  ``gphoto2 --capture-movie`` yields about 1.1 fps and ``--capture-preview`` about
  8 fps, because each invocation reconnects. A persistent libgphoto2 session
  polling ``gp_camera_capture_preview`` delivers ~43 changing frames per second,
  which is the difference between usable focusing and a slideshow.
* **``ptpcamera`` must be released first.** ``/usr/libexec/ptpcamera`` claims
  PTP devices on macOS and cannot be disabled under SIP; it respawns within
  milliseconds of being killed, so ``init`` is retried in a loop.
* **The viewfinder widget wants an integer.** Setting it to the string ``'1'``
  raises BAD_PARAMETERS on this build; ``1`` works.
* **Choice lists are localised.** ``capturetarget`` offers 'Interní paměť' /
  'Paměťová karta' under a Czech locale, so values are matched by scanning the
  choices rather than by hardcoding English strings.
* **Aperture is not exposed.** A manual Micro-Nikkor has no electronic
  connection, so ``f_number`` comes from EXIF after the fact, never from here.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path

import gphoto2 as gp

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

#: Seconds to keep trying while ptpcamera releases the device.
_CONNECT_TIMEOUT = 12.0
_CONNECT_RETRY_DELAY = 0.5

_SHUTTER_RE = re.compile(r"^\s*(?:(\d+)\s*/\s*(\d+)|(\d+(?:\.\d+)?))")


def parse_shutter(value: str) -> float | None:
    """Parse gphoto2's shutter vocabulary into seconds.

    Handles '0.0400 s', '1/60' and '30"'. Returns None for entries that are not
    numeric speeds ('Bulb', 'Time'), which are handled separately.
    """
    m = _SHUTTER_RE.match(value)
    if not m:
        return None
    num, den, whole = m.groups()
    if num is not None:
        return int(num) / int(den) if int(den) else None
    return float(whole)


def parse_iso(value: str) -> int | None:
    m = re.search(r"\d+", value)
    return int(m.group()) if m else None


class GPhoto2Backend(CameraBackend):
    """Nikon D750 over PTP/USB via libgphoto2."""

    def __init__(self, port: str | None = None, connect_timeout: float = _CONNECT_TIMEOUT) -> None:
        self._port = port
        self._connect_timeout = connect_timeout
        self._camera: gp.Camera | None = None
        self._config: object | None = None
        self._info = CameraInfo(model="unknown")
        self._live_view = False

    # ---------------------------------------------------------------- lifecycle

    def connect(self) -> CameraInfo:
        """Open a session, retrying while macOS ptpcamera holds the device."""
        deadline = time.monotonic() + self._connect_timeout
        attempt = 0
        last_error: Exception | None = None
        while True:
            attempt += 1
            _release_ptpcamera()
            camera = gp.Camera()
            try:
                if self._port:
                    camera.set_port_info(_port_info(self._port))
                camera.init()
            except gp.GPhoto2Error as exc:
                last_error = exc
                if time.monotonic() >= deadline:
                    raise CameraError(
                        "Nelze se připojit k fotoaparátu. Zavřete Foto/Aperture a "
                        "ubezpečte se, že '/usr/libexec/ptpcamera' zařízení nedrží."
                    ) from exc
                time.sleep(_CONNECT_RETRY_DELAY)
                continue

            self._camera = camera
            try:
                self._config = camera.get_config()
                self._info = self._read_info(camera)
            except gp.GPhoto2Error as exc:
                _safe_exit(camera)
                self._camera = None
                raise CameraError(f"Fotoaparát se připojil, ale nelze načíst nastavení: {exc}") from exc
            log.info("connected to %s (attempt %d)", self._info.model, attempt)
            return self._info

    def disconnect(self) -> None:
        if self._camera is None:
            return
        if self._live_view:
            try:
                self.stop_live_view()
            except CameraError:
                pass
        _safe_exit(self._camera)
        self._camera = None
        self._config = None

    @property
    def info(self) -> CameraInfo:
        return self._info

    def capabilities(self) -> CameraCapabilities:
        return CameraCapabilities(
            live_view=True,
            shutter=True,
            iso=True,
            aperture=False,
            focus_drive=True,
            notes=(
                "Clonu nelze ovládat z počítače - nastavte ji na objektivu a zkontrolujte v EXIF.",
                "Live View je 640x424 JPEG; pro zaostřování použijte zoom v náhledu.",
            ),
        )

    # ------------------------------------------------------------------ settings

    def get_settings(self) -> ExposureSettings:
        self._require()
        shutter = parse_shutter(str(self._value("shutterspeed"))) or 1.0
        iso = parse_iso(str(self._value("iso"))) or 100
        return ExposureSettings(shutter=shutter, iso=iso, aperture=None)

    def set_shutter(self, seconds: float) -> float:
        self._require()
        if seconds <= 0:
            raise ValueError("shutter must be positive")
        choices = self._choices("shutterspeed")
        parsed = [(c, parse_shutter(c)) for c in choices]
        usable = [(c, v) for c, v in parsed if v]
        if not usable:
            raise CameraError("fotoaparát nenabízí žádné numerické expoziční časy")
        best, _ = min(usable, key=lambda cv: abs(cv[1] - seconds))
        self._set("shutterspeed", best)
        applied = parse_shutter(str(self._value("shutterspeed"))) or seconds
        if abs(applied - seconds) / seconds > 0.02:
            log.info("shutter %.4fs snapped to %.4fs", seconds, applied)
        return applied

    def set_iso(self, iso: int) -> int:
        self._require()
        if iso <= 0:
            raise ValueError("iso must be positive")
        choices = self._choices("iso")
        parsed = [(c, parse_iso(c)) for c in choices]
        usable = [(c, v) for c, v in parsed if v]
        if not usable:
            raise CameraError("fotoaparát nenabízí žádné hodnoty ISO")
        best, _ = min(usable, key=lambda cv: abs(cv[1] - iso))
        self._set("iso", best)
        return parse_iso(str(self._value("iso"))) or iso

    # ----------------------------------------------------------------- live view

    def start_live_view(self) -> None:
        self._require()
        # Integer, not string: this build rejects '1' with BAD_PARAMETERS.
        self._set("viewfinder", 1)
        self._live_view = True
        # The body needs a moment before the first frame is meaningful.
        time.sleep(0.4)
        self._drain_events()

    def stop_live_view(self) -> None:
        if self._camera is None:
            self._live_view = False
            return
        try:
            self._set("viewfinder", 0)
        finally:
            self._live_view = False

    def next_live_frame(self) -> LiveFrame | None:
        if self._camera is None:
            raise NotConnectedError("fotoaparát není připojen")
        try:
            cfile = gp.check_result(gp.gp_camera_capture_preview(self._camera.this))
        except gp.GPhoto2Error as exc:
            raise CameraError(f"Live View snímek se nepodařilo získat: {exc}") from exc
        # check_result unwraps the [retcode, memoryview] pair this returns; the
        # memoryview points into libgphoto2-owned memory that dies with cfile,
        # so bytes() must copy before this function returns.
        data = gp.check_result(gp.gp_file_get_data_and_size(cfile))
        return LiveFrame(jpeg=bytes(data))

    # ------------------------------------------------------------------ capture

    def capture(self, destination: Path, filename_stem: str) -> CaptureResult:
        """Release the shutter, then pull the raw file off the body.

        The frame is written to the memory card rather than internal RAM: the D750
        holds only a handful of frames in RAM, and a 30 MB NEF is not one of them.
        The card copy is kept, which doubles as an in-camera backup.
        """
        if self._camera is None:
            raise NotConnectedError("fotoaparát není připojen")
        settings = self.get_settings()
        started = time.monotonic()
        self._set_capture_target_card()
        try:
            path = self._camera.capture(gp.GP_CAPTURE_IMAGE)
        except gp.GPhoto2Error as exc:
            raise CameraError(f"Snímek se nepodařilo pořídit: {exc}") from exc

        cfile = self._await_file(path.folder, path.name)
        destination.mkdir(parents=True, exist_ok=True)
        suffix = Path(path.name).suffix or ".nef"
        target = destination / f"{filename_stem}{suffix.lower()}"
        cfile.save(str(target))
        elapsed = time.monotonic() - started
        self._drain_events()
        return CaptureResult(
            path=target,
            size_bytes=target.stat().st_size,
            settings=settings,
            elapsed=elapsed,
            capture_target="card",
        )

    # ------------------------------------------------------------------ internals

    def _require(self) -> None:
        if self._camera is None or self._config is None:
            raise NotConnectedError("fotoaparát není připojen")

    def _child(self, name: str) -> object:
        ok, widget = gp.gp_widget_get_child_by_name(self._config, name)
        if ok < gp.GP_OK:
            raise CameraError(f"fotoaparát neposkytuje nastavení '{name}'")
        return widget

    def _value(self, name: str) -> object:
        return self._child(name).get_value()

    def _choices(self, name: str) -> list[str]:
        widget = self._child(name)
        return [widget.get_choice(i) for i in range(widget.count_choices())]

    def _set(self, name: str, value: object) -> None:
        self._require()
        widget = self._child(name)
        try:
            widget.set_value(value)
            self._camera.set_config(self._config)
        except gp.GPhoto2Error as exc:
            raise CameraError(f"Nelze nastavit '{name}' na {value!r}: {exc}") from exc

    def _set_capture_target_card(self) -> None:
        """Select the memory card by scanning choices, which are localised."""
        try:
            choices = self._choices("capturetarget")
        except CameraError:
            return
        markers = ("card", "karta", "paměťová", "pametova")
        for text in choices:
            if any(m in text.lower() for m in markers):
                self._set("capturetarget", text)
                return
        # Nothing recognisable; index 1 is the card on every libgphoto2 Nikon build.
        if len(choices) > 1:
            self._set("capturetarget", choices[1])

    def _await_file(self, folder: str, name: str, timeout: float = 30.0) -> object:
        """Poll for the finished frame; the body writes it asynchronously.

        ``wait_for_event`` is the documented mechanism but returns unreliably over
        the Nikon PTP extension, so it is used only to warm the queue while the
        actual wait is a timed poll on ``file_get``.
        """
        deadline = time.monotonic() + timeout
        last: Exception | None = None
        while time.monotonic() < deadline:
            self._drain_events(20)
            try:
                return self._camera.file_get(folder, name, gp.GP_FILE_TYPE_NORMAL)
            except gp.GPhoto2Error as exc:
                last = exc
                time.sleep(0.1)
        raise CameraError(f"NEF se nepodařilo stáhnout ({folder}/{name}): {last}")

    def _drain_events(self, max_events: int = 12) -> None:
        if self._camera is None:
            return
        for _ in range(max_events):
            try:
                kind, _data = self._camera.wait_for_event(10)
            except gp.GPhoto2Error:
                return
            if kind == gp.GP_EVENT_TIMEOUT:
                return

    def _read_info(self, camera: gp.Camera) -> CameraInfo:
        summary = str(camera.get_summary())
        model = _extract(summary, "Model") or "unknown"
        manufacturer = _extract(summary, "Výrobce") or _extract(summary, "Manufacturer")
        serial = _extract(summary, "Sériové číslo") or _extract(summary, "Serial Number")
        shutter_choices: list[float] = []
        iso_choices: list[int] = []
        battery: int | None = None
        try:
            root = camera.get_config()
            for name, cast in (("shutterspeed", parse_shutter), ("iso", parse_iso)):
                ok, w = gp.gp_widget_get_child_by_name(root, name)
                if ok < gp.GP_OK:
                    continue
                for i in range(w.count_choices()):
                    v = cast(w.get_choice(i))
                    if v is None:
                        continue
                    (shutter_choices if name == "shutterspeed" else iso_choices).append(v)
            ok, w = gp.gp_widget_get_child_by_name(root, "batterylevel")
            if ok >= gp.GP_OK:
                m = re.search(r"\d+", str(w.get_value()))
                battery = int(m.group()) if m else None
        except gp.GPhoto2Error:
            pass
        return CameraInfo(
            model=model,
            manufacturer=manufacturer,
            serial=serial,
            battery_percent=battery,
            shutter_choices=tuple(sorted(set(shutter_choices), reverse=True)),
            iso_choices=tuple(sorted(set(iso_choices))),
        )


def _release_ptpcamera() -> None:
    """Best-effort release of the macOS PTP daemon.

    It cannot be disabled under SIP and respawns almost immediately, so this is a
    race we win by retrying rather than by killing decisively. Only harmless if
    the process does not exist.
    """
    subprocess.run(
        ["pkill", "-9", "-f", "ptpcamerad"],
        capture_output=True,
        check=False,
    )


def _safe_exit(camera: gp.Camera) -> None:
    try:
        camera.exit()
    except Exception:  # noqa: BLE001 - teardown must never raise
        pass


def _port_info(port: str) -> object:
    plist = gp.PortInfoList()
    plist.load()
    idx = plist.find_by_path(port) if hasattr(plist, "find_by_path") else -1
    if idx < 0:
        raise CameraError(f"Port '{port}' neexistuje")
    return plist.get_info(idx)


def _extract(summary: str, key: str) -> str | None:
    for line in summary.splitlines():
        if line.strip().startswith(key + ":"):
            return line.split(":", 1)[1].strip() or None
    return None
