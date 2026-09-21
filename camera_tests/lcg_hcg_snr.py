"""LCG vs. HCG × Low Noise vs. Normal — co to reálně přináší filmovému skeneru.

Otázky (ze zadání 2026-09-20):
1. Je split HCG/LCG na tomto kuse vidět na šumu (read noise) i full well?
   (Manuál: gain ratio 3.01; Tab. 3: HCG FW 16.5 ke− RN 0.94 e− @GV100,
    Tab. 5: LCG FW 51 ke− RN 2.27 e− @GV100.)
2. Co dělá Low Noise Mode — sníží temporální σ, a za jakou cenu?
3. Který mód vyhrává při NAŠEM provozu (sekundové expozice, silné světlo)?

Měří se dvě fáze za sebou; mezi nimi MUSÍ OBSLUHA PŘEPNOUT SVĚTLO
(skript vždy počká na ENTER):

  FÁZE TMA     — zatemni světelnou cestu; 10 stillů na mód a expozici.
                  Per-pixel temporální σ = read noise (+ dark shot noise);
                  medián = dark proud + pedestal.
  FÁZE SVĚTLO  — homogenní bílé pozadí; expozice se DOHLEDÁ PRO KAŽDÝ MÓD
                  zvlášť (DN stupnice se módem měří jinou!) na cíl ~20 k DN
                  (preview-optimum) a ~60 k DN (blízko stropu); 5 stillů.

Výpočet elektronů: DN↔e− škáluje s 1/(CG·LN ziskem). Poměr DN mezi módy
při STEJNÉ scéně (fáze světlo) přímo dává poměr e/ADU; σ_DN × (e/ADU) pak
porovnává read noise v elektronech mezi módy (v jednotkách e/ADU referenčního
módu HCG — absolutní e− vyžaduje Kalifornský protokol, tohle je fér srovnání).

Spustit s kamerou: .venv/bin/python camera_tests/lcg_hcg_snr.py
Výstupy: data/lcg_hcg_snr.csv, data/lcg_hcg_frames/*.npy, lcg_hcg_snr.png
"""

from __future__ import annotations

import csv

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from _common import DATA, HERE, center_quarter, connect, set_expo_us
from _common import wait_temperature

from filmscan_studio.capture._toupcam import toupcam as sdk  # noqa: E402
from filmscan_studio.core.rawio import open_frame

#: Režimy: (popisek, CG volba, LOW_NOISE volba). LCG+LN = vize optima.
MODES = [
    ("HCG",    1, 0),
    ("HCG+LN", 1, 1),
    ("LCG",    0, 0),
    ("LCG+LN", 0, 1),
]
GAIN_VALUE = 100               # Gain Value 100 = 1,00× — archivační bod
DARK_FRAMES = 10
DARK_EXPOSURES_S = [1.0, 10.0]
DARK_TEMPERATURE_C = 10.0      # srovnávací teplota jako v testu (b)
LIVE_TARGETS = [20_000.0, 60_000.0]
LIVE_FRAMES = 5
FRAMES_DIR = DATA / "lcg_hcg_frames"


def apply_mode(hcam, cg: int, ln: int) -> dict:
    """Přepni mód, vrať readback; varuj při nesouladu."""
    hcam.put_Option(sdk.TOUPCAM_OPTION_CG, cg)
    hcam.put_Option(sdk.TOUPCAM_OPTION_LOW_NOISE, ln)
    got = {"cg": hcam.get_Option(sdk.TOUPCAM_OPTION_CG),
           "ln": hcam.get_Option(sdk.TOUPCAM_OPTION_LOW_NOISE)}
    got["as_asked"] = (got["cg"] == cg and got["ln"] == ln)
    if not got["as_asked"]:
        print(f"  [!] kamera hlásí CG={got['cg']} LN={got['ln']}, "
              f"žádáno CG={cg} LN={ln}", flush=True)
    return got


def shoot_roi_stack(cam, n: int, tag: str) -> np.ndarray:
    """N stillů přes produkční cestu; uloží středové čtvrtiny jako stack."""
    stack = []
    for k in range(n):
        res = cam.capture(DATA, f"{tag}_{k:02d}", frames=1)
        frame = open_frame(res.path)
        roi = center_quarter(frame.data).astype(np.float32)
        stack.append(roi)
        print(f"    {tag}_{k:02d}: med {np.median(roi):8.0f} DN "
              f"(max {roi.max():.0f})", flush=True)
        res.path.unlink(missing_ok=True)
    arr = np.stack(stack)
    np.save(FRAMES_DIR / f"{tag}.npy", arr)
    return arr


def stats_of(stack: np.ndarray) -> dict:
    """Per-pixel temporální σ (průměr přes pixely) + medián signálu."""
    temporal = stack.std(axis=0, ddof=1)
    return {
        "median_dn": float(np.median(stack)),
        "sigma_dn": float(temporal.mean()),
        "max_dn": float(stack.max()),
    }


