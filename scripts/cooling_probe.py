"""Hardware probe: TEC cooling + live view fps on the real ATR2600M.

Three minutes of binned-overview Live View with TEC on, temperature sampled
every 15 s, then one full-sensor still to prove the RAW capture path and its
recorded temperature. Run: .venv/bin/python scripts/cooling_probe.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from filmscan_studio.capture.touptek import TouptekCamera  # noqa: E402

TARGET_C = -5.0
SECONDS = 180
SAMPLE_EVERY = 15


def main() -> None:
    cam = TouptekCamera()
    cam.connect()
    print(f"connected: {cam.info.model}, gain range {cam.info.gain_range}")
    cam.set_tec_enabled(True)
    applied = cam.set_target_temperature_c(TARGET_C)
    print(f"TEC on, target set to {applied:+.1f} C")
    cam.start_live_view()
    print(f"{'t [s]':>6} {'temp [C]':>9} {'fps':>6}")
    started = time.monotonic()
    frames = 0
    fps_window_frames = 0
    fps_window_start = time.monotonic()
    fps_last = 0.0
    next_sample = 0.0
    while (elapsed := time.monotonic() - started) < SECONDS:
        frame = cam.next_live_frame()
        frames += 1
        fps_window_frames += 1
        if time.monotonic() - fps_window_start >= 5.0:
            fps_last = fps_window_frames / (time.monotonic() - fps_window_start)
            fps_window_frames = 0
            fps_window_start = time.monotonic()
        if elapsed >= next_sample:
            temp = cam.get_temperature_c()
            print(f"{elapsed:6.0f} {temp if temp is not None else float('nan'):9.1f} "
                  f"{fps_last:6.1f}")
            next_sample += SAMPLE_EVERY
    cam.stop_live_view()
    print(f"live view: {frames} frames in {SECONDS} s "
          f"= {frames / SECONDS:.1f} fps average, {fps_last:.1f} fps steady")

    print("capturing one full-sensor still at 1 s / gain 1.00x ...")
    cam.set_gain(1.0)
    cam.set_shutter(1.0)
    out = cam.capture(Path("/tmp/filmscan_probe"), "cooling_probe",
                      keep_live_view=False)
    print(f"still: {out.path.name} {out.size_bytes / 1e6:.1f} MB "
          f"temp={out.sensor_temperature_c} elapsed={out.elapsed:.1f}s")
    if out.notes:
        print("notes:", out.notes)
    cam.disconnect()
    print("done")


if __name__ == "__main__":
    main()
