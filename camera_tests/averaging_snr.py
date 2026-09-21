"""(b) Averaging: jak roste SNR s počtem průměrovaných snímků.

Podmínky uživatele: gain 1,00x, TEC na +10 °C, bílé homogenní pozadí,
~30 snímků, měřeno ve středové čtvrtině snímku (hrana = 1/2 rozměru).

Metrika (pro každý K = 1..N, průměr prvních K snímků):
  signal  = střední hodnota DN výřezu
  sigma   = smerodat. hodnota DN výřesu (space + cas)
  SNR     = signal / sigma
Druha krivka cisti "casovy" sum: per-pixel sigma napric snimkami (fixni
vzor osvetleni/vignety se prumerovanim nevypravi — ten zůstává, proto SNR
muze saturat pod Cistou sqrt(K) idealizaci).

Vystupy: data/averaging_snr.csv, data/averaging_mean.npy,
averaging_snr.png, stipky na stdout.

Run:  .venv/bin/python camera_tests/averaging_snr.py
"""

from __future__ import annotations

import csv
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from _common import (
    DATA, WHITE, center_quarter, connect, expose_to_target, wait_temperature,
)

N_FRAMES = 30
TARGET_C = 10.0
TARGET_DN = 26000.0     # ~40 % plne laodne, pod kvantizacnim krokem k saturaci

OUT = Path(__file__).resolve().parent
FRAMES_DIR = DATA / "averaging_frames"


def main() -> None:
    DATA.mkdir(exist_ok=True)
    FRAMES_DIR.mkdir(exist_ok=True)
    cam = connect()
    cam.set_gain(1.0)
    cam.set_tec_enabled(True)
    cam.set_target_temperature_c(TARGET_C)
    temp = wait_temperature(cam, TARGET_C, label=" b")
    shutter = expose_to_target(cam, TARGET_DN)
    cam.set_gain(1.0)

    stack = []
    for i in range(1, N_FRAMES + 1):
        t0 = time.monotonic()
        res = cam.capture(FRAMES_DIR, f"avg_{i:02d}", keep_live_view=False)
        frame = read_roi(res.path)
        stack.append(frame)
        print(f"[{i:2d}/{N_FRAMES}] {res.path.name} "
              f"med {np.median(frame):7.0f} DN  temp {res.sensor_temperature_c:+.1f} C "
              f"({time.monotonic() - t0:.1f} s)", flush=True)
    cam.disconnect()

    stack = np.stack(stack)                      # (N, h, w) float32
    np.save(DATA / "averaging_mean.npy", stack.mean(axis=0).astype(np.float32))

    signal = float(stack.mean())
    # per-pixel temporalni sigma (pres snimky), prumer přes pixely
    sigma_temp = float(stack.std(axis=0, ddof=1).mean())
    sigma_single = float(stack[0].std())         # priestorova incl. fixniho vzoru

    rows = []
    for k in range(1, N_FRAMES + 1):
        avg = stack[:k].mean(axis=0)
        mu = float(avg.mean())
        sigma_tot = float(avg.std())
        snr_tot = mu / sigma_tot if sigma_tot else float("nan")
        # cisty sum: fixni vzor odecten — sigma prumeru klesa 1/sqrt(K)
        sigma_k = sigma_temp / np.sqrt(k)
        fp_var = max(0.0, sigma_single ** 2 - sigma_temp ** 2)
        sigma_pred = float(np.sqrt(sigma_k ** 2 + fp_var))
        rows.append({
            "k": k, "signal_dn": round(mu, 2),
            "sigma_total_dn": round(sigma_tot, 2),
            "snr_total": round(snr_tot, 1),
            "snr_total_db": round(20 * np.log10(snr_tot), 2),
            "sigma_pred_dn": round(sigma_pred, 2),
            "snr_pred": round(mu / sigma_pred, 1),
        })
        print(f"K={k:2d}  signal {mu:8.1f} DN  sigma {sigma_tot:7.2f}  "
              f"SNR {snr_tot:8.1f} ({20 * np.log10(snr_tot):5.1f} dB)",
              flush=True)

    with open(DATA / "averaging_snr.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)

    k = np.array([r["k"] for r in rows])
    snr = np.array([r["snr_total"] for r in rows])
    snr_pred = np.array([r["snr_pred"] for r in rows])
    ideal = snr[0] * np.sqrt(k)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.5))
    ax1.plot(k, snr, "o-", ms=4, label="měřeno (centrální čtvrtina)")
    ax1.plot(k, snr_pred, "--", color="tab:orange",
             label="model: čas. šum/√K + fixní vzor")
    ax1.plot(k, ideal, ":", color="gray", label="ideální √K (bez fix. vzoru)")
    ax1.set_xlabel("K — počet průměrovaných snímků")
    ax1.set_ylabel("SNR = mean/σ [lineárně]")
    ax1.set_title(f"Averaging SNR — gain 1×, {TARGET_C:+.0f} °C, "
                  f"{shutter * 1e6:.0f} µs")
    ax1.legend(fontsize=8); ax1.grid(True, alpha=0.3)

    db = 20 * np.log10(snr / snr[0])
    ax2.plot(k, db, "o-", ms=4, label="měřeno, relativně ke K=1")
    ax2.plot(k, 10 * np.log10(k), ":", color="gray",
             label="ideální +10·log₁₀K")
    ax2.set_xlabel("K"); ax2.set_ylabel("[dB]")
    ax2.set_title("Zisk averageingu (dB nad jeden snímek)")
    ax2.legend(fontsize=8); ax2.grid(True, alpha=0.3)
    fig.tight_layout(); fig.savefig(OUT / "averaging_snr.png"); plt.close(fig)

    print("\n=== VERDIKT ===")
    print(f"signál {signal:.0f} DN, čas. σ jednoho snímku {sigma_temp:.2f} DN, "
          f"fixní vzor σ {np.sqrt(max(0.0, sigma_single**2 - sigma_temp**2)):.2f} DN")
    print(f"SNR(K=1) {rows[0]['snr_total']}, SNR(K={N_FRAMES}) "
          f"{rows[-1]['snr_total']} = "
          f"{20 * np.log10(rows[-1]['snr_total'] / rows[0]['snr_total']):.1f} dB "
          f"(ideál {10 * np.log10(N_FRAMES):.1f} dB)")


def read_roi(path: Path) -> np.ndarray:
    """Středová čtvrtina (plochově) stillu jako float32."""
    from filmscan_studio.core.rawio import open_frame
    frame = open_frame(path)
    return center_quarter(frame.data.astype(np.float32))


if __name__ == "__main__":
    main()
