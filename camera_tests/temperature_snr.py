"""(c) Optimalizace teplota × SNR (na 1 snímek).

Rozsah: bez chlazení (pokojová) → +20, +15, +10, +5, 0, −5 °C.

Na bílém homogenním pozadí je SNR jednoho snímku řízen głównie fotony a
readoutem — termický šum tam hraje malou roli. Proto skript měří DVA
úhly pohledu:

  1. SNR bílého pole (1 snímek) v každé teplotě — praktická metrika.
  2. TEMPORÁLNÍ šum: sigma mezi dvěma po sobě jdoucími snímky ve tmě?
     Ne — v osvitove nemame tmu. Merime per-pixel temporalni sigma
     (rozdil dvou sousnich expozic / sqrt(2)) — ten obsahuje i termicky
     slozku; pokles s teplotou ukazuje, kde TEC ma vubec co delat.

Kazdy bod: nastav cil,-cekej na ustareni, porid 2 snimky (gain 1, cas z
exposometrickeho hledani pri prvnim bodu — stejny osvit vsude).

Vystupy: data/temperature_snr.csv, temperature_snr.png, stdout.

Run:  .venv/bin/python camera_tests/temperature_snr.py
"""

from __future__ import annotations

import csv
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from _common import (
    DATA, center_quarter, connect, set_expo_us, wait_temperature,
)

#: None = TEC vypnuty (pokojova); pak sestupne po 5 stupnich.
SETPOINTS_C: list[float | None] = [None, 20.0, 15.0, 10.0, 5.0, 0.0, -5.0]
TARGET_DN = 26000.0
FRAMES_PER_POINT = 2

OUT = Path(__file__).resolve().parent


def read_roi(path: Path) -> np.ndarray:
    from filmscan_studio.core.rawio import open_frame
    frame = open_frame(path)
    return center_quarter(frame.data.astype(np.float32))


def main() -> None:
    DATA.mkdir(exist_ok=True)
    frames_dir = DATA / "temperature_frames"
    frames_dir.mkdir(exist_ok=True)
    cam = connect()
    cam.set_gain(1.0)

    # Same exposure everywhere; measured once at the first (warm) point.
    from _common import expose_to_target
    shutter_us = round(expose_to_target(cam, TARGET_DN) * 1e6)
    print(f"[expo] {shutter_us} us pro vsechny body", flush=True)

    rows = []
    for i, sp in enumerate(SETPOINTS_C):
        label = f" {i + 1}/{len(SETPOINTS_C)}"
        if sp is None:
            cam.set_tec_enabled(False)
        else:
            cam.set_tec_enabled(True)
            cam.set_target_temperature_c(sp)
        temp = wait_temperature(cam, sp, tol_c=0.4, timeout_s=900, label=label)
        set_expo_us(cam, shutter_us)
        cam.set_gain(1.0)

        shots = []
        temps = []
        for j in range(FRAMES_PER_POINT):
            res = cam.capture(frames_dir,
                              f"t{i}_{sp if sp is not None else 'off'}_{j}",
                              keep_live_view=False)
            shots.append(read_roi(res.path))
            temps.append(res.sensor_temperature_c)
        a, b = shots[0], shots[1]
        signal = float((a.mean() + b.mean()) / 2)
        sigma_single = float((a.std() + b.std()) / 2)
        # temporalni slozka: sigma_between = std(b - a) / sqrt(2)
        sigma_temp = float((b - a).std() / np.sqrt(2.0))
        snr = signal / sigma_single if sigma_single else float("nan")
        rows.append({
            "setpoint": "off" if sp is None else f"{sp:+.0f}",
            "temp_c": round(float(np.mean(temps)), 2),
            "signal_dn": round(signal, 1),
            "sigma_total_dn": round(sigma_single, 2),
            "sigma_temporal_dn": round(sigma_temp, 2),
            "snr_1frame": round(snr, 1),
            "snr_db": round(20 * np.log10(snr), 2),
        })
        print(f"[{label}] senzor {np.mean(temps):+6.1f} C  "
              f"DN {signal:7.0f}  sigma {sigma_single:6.2f} "
              f"(temp {sigma_temp:5.2f})  SNR {snr:7.1f}", flush=True)

    cam.set_tec_enabled(False)
    cam.disconnect()

    with open(DATA / "temperature_snr.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)

    temps = np.array([r["temp_c"] for r in rows])
    snr = np.array([r["snr_1frame"] for r in rows])
    stemp = np.array([r["sigma_temporal_dn"] for r in rows])

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.5))
    order = np.argsort(temps)
    ax1.plot(temps[order], snr[order], "o-")
    for t, s in zip(temps, snr):
        ax1.annotate(f"{t:+.0f}°", (t, s), textcoords="offset points",
                     xytext=(0, 6), ha="center", fontsize=7)
    ax1.set_xlabel("teplota senzoru [°C]")
    ax1.set_ylabel("SNR 1 snímku [mean/σ]")
    ax1.set_title("Teplota × SNR (1 snímek, bílé pozadí, gain 1×)")
    ax1.grid(True, alpha=0.3)
    ax2.plot(temps[order], stemp[order], "s-", color="tab:red")
    ax2.set_xlabel("teplota senzoru [°C]")
    ax2.set_ylabel("temporální σ [DN]")
    ax2.set_title("Časová složka šumu (z rozdílu 2 expozic)")
    ax2.grid(True, alpha=0.3)
    fig.tight_layout(); fig.savefig(OUT / "temperature_snr.png"); plt.close(fig)

    print("\n=== VERDIKT ===")
    best = max(rows, key=lambda r: r["snr_1frame"])
    worst = min(rows, key=lambda r: r["snr_1frame"])
    print(f"nejlepší: {best['temp_c']:+.1f} °C (SNR {best['snr_1frame']}), "
          f"nejhorší: {worst['temp_c']:+.1f} °C (SNR {worst['snr_1frame']})")
    spread_db = 20 * np.log10(best["snr_1frame"] / worst["snr_1frame"])
    print(f"rozdíl {spread_db:.2f} dB — při bright poli je fotony strčí do "
          "všeho; viz temporální σ (ta s teplotou klesá citelněji).")


if __name__ == "__main__":
    main()
