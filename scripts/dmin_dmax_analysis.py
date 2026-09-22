#!/usr/bin/env python3
"""Roll-wide Dmin/Dmax analysis over developed session folders.

For every session folder in a scans directory (each one a ``DevelopProject``:
``frames/`` + ``film_base.json``):

* **Dmin** — fixed per film: ``DevelopProject.dmin_auto()``, i.e. the newest
  frame-kind film-base sample, dark-adjusted and referenced to the flat
  (``core.density.measure_dmin``). All base candidates are reported too.
* **Dmax** — per frame: ``core.density.estimate_dmax`` (p99.9 + margin) of the
  frame's density map, exactly what ``suggested_dmax`` shows in the GUI.
  Per film the median and the maximum over its frames are reported.

Output: a JSON sidecar with every number plus one PNG chart
(Dmin line + per-frame Dmax dots + median/max bars per film).

Usage::

    uv run python scripts/dmin_dmax_analysis.py [--scans DIR] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np

from filmscan_studio.core import density as dens
from filmscan_studio.developer.project import DevelopProject


def film_label(root: Path) -> tuple[str, str]:
    """(film_id, film_name) for the folder — project.json, catalog, name."""
    film_id = root.name
    name = ""
    try:
        data = json.loads((root / "project.json").read_text(encoding="utf-8"))
        film = data.get("film") or {}
        film_id = film.get("film_id") or film_id
        name = film.get("film_name") or ""
    except (OSError, json.JSONDecodeError):
        pass
    if not name:  # older sessions wrote project.json only to the catalog
        import sqlite3
        db = root / "catalog.sqlite"
        if db.exists():
            try:
                with sqlite3.connect(db) as con:
                    name = con.execute(
                        "SELECT label FROM films LIMIT 1").fetchone()[0]
            except sqlite3.Error:
                pass
    return film_id, (name or "??")


def analyze_folder(root: Path) -> dict:
    t0 = time.time()
    proj = DevelopProject.open(root)
    film_id, name = film_label(root)
    dmin = proj.dmin_auto()
    dmin_all = proj.dmin_candidates()

    frames = []
    for fname in proj.frame_names:
        try:
            d, prov = proj.build_density(fname)
            finite = d[np.isfinite(d)]
            entry = {
                "frame": fname,
                "dmax": dens.estimate_dmax(d),
                "p999": (float(np.percentile(finite, 99.9))
                         if finite.size else None),
                "valid_fraction": round(prov.valid_fraction, 4),
                "dense_fraction": round(float(np.isposinf(d).mean()), 4),
            }
        except Exception as exc:                       # noqa: BLE001
            entry = {"frame": fname, "error": f"{type(exc).__name__}: {exc}"}
            print(f"    ! {fname}: {entry['error']}", flush=True)
        frames.append(entry)
        print(f"    {fname}: dmax={entry.get('dmax', float('nan')):.3f}"
              f"  valid={entry.get('valid_fraction', 0):.2f}", flush=True)

    ok = [f["dmax"] for f in frames if "dmax" in f]
    # Roll-wide Dmax as a quantile of the per-frame tails (p99.9 each,
    # no margin): max is dirt-sensitive, median is scene-dependent; p90 of
    # the tails converges to the film's achievable Dmax as dark frames
    # accumulate and bright frames only fill the low end (order 2026-09-22).
    tails = sorted(f["p999"] for f in frames if f.get("p999") is not None)
    result = {
        "folder": root.name,
        "film_id": film_id,
        "film_name": name,
        "dmin": dmin,
        "dmin_candidates": [round(x, 4) for x in dmin_all],
        "scans": len(frames),
        "measured": len(ok),
        "dmax_median": round(statistics.median(ok), 4) if ok else None,
        "dmax_max": round(max(ok), 4) if ok else None,
        "dmax_min": round(min(ok), 4) if ok else None,
        "dmax_p90": (round(float(np.percentile(tails, 90)), 4)
                     if tails else None),
        "dmax_p75": (round(float(np.percentile(tails, 75)), 4)
                     if tails else None),
        "frames": frames,
        "seconds": round(time.time() - t0, 1),
    }
    del proj                                # free the frames before next roll
    return result


def roll_dmax(r: dict, q: float) -> float | None:
    """Quantile `q` of the roll's per-frame p99.9 tails (margin-free)."""
    tails = sorted(f["p999"] for f in r["frames"] if f.get("p999") is not None)
    if not tails:
        return None
    return float(np.percentile(tails, q))


