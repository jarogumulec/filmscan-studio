"""Společné pomůcky pro standalone kamarové testy (bokem od aplikace).

Žádná změna capture pipeline — jen čtené využití TouptekCamera backendu.
Data se píší do camera_tests/data/, grafy do camera_tests/.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from filmscan_studio.capture.touptek import TouptekCamera  # noqa: E402

DATA = HERE / "data"
WHITE = 65535.0


def connect() -> TouptekCamera:
    cam = TouptekCamera()
    cam.connect()
    print(f"[camera] {cam.info.model}, gain range {cam.info.gain_range}, "
          f"sensor {cam.info.sensor_width}x{cam.info.sensor_height}", flush=True)
    return cam


def center_quarter(data: np.ndarray) -> np.ndarray:
    """Střed snímku o hraně 1/2 rozměru => plocha čtvrtiny celého senzoru."""
    h, w = data.shape
    ch, cw = h // 2, w // 2
    y0, x0 = (h - ch) // 2, (w - cw) // 2
    return data[y0:y0 + ch, x0:x0 + cw]


def wait_temperature(cam: TouptekCamera, target_c: float | None,
                     tol_c: float = 0.4, timeout_s: float = 600.0,
                     label: str = "") -> float:
    """Čkej, až senzor dosáhne cíle (None = TEC off: nech dojít na pokojovou).

    Vrací poslední naměřenou teplotu. Při timeoutu jen varuje a pokračuje —
    test má pořád hodnotu změřit, jen ji popsat.
    """
    started = time.monotonic()
    last = cam.get_temperature_c()
    stable_since: float | None = None
    while time.monotonic() - started < timeout_s:
        temp = cam.get_temperature_c()
        if target_c is None:
            # TEC off: "pokojová" = teplota se přestala měnit (>60 s pod 0,3 C)
            if last is not None and abs(temp - last) <= 0.1:
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= 60:
                    print(f"[temp{label}] ustáleno na {temp:+.1f} C "
                          f"(po {time.monotonic() - started:.0f} s)", flush=True)
                    return temp
            else:
                stable_since = None
        elif abs(temp - target_c) <= tol_c:
            print(f"[temp{label}] {temp:+.1f} C (cíl {target_c:+.1f}, "
                  f"po {time.monotonic() - started:.0f} s)", flush=True)
            return temp
        elapsed = time.monotonic() - started
        if int(elapsed) % 30 < 5:
            print(f"[temp{label}] {temp:+.1f} C, cíl "
                  f"{'pokoj' if target_c is None else f'{target_c:+.1f}'}, "
                  f"{elapsed:.0f} s", flush=True)
        last = temp
        time.sleep(5.0)
    print(f"[temp{label}] TIMEOUT {timeout_s:.0f} s — posledních "
          f"{last:+.1f} C, pokračuji s touto hodnotou", flush=True)
    return last


def set_expo_us(cam: TouptekCamera, us: float) -> float:
    """Čas primou cestou přes SDK — aplikace clampuje na ≥300 µs, hw dno je
    100 µs (změřeno probe_min_exposure). Vrací readback v sekundách."""
    cam._hcam.put_ExpoTime(max(100, round(us)))
    accepted = cam._hcam.get_ExpoTime() / 1e6
    cam._settings = cam._settings.with_shutter(accepted)
    return accepted


def expose_to_target(cam: TouptekCamera, target_dn: float,
                     discard: int = 3, max_iter: int = 8) -> float:
    """Najdi čas, při kterém je medián středové čtvrtiny ~target_dn.

    Běží na binned overview proudu (lineární, bez tone-mappingu). Vrací
   accepted shutter v sekundách; saturaci nehlídá — to kontroluje volající.
    """
    cam.start_live_view()
    shutter_s = cam.get_settings().shutter or 0.01
    seen: set[int] = set()
    best_us, best_dn = None, float("inf")
    for _ in range(max_iter):
        us = max(100, round(shutter_s * 1e6))
        if us in seen:
            break
        seen.add(us)
        set_expo_us(cam, us)
        for _ in range(discard):
            cam.next_live_frame()
        frame = cam.next_live_frame()
        med = float(np.median(center_quarter(frame.data)))
        print(f"[expose] {us:7} us -> median {med:7.0f} DN "
              f"(žádáno {target_dn:.0f})", flush=True)
        if abs(med - target_dn) < abs(best_dn - target_dn):
            best_us, best_dn = us, med
        if med <= 1.0:
            shutter_s = us * 20 / 1e6
            continue
        shutter_s = min(30.0, us * (target_dn / med) / 1e6)
    cam.stop_live_view()
    accepted = set_expo_us(cam, best_us)
    print(f"[expose] nejlepší {best_us} us -> {best_dn:.0f} DN "
          f"(nastaveno {accepted * 1e6:.0f} us)", flush=True)
    return accepted
