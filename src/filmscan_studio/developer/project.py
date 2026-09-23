"""Developer project folder: the bridge from a capture session to densities.

Opens a session directory as written by the capture layer (B1 layout:
``frames/*.tif`` + ``*.tif.json`` sidecars + ``film_base.json``), classifies
frames by their sidecar ``kind``, builds the calibration masters, and answers
the two questions the GUI asks:

* ``build_density(frame)`` -> (D map, provenance)   — measurement
* ``dmin_candidates() / suggested_dmax``           — scale points

**The image-area rect (ROI).** The capture layer draws a rect marking where
the actual film frame is, but as of 2026-09-18 it does not record it in the
sidecar (documented as a capture-layer requirement in
``Documentation_image_processing/06_roi_metadata.md``). Everything here treats
the rect as optional:

* ``CaptureRecord.image_rect`` if a future capture version records it;
* else a per-frame rect set by the operator (Shift-drag in the GUI);
* else the whole frame, with the illuminated-area mask doing what triage it
  can — the holder's black border then merely wastes pixels, it cannot corrupt
  densities because the mask marks it NaN before it can.

Calibration is deliberately *whole-frame* even when the density map is cropped
to a rect: the dark and flat masters are per-pixel instruments and the crop
indexes them in full-frame coordinates, never the other way round.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

from filmscan_studio.core import density as dens
from filmscan_studio.core.calibration import (
    build_flat,
    rescale_dark,
    stack_darks,
)
from filmscan_studio.core.filmbase import FILM_BASE_FILENAME, load_samples
from filmscan_studio.core.orientation import apply_orientation
from filmscan_studio.core.models import FrameKind
from filmscan_studio.core.rawio import RAW_SUFFIX, RawFrame, open_frame

log = logging.getLogger(__name__)

#: Per-frame develop settings + manual rects, written next to the session's
#: own JSONs so reopening a folder restores the operator's work.
SETTINGS_FILENAME = "develop_settings.json"

#: Fallback no-flat reference: percentile of the frame's own above-black
#: signal standing in for the flat level (design 04: percentile base is a
#: preview fallback only, never a measurement).
FLAT_FALLBACK_PERCENTILE = 99.9


@dataclass
class FrameEntry:
    """One scan of the project with its file and sidecar facts."""

    frame: RawFrame
    record: dict
    #: Image-area rect (x0, y0, x1, y1) full-frame pixels; None = not set.
    rect: tuple[int, int, int, int] | None = None

    @property
    def name(self) -> str:
        return self.frame.path.name if self.frame.path else "?"

    @property
    def shutter(self) -> float:
        return self.frame.acquisition.exposure_time or 1.0

    @property
    def gain(self) -> float | None:
        return self.frame.acquisition.gain


@dataclass
class DevelopProject:
    """A opened session folder, ready to develop."""

    root: Path
    frames: list[FrameEntry] = field(default_factory=list)
    dark_paths: list[Path] = field(default_factory=list)
    flat_paths: list[Path] = field(default_factory=list)
    #: The latest frame-kind film-base sample (design: newest is what the
    #: operator sees; the history stays on disk).
    base_sample = None
    flat_signal: np.ndarray | None = None   # above-black, flat's own shutter
    flat_shutter: float | None = None
    flat_gain: float | None = None
    dark_signal: np.ndarray | None = None   # above-black, master shutter
    dark_shutter: float | None = None
    #: Per-frame operator rects by frame name (GUI edits; persisted to
    #: ``develop_settings.json``).
    manual_rects: dict[str, tuple[int, int, int, int]] = field(
        default_factory=dict)
    #: Per-frame render settings (dicts of :class:`RenderParams.to_dict`) by
    #: frame name; loaded from / saved to ``develop_settings.json``.
    frame_settings: dict[str, dict] = field(default_factory=dict)
    #: Output sharpening for the WHOLE project (unsharp % and radius px).
    #: Operator order 2026-09-22: „přesuň ostření do Exportu a ať funguje
    #: v režimu stejné nastavení pro všechny fotky — tohle se nebude
    #: upravovat per fotka". Žije vedle rects/settings ve stejném JSONu a
    #: jde do každého renderu, bez ohledu na per-frame dict.
    global_sharpen: float = 20.0
    global_sharpen_radius: float = 1.0
    #: Film orientation as recorded by the capture app (``project.json`` ->
    #: ``film``). The density archive stays raw sensor orientation; only
    #: renders (preview + exports) flip by these flags.
    mirrored_horizontal: bool = False
    mirrored_vertical: bool = False
    rotated_180: bool = False
    #: ``film.film_id`` of the opened roll ("K16O04_2026-07-22"); empty for
    #: sessions that carry none. Exports are named by :attr:`export_prefix`.
    film_id: str = ""

    @property
    def export_prefix(self) -> str:
        """Roll identifier for export names (order 2026-09-22: „z
        'K16O04_2026-07-22' udělej název 'K16O04_frame001.mono10.heic'").
        The part of ``film_id`` before the first underscore — the date tail
        would only bloat the name; the folder already carries it. Empty
        film_id → no prefix, old naming stays."""
        return self.film_id.split("_", 1)[0].strip()

    # ------------------------------------------------------------------ open

    @classmethod
    def open(cls, folder: str | Path) -> "DevelopProject":
        """Load a session folder. Unreadable sidecars and stray files are
        skipped with a warning — a project opens even when half its files are
        foreign; refusing to open would lock the operator out of their films.
        """
        root = Path(folder)
        frames_dir = root / "frames"
        if not frames_dir.is_dir():
            frames_dir = root          # tolerate a flat folder of TIFs
        if not frames_dir.is_dir():
            raise ValueError(f"{folder} has no frames/ directory")

        proj = cls(root=root)
        for path in sorted(frames_dir.glob(f"*{RAW_SUFFIX}")):
            sidecar = path.with_suffix(path.suffix + ".json")
            record: dict = {}
            if sidecar.exists():
                try:
                    record = json.loads(sidecar.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError) as exc:
                    log.warning("sidecar %s unreadable: %s", sidecar.name, exc)
            else:
                log.warning("%s has no sidecar; guessing kind from its name",
                            path.name)
                record = {"kind": _kind_from_name(path.name)}
            kind = record.get("kind", "scan")
            try:
                frame = open_frame(path)
            except Exception as exc:  # noqa: BLE001 - keep the project open
                log.warning("cannot read %s: %s", path.name, exc)
                continue
            if kind == FrameKind.DARK:
                proj.dark_paths.append(path)
            elif kind == FrameKind.FLAT:
                proj.flat_paths.append(path)
            elif kind == FrameKind.BASE:
                pass  # base frames are reached through film_base.json samples
            else:
                rect = _rect_from_record(record, frame)
                proj.frames.append(FrameEntry(frame=frame, record=record,
                                              rect=rect))
        proj._load_base_sample(root)
        proj._build_calibration()
        film = _film_block(root, proj.frames)
        proj._load_orientation(film)
        proj.film_id = str(film.get("film_id") or "")
        proj.load_settings()
        return proj

    def _load_orientation(self, film: dict) -> None:
        """Film orientation flags from the capture app's ``film`` block.

        The capture UI records how the strip sits in the holder
        (``mirrored_horizontal`` / ``mirrored_vertical`` / ``rotated_180``);
        the developer shows the *viewer* orientation, so these drive flips in
        preview and exports only — never the density archive, which stays in
        raw sensor orientation.
        """
        self.mirrored_horizontal = bool(film.get("mirrored_horizontal"))
        self.mirrored_vertical = bool(film.get("mirrored_vertical"))
        self.rotated_180 = bool(film.get("rotated_180"))

    @property
    def oriented(self) -> bool:
        return bool(self.mirrored_horizontal or self.mirrored_vertical
                    or self.rotated_180)

    def orientation_apply(self, image: np.ndarray) -> np.ndarray:
        """Flip/rotate a 2-D map (or HxWxN array) into viewer orientation.

        The transform itself is :func:`filmscan_studio.core.orientation.
        apply_orientation`, shared with the annotator's previews so the two
        apps can never drift on what "as recorded" means.
        """
        return apply_orientation(
            image,
            mirrored_horizontal=self.mirrored_horizontal,
            mirrored_vertical=self.mirrored_vertical,
            rotated_180=self.rotated_180,
        )

    def frame_rotation(self, name: str) -> int:
        """Per-frame viewer rotation (annotator "otáčet 90°"), CW degrees.

        The film flags are whole-strip; this rides in the frame's own
        ``annotation`` block. The developer bakes it into renders and exports
        (operator order 2026-09-22: „implementuj ať exportovaný soubor je
        takto otočen developerem") — the density archive stays raw, and the
        rotation is applied AFTER ``orientation_apply`` and AFTER the rect
        crop so the drawn rect keeps addressing sensor pixels.
        """
        try:
            entry = self.entry(name)
        except KeyError:
            return 0
        ann = entry.record.get("annotation") or {}
        return int(ann.get("rotation_degrees") or 0) % 360

    def rotate_frame(self, image: np.ndarray, name: str) -> np.ndarray:
        """Apply :meth:`frame_rotation` to a pixel/map array (rot90 otáčí
    counter-clockwise, proto záporné k)."""
        rot = self.frame_rotation(name)
        return np.rot90(image, -(rot // 90)) if rot % 360 else image

    def _flip_rect(self, rect: tuple[int, int, int, int],
                   frame_wh: tuple[int, int]) -> tuple[int, int, int, int]:
        """The film-flag flips (mirrored H/V, rotated 180) on a rect.

        Involution: applying it twice returns the input, which is what lets
        the GUI use one function for both directions of the film flips.
        """
        x0, y0, x1, y1 = rect
        w, h = frame_wh
        if self.rotated_180:
            x0, x1 = w - x1, w - x0
            y0, y1 = h - y1, h - y0
        if self.mirrored_horizontal:
            x0, x1 = w - x1, w - x0
        if self.mirrored_vertical:
            y0, y1 = h - y1, h - y0
        return (int(x0), int(y0), int(x1), int(y1))

    @staticmethod
    def _rotate_rect(rect: tuple[int, int, int, int],
                     frame_wh: tuple[int, int],
                     rot: int, *, inverse: bool = False) -> tuple[int, int, int, int]:
        """Per-frame CW rotation of a rect; (W, H) are the SENSOR extents.

        Matches :meth:`rotate_frame` (``np.rot90(a, -k)`` = CW): a sensor
        point (x, y) lands at (H − y, x) for 90°, and the inverse rotation —
        which is NOT the same transform, 90° is no involution — maps a
        displayed point back. Drawing the ROI and reading a drawn ROI are
        inverse journeys and need ``inverse=True`` on the way back
        (maska v otočeném náhledu, operátor 2026-09-22).
        """
        x0, y0, x1, y1 = rect
        w, h = frame_wh
        if rot == 90:
            if not inverse:    # x' = H - y ; y' = x
                return (h - y1, x0, h - y0, x1)
            return (y0, h - x1, y1, h - x0)     # x = y' ; y = H - x'
        if rot == 270:
            if not inverse:    # x' = y ; y' = W - x
                return (y0, w - x1, y1, w - x0)
            return (w - y1, x0, w - y0, x1)     # x = W - y' ; y = x'
        # 180° is an involution, same formula both ways
        return (w - x1, h - y1, w - x0, h - y0)

    def rect_apply(self, rect: tuple[int, int, int, int] | None,
                   frame_wh: tuple[int, int],
                   rotation_degrees: int = 0) -> tuple[int, int, int, int] | None:
        """Transform a SENSOR whole-frame rect into viewer orientation.

        ``orientation_apply`` + :meth:`rotate_frame` flip/rotate pixel maps;
        a rectangle needs its corners mapped by the same transforms in the
        same order — film flags first, per-frame 90° rotation last (the
        rotation is baked after the crop in the render pipeline). ``frame_wh``
        is the SENSOR (W, H); the returned rect lives in the viewer extents
        (for 90/270 that is (H, W)).
        """
        if rect is None:
            return None
        if self.oriented:
            rect = self._flip_rect(rect, frame_wh)
        rot = rotation_degrees % 360
        if rot:
            rect = self._rotate_rect(rect, frame_wh, rot)
        return tuple(int(v) for v in rect)

    def rect_unapply(self, rect: tuple[int, int, int, int] | None,
                     frame_wh: tuple[int, int],
                     rotation_degrees: int = 0) -> tuple[int, int, int, int] | None:
        """A rect drawn in VIEWER coords back to SENSOR coords.

        The exact inverse of :meth:`rect_apply`: undo the per-frame rotation
        first (with its inverse, which is NOT the forward transform), then
        the film-flag flips (those are involutions). Using ``rect_apply``
        here — as long as only flips existed and inverting meant re-calling
        — silently mirrored a drawn ROI into the wrong half of the frame
        once 90° rotations existed.
        """
        if rect is None:
            return None
        rot = rotation_degrees % 360
        if rot:
            rect = self._rotate_rect(rect, frame_wh, rot, inverse=True)
        if self.oriented:
            rect = self._flip_rect(rect, frame_wh)
        return tuple(int(v) for v in rect)

    # ------------------------------------------------------------ settings

    def settings_path(self) -> Path:
        return self.root / SETTINGS_FILENAME

    def load_settings(self) -> None:
        """Restore manual rects + per-frame render params from the JSON."""
        try:
            data = json.loads(self.settings_path().read_text(
                encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return          # first open: nothing to restore yet
        self.manual_rects.update(
            {k: tuple(int(v) for v in r)
             for k, r in (data.get("rects") or {}).items()
             if r and len(r) == 4})
        self.frame_settings.update(
            {k: dict(v) for k, v in (data.get("settings") or {}).items()
             if isinstance(v, dict)})
        g = data.get("sharpen")
        if isinstance(g, dict):
            # Globální ostření mělo před přestavbou (2026-09-22) per-frame
            # život; tady je jediná platná kopie.
            self.global_sharpen = float(g.get("amount",
                                              self.global_sharpen))
            self.global_sharpen_radius = float(
                g.get("radius", self.global_sharpen_radius))
        else:
            # Starý formát: ostření žilo per-frame. Přebírá se první
            # per-frame hodnota lišící se od předvolby — explicitní vypnutí
            # (nula) tak migrací nepřijde o zvyk; bez odlišení zůstává 20 %.
            for fs in self.frame_settings.values():
                v = fs.get("sharpen")
                if v is not None and float(v) != 20.0:
                    self.global_sharpen = float(v)
                    r = fs.get("sharpen_radius")
                    if r is not None:
                        self.global_sharpen_radius = float(r)
                    break

    def save_settings(self) -> None:
        """Write rects + all known per-frame settings (atomic-ish replace)."""
        payload = {
            "schema_version": 1,
            "rects": {k: list(v) for k, v in self.manual_rects.items()
                      if v is not None},
            "settings": self.frame_settings,
            "sharpen": {"amount": self.global_sharpen,
                        "radius": self.global_sharpen_radius},
        }
        tmp = self.settings_path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(self.settings_path())

    def _load_base_sample(self, root: Path) -> None:
        samples = [s for s in load_samples(root / FILM_BASE_FILENAME)
                   if s.kind == "frame"]
        self.base_sample = samples[-1] if samples else None

    def _build_calibration(self) -> None:
        if self.dark_paths:
            frames = [open_frame(p) for p in self.dark_paths]
            shutters = [f.acquisition.exposure_time or 1.0 for f in frames]
            black = frames[0].black_level
            med, _ = stack_darks([f.data for f in frames], shutters, black)
            self.dark_signal = med - black   # above-black, at max(shutters)
            self.dark_shutter = max(shutters)
        if self.flat_paths:
            frames = [open_frame(p) for p in self.flat_paths]
            flat_med = build_flat([f.data for f in frames])
            black = frames[0].black_level
            self.flat_shutter = frames[0].acquisition.exposure_time or 1.0
            self.flat_gain = frames[0].acquisition.gain
            signal = flat_med.astype(np.float64)
            if self.dark_signal is not None:
                signal -= rescale_dark(
                    self.dark_signal + self._dark_black(),
                    self.dark_shutter, self.flat_shutter,
                    self._dark_black())
            else:
                signal -= black
            self.flat_signal = signal

    def _dark_black(self) -> float:
        return open_frame(self.dark_paths[0]).black_level if self.dark_paths \
            else 0.0

    # ------------------------------------------------------------ inventory

    @property
    def frame_names(self) -> list[str]:
        return [f.name for f in self.frames]

    def entry(self, name: str) -> FrameEntry:
        for f in self.frames:
            if f.name == name:
                return f
        raise KeyError(name)

    def set_rect(self, name: str,
                 rect: tuple[int, int, int, int] | None) -> None:
        """Operator's image-area rect for one frame (GUI Shift-drag)."""
        self.manual_rects[name] = rect
        self.entry(name).rect = rect

    def rect_for(self, entry: FrameEntry) -> tuple[int, int, int, int] | None:
        return self.manual_rects.get(entry.name, entry.rect)

    # ------------------------------------------------------------ measuring

    def flat_at(self, shutter: float,
                gain: float | None = None) -> np.ndarray:
        """Flat signal (DN, above black) at a scan's exposure."""
        if self.flat_signal is None or self.flat_shutter is None:
            raise ValueError("project has no flat frames")
        factor = shutter / self.flat_shutter
        if gain is not None and self.flat_gain:
            factor *= gain / self.flat_gain
        return self.flat_signal * factor

    def scan_above_black(self, entry: FrameEntry) -> np.ndarray:
        """Scan DN with dark subtracted, above black, float64."""
        dn = entry.frame.data.astype(np.float64) - entry.frame.black_level
        if self.dark_signal is not None and self.dark_shutter is not None:
            # dark_signal is already above black; only the dark current --
            # never a pedestal, there is none left -- scales with shutter.
            dn -= self.dark_signal * (entry.shutter / self.dark_shutter)
        return dn

    def build_density(
        self, name: str, crop: bool = True, border: int = 0
    ) -> tuple[np.ndarray, dens.DensityProvenance]:
        """Density map for one frame, cropped to its image rect if known.

        The flat gain map is applied in full-frame coordinates before the crop
        (see module docstring). The returned map is float32 with NaN where the
        reading is not a measurement; provenance records the crop and the
        valid-pixel fraction.

        ``border`` (export "including border", user order 2026-09-22) grows
        the crop rect by that many sensor pixels on each side before clamping
        — the film edge the ROI hides from correction rides into the export.
        Where the frame runs out sooner than ``border``, the crop goes to the
        frame edge in that direction ("pokud by zbývalo méně, dej po kraj").
        """
        entry = self.entry(name)
        dn = self.scan_above_black(entry)
        flat_fallback = self.flat_signal is None
        if flat_fallback:
            # No flat frames: the operator still must *see* the frame. A
            # uniform reference from the frame's own highlights stands in for
            # the flat (design 04: the percentile base is a preview fallback,
            # never a measurement). Densities are then relative to the
            # frame's brightest 0.1 % -- recorded in provenance, flagged in
            # the GUI status; per-pixel flat structure is of course absent.
            level = float(np.percentile(dn, FLAT_FALLBACK_PERCENTILE))
            if level <= 0:
                raise ValueError(
                    f"{name} carries no signal and the project has no "
                    f"flat frames -- nothing to measure or preview")
            flat_sig = np.full(dn.shape, level, dtype=np.float64)
            flat_shutter = entry.shutter
        else:
            flat_sig = self.flat_at(entry.shutter, entry.gain)
            flat_shutter = self.flat_shutter
        # No separate gain-map correction: T = scan/flat per pixel *is* the
        # flat-field correction (per-pixel sensitivity and light fall-off
        # appear in numerator and denominator alike) -- dividing by a unit-mean
        # gain map first would cancel out. This is what makes transmittance
        # the honest measurement domain.
        t = dens.transmittance(dn, flat_sig)
        # The map's mask is the *illuminated* area; within it, T <= 0 becomes
        # +inf D (denser than measurable) and raw-clipped pixels -inf D
        # (brighter than recordable) -- directions on the scale, not unknowns.
        illum = dens.illuminated_mask(t, flat_sig)
        clipped = entry.frame.data >= entry.frame.white_level
        d = dens.density(t, illum, clipped=clipped)

        rect = self.rect_for(entry)
        prov = dens.DensityProvenance(
            source=entry.name,
            shutter=entry.shutter,
            gain=entry.gain,
            black_level=entry.frame.black_level,
            white_level=entry.frame.white_level,
            dark_files=tuple(p.name for p in self.dark_paths),
            flat_files=tuple(p.name for p in self.flat_paths),
            dark_shutter=self.dark_shutter,
            flat_shutter=flat_shutter,
            dmin_density=self.dmin_auto(),
            dmin_source=(self.base_sample.source if self.base_sample
                         else "none"),
            crop_rect=rect,
            valid_fraction=float(np.isfinite(d).mean()),
            flat_fallback=flat_fallback,
        )
        if rect is not None and crop:
            x0, y0, x1, y1 = rect
            if border:
                # Rozšíření O border pixelů: teprve pak se clampuje na rámec —
                # ubude-li pixelů méně než border, směr jde „po kraj“.
                x0, y0 = x0 - int(border), y0 - int(border)
                x1, y1 = x1 + int(border), y1 + int(border)
            h, w = d.shape
            x0, y0 = max(0, min(int(x0), w - 1)), max(0, min(int(y0), h - 1))
            x1, y1 = max(x0 + 1, min(int(x1), w)), max(y0 + 1, min(int(y1), h))
            d = np.ascontiguousarray(d[y0:y1, x0:x1])
            prov = replace(prov, crop_rect=(x0, y0, x1, y1),
                           valid_fraction=float(np.isfinite(d).mean()))
        return d, prov

    # ---------------------------------------------------------- scale points

    def dark_adjusted(self, sample) -> "object":
        """A base sample with the dark current its frame accumulated removed.

        ``FilmBaseSample.mean_dn`` is a raw region mean -- dark current and all.
        The flat signal it gets divided by is dark-subtracted, so comparing
        them directly overstates T_base (on B1: ~0.009 D). The dark master is
        per-pixel, so the correction is the dark under the sample's rect,
        scaled to the sample's shutter.
        """
        if self.dark_signal is None or self.dark_shutter is None:
            return sample
        from filmscan_studio.core.filmbase import region_mean
        dark_dn = region_mean(
            self.dark_signal * (sample.shutter / self.dark_shutter),
            sample.rect)
        return replace(sample, mean_dn=sample.mean_dn - dark_dn)

    def dmin_auto(self) -> float | None:
        """Dmin from the stored base sample, or None when unmeasured.

        A *suggested* scale point only — densities in the archive stay
        absolute; nothing here subtracts Dmin from pixels.
        """
        if self.base_sample is None or self.flat_signal is None:
            return None
        try:
            return dens.measure_dmin(self.dark_adjusted(self.base_sample),
                                     self.flat_signal,
                                     self.flat_shutter, self.flat_gain)
        except (ValueError, IndexError) as exc:
            log.warning("Dmin measurement failed: %s", exc)
            return None

    def dmin_candidates(self) -> list[float]:
        """Every storable base sample as a Dmin, for the reproducibility check."""
        if self.flat_signal is None:
            return []
        samples = load_samples(self.root / FILM_BASE_FILENAME)
        out: list[float] = []
        for s in samples:
            try:
                out.append(dens.measure_dmin(self.dark_adjusted(s),
                                             self.flat_signal,
                                             self.flat_shutter,
                                             self.flat_gain))
            except (ValueError, IndexError):
                continue
        return out

    def suggested_dmax(self, name: str) -> float:
        """Per-frame dmax estimate (04 Q1: strategy still open, per-frame is
        the GUI default until roll-wide data exists)."""
        d, _ = self.build_density(name)
        return dens.estimate_dmax(d)


def _film_block(root: Path, frames: "list[FrameEntry]") -> dict:
    """The roll's ``film`` block: ``project.json`` first, sidecars fallback.

    Older sessions predate ``project.json`` on disk (the capture app wrote
    it only to the catalog) — the same ``film`` block travels in every
    frame sidecar, so fall back to the first sidecar that carries it.
    Without the fallback the orientation flag was silently ignored and the
    picture never flipped (K16_O02, 2026-09-21).
    """
    try:
        data = json.loads((root / "project.json").read_text(encoding="utf-8"))
        film = data.get("film") or {}
        if film:
            return film
    except (OSError, json.JSONDecodeError):
        pass                        # no capture project.json: try sidecars
    for entry in frames:
        film = entry.record.get("film") or {}
        if film:
            return film
    return {}


def _kind_from_name(filename: str) -> str:
    lower = filename.lower()
    for kind in (FrameKind.DARK, FrameKind.FLAT, FrameKind.BASE):
        if lower.startswith(kind.value):
            return kind.value
    return FrameKind.SCAN.value


def _rect_from_record(record: dict,
                      frame: RawFrame) -> tuple[int, int, int, int] | None:
    """The image-area rect the capture layer recorded (doc 06).

    The capture layer's field is ``crop_rect`` at the sidecar top level;
    ``image_rect`` (the name this repo asked for first) is accepted too, in
    the sidecar or inside ``acquisition``, so either spelling works.
    """
    rect = record.get("crop_rect")
    if rect is None:
        rect = record.get("image_rect")
    if rect is None:
        acq = record.get("acquisition") or {}
        rect = acq.get("image_rect") or acq.get("crop_rect")
    if not rect or len(rect) != 4:
        return None
    x0, y0, x1, y1 = (int(v) for v in rect)
    # Entirely outside this frame's geometry (a rect from another sensor
    # version) is "no rect", not a degenerate sliver.
    if x1 <= x0 or y1 <= y0 or x0 >= frame.width or y0 >= frame.height:
        return None
    return (max(0, x0), max(0, y0), min(x1, frame.width),
            min(y1, frame.height))