def plot(results: list[dict], out_png: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(11, 6.5))
    width = 0.55
    for i, r in enumerate(results):
        xs = [f["dmax"] for f in r["frames"] if "dmax" in f]
        # per-frame dots, slightly jittered around the film's x position
        rng = np.random.default_rng(42 + i)
        jx = i + rng.uniform(-0.18, 0.18, len(xs))
        ax.scatter(jx, xs, s=14, alpha=0.45, color="#3b6ea5", zorder=2,
                   label="Dmax per snímek" if i == 0 else None)
        med = roll_dmax(r, 50)
        p90 = roll_dmax(r, 90)
        mx = roll_dmax(r, 100)
        if med is not None:
            ax.bar(i, med, width=width, color="#3b6ea5",
                   alpha=0.30, zorder=1,
                   label="Dmax median scén" if i == 0 else None)
        if p90 is not None:
            ax.hlines(p90, i - width / 2, i + width / 2,
                      color="#173c63", lw=3.0, zorder=3,
                      label="Dmax filmu = p90 z chvostů" if i == 0 else None)
            ax.annotate(f"{p90:.2f}", (i + width / 2 + 0.02, p90),
                        va="center", fontsize=9, fontweight="bold",
                        color="#173c63")
        if mx is not None and mx - p90 > 0.15:
            # the single darkest frame sits well above p90: show it as a
            # faint outlier tick so dirt-vs-real stays inspectable
            ax.hlines(mx, i - 0.05, i + 0.05, color="#173c63", lw=1.2,
                      alpha=0.5, linestyle="--", zorder=3,
                      label="nejtemnější snímek (odlehlý)"
                           if i == 0 else None)
        if r["dmin"] is not None:
            ax.hlines(r["dmin"], i - width / 2, i + width / 2,
                      color="#b5462b", lw=2.4, linestyle=":", zorder=3)
            ax.plot(i, r["dmin"], "s", color="#b5462b", ms=7, zorder=4,
                    label="Dmin (fixní per film)" if i == 0 else None)
            ax.annotate(f"{r['dmin']:.3f}", (i - width / 2 - 0.02,
                        r["dmin"]), va="center", ha="right", fontsize=8,
                        color="#b5462b")
            # the newest base sample can be a failed measurement (a patch with
            # no film -> T≈1, Dmin≈0); flag it against the other candidates,
            # which the GUI's dmin_candidates() shows too
            others = r["dmin_candidates"][:-1]
            if others and abs(r["dmin"] - statistics.median(others)) > 0.1:
                alt = statistics.median(others)
                ax.plot(i, alt, "x", color="#b5462b", ms=9, mew=2, zorder=4)
                ax.annotate(f"posledni base selhal\n(dalsi vzorky: "
                            f"{alt:.3f})", (i + 0.06, alt), va="center",
                            fontsize=8, color="#b5462b")

    ax.set_xticks(range(len(results)))
    ax.set_xticklabels([f"{r['film_id'].split('_')[0]} · {r['film_name']}"
                        for r in results], fontsize=9)
    ax.set_ylabel("optická densita D")
    ax.set_ylim(bottom=0)
    ax.grid(axis="y", alpha=0.3)
    ax.set_title("Dmin (fixní base) a Dmax filmu jako p90 z per-snímkových "
                 "chvostů p99.9; median scén a odlehlé snímky pro kontrolu")
    ax.legend(loc="upper left", fontsize=9)
    fig.tight_layout()
    fig.savefig(out_png, dpi=150)
    print(f"chart: {out_png}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scans", default="/Users/jarogumulec/Downloads/skeny",
                    type=Path, help="složka se session foldery K16O…")
    ap.add_argument("--out", default=None, type=Path,
                    help="výstupní složka (default: --scans)")
    ap.add_argument("--from-json", metavar="RESULTS.JSON", type=Path,
                    default=None, help="jen překresli graf z hotových výsledků")
    args = ap.parse_args()
    out_dir = args.out or args.scans

    if args.from_json:
        results = json.loads(args.from_json.read_text(encoding="utf-8"))
    else:
        folders = sorted(p for p in args.scans.iterdir()
                         if (p / "frames").is_dir())
        results = []
        for root in folders:
            print(f"== {root.name}", flush=True)
            results.append(analyze_folder(root))
            # keep whatever was measured so far, even if a later folder dies
            (out_dir / "dmin_dmax_results.json").write_text(
                json.dumps(results, indent=1, ensure_ascii=False),
                encoding="utf-8")

    print()
    print(f"{'film':<26}{'Dmin':>8}{'Dmax p90':>10}{'med':>8}{'max':>8}"
          f"{'snímku':>8}")
    for r in results:
        dmin = f"{r['dmin']:.3f}" if r["dmin"] is not None else "–"
        ident = r["film_id"].split("_")[0] + " " + r["film_name"]
        med = roll_dmax(r, 50) or 0.0
        p90 = roll_dmax(r, 90) or 0.0
        mx = roll_dmax(r, 100) or 0.0
        print(f"{ident:<26}{dmin:>8}{p90:>10.3f}{med:>8.2f}{mx:>8.2f}"
              f"{r['measured']:>8}")

    plot(results, out_dir / "dmin_dmax_analysis.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
