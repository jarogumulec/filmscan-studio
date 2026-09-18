"""Film base / min point: the emulsion's clear-base level, measured and stored.

What this is NOT: a flat field. A flat is shot with *no film* in the holder and
divides out vignetting and dust (multiplicative correction, whole frame). The
film base / min point is measured *through the held film* on a patch of clear
base (the film's per-frame edge or between perforations — not necessarily the
whole frame), and it is a subtraction reference: the floor below which no
usable density exists. It is the raw material for a per-film Hurter–Driffield
characterisation — min from this measurement, max from each frame's brightest
real value.

One measurement is meaningless without its exposure. A base level read at 2 s
is twice the DN of the same base at 1 s, so every sample records the shutter,
gain, temperature and black/white it was taken at, and
:meth:`FilmBaseSample.scaled_above_black` folds any other exposure's ratio in
— the same shutter/gain-ratio normalisation the dark and flat stacks use
(:mod:`filmscan_studio.core.calibration`: an illuminated photon signal scales
with the full exposure factor, the pedestal does not scale at all).

Coordinate contract (the binned-stream / full-frame collision): a rect is
interpreted in the pixel grid of the array it is handed to. The Live View
stream and the drawn rect share that grid 1:1; a full-size frame needs the
rect converted to sensor pixels first. The conversion belongs to the GUI —
it owns the stream-mode bookkeeping — and this module refuses nothing but
measures exactly what it is given.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

#: Sidecar holding one film's base measurements; one file per project, a list
#: of samples in append order (re-shoots are legitimate, e.g. after a lamp
#: change — the newest is what the operator sees, the history stays).
FILM_BASE_FILENAME = "film_base.json"

FILE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class FilmBaseSample:
    """One clear-base reading: mean DN over the rect, plus its full exposure."""

    #: "stream" — measured on a Live View frame (no new exposure was made);
    #: "frame" — measured on a captured full-size frame stored on disk.
    kind: str
    #: Mean DN over the measured region, in the source frame's DN scale.
    mean_dn: float
    #: Pedestal of the source the mean was taken from — subtracted before any
    #: exposure rescale (the pedestal does not scale; see ``scaled_above_black``).
    black_level: float
    white_level: float
    #: The exposure this reading belongs to. ``shutter`` is seconds;
    #: ``gain`` is the analog multiplier (None for ISO bodies).
    shutter: float
    gain: float | None = None
    sensor_temperature_c: float | None = None
    #: Region in the source array's own pixel grid, (x0, y0, x1, y1).
    rect: tuple[int, int, int, int] | None = None
    #: File the reading came from, or "stream" for a Live View measurement.
    source: str = "stream"
    captured_at: datetime | None = None

    def above_black(self) -> float:
        """The photon signal above the pedestal — what exposure scales."""
        return self.mean_dn - self.black_level

    def scaled_above_black(
        self,
        target_shutter: float,
        target_gain: float | None = None,
    ) -> float:
        """This base's above-black signal, moved onto another exposure.

        Illuminated signal scales with shutter *and* gain (photon statistics —
        the flat-field rule in :mod:`filmscan_studio.core.calibration`), while
        the pedestal stays fixed and is re-added, not multiplied. At the
        archival gain this is a pure shutter ratio; the gain term matters only
        when a sample was taken away from the noise floor.
        """
        if target_shutter <= 0 or self.shutter <= 0:
            raise ValueError("shutter times must be positive")
        factor = target_shutter / self.shutter
        if target_gain is not None and self.gain:
            factor *= target_gain / self.gain
        return self.above_black() * factor

    def scaled_mean(self, target_shutter: float,
                    target_gain: float | None = None) -> float:
        """Full DN at another exposure: scaled signal back over the pedestal."""
        return self.black_level + self.scaled_above_black(
            target_shutter, target_gain)

    # ------------------------------------------------------------------- JSON

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "mean_dn": self.mean_dn,
            "black_level": self.black_level,
            "white_level": self.white_level,
            "shutter": self.shutter,
            "gain": self.gain,
            "sensor_temperature_c": self.sensor_temperature_c,
            "rect": list(self.rect) if self.rect is not None else None,
            "source": self.source,
            "captured_at": (self.captured_at.isoformat()
                            if self.captured_at else None),
        }

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> "FilmBaseSample":
        captured = data.get("captured_at")
        rect = data.get("rect")
        return cls(
            kind=data["kind"],
            mean_dn=float(data["mean_dn"]),
            black_level=float(data["black_level"]),
            white_level=float(data["white_level"]),
            shutter=float(data["shutter"]),
            gain=(float(data["gain"]) if data.get("gain") is not None else None),
            sensor_temperature_c=(
                float(data["sensor_temperature_c"])
                if data.get("sensor_temperature_c") is not None else None),
            rect=(tuple(int(v) for v in rect) if rect is not None else None),
            source=data.get("source", "stream"),
            captured_at=(datetime.fromisoformat(captured) if captured else None),
        )


def region_mean(data: np.ndarray, rect: tuple[int, int, int, int] | None) -> float:
    """Mean DN of the region (rect in ``data``'s own grid), or the whole array.

    Out-of-frame edges clamp rather than raise: a rect dragged past the frame
    edge measures what is inside it, which is what the operator aimed at.
    An empty region (rect fully outside) raises — measuring nothing and
    calling it a base level would poison every later scaling.
    """
    a = np.asarray(data, dtype=np.float64)
    if rect is not None:
        x0, y0, x1, y1 = (int(v) for v in rect)
        h, w = a.shape[:2]
        x0, y0 = max(x0, 0), max(y0, 0)
        x1, y1 = min(x1, w), min(y1, h)
        if x1 <= x0 or y1 <= y0:
            raise ValueError("film base obdélník leží mimo snímek")
        a = a[y0:y1, x0:x1]
    if a.size == 0:
        raise ValueError("film base obdélník je prázdný")
    return float(a.mean())


# --------------------------------------------------------------------- JSON store

def load_samples(path: Path) -> list[FilmBaseSample]:
    """All stored samples; a missing file is an empty history, not an error."""
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [FilmBaseSample.from_json_dict(s)
            for s in payload.get("samples", [])]


def append_sample(path: Path, sample: FilmBaseSample) -> list[FilmBaseSample]:
    """Append one sample and rewrite the file; returns the new history."""
    samples = load_samples(path)
    samples.append(sample)
    payload = {
        "schema_version": FILE_SCHEMA_VERSION,
        "samples": [s.to_json_dict() for s in samples],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    return samples
