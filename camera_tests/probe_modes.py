"""Probe: konverzní gain (LCG/HCG) a Low Noise Mode — co kamera umí a co to dělá.

Rychlý probe bez tmy (žádné měření šumu — to až lcg_hcg_snr.py):

1. vlajky z EnumV2 (FLAG_CG, FLAG_LOW_NOISE, FLAG_CGHDR)
2. put/get TOUPCAM_OPTION_CG (0=LCG, 1=HCG[, 2=HDR]) — co kamera přijme
3. put/get TOUPCAM_OPTION_LOW_NOISE (0x38) — co kamera přijme
4. kadence proudu full-res 16bit: LN off vs. LN on (manuál slibuje 6,8 → 3,4 fps)
5. LN + 3x3 binning (aplikační overview) — přežije to, a jaká je kadence?
   Manuál: "Low Noise Mode is only available in All Pixel Readout Mode".
6. still capture s LN on — produkční cesta TouptekCamera.capture() to snese?

Spustit s kamerou na USB: .venv/bin/python camera_tests/probe_modes.py
"""

from __future__ import annotations

import time

from _common import ROOT  # noqa: F401
from _common import connect

from filmscan_studio.capture._toupcam import toupcam as sdk  # noqa: E402


def measure_fps(cam, seconds: float = 6.0) -> tuple[float, str]:
    """Rychlost proudu [fps] za `seconds`; vrací (fps, popis rámce)."""
    cam.start_live_view()
    size = "?"
    try:
        first = cam.next_live_frame()  # první = ať počítáme ustálený proud
        if first is None:
            return 0.0, "proud nedodává (next_live_frame -> None)"
        size = f"{first.width}x{first.height}"
        count, started = 1, time.monotonic()
        while time.monotonic() - started < seconds:
            try:
                cam.next_live_frame()
                count += 1
            except Exception as exc:   # noqa: BLE001 - při LN+binning může mrdat
                print(f"    [fps] proud selhal po {count} snímcích: {exc}")
                break
        elapsed = time.monotonic() - started
        return (count / elapsed if elapsed else 0.0), size
    finally:
        cam.stop_live_view()


def try_option(hcam, opt: int, values: list[int]) -> dict[int, object]:
    """Napiš každou hodnotu, přečti zpátky; vrací {žádáno: readback|CHYBA}."""
    out: dict[int, object] = {}
    for v in values:
        try:
            hcam.put_Option(opt, v)
        except sdk.HRESULTException as exc:
            out[v] = f"ODMÍTNUTO hr=0x{exc.hr & 0xffffffff:x}"
            continue
        try:
            out[v] = hcam.get_Option(opt)
        except sdk.HRESULTException as exc:
            out[v] = f"čitelné? hr=0x{exc.hr & 0xffffffff:x}"
    return out


def main() -> None:
    cam = connect()
    hcam = cam._hcam

    # 1. vlajky -------------------------------------------------------------
    dev = next(iter(sdk.Toupcam.EnumV2()), None)
    flags = int(getattr(dev.model, "flag", 0)) if dev else 0
    print(f"[flags] 0x{flags:x}")
    for name in ("CG", "CGHDR", "LOW_NOISE"):
        bit = getattr(sdk, f"TOUPCAM_FLAG_{name}")
        print(f"  FLAG_{name:<9} = {'ANO' if flags & bit else 'ne'}")

    # 2./3. CG a LOW_NOISE options ------------------------------------------
    cg = sdk.TOUPCAM_OPTION_CG
    ln = sdk.TOUPCAM_OPTION_LOW_NOISE
    print(f"[CG 0x{cg:x}] readback po zápisu 0/1/2: "
          f"{try_option(hcam, cg, [0, 1, 2])}")
    print(f"[LN 0x{ln:x}] readback po zápisu 0/1:   "
          f"{try_option(hcam, ln, [0, 1])}")

    # gain range — kontrola jednotek (očekáváme (100, 10000, ...) = procenta)
    print(f"[ExpoAGainRange] {hcam.get_ExpoAGainRange()} (procenta: 100 = 1x)")

    # 4. fps full-res, LN off/on --------------------------------------------
    # overview proudu aplikace = 0x83 (3x3 average); tady chceme ALL PIXEL,
    # takže NO_BINNING + full ROI — přesně to _configure_stream umí přes
    # set_live_view_roi(None) jen s binned; nastavujeme primě přes SDK.
    print("[fps] full-res 16bit (6224x4168):")
    for ln_on in (0, 1):
        try:
            hcam.put_Option(ln, ln_on)
        except sdk.HRESULTException as exc:
            print(f"  LN={ln_on}: option ODMÍTNUTO hr=0x{exc.hr & 0xffffffff:x}")
            continue
        cam.stop_live_view()
        cam._binning, cam._roi = 0x01, None   # NO_BINNING, full sensor
        fps, size = measure_fps(cam)
        print(f"  LN={ln_on}: {fps:5.2f} fps, rámce {size}")
    # zpět na aplikační overview, ať je kamera v obvyklém stavu
    hcam.put_Option(ln, 0)
    cam.set_live_view_roi(None)

    # 5. LN + binning overview ------------------------------------------------
    # manuál: LN jen v All-Pixel readout — co dělá 3x3 overview (0x83) s LN=1?
    # (měřeno 2026-09-20: kamera LN při startu tichounce VYPNE a streamuje
    # binned 12,6 fps — proto čteme readback LN volby i po Start())
    print("[LN+binning] overview 0x83 s LN=1:")
    try:
        hcam.put_Option(ln, 1)
        cam._binning, cam._roi = 0x83, None
        fps, size = measure_fps(cam, seconds=4.0)
        try:
            ln_after = hcam.get_Option(ln)
        except sdk.HRESULTException:
            ln_after = "?"
        print(f"  {fps:5.2f} fps, rámce {size}, LN readback po startu = "
              f"{ln_after} (1 = LNdržel, 0 = kamera LN při binningu sama vypnula)")
    except Exception as exc:  # noqa: BLE001
        print(f"  selhalo: {exc!r}")
    finally:
        cam.stop_live_view()
        try:
            hcam.put_Option(ln, 0)
        except sdk.HRESULTException:
            pass
        cam._binning, cam._roi = 0x83, None

    # 6. still s LN on — produkční cesta --------------------------------------
    print("[still+LN] capture(frames=1) s LN=1 přes TouptekCamera.capture():")
    out = ROOT / "camera_tests" / "data"
    try:
        hcam.put_Option(ln, 1)
        res = cam.capture(out, "probe_modes_still_ln", frames=1)
        print(f"  ok: {res.path.name}, {res.size_bytes} B, notes={res.notes}")
    except Exception as exc:  # noqa: BLE001
        print(f"  selhalo: {exc}")
    finally:
        hcam.put_Option(ln, 0)

    # návrat do LCG (hodnota, kterou kamera převezme pro trvalý provoz)
    try:
        hcam.put_Option(cg, 0)
        print(f"[návrat] CG readback = {hcam.get_Option(cg)} (0 = LCG)")
    except sdk.HRESULTException as exc:
        print(f"[návrat] CG nelze nastavit: hr=0x{exc.hr & 0xffffffff:x}")
    cam.disconnect()
    print("hotovo")


if __name__ == "__main__":
    main()