def find_exposure_for_mode(cam, target_dn: float, max_iter: int = 7) -> float:
    """Najdi čas, při kterém median středové čtvrtiny sedne cíli — v AKTUÁLNÍM
    módu (DN stupnice se módem mění, každý mód má vlastní hledání).

    Spodek hledání 400 µs = nad kvantizačním pásmem (~75 µs schody pod ní,
    camera_tests (a)); strop 30 s. Na světlém rigu cíl vychází na ms řády
    (stejně jako v (b)/(d)) — je to limit pozadí, ne záměr.
    """
    floor_us = 400
    cam.start_live_view()
    try:
        us = max(floor_us, round((cam.get_settings().shutter or 1.0) * 1e6))
        best = (None, float("inf"))
        seen: set[int] = set()
        for _ in range(max_iter):
            if us in seen:
                break
            seen.add(us)
            set_expo_us(cam, us)
            for _ in range(2):
                cam.next_live_frame()
            f = cam.next_live_frame()
            med = float(np.median(center_quarter(f.data)))
            print(f"    [expose] {us/1e6:8.4f} s → {med:7.0f} DN "
                  f"(cíl {target_dn:.0f})", flush=True)
            if abs(med - target_dn) < abs(best[1] - target_dn):
                best = (us, med)
            if med < 50:
                us = min(int(us * 20), 30_000_000)
                continue
            scaled = int(round(us * target_dn / med))
            us = min(30_000_000, max(floor_us, scaled))
            if abs(med - target_dn) < 0.05 * target_dn:
                break
        if best[0] is not None:
            set_expo_us(cam, best[0])
        return best[0] or us
    finally:
        cam.stop_live_view()


def phase_dark(cam) -> list[dict]:
    rows: list[dict] = []
    for expo in DARK_EXPOSURES_S:
        for label, cg, ln in MODES:
            info = apply_mode(cam._hcam, cg, ln)
            cam.set_shutter(expo)
            print(f"[tma {expo:g} s] {label}:pořídím {DARK_FRAMES} stillů — "
                  f"NEOTVÍRAT světlo", flush=True)
            stack = shoot_roi_stack(cam, DARK_FRAMES, f"dark_{expo:g}s_{label}")
            s = stats_of(stack)
            print(f"  → med {s['median_dn']:.0f} DN, per-pixel temporální σ "
                  f"{s['sigma_dn']:.1f} DN", flush=True)
            rows.append({"phase": "dark", "exposure_s": expo, "mode": label,
                         **info, **s})
    return rows


def phase_live(cam) -> list[dict]:
    rows: list[dict] = []
    for target in LIVE_TARGETS:
        for label, cg, ln in MODES:
            info = apply_mode(cam._hcam, cg, ln)
            us = find_exposure_for_mode(cam, target)
            print(f"[světlo ~{target/1000:.0f} k DN] {label}:pořídím "
                  f"{LIVE_FRAMES} stillů při {us/1e6:.3f} s", flush=True)
            stack = shoot_roi_stack(cam, LIVE_FRAMES,
                                    f"live_{target:.0f}_{label}")
            s = stats_of(stack)
            print(f"  → med {s['median_dn']:.0f} DN, σ {s['sigma_dn']:.1f} DN, "
                  f"max {s['max_dn']:.0f} DN", flush=True)
            rows.append({"phase": "live", "exposure_s": us / 1e6,
                         "mode": label, **info, **s})
    return rows


def load_existing_rows() -> list[dict]:
    """Řádky z předchozích fází (spouští se zvlášť: světlo i tma)."""
    out = DATA / "lcg_hcg_snr.csv"
    if not out.exists():
        return []
    with out.open() as fh:
        rows = [{k: (float(v) if k in ("exposure_s", "median_dn", "sigma_dn",
                                        "max_dn") and v else v)
                 for k, v in r.items()}
                for r in csv.DictReader(fh)]
    print(f"[csv] načteno {len(rows)} řádků z předchozích fází")
    return rows


def merge_rows(rows: list[dict]) -> list[dict]:
    """Sloučí staré řádky s novými (nová fáze nahradí totéž phase)."""
    new_phase = {r["phase"] for r in rows}
    return [r for r in load_existing_rows() if r["phase"] not in new_phase] + rows


