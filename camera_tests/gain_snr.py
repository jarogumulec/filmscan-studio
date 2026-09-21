"""(d) Optimalizace gain × SNR (na 1 snímku), gain 0.1 → 10×.

Otázky:
  1. Je gain 0.1 jen matematická operace, nebo reálná vlastnost čipu?
     - Pokud je analogový PŘED AD převodníkem, mění se i rozlišení podlahy:
       readout sigma v DN bude s gainem klesat ~úměrně (sigma_e zůstane
       konstantní). Čistě digitální násobek by nechal sigma_e růst s 1/g.
  2. Je při EXPozIČNĚ KOMPENZOVANÉ expozici (stejný signál ~26 000 DN)
     na SNR zisk vidět? Fotonový šum roste se skutečnými fotony,
     tj. při nízkém gainu potřebujeme delší čas => MORE fotonů => MORE
     shot noise => SNR by mělo KLESAT s klesajícím gainem.

Design:
  - gain body: 0.1, 0.2, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0 (readback).
  - TEC +10 °C (stejně jako (b)), TEPELNÝ PODKLAD 100 µs (světlo zhasnuté,
     2 snímky) — podlaha. Pokud je tma nedostupná, body jen chybí.
  - bílé pole: exponuj na ~26 000 DN hledáním času (kvantování ~75 µs
     bereme jako nejmenší přebytek); 3 snímky => SNR(1) = mean/std,
     temporalni sigma = std(b-a)/sqrt(2) => odhad elektronu.
  - pri gainu >= ~3 nelzi pri hw danimu 100 us udrzet 26 000 DN (nej nizsi
     kvant. schod nasiti) — bod se vyfoti na nej nizsim ne-saturovanem
     casu a DN se upozorneni v tabulce.

Vystupy: data/gain_snr.csv, gain_snr.png, stdout.

Run:  .venv/bin/python camera_tests/gain_snr.py
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
    DATA, center_quarter, connect, set_expo_us, wait_temperature,
)

GAINS: list[float] = [0.1, 0.2, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0]
TARGET_C = 10.0
TARGET_DN = 26000.0
SAT_LIMIT = 58000.0          # medián nad => považujeme za saturaci
FRAMES_WHITE = 3

OUT = Path(__file__).resolve().parent


def read_roi(path: Path) -> np.ndarray:
    from filmscan_studio.core.rawio import open_frame
    frame = open_frame(path)
    return center_quarter(frame.data.astype(np.float32))


def expose_roi(cam, target_dn: float, sat_limit: float, max_iter: int = 10):
    """Hledej čas na bílém poli pro aktuální gain; vrať (us, median_dn).

    Bere nejlepší pod-saturaci; při vysokých gainech nemůže pod 100 µs —
    pak vrací nejnižší ne-saturovaný schod (nebo None pokud neexistuje).
    """
    cam.start_live_view()
    try:
        shutter_us = max(100.0, TARGET_DN / 170.0)  # start dle kalibrace gain 1
        seen: set[int] = set()
        best = None
        for _ in range(max_iter):
            us = max(100, round(shutter_us))
            if us in seen:
                break
            seen.add(us)
            set_expo_us(cam, us)
            for _ in range(3):
                cam.next_live_frame()
            med = float(np.median(center_quarter(
                cam.next_live_frame().data.astype(np.float32))))
            print(f"[expose g={cam.get_settings().gain:5.2f}] "
                  f"{us:7} us -> {med:7.0f} DN", flush=True)
            if med < sat_limit and (best is None or abs(med - target_dn)
                                    < abs(best[1] - target_dn)):
                best = (us, med)
            if med <= 1.0:
                shutter_us = us * 20
                continue
            shutter_us = us * target_dn / med
        return best
    finally:
        cam.stop_live_view()


def shoot(cam, path: Path) -> np.ndarray:
    res = cam.capture(path.parent, path.stem, keep_live_view=False)
    return read_roi(res.path)


def main() -> None:
    DATA.mkdir(exist_ok=True)
    frames_dir = DATA / "gain_frames"
    frames_dir.mkdir(exist_ok=True)
    cam = connect()
    wait_temperature(cam, TARGET_C, label=" g")

    rows = []
    for g in GAINS:
        applied = cam.set_gain(g)
        print(f"\n=== gain {g} (applied {applied:.2f}) ===", flush=True)

        # --- podlaha: 100 µs, světlo zhasnuté (ověřeno dvojexpozicí) ------
        # "tma" = medián se zdvojnásobením času NEVYROSTE (světlo by rostlo
        # lineárně); bez toho by 100 µs na osvětleném bílém poli při gainu
        # 0.1 (~1 750 DN) vypadalo jako temný snímek.
        floor_pt = None
        set_expo_us(cam, 100)
        d1 = shoot(cam, frames_dir / f"g{applied:.2f}_dark1.tif")
        set_expo_us(cam, 300)
        d3 = shoot(cam, frames_dir / f"g{applied:.2f}_dark300us.tif")
        dmed = float(np.median(d1))
        m3 = float(np.median(d3))
        # test klame, když je sám referenční 300µs snímek u saturace
        # (65535 < 1,5·44 500) — falešná „tma". Pravá tma musí být i
        # absolutně nízko.
        dark = m3 < 1.5 * dmed + 100 and dmed < 2000.0
        if dark:
            set_expo_us(cam, 100)
            d2 = shoot(cam, frames_dir / f"g{applied:.2f}_dark2.tif")
            floor_pt = (dmed,
                        float(np.std((d2 - d1) / np.sqrt(2))),
                        float(np.std(d1)))
            print(f"[floor] tma: median {dmed:.0f} DN, "
                  f"temporal {floor_pt[1]:.0f} DN", flush=True)
        else:
            print(f"[floor] není tma (100→300 µs: {dmed:.0f}→{m3:.0f} DN) "
                  f"— podlahu přeskočuji", flush=True)

        # --- bílé pole na kompenzovaný čas ---------------------------------
        best = expose_roi(cam, TARGET_DN, SAT_LIMIT)
        if best is None:
            print(f"[white] gain {applied}: nelze pod saturaci — skip", flush=True)
            continue
        us, _ = best
        set_expo_us(cam, us)
        rois = [shoot(cam, frames_dir / f"g{applied:.2f}_w{i}.tif")
                for i in range(FRAMES_WHITE)]
        means = np.array([float(r.mean()) for r in rois])
        stds = np.array([float(r.std()) for r in rois])
        snr1 = float(means.mean() / stds.mean())
        temporal = float(np.mean([
            np.std((rois[i + 1] - rois[i]) / np.sqrt(2))
            for i in range(FRAMES_WHITE - 1)]))
        sat = "SAT?" if means.mean() > SAT_LIMIT else ""
        print(f"[white] {us} us: mean {means.mean():.0f} DN, std {stds.mean():.0f} DN, "
              f"SNR(1) {snr1:.1f}, temporal {temporal:.0f} DN {sat}", flush=True)

        rows.append({
            "gain": applied, "expo_us": us,
            "mean_dn": round(means.mean(), 1), "std_dn": round(stds.mean(), 1),
            "snr1": round(snr1, 2), "temporal_dn": round(temporal, 1),
            "dark_median_dn": round(floor_pt[0], 1) if floor_pt else "",
            "dark_temporal_dn": round(floor_pt[1], 1) if floor_pt else "",
            "saturated": sat,
        })
        with open(DATA / "gain_snr.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    cam.set_gain(1.0)

    # ---------------- graf -------------------------------------------------
    if not rows:
        print("bez dat — žádný graf")
        return
    gains = np.array([r["gain"] for r in rows])
    snr = np.array([r["snr1"] for r in rows])
    temp = np.array([r["temporal_dn"] for r in rows], dtype=float)
    dtemp = np.array([float(r["dark_temporal_dn"]) for r in rows
                      if r["dark_temporal_dn"] != ""], dtype=float)
    dgain = np.array([r["gain"] for r in rows
                      if r["dark_temporal_dn"] != ""])

    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    ax = axes[0]
    ax.plot(gains, snr, "o-", color="tab:blue", label="SNR(1) bílé pole")
    for r in rows:
        if r["saturated"]:
            ax.axvline(r["gain"], color="red", ls=":", alpha=0.4)
    ax.set_xscale("log")
    ax.set_xticks(gains)
    ax.set_xticklabels([f"{g:g}" for g in gains])
    ax.set_xlabel("gain [×]")
    ax.set_ylabel("SNR (mean/std, středová čtvrtina)")
    ax.set_title("(d) gain × SNR při expozičně kompenzované expozici\n"
                 f"(stejný signál ~{TARGET_DN:.0f} DN, TEPC {TARGET_C:+.0f} °C)")
    ax.grid(alpha=0.3)
    ax.legend()

    ax = axes[1]
    ax.plot(gains, temp, "s-", color="tab:orange",
            label="temporal σ bílé pole [DN]")
    if dtemp.size:
        ax.plot(dgain, dtemp, "^--", color="tab:gray",
                label="temporal σ podlahy [DN]")
        ref = dtemp[0] * dgain[0] / dgain  # škálování 1/g, norm. na první bod
        ax.plot(dgain, ref, ":", color="tab:gray", alpha=0.6,
                label="model σ∝1/gain (analogový před ADC)")
    ax.set_xscale("log")
    ax.set_xticks(gains)
    ax.set_xticklabels([f"{g:g}" for g in gains])
    ax.set_xlabel("gain [×]")
    ax.set_ylabel("σ [DN]")
    ax.set_title("Temporální šum v DN\n(pokud σ podlahy klesá ∝1/gain → "
                 "analogový gain, podlaha se rozlišuje lépe)")
    ax.grid(alpha=0.3)
    ax.legend()

    fig.tight_layout()
    fig.savefig(OUT / "gain_snr.png", dpi=130)
    print(f"\n[out] {OUT / 'gain_snr.png'}, {DATA / 'gain_snr.csv'}")


if __name__ == "__main__":
    main()
