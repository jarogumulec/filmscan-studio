"""(a) Jaká je skutečná nejkratší expozice ATR2600M? SW vs. HW limit.

Aplikace clampuje čas na 300 us (~1/3333) — konstanta EXPO_TIME_RANGE_US v
capture/touptek.py je to, co vidí operátor jako dno ("nepustilo kratši nez
1/3330"). Konstanta je ale odhad, ne měření. Tento skript JDE PŘÍMO ZA SDK
přes `hcam.put_ExpoTime` (obchází clamp aplikace, nic v ní nemění) a měří:

  1. CO FIRMWARE VRÁTÍ: put_ExpoTime(v) -> get_ExpoTime() pro v od 1 us nahoru
     (still, bez proudu) — klame firmware, nebo bere pod 300 us?
  2. CO SKUTEČNĚ EXPONUJE: proud s ROI 1200x1200, vzorky expotime stampů
     z info rámčků (info.v3.expotime) — firemní readback může lhát.
  3. Linearitu signálu: medián DN vs. žádaný čas na bílém poli — exponuje-li
     kamera skutečně kratše, DN klesají s časem; kde to přestane platit,
     tam je skutečné dno.
  4. Rámec: FRAMEINTERVAL_MIN / MAX_PRECISE_FRAMERATE v ROI módu — i kdyby
     šlo 1 us, readout+USB může být reálné dno.

Výstupy: data/min_exposure.csv, min_exposure_linearity.png,
min_exposure_applied.png, shrnutí na stdout.

Run:  .venv/bin/python camera_tests/probe_min_exposure.py
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from _common import DATA, WHITE, center_quarter, connect

DATA.mkdir(exist_ok=True)

#: Žádané časy v us — od 1 us přes domnělých 300 us až po rozumnou rezervu.
REQUEST_US = [1, 2, 5, 10, 20, 30, 50, 100, 150, 200, 250, 300,
              400, 500, 750, 1000, 2000, 5000, 10000, 20000]
ROI = (2000, 1200, 1200, 1200)      # x, y, w, h — rychlý readout pro kr. časy
STAMP_SAMPLES = 5                    # frame stampů na jeden žádaný čas
#: Bílé pozadí ~170 DN/us => linearita je měřitelná do ~350 us, pak saturace.
LINEARITY_US = [100, 125, 150, 200, 250, 300, 325, 350, 400]


def probe_readback(cam) -> list[dict]:
    """put_ExpoTime -> get_ExpoTime bez proudu: co firmware přijme.

    Firmware může čas rovnou ODMÍTNOUT (E_INVALIDARG) — to je tvrdé hw dno,
    ne jen clamp. Každá hodnota se zkouší zvlášť.
    """
    from filmscan_studio.capture._toupcam.toupcam import HRESULTException
    rows = []
    for us in REQUEST_US:
        try:
            cam._hcam.put_ExpoTime(us)
        except HRESULTException as exc:
            rows.append({"requested_us": us, "readback_us": None,
                         "error": f"0x{exc.hr & 0xffffffff:x}"})
            print(f"  put {us:6} us -> ODMÍTNUTO "
                  f"(0x{exc.hr & 0xffffffff:x})", flush=True)
            continue
        got = cam._hcam.get_ExpoTime()
        rows.append({"requested_us": us, "readback_us": int(got), "error": ""})
        flag = "" if got == us else f"  <- firmware vrátil {got}"
        print(f"  put {us:6} us -> get {got:6}{flag}", flush=True)
    return rows


def probe_stamps(cam) -> list[dict]:
    """Skutečná expozice v proudu: stampy z info rámčků, ROI mód."""
    cam.set_live_view_roi(ROI)
    cam.start_live_view()
    from filmscan_studio.capture._toupcam.toupcam import HRESULTException
    rows = []
    try:
        for us in REQUEST_US:
            try:
                cam._hcam.put_ExpoTime(us)
            except HRESULTException as exc:
                rows.append({"requested_us": us, "stamp_median_us": None,
                             "fps": 0.0, "error": f"0x{exc.hr & 0xffffffff:x}"})
                print(f"  {us:6} us -> put ODMÍTNUTO", flush=True)
                continue
            # Live rámce na tomto modelu hlásí expotime = 0 (měřeno) —
            # kadenci proto měříme počtem/frame za časové okno; stampy
            # sbíráme jen pokud je kamera skutečně plní.
            stamps, frames, ts = [], 0, time.monotonic()
            while time.monotonic() - ts < 3.0:
                frame = cam.next_live_frame()
                frames += 1
                if frame.expotime_us:
                    stamps.append(frame.expotime_us)
            fps = frames / 3.0
            med = float(np.median(stamps)) if stamps else None
            rows.append({"requested_us": us, "stamp_median_us": med,
                         "fps": round(fps, 1)})
            stamp_s = f"stamp med {med:7.1f} us" if stamps else "stamp 0     "
            print(f"  {us:6} us -> {frames:4} frame/3 s = {fps:5.1f} fps, "
                  f"{stamp_s}", flush=True)
    finally:
        cam.stop_live_view()
    return rows


def probe_linearity(cam, target_dn: float = 20000.0) -> list[dict]:
    """DN vs. čas: exponuje kamera v ROI módu skutečně lineárně dolů?"""
    rows = []
    cam.set_live_view_roi(ROI)
    cam.start_live_view()
    try:
        for us in LINEARITY_US:
            cam._hcam.put_ExpoTime(us)
            for _ in range(3):                    # zahodni prvním snímky
                cam.next_live_frame()
            med = None
            for _ in range(3):
                frame = cam.next_live_frame()
                med = float(np.median(center_quarter(frame.data)))
            rows.append({"requested_us": us, "median_dn": med,
                         "dn_per_us": (med / us) if med else None})
            print(f"  {us:6} us -> median {med:8.1f} DN "
                  f"({med / us:7.2f} DN/us)", flush=True)
    finally:
        cam.stop_live_view()
        cam.set_live_view_roi(None)
    return rows


def probe_fine_ladder(cam, target_dn: float = 20000.0) -> list[dict]:
    """Jemý sweep 100–450 us v PREHLEDU (plný readout jako pri archivu).

    Otazka z hrubeho ROI sweepu: pod ~400 us DN nesleduji Cas spojite, ale
    po skaocich schodech (~12 300 DN). Je to kvantovani expozicniho casu?
    Merge do jedne tabulkyCas -> uroven.
    """
    from filmscan_studio.capture._toupcam.toupcam import HRESULTException
    us_values = list(range(100, 475, 25))
    cam.set_live_view_roi(None)
    cam.start_live_view()
    rows = []
    try:
        for us in us_values:
            try:
                cam._hcam.put_ExpoTime(us)
            except HRESULTException as exc:
                rows.append({"requested_us": us, "median_dn": None,
                             "error": f"0x{exc.hr & 0xffffffff:x}"})
                print(f"  {us:4} us -> ODMÍTNUTO", flush=True)
                continue
            for _ in range(3):
                cam.next_live_frame()
            meds = []
            for _ in range(3):
                frame = cam.next_live_frame()
                meds.append(float(np.median(center_quarter(frame.data))))
            med = float(np.median(meds))
            rows.append({"requested_us": us, "median_dn": med, "error": ""})
            print(f"  {us:4} us -> median {med:8.1f} DN", flush=True)
    finally:
        cam.stop_live_view()
    return rows


def main() -> None:
    cam = connect()
    cam.set_gain(1.0)
    # TEC nezapínáme — expoziční spod je na teplotě nezávislý.
    from filmscan_studio.capture._toupcam import toupcam as sdk
    extras = {}
    for name in ("FRAMEINTERVAL_MIN", "FRAMEINTERVAL_MAX",
                 "MAX_PRECISE_FRAMERATE", "MIN_PRECISE_FRAMERATE"):
        try:
            extras[name] = cam._hcam.get_Option(getattr(sdk, f"TOUPCAM_OPTION_{name}"))
        except sdk.HRESULTException as exc:
            extras[name] = f"E_INVALIDARG 0x{exc.hr & 0xffffffff:x}"
    print("options:", extras, flush=True)

    print("\n== 1. readback bez proudu ==", flush=True)
    readback = probe_readback(cam)

    print("\n== 2. expotime stampy v proudu (ROI) ==", flush=True)
    stamps = probe_stamps(cam)

    print("\n== 3. linearita DN vs. čas (ROI) ==", flush=True)
    lin = probe_linearity(cam)

    print("\n== 4. jemý sweep 100-475 us, plny readout (prehled) ==",
          flush=True)
    fine = probe_fine_ladder(cam)

    # ---------- ulož data ----------
    with open(DATA / "min_exposure_readback.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(readback[0]))
        w.writeheader(); w.writerows(readback)
    with open(DATA / "min_exposure_stamps.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(stamps[0]))
        w.writeheader(); w.writerows(stamps)
    with open(DATA / "min_exposure_linearity.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(lin[0]))
        w.writeheader(); w.writerows(lin)
    with open(DATA / "min_exposure_fine.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(fine[0]))
        w.writeheader(); w.writerows(fine)
    (DATA / "min_exposure_meta.json").write_text(json.dumps(
        {"options": {k: str(v) for k, v in extras.items()}}, indent=2))

    # ---------- grafy ----------
    fig, ax = plt.subplots(figsize=(7, 5))
    req = [r["requested_us"] for r in readback]
    got = [r["readback_us"] for r in readback]
    ax.plot(req, req, "k--", lw=0.8, label="ideal (vrací = žádáno)")
    ax.step(req, got, where="mid", marker="o", ms=4, label="readback")
    sm = [(r["requested_us"], r["stamp_median_us"]) for r in stamps
          if r["stamp_median_us"]]
    ax.plot([a for a, _ in sm], [b for _, b in sm], "s", ms=5,
            color="tab:red", label="skutečná expozice (stamp)")
    ax.axvline(300, color="tab:blue", ls=":", label="SW clamp aplikace 300 µs")
    ax.set_xscale("log"); ax.set_yscale("log")
    ax.set_xlabel("žádaný čas [µs]"); ax.set_ylabel("skutečný čas [µs]")
    ax.set_title("ATR2600M: co firmware vezme vs. co expenuje")
    ax.legend(fontsize=8); ax.grid(True, which="both", alpha=0.3)
    fig.tight_layout(); fig.savefig(HERE_PNG("applied")); plt.close(fig)

    fig, ax = plt.subplots(figsize=(7, 4.5))
    t_us = [r["requested_us"] for r in lin]
    dn = [r["median_dn"] for r in lin]
    ax.plot([t / 1000 for t in t_us], dn, "o-", label="medián DN (střed čtvrtiny)")
    k = np.polyfit(np.array(t_us), np.array(dn), 1)
    fit = np.polyval(k, np.array(t_us))
    ax.plot([t / 1000 for t in t_us], fit, "--", color="gray",
            label=f"lineární fit ({k[0]:.1f} DN/ms)")
    ax.axvline(0.3, color="tab:blue", ls=":", label="SW clamp 300 µs")
    ax.set_xlabel("žádaný čas [ms]"); ax.set_ylabel("median [DN]")
    ax.set_title("Linearita signálu ke spodku expozice (bílý podklad)")
    ax.legend(fontsize=8); ax.grid(True, alpha=0.3)
    fig.tight_layout(); fig.savefig(HERE_PNG("linearity")); plt.close(fig)

    fig, ax = plt.subplots(figsize=(7, 4.5))
    fu = [r["requested_us"] for r in fine if r["median_dn"] is not None]
    fd = [r["median_dn"] for r in fine if r["median_dn"] is not None]
    ax.step(fu, fd, where="post", marker="o", ms=4,
            label="medián DN (plný readout)")
    ax.axvline(100, color="tab:red", ls=":", label="hw dno kamery 100 µs")
    ax.axvline(300, color="tab:blue", ls=":", label="SW clamp aplikace 300 µs")
    ax.set_xlabel("žádaný čas [µs]"); ax.set_ylabel("median [DN]")
    ax.set_title("Kvantizace krátkých expozic: DN po skocích ~12 150 DN")
    ax.legend(fontsize=8); ax.grid(True, alpha=0.3)
    fig.tight_layout(); fig.savefig(HERE_PNG("fine")); plt.close(fig)

    # ---------- verdikt ----------
    accepted = [r["requested_us"] for r in readback
                if r["readback_us"] == r["requested_us"]]
    min_hw = min(accepted) if accepted else None
    print("\n=== VERDIKT ===")
    print(f"nejkratší akceptovaný čas (readback): {min_hw} us")
    below = [r for r in readback if r["requested_us"] < 300
             and r["readback_us"] == r["requested_us"]]
    print(f"pod 300 us akceptováno: {len(below)}/"
          f"{sum(1 for r in readback if r['requested_us'] < 300)}")
    print("→ SW limit (EXPO_TIME_RANGE_US ve `touptek.py`) je dno aplikace; "
          "hw dno určuje tento měření (viz grafy).")


def HERE_PNG(kind: str) -> Path:
    return Path(__file__).resolve().parent / f"min_exposure_{kind}.png"


if __name__ == "__main__":
    main()
