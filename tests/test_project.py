"""Tests for the developer project folder loader, on a synthetic session.

Builds a miniature B1-layout folder (frames/ with TIFs + sidecars +
film_base.json) so the loading, kind classification, calibration and density
math are all pinned without requiring the real sample folder.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from filmscan_studio.core.filmbase import FILM_BASE_FILENAME, FilmBaseSample, append_sample
from filmscan_studio.core.models import AcquisitionMetadata
from filmscan_studio.core.rawio import write_frame
from filmscan_studio.developer.project import DevelopProject

SHUTTER_DARK = 0.001
SHUTTER_FLAT = 0.0003
SHUTTER_SCAN = 0.001
FLAT_LEVEL = 40_000.0
TRUE_DMIN = 0.40            # base transmits 10^-0.4 = 0.398 of the light


def _write(path: Path, data: np.ndarray, kind: str, shutter: float,
           extra: dict | None = None) -> None:
    """Write frame + sidecar the way the capture layer does.

    Exposure lives in the *embedded* acquisition JSON (rawio's self-describing
    header); the sidecar repeats the facts and adds ``kind``.
    """
    acq = AcquisitionMetadata(exposure_time=shutter, gain=1.0)
    write_frame(path, data, acquisition=acq)
    payload = {
        "schema_version": 3,
        "kind": kind,
        "filename": path.name,
        "width": data.shape[1], "height": data.shape[0],
        "black_level": 0, "white_level": 65535,
        "acquisition": {
            "exposure_time": shutter, "gain": 1.0, "iso": None,
            "capture_date": datetime.now(timezone.utc).isoformat(),
        },
    }
    payload.update(extra or {})
    path.with_suffix(path.suffix + ".json").write_text(
        json.dumps(payload), encoding="utf-8")


@pytest.fixture
def session(tmp_path: Path) -> Path:
    frames = tmp_path / "frames"
    frames.mkdir()
    rng = np.random.default_rng(7)
    dark = np.full((48, 64), 260.0) + rng.normal(0, 5, (48, 64))
    flat = np.full((48, 64), FLAT_LEVEL) * rng.uniform(0.97, 1.03, (48, 64))

    for i in range(3):
        _write(frames / f"dark_{i:03d}.tif", dark.astype(np.uint16),
               "dark", SHUTTER_DARK)
    for i in range(3):
        _write(frames / f"flat_{i:03d}.tif", flat.astype(np.uint16),
               "flat", SHUTTER_FLAT)

    # A negative frame: a D-log-E ramp from base to base+2 across x.
    density = 0.4 + 2.0 * np.linspace(0.0, 1.0, 64)[None, :]
    density = np.repeat(density, 48, axis=0)
    signal = FLAT_LEVEL * 10.0 ** (-density) * (SHUTTER_SCAN / SHUTTER_FLAT)
    scan_dn = np.clip(signal + 260.0, 0, 65535).astype(np.uint16)
    p = frames / "frame001.tif"
    _write(p, scan_dn, "scan", SHUTTER_SCAN)

    # A base frame: clear film only.
    base_level = FLAT_LEVEL * 10 ** (-TRUE_DMIN) * (0.0005 / SHUTTER_FLAT) \
        + 260.0
    base_dn = np.full((48, 64), base_level).clip(0, 65535).astype(np.uint16)
    _write(frames / "base_001.tif", base_dn, "base", 0.0005)

    append_sample(tmp_path / FILM_BASE_FILENAME, FilmBaseSample(
        kind="frame", mean_dn=float(base_dn[10:40, 10:54].mean()),
        black_level=0.0, white_level=65535.0, shutter=0.0005, gain=1.0,
        rect=(10, 10, 54, 40), source="base_001.tif",
        captured_at=datetime.now(timezone.utc),
    ))
    return tmp_path


class TestOpen:
    def test_classifies_by_sidecar_kind(self, session) -> None:
        proj = DevelopProject.open(session)
        assert proj.frame_names == ["frame001.tif"]
        assert len(proj.dark_paths) == 3
        assert len(proj.flat_paths) == 3
        assert proj.base_sample is not None
        assert proj.dark_shutter == SHUTTER_DARK
        assert proj.flat_shutter == SHUTTER_FLAT

    def test_kind_from_name_fallback_without_sidecar(self, session) -> None:
        # Move a dark's sidecar away: the name prefix must classify it.
        side = session / "frames" / "dark_000.tif.json"
        side.unlink()
        proj = DevelopProject.open(session)
        assert len(proj.dark_paths) == 3

    def test_missing_folder_raises(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="no frames"):
            DevelopProject.open(tmp_path / "nonexistent")


class TestDensity:
    def test_density_recovers_the_true_ramp(self, session) -> None:
        proj = DevelopProject.open(session)
        d, prov = proj.build_density("frame001.tif")
        assert d.shape == (48, 64) and prov.crop_rect is None
        # The synthetic ramp is noiseless except dark noise + flat texture;
        # median column-wise and compare to truth at mid-columns.
        col = np.nanmedian(d, axis=0)
        truth = 0.4 + 2.0 * np.linspace(0.0, 1.0, 64)
        mid = slice(8, 56)
        assert np.abs(col[mid] - truth[mid]).max() < 0.05

    def test_dmin_matches_true_base(self, session) -> None:
        proj = DevelopProject.open(session)
        assert proj.dmin_auto() == pytest.approx(TRUE_DMIN, abs=0.01)

    def test_suggested_dmax_covers_ramp(self, session) -> None:
        proj = DevelopProject.open(session)
        assert 2.2 < proj.suggested_dmax("frame001.tif") < 2.7

    def test_crop_reduces_map_and_records_rect(self, session) -> None:
        proj = DevelopProject.open(session)
        proj.set_rect("frame001.tif", (16, 8, 48, 40))
        d, prov = proj.build_density("frame001.tif")
        assert d.shape == (32, 32)
        assert prov.crop_rect == (16, 8, 48, 40)

    def test_image_rect_read_from_record(self, session) -> None:
        side = session / "frames" / "frame001.tif.json"
        data = json.loads(side.read_text())
        data["image_rect"] = [4, 4, 60, 44]
        side.write_text(json.dumps(data))
        proj = DevelopProject.open(session)
        assert proj.rect_for(proj.frames[0]) == (4, 4, 60, 44)

    def test_crop_rect_outside_frame_falls_back_to_none(self, session) -> None:
        # A rect from a different sensor geometry (e.g. the real test2 rect
        # on our tiny synthetic frame) must not produce a degenerate crop.
        side = session / "frames" / "frame001.tif.json"
        data = json.loads(side.read_text())
        data["crop_rect"] = [597, 216, 6164, 3889]
        side.write_text(json.dumps(data))
        proj = DevelopProject.open(session)
        assert proj.rect_for(proj.frames[0]) is None

    def test_crop_rect_in_frame_geometry(self, session) -> None:
        side = session / "frames" / "frame001.tif.json"
        data = json.loads(side.read_text())
        data["crop_rect"] = [8, 6, 56, 40]
        side.write_text(json.dumps(data))
        proj = DevelopProject.open(session)
        assert proj.rect_for(proj.frames[0]) == (8, 6, 56, 40)
        d, prov = proj.build_density("frame001.tif")
        assert d.shape == (34, 48) and prov.crop_rect == (8, 6, 56, 40)

    def test_saturation_marked_minus_inf(self, session) -> None:
        # Blow the frame's left edge to full scale: the film there let more
        # light through than the sensor can hold -- the direction is known
        # (thinner than measurable), so the pixel carries -inf D, not NaN.
        # NaN is reserved for "no light reached".
        p = session / "frames" / "frame001.tif"
        from filmscan_studio.core.rawio import open_frame, write_frame
        fr = open_frame(p)
        data = fr.data.copy()
        data[:, :8] = 65535
        write_frame(p, data, acquisition=fr.acquisition)
        proj = DevelopProject.open(session)
        d, _ = proj.build_density("frame001.tif")
        assert np.isneginf(d[:, :8]).all()
        assert np.isfinite(d[:, 16:]).all()


class TestOrientation:
    def test_flags_loaded_from_project_json(self, session) -> None:
        payload = {"schema_version": 1, "film": {
            "film_id": "x", "mirrored_horizontal": True,
            "mirrored_vertical": False, "rotated_180": False}}
        (session / "project.json").write_text(json.dumps(payload))
        proj = DevelopProject.open(session)
        assert proj.mirrored_horizontal and not proj.mirrored_vertical
        assert not proj.rotated_180

    def test_no_project_json_means_identity(self, session) -> None:
        proj = DevelopProject.open(session)
        assert not (proj.mirrored_horizontal or proj.mirrored_vertical
                    or proj.rotated_180)
        d = np.arange(12, dtype=np.float32).reshape(3, 4)
        assert np.array_equal(proj.orientation_apply(d), d)

    def test_horizontal_mirror_flips_columns(self, session) -> None:
        payload = {"film": {"film_id": "x", "mirrored_horizontal": True}}
        (session / "project.json").write_text(json.dumps(payload))
        proj = DevelopProject.open(session)
        d = np.arange(6, dtype=np.float32).reshape(2, 3)
        assert np.array_equal(proj.orientation_apply(d), d[:, ::-1])

    def test_all_three_compose(self, session) -> None:
        payload = {"film": {"film_id": "x", "mirrored_horizontal": True,
                            "mirrored_vertical": True}}
        (session / "project.json").write_text(json.dumps(payload))
        proj = DevelopProject.open(session)
        d = np.arange(6, dtype=np.float32).reshape(2, 3)
        # H+V mirror == rot180.
        assert np.array_equal(proj.orientation_apply(d), np.rot90(d, 2))

    def test_density_archive_stays_unflipped(self, session) -> None:
        payload = {"film": {"film_id": "x", "mirrored_horizontal": True}}
        (session / "project.json").write_text(json.dumps(payload))
        proj = DevelopProject.open(session)
        d, _ = proj.build_density("frame001.tif")
        # The ramp rises with x in raw sensor orientation; a flipped map would
        # fall. The archive must keep rising.
        assert d[24, 60] > d[24, 4]


class TestSettingsPersistence:
    def test_manual_rect_survives_reopen(self, session) -> None:
        proj = DevelopProject.open(session)
        proj.set_rect("frame001.tif", (4, 4, 60, 44))
        proj.save_settings()
        again = DevelopProject.open(session)
        assert again.rect_for(again.entry("frame001.tif")) == (4, 4, 60, 44)

    def test_frame_settings_round_trip(self, session) -> None:
        proj = DevelopProject.open(session)
        proj.frame_settings["frame001.tif"] = {
            "dmin": 0.5, "dmax": 3.0, "exposure_ev": 0.7,
            "curve_model": "spline", "dmax_source": "manual",
            "name": "custom", "toe": 0.2, "gamma": 1.3, "shoulder": 0.6,
            "dmin_manual": True}
        proj.save_settings()
        again = DevelopProject.open(session)
        stored = again.frame_settings["frame001.tif"]
        assert stored["dmax"] == 3.0 and stored["dmin_manual"] is True

    def test_missing_settings_file_is_not_an_error(self, session) -> None:
        proj = DevelopProject.open(session)   # no develop_settings.json yet
        assert proj.frame_settings == {}