def plot(rows: list[dict]) -> None:
    """Grafy jen pro fáze, které mají data (fáze se měří zvlášť)."""
    # ------------------------------------------------------------------- grafy
    # Fáze se mohou měřit v oddělených bězích — kreslíme jen panely s daty.
    xs_order = [m[0] for m in MODES]
    dark_rows = [r for r in rows if r["phase"] == "dark"]
    live_rows = [r for r in rows if r["phase"] == "live"]
    if not (dark_rows or live_rows):
        print("[graf] žádná data — nic kreslit")
        return
    n_panels = bool(dark_rows) + (2 * bool(live_rows))
    fig, axes = plt.subplots(1, n_panels, figsize=(5.4 * n_panels, 4.8))
    axes = np.atleast_1d(axes)
    ai = 0

    if dark_rows:
        ax = axes[ai]
        ai += 1
        for expo in DARK_EXPOSURES_S:
            sub = {r["mode"]: r for r in dark_rows
                   if r["exposure_s"] == expo}
            if len(sub) < len(xs_order):
                continue        # nedokreslená expozice — vynechat řadu
            ax.plot(xs_order, [sub[m]["sigma_dn"] for m in xs_order], "o-",
                    label=f"{expo:g} s")
        ax.set_yscale("log")
        ax.set_ylabel("per-pixel temporální σ [DN]")
        ax.set_title("TMA: read noise (+dark) — menší = lepší")
        ax.grid(alpha=0.3)
        ax.legend()

    if live_rows:
        ax = axes[ai]
        ai += 1
        for target in LIVE_TARGETS:
            sub = [r for r in live_rows
                   if abs(r["median_dn"] - target) < 0.4 * target]
            sub.sort(key=lambda r: xs_order.index(r["mode"]))
            ax.plot([r["mode"] for r in sub],
                    [r["sigma_dn"] / max(r["median_dn"], 1) * 100 for r in sub],
                    "s--", label=f"cíl {target/1000:.0f} k DN")
            for r in sub:
                ax.annotate(f"{r['median_dn']/1000:.1f}k",
                            (r["mode"], r["sigma_dn"] / r["median_dn"] * 100),
                            textcoords="offset points", xytext=(0, 7),
                            fontsize=8, ha="center")
        ax.set_ylabel("relativní temporální σ [%]")
        ax.set_title("SVĚTLO: σ/mean (skutečný provoz — menší = lepší)")
        ax.grid(alpha=0.3)
        ax.legend()

        # DN poměry (→ e/ADU poměry): vše vztaženo k LCG+LN = 1
        ax = axes[ai]
        for target in LIVE_TARGETS:
            same = [r for r in live_rows
                    if abs(r["median_dn"] - target) < 0.4 * target]
            if not same:
                continue
            # normalizuj na stejný světelný tok: dn_per_s = med/exposure
            dps = {r["mode"]: r["median_dn"] / max(r["exposure_s"], 1e-6)
                   for r in same}
            base = dps.get("LCG+LN") or max(dps.values())
            ax.bar(xs_order,
                   [dps.get(m, np.nan) / base for m in xs_order],
                   alpha=0.55, label=f"cíl {target/1000:.0f} k DN")
        ax.axhline(1.0, color="k", lw=0.8)
        ax.set_ylabel("DN na sekundu expozice ÷ (LCG+LN)")
        ax.set_title("Citlivost DN/s: poměr módů → e/ADU poměry")
        ax.grid(alpha=0.3, axis="y")
        ax.legend()

    fig.suptitle("ATR2600M: konverzní gain × low noise — nameřeno na rigu "
                 "(Gain Value 100 = 1,00×)")
    fig.tight_layout()
    fig_path = HERE / "lcg_hcg_snr.png"
    fig.savefig(fig_path, dpi=110)
    print(f"[graf] {fig_path}")
    print("Hotovo. (Vyhodnocení doplní asistent do README.md.)")




def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("phase", choices=("dark", "light", "both", "plot"),
                    help="dark = zatemněno, light = rozsvíceno, "
                         "both = interaktivní tma→světlo, "
                         "plot = jen grafy z existující CSV")
    args = ap.parse_args()

    if args.phase == "plot":
        plot(load_existing_rows())
        return

    cam = connect()
    hcam = cam._hcam
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    hcam.put_ExpoAGain(GAIN_VALUE)
    print(f"[gain] ExpoAGain readback {hcam.get_ExpoAGain()} "
          f"(procenta: 100 = 1×)")

    rows: list[dict] = []
    if args.phase in ("dark", "both"):
        if args.phase == "both":
            print("=" * 70)
            print("FÁZE 1: TMA — zatemni světelnou cestu rigu.")
            input("Jak je zatemněno, stiskni ENTER... ")
        try:
            cam.set_tec_enabled(True)
            wait_temperature(cam, DARK_TEMPERATURE_C)
        except Exception as exc:  # noqa: BLE001 - teplota není podmínka měření
            print(f"[teplota] pokračuji bez stabilizace: {exc}")
        rows += phase_dark(cam)
    if args.phase in ("light", "both"):
        if args.phase == "both":
            print("=" * 70)
            print("FÁZE 2: SVĚTLO — rozsviť homogenní bílé pozadí.")
            input("Jak je rozsvíceno, stiskni ENTER... ")
        rows += phase_live(cam)

    apply_mode(hcam, 0, 1)      # návrat do provozního optima LCG+LN
    cam.disconnect()

    rows = merge_rows(rows) if args.phase != "both" else rows
    out = DATA / "lcg_hcg_snr.csv"
    fields = ["phase", "exposure_s", "mode", "cg", "ln", "as_asked",
              "median_dn", "sigma_dn", "max_dn"]
    with out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    print(f"[csv] {out} ({len(rows)} řádků)")

    plot(rows)


if __name__ == "__main__":
    main()
