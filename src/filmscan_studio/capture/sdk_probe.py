"""Hardware probe for the Nikon Type0015 module — run this on the real D750.

Answers, on real hardware, the questions the plan cannot settle from headers:

* does the module load on this macOS (15.5 is above its tested 14) under
  Rosetta 2 with ``ptpcamerad`` running?
* does the D750 send a 384-byte LiveView header (the sample hardcodes it for
  the D810 and 0 otherwise — we detect the JPEG magic instead)?
* does ``LiveViewImageZoomRate`` actually reframe the stream (and at what fps
  vs whole-frame), which is the entire reason to replace gphoto2;
* does ``MfDrive`` enumerate at all on a manual AI lens (expected: no);
* can a still capture + Acquire download a NEF through DataProc.

Run (camera on, USB, PTP, no card needed for live view):

    scripts/probe.sh            # or: uv run filmscan-studio sdk-probe
    scripts/probe.sh --capture  # also test the still + download path

Writes ``sdk_probe_results.json`` (+ sample JPEGs) to ``--out`` (default cwd)
and prints a human summary. The JSON is the handoff artifact for wiring the
real backend.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):      # allow `python sdk_probe.py` too
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from filmscan_studio.capture import nikon_sdk as sdk

JPEG_MAGIC = b"\xff\xd8\xff"


def _check_env(report: dict) -> bool:
    ok = True
    report["machine"] = platform.machine()
    report["macos"] = subprocess.run(["sw_vers", "-productVersion"],
                                     capture_output=True, text=True
                                     ).stdout.strip()
    report["python"] = sys.version.split()[0]
    if platform.machine() != "x86_64":
        print("!! tento interpreter není x86_64 — spouštěj přes scripts/probe.sh")
        ok = False
    if not shutil.which("cc"):
        print("!! chybí cc (xcode-select --install) — nutný pro trampolíny")
        ok = False
    report["sdk_installed"] = sdk.sdk_installed()
    if not report["sdk_installed"]:
        print("!! SDK nenainstalováno v /Library/Application Support/Nikon — "
              "spusť scripts/install_sdk.sh (sudo)")
        ok = False
    return ok


def _probe_live_view(source, report: dict, outdir: Path) -> None:
    lv: dict = {}
    report["live_view"] = lv
    bits = source.lv_prohibit()
    lv["prohibit"] = hex(bits)
    lv["prohibit_reasons"] = sdk.lv_prohibit_reasons(bits)
    if bits:
        lv["prohibit_note"] = ("LiveViewProhibit != 0 — camera říká proč v "
                               "prohibit_reasons (Battery = slabá baterie, "
                               "Retractable = vysunutý zoom prstenec, "
                               "NonCPU/ApertureRing = clona…). Zkusím LV i tak, "
                               "bit může být dočasný.")
    try:
        source.lv_on()
    except sdk.MaidError as exc:
        lv["error"] = f"lv_on: {exc}"
        return
    try:
        lv["status_after_on"] = source.lv_status()
        cur, vals = source.zoom_values()
        lv["zoom_rate"] = {"current": cur, "values": vals}
        try:
            size_cur, size_vals = source.lv_image_size_values()
            lv["image_size"] = {"current": size_cur, "values": size_vals}
        except sdk.MaidError as exc:
            lv["image_size"] = f"unavailable: {exc}"

        # Meter reading while LV runs — this is the AE feedback signal the
        # scan pipeline will close the loop on.
        try:
            lv["exposure_status_ev_in_lv"] = source.exposure_status()
        except sdk.MaidError as exc:
            lv["exposure_status_ev_in_lv"] = f"error: {exc}"

        # First frame: the mirror needs ~1 s to drop after LiveViewStatus=1;
        # GetLiveViewImage answers 159 NotLiveView until then.
        first = time.monotonic()
        frame = None
        while time.monotonic() - first < 5.0 and frame is None:
            try:
                frame = source.lv_image(retries=0)
            except sdk.MaidError as exc:
                lv.setdefault("lv_errors", []).append(str(exc))
                time.sleep(0.25)
        if frame is None:
            lv["error"] = ("GetLiveViewImage never delivered — 159 "
                           "NotLiveView persists; check the LV switch on the "
                           "body / shooting mode")
            source.lv_off()
            return
        off = frame.find(JPEG_MAGIC)
        lv["header_bytes_all"] = off
        (outdir / "lv_all.jpg").write_bytes(frame[off:] if off >= 0 else frame)

        for label, rate in (("all", sdk.ZOOM_ALL), ("100pct", sdk.ZOOM_100)):
            if rate not in vals and rate != sdk.ZOOM_ALL:
                lv[f"fps_{label}"] = "rate not enumerated"
                continue
            try:
                source.set_zoom(rate)
            except sdk.MaidError as exc:
                lv[f"fps_{label}"] = f"set failed: {exc}"
                continue
            time.sleep(0.4)
            # Paced slightly slower than the D750's real LV rate (~7 fps):
            # unpaced GetLiveViewImage just re-reads the cached DRAM frame
            # (measured 112 "fps" of identical bytes), which would lie here.
            frames, errors, uniq, seen = 0, 0, 0, set()
            started = time.monotonic()
            while time.monotonic() - started < 3.0:
                try:
                    blob = source.lv_image()
                except sdk.MaidError as exc:
                    # transient 152/159 after internal retries — count, keep
                    errors += 1
                    lv.setdefault("lv_errors", []).append(str(exc))
                    continue
                frames += 1
                h = hashlib.md5(blob[384:]).hexdigest()
                if h not in seen:
                    uniq += 1
                    seen.add(h)
                if label == "100pct" and frames == 1:
                    off = blob.find(JPEG_MAGIC)
                    (outdir / "lv_100pct.jpg").write_bytes(
                        blob[off:] if off >= 0 else blob)
                time.sleep(0.14)
            elapsed = time.monotonic() - started
            lv[f"fps_{label}"] = round(uniq / elapsed, 1) if elapsed else 0
            lv[f"frames_pulled_{label}"] = frames
            lv[f"busy_errors_{label}"] = errors
    finally:
        source.lv_off()


def _probe_focus(source, report: dict) -> None:
    focus: dict = {}
    report["focus"] = focus
    focus["has_MfDrive"] = source.has(sdk.CAP_MF_DRIVE)
    focus["has_ContrastAF"] = source.has(sdk.CAP_CONTRAST_AF)
    focus["has_LiveViewAF"] = source.has(sdk.CAP_LIVE_VIEW_AF)
    if source.has(sdk.CAP_MF_DRIVE_STEP):
        try:
            focus["mf_step_range"] = source.mf_step_range()
        except sdk.MaidError as exc:
            focus["mf_step_range"] = f"error: {exc}"
    if not focus["has_MfDrive"]:
        focus["note"] = ("MfDrive not enumerated — expected on a manual "
                         "AI/AI-s lens; focus stays a hand wheel")


def _probe_exposure(source, report: dict) -> None:
    exp: dict = {}
    report["exposure"] = exp
    for label, getter in (("shutter", source.shutter_values),
                          ("iso", source.iso_values)):
        try:
            cur, vals = getter()
            exp[label] = {"current": cur, "count": len(vals),
                          "values": vals[:60]}
        except sdk.MaidError as exc:
            exp[label] = f"error: {exc}"
    try:
        # Float cap (sample SetFloatCapability) — only meaningful while the
        # meter is live, i.e. during Live View / after PreCapture.
        exp["exposure_status_ev"] = source.exposure_status()
    except sdk.MaidError as exc:
        exp["exposure_status"] = f"error: {exc}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sdk-probe")
    parser.add_argument("--out", default=".", type=Path)
    parser.add_argument("--capture", action="store_true",
                        help="otestovat i still + Acquire (NEF, ~s)")
    parser.add_argument("--mf-test", action="store_true",
                        help="zkusit MfDrive krok (pouze z elektronikou!)")
    args = parser.parse_args(argv)

    report: dict = {"steps": []}
    outdir = args.out
    outdir.mkdir(parents=True, exist_ok=True)
    if not _check_env(report):
        report["aborted"] = "environment check failed"
        _write(report, outdir)
        return 2

    print("→ otevírám Type0015 Module…")
    try:
        mod = sdk.MaidModule()
    except Exception as exc:  # noqa: BLE001 - probe reports, never raises
        report["open_error"] = f"{type(exc).__name__}: {exc}"
        print(f"!! modul se nepodařilo otevřít: {exc}")
        _write(report, outdir)
        return 3

    try:
        report["module_caps"] = [
            {"id": f"0x{c['id']:x}", "name": c["name"], "ops": c["ops"]}
            for c in mod._module_caps]
        print(f"  modul: {len(mod._module_caps)} kapacit")
        devs = sdk.devices(mod)
        report["devices"] = devs
        print(f"→ zařízení: {devs or 'ŽÁDNÉ'}")
        if not devs:
            report["aborted"] = ("no device — zkontroluj USB, PTP režim; "
                                 "nechápu ptpcamerad drží-li zařízení (SDK "
                                 "přes ImageCaptureCore by ho měl sdílet)")
            _write(report, outdir)
            return 4

        src = sdk.Source(mod, devs[0])
        report["source_name"] = src.describe()
        try:
            report["camera_type"] = src.camera_type()
        except sdk.MaidError as exc:
            report["camera_type"] = f"error: {exc}"
        report["source_caps"] = [
            {"id": f"0x{c:04x}", "name": info["name"], "ops": info["ops"],
             "type": info["type"]}
            for c, info in sorted(src.caps.items())]
        print(f"→ {report['source_name']} (cameraType={report.get('camera_type')})")

        _probe_live_view(src, report, outdir)
        print(f"  live view: {json.dumps({k: v for k, v in report['live_view'].items() if k.startswith(('fps', 'header'))}, ensure_ascii=False)}")
        _probe_focus(src, report)
        print(f"  focus: MfDrive={report['focus']['has_MfDrive']}")
        _probe_exposure(src, report)

        if args.mf_test and report["focus"]["has_MfDrive"]:
            try:
                src.lv_on()
                time.sleep(0.3)
                cur, vals = src._mod.get_enum_values(src.obj, sdk.CAP_MF_DRIVE)
                report["mf_drive_enum"] = {"current": cur, "values": vals}
                if vals:
                    src.mf_drive(vals[0])
                    report["mf_drive_first"] = f"started with element {vals[0]}"
                src.lv_off()
            except sdk.MaidError as exc:
                report["mf_drive_error"] = str(exc)

        if args.capture:
            print("→ still capture (NEF na kartu + stažení)…")
            cap: dict = {}
            report["capture"] = cap
            try:
                target = outdir / "probe_capture.NEF"
                t0 = time.monotonic()
                src.capture_still(target)
                cap["ok"] = True
                cap["seconds"] = round(time.monotonic() - t0, 2)
                cap["bytes"] = target.stat().st_size
                print(f"  {target} ({cap['bytes']} B, {cap['seconds']} s)")
            except Exception as exc:  # noqa: BLE001
                cap["ok"] = False
                cap["error"] = f"{type(exc).__name__}: {exc}"
                print(f"  !! {cap['error']}")
    finally:
        mod.close()

    _write(report, outdir)
    print(f"\n✓ výsledky: {outdir / 'sdk_probe_results.json'}")
    return 0


def _write(report: dict, outdir: Path) -> None:
    (outdir / "sdk_probe_results.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    raise SystemExit(main())
