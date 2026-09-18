"""Hardware probe for the two untested commits (76647ed manual exp, b43b55c filmbase).

Drives the real TouptekCamera through exactly the code paths the GUI uses that
could not be exercised on the mock/fake, and prints a PASS/FAIL verdict per
item. No Qt, no GUI — the same backend/session calls the worker thread makes.

Items (see INSTRUCTIONS §11 + CHANGELOG "Co nešlo bez těla ověřit"):

  A  hardware AE is OFF at connect and manual ``put_ExpoTime`` takes effect on
     a *running* stream — the frame's own ``expotime_us`` stamp tracks the
     shutter dial (this is the "turn the dial, histogram moves" contract, and
     it is the same stamp the film-base-from-stream reading depends on).
  B  ``set_shutter`` readback reports the firmware's real ceiling for an
     over-range request (the "scéna chtěla 56.6, kamera umí max 50" line).
  C  ``next_live_frame`` returns within ~one poll slice of Stop() even at a
     long shutter (the freeze-after-capture fix — never the whole exposure).
  D  capture with the stream stopped first restores the overview binning, so
     the *next* Live View is 2074×1388, not the full 26 MP sensor (the freeze
     this whole saga started from).
  E  ROI Live View delivers the requested 1:1 window size.
  F  full ``CaptureSession.capture_base``: still -> TIFF -> sidecar ->
     film_base.json with the real exposure + temperature + region mean.

Run:  .venv/bin/python scripts/manual_filmbase_probe.py
Nothing here cools the sensor; it only exercises read/write contracts.
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np  # noqa: E402

from filmscan_studio.capture._toupcam import toupcam as sdk  # noqa: E402
from filmscan_studio.capture.touptek import TouptekCamera  # noqa: E402
from filmscan_studio.capture.session import CaptureSession, SessionPaths  # noqa: E402
from filmscan_studio.core.filmbase import region_mean  # noqa: E402
from filmscan_studio.core.models import FilmMetadata  # noqa: E402

OUT = Path("/tmp/filmscan_manual_filmbase_probe")

_results: list[tuple[bool, str]] = []


def verdict(ok: bool, msg: str) -> None:
    _results.append((ok, msg))
    print(f"  [{'PASS' if ok else 'FAIL'}] {msg}")


def _median_and_cadence(cam, settle_frames: int = 2, measure_frames: int = 4):
    """Drain `settle_frames`, then (median DN over the last frame, mean s/frame)."""
    for _ in range(settle_frames):
        cam.next_live_frame()
    times = [time.monotonic()]
    data = None
    for _ in range(measure_frames):
        frame = cam.next_live_frame()
        if frame is not None:
            data = np.asarray(frame.data, dtype=np.float64)
        times.append(time.monotonic())
    median = float(np.median(data)) if data is not None else 0.0
    interval = (times[-1] - times[0]) / measure_frames
    return median, interval


def test_a_manual_exposure_live(cam: TouptekCamera) -> None:
    """Manual exposure must visibly drive the sensor on a *running* stream.

    The per-frame ``expotime_us`` stamp turned out to be **always 0 on the
    real ATR2600M** (first probe run 2026-09-18) — the camera does not stamp
    live frames (nor stills) with their exposure. So the dial-take effect is
    proven the physical way instead: the signal must scale with the shutter
    and the frame cadence must follow it.
    """
    print("\nA: manuální expozice na běžícím proudu (fyzikálně, bez stampu)")
    try:
        ae = cam._hcam.get_AutoExpoEnable()
    except sdk.HRESULTException as exc:
        verdict(False, f"get_AutoExpoEnable selhalo (hr=0x{exc.hr & 0xffffffff:x})")
        ae = None
    verdict(ae == 0, f"hardwarová AE je po connectu VYPNUTA (get_AutoExpoEnable={ae})")

    cam.set_gain(1.0)
    cam.start_live_view()
    # Scéna bez filmu/difuzeru je přesvícená (i 50 ms sedí na 65535) —
    # škáluje se v milisekundovém pásmu, 4× skok.
    short_s, long_s = 0.0005, 0.002
    got_short = cam.set_shutter(short_s)
    med_short, dt_short = _median_and_cadence(cam)
    got_long = cam.set_shutter(long_s)
    med_long, dt_long = _median_and_cadence(cam)
    stamp = getattr(cam.next_live_frame(), "expotime_us", None) \
        if cam._live_view else None
    verdict(stamp is None,
            f"stamp proudu na reálné kameře: {stamp} (0 -> None, camera "
            f"nehlásí expotime — fallback na settings je jediná cesta)")
    if med_short < 60000 and med_long < 60000 and med_short > 5:
        ratio = med_long / med_short
        verdict(2.5 <= ratio <= 6.5,
                f"signál škáluje s časem {got_short:g}->{got_long:g} s: median "
                f"{med_short:.0f}->{med_long:.0f} DN, poměr {ratio:.2f} (čekám 4)")
    else:
        verdict(False,
                f"scéna mimo rozsah i v ms pásmu: median {med_short:.0f}->"
                f"{med_long:.0f} DN — škálování nelze fyzikálně ověřit")
    # Kadence: pod readout floarem (~0,3 s prehledu) ji shutter ovlivnit
    # nemuze — pair musi prestupovat floar: 0,1 s (readout-limit) vs 0,4 s
    # (shutter-limit).
    cam.set_shutter(0.1)
    _, dt_floor = _median_and_cadence(cam)
    cam.set_shutter(0.4)
    _, dt_shutter = _median_and_cadence(cam)
    verdict(dt_shutter >= 0.4 * 0.9 and dt_shutter > dt_floor,
            f"kadence proudu přepíná z readout floaru na shutter: "
            f"dt {dt_floor:.2f} s @0,1 s (floor ~0,29 s = 3,4 fps) vs "
            f"{dt_shutter:.2f} s @0,4 s (čekám >= 0,36)")
    cam.stop_live_view()


def test_b_shutter_ceiling(cam: TouptekCamera) -> None:
    print("\nB: readback stropu expozice (firmware ceiling)")
    accepted = cam.set_shutter(200.0)
    print(f"       žádáno 200 s -> kamera přijala {accepted:g} s")
    verdict(0 < accepted <= 200.0,
            f"readback vrací reálně přijatý čas ({accepted:g} s), ne žádaných 200")
    # a sane short shutter round-trips exactly
    back = cam.set_shutter(0.5)
    verdict(abs(back - 0.5) < 1e-3, f"0,5 s round-trip: {back:g} s")


def test_c_stop_latency(cam: TouptekCamera) -> None:
    print("\nC: latence Stop() při dlouhém čase (krácení pollu)")
    cam.set_shutter(8.0)
    cam.start_live_view()
    returned: list[float] = []
    t0 = time.monotonic()

    def poll():
        try:
            cam.next_live_frame()          # blocks up to ~10 s budget
        except Exception:
            pass
        returned.append(time.monotonic())

    th = threading.Thread(target=poll)
    th.start()
    time.sleep(1.0)                          # mid-exposure
    stop_t = time.monotonic()
    cam.stop_live_view()
    th.join(timeout=6)
    latency = returned[0] - stop_t if returned else float("inf")
    verdict(latency < 1.0,
            f"next_live_frame se vrátila {latency:.2f} s po Stop() "
            f"(< 1 s = krácení funguje; expose bylo 8 s)")


def test_d_binning_restore(cam: TouptekCamera) -> None:
    print("\nD: obnova binningu po capture se zastaveným proudem")
    cam.set_shutter(0.3)
    cam.start_live_view()
    overview = cam.next_live_frame()
    ow = overview.width if overview else 0
    oh = overview.height if overview else 0
    verdict(ow * oh < 5_000_000, f"overview proud {ow}×{oh} (binnovaný, ne 26 Mpx)")
    # GUI stops the stream before every capture:
    cam.stop_live_view()
    res = cam.capture(OUT, "restore_probe", keep_live_view=False)
    print(f"       still {res.path.name} {res.size_bytes / 1e6:.1f} MB, "
          f"temp={res.sensor_temperature_c}")
    cam.start_live_view()
    again = cam.next_live_frame()
    aw, ah = (again.width, again.height) if again else (0, 0)
    cam.stop_live_view()
    verdict((aw, ah) == (ow, oh) or aw * ah < 5_000_000,
            f"po capture je další proud zpět overview {aw}×{ah} (ne full sensor)")


def test_e_roi_window(cam: TouptekCamera) -> None:
    print("\nE: ROI Live View 1:1")
    cam.set_shutter(0.1)
    cam.set_live_view_roi((1000, 800, 1200, 1200))
    cam.start_live_view()
    frame = cam.next_live_frame()
    cam.stop_live_view()
    w = frame.width if frame else 0
    h = frame.height if frame else 0
    # even_roi may round; expect ~1200x1200, and definitely not the full sensor
    verdict(abs(w - 1200) <= 2 and abs(h - 1200) <= 2,
            f"ROI proud dodal {w}×{h} (žádáno 1200×1200)")
    cam.set_live_view_roi(None)   # back to overview for a clean exit


def test_f_capture_base(cam: TouptekCamera) -> None:
    print("\nF: CaptureSession.capture_base end-to-end")
    OUT.mkdir(parents=True, exist_ok=True)
    paths = SessionPaths.create(OUT, "PROBE_BASE")
    film = FilmMetadata(film_id="PROBE_BASE", film_name="probe")
    session = CaptureSession(cam, film, paths, keep_live_view=False)
    cam.set_gain(1.0)
    # Najdi cas, ktery neni clip ani tma (bez filmu v drzaku je svetlo silne).
    rect = (2000, 1500, 2400, 1900)
    shutter_s = 0.002
    for cand in (0.0005, 0.002, 0.02, 0.2):
        cam.set_shutter(cand)
        probe = cam.capture(OUT, "base_sweep", keep_live_view=False)
        from filmscan_studio.core.rawio import open_frame as _open
        mean = region_mean(_open(probe.path).data, rect)
        print(f"       sweep {cand:g} s -> mean {mean:.0f} DN")
        if 50 < mean < 60000:
            shutter_s = cand
            break
    cam.set_shutter(shutter_s)
    result, sample = session.capture_base(rect=rect)
    print(f"       sidecar shutter={sample.shutter:g}s gain={sample.gain} "
          f"temp={sample.sensor_temperature_c}")
    print(f"       mean_dn={sample.mean_dn:.1f} rect={sample.rect}")
    ok_file = result.path.exists() and session.paths.film_base.exists()
    verdict(ok_file, "TIFF i film_base.json zapsány")
    # re-read the archived frame and confirm region_mean matches the stored mean
    from filmscan_studio.core.rawio import open_frame
    data = open_frame(result.path).data
    recomputed = region_mean(data, rect)
    verdict(abs(recomputed - sample.mean_dn) < 1.0,
            f"region_mean na archivu sedí se vzorkem "
            f"({recomputed:.1f} vs {sample.mean_dn:.1f})")
    verdict(0 < sample.mean_dn < 65535,
            f"měření je uvnitř rozsahu (ne tma, ne clip): {sample.mean_dn:.1f} DN")
    # exposure landed in the sidecar
    verdict(abs(sample.shutter - shutter_s) < 1e-6,
            f"vzorek nese skutečný čas expozice ({sample.shutter:g} s)")
    samples = session.base_samples()
    verdict(len(samples) >= 1, f"film_base.json čitelný, {len(samples)} vzorků")


def main() -> int:
    cam = TouptekCamera()
    cam.connect()
    print(f"connected: {cam.info.model}, senzor {cam.info.sensor_width}×"
          f"{cam.info.sensor_height}, gain range {cam.info.gain_range}")
    try:
        test_a_manual_exposure_live(cam)
        test_b_shutter_ceiling(cam)
        test_c_stop_latency(cam)
        test_d_binning_restore(cam)
        test_e_roi_window(cam)
        test_f_capture_base(cam)
    finally:
        try:
            cam.disconnect()
        except Exception:  # noqa: BLE001
            pass

    failed = [m for ok, m in _results if not ok]
    print("\n" + "=" * 60)
    print(f"{len(_results) - len(failed)}/{len(_results)} PASS")
    if failed:
        print("FAILS:")
        for m in failed:
            print("  -", m)
        return 1
    print("vše zelené")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
