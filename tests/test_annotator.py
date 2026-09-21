"""Annotator store tests: parsers and the merge-save that must never lose data.

The fixtures mirror the real sidecar shape written by the capture app
(schema-v3 sidecar with film/acquisition blocks, a preview jpg beside the
TIFF, dark/flat/base frames mixed into the same folder).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from filmscan_studio.annotator.store import (
    AnnotationItem,
    apply_common,
    build_common_patch,
    gps_fields,
    load_folder,
    parse_capture_datetime,
    parse_wgs84_pair,
    save_annotation,
    to_dms,
)
from filmscan_studio.core.models import CaptureRecord, FrameAnnotation


# ------------------------------------------------------------------ datetime

@pytest.mark.parametrize("raw,expected", [
    ("12.8.1968", "1968:08:12 00:00:00"),
    ("12.08.1968 14:30", "1968:08:12 14:30:00"),
    ("1968-08-12", "1968:08:12 00:00:00"),
    ("1968-08-12 14:30:05", "1968:08:12 14:30:05"),
    ("1968:08:12 14:30:05", "1968:08:12 14:30:05"),   # EXIF round-trip
    ("1968", "1968:01:01 00:00:00"),                   # year only -> January
    ("  12.8.1968  ", "1968:08:12 00:00:00"),          # whitespace
])
def test_parse_capture_datetime(raw: str, expected: str) -> None:
    assert parse_capture_datetime(raw) == expected


def test_parse_capture_datetime_empty_and_garbage() -> None:
    assert parse_capture_datetime("  ") == ""
    for bad in ("léto 1968", "13.13.1968", "1.1.68", "když jsem byl malej"):
        with pytest.raises(ValueError):
            parse_capture_datetime(bad)


def test_parsed_datetime_passes_model_validation() -> None:
    """What the parser emits, the model must accept (they are one contract)."""
    FrameAnnotation(capture_datetime=parse_capture_datetime("12.8.1968"))
    with pytest.raises(ValueError):
        FrameAnnotation(capture_datetime="léto 1968")


# ---------------------------------------------------------------------- GPS

def test_parse_wgs84_pair_suffixes_and_signs() -> None:
    lat, lon, la, lo = parse_wgs84_pair("49.1124306N, 9.7371244E")
    assert (lat, lon, la, lo) == (pytest.approx(49.1124306),
                                  pytest.approx(9.7371244), "N", "E")
    lat, lon, la, lo = parse_wgs84_pair("49.1124306, 9.7371244")
    assert (la, lo) == ("N", "E")
    lat, lon, la, lo = parse_wgs84_pair("-33.87 S 151.21 W")
    assert lat == pytest.approx(-33.87) and la == "S"
    assert lon == pytest.approx(-151.21) and lo == "W"


def test_parse_wgs84_pair_rejects_garbage() -> None:
    for bad in ("49.1", "N, E", "1234.5, 9.7", "91, 0", "0, 181"):
        with pytest.raises(ValueError):
            parse_wgs84_pair(bad)


def test_to_dms() -> None:
    assert to_dms(49.1124306, "N") == '49 deg 6\' 44.7502" N'


def test_gps_fields_group() -> None:
    fields = gps_fields("49.1124306N, 9.7371244E")
    assert fields["gps_lat"] == "49.11243060"
    assert fields["gps_lat_ref"] == "N"
    assert fields["gps_lon_exif_dms"].endswith('" E')
    assert gps_fields("") == {}


# ------------------------------------------------------------- folder loading

def _write_sidecar(frames: Path, name: str, kind: str, **extra) -> Path:
    tif = frames / name
    tif.write_bytes(b"\x00")   # pixels are irrelevant to the store
    sidecar = tif.with_suffix(tif.suffix + ".json")
    record = {"schema_version": 3, "kind": kind, "filename": name,
              "film": {"schema_version": 3, "film_id": "K16_X",
                       "mirrored_vertical": True},
              "acquisition": {"exposure_time": 0.011}}
    record.update(extra)
    sidecar.write_text(json.dumps(record, ensure_ascii=False),
                       encoding="utf-8")
    return tif


def test_load_folder_lists_scans_only(tmp_path: Path) -> None:
    frames = tmp_path / "frames"
    frames.mkdir()
    _write_sidecar(frames, "frame001.tif", "scan")
    _write_sidecar(frames, "frame002.tif", "scan")
    _write_sidecar(frames, "dark_001.tif", "dark")
    _write_sidecar(frames, "flat_001.tif", "flat")
    _write_sidecar(frames, "base_001.tif", "base")
    # A preview jpg joins frame001 only.
    (frames / "frame001.jpg").write_bytes(b"\xff\xd8")

    proj = load_folder(tmp_path)
    assert [i.name for i in proj.items] == ["frame001.tif", "frame002.tif"]
    assert proj.items[0].preview_path == frames / "frame001.jpg"
    assert proj.items[1].preview_path is None
    # Orientation from the film block of the first sidecar.
    assert proj.mirrored_vertical is True
    assert proj.film_id == "K16_X"


def test_load_folder_flat_layout_and_name_guessing(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "frame001.tif", "scan")
    (tmp_path / "stray.tif").write_bytes(b"\x00")   # no sidecar: guess scan
    proj = load_folder(tmp_path)
    assert {i.name for i in proj.items} == {"frame001.tif", "stray.tif"}
    assert proj.items[1].record == {}               # no sidecar, thin item


def test_load_folder_orientation_prefers_project_json(tmp_path: Path) -> None:
    frames = tmp_path / "frames"
    frames.mkdir()
    _write_sidecar(frames, "frame001.tif", "scan")   # sidecar: mirrored_vertical
    (tmp_path / "project.json").write_text(json.dumps(
        {"film": {"film_id": "K16_X", "rotated_180": True}}), encoding="utf-8")
    proj = load_folder(tmp_path)
    assert proj.rotated_180 is True
    assert proj.mirrored_vertical is False


# ------------------------------------------------------------ merge-save safety

def _full_record() -> dict:
    """A sidecar shaped like the real K16O04 one, incl. a key this repo's
    model does not know (a foreign tool's) — merge must keep it verbatim."""
    return {
        "schema_version": 3,
        "capture_id": "890c28fa86ec429a90a42796e7174dd6",
        "film_id": "K16O04_2026-07-22",
        "frame_number": 1,
        "kind": "scan",
        "filename": "frame001.tif",
        "file_format": "raw",
        "width": 6224,
        "height": 4168,
        "black_level": 0.0,
        "white_level": 65535.0,
        "sensor_temperature_c": 10.0,
        "film": {
            "schema_version": 3, "film_id": "K16O04_2026-07-22",
            "film_name": "Fomapan 100 Classic", "camera": "Nikon FM2",
            "format": "35mm", "film_type_class": "bw_negative",
            "mirrored_horizontal": False, "mirrored_vertical": True,
            "rotated_180": False, "operator": "JG", "pushed_stops": 0.0,
        },
        "acquisition": {
            "schema_version": 3, "camera": "ATR2600M", "gain": 1.0,
            "exposure_time": 0.011111,
            "capture_date": "2026-09-20T16:49:09.770479+02:00",
            "copy_number": 1, "frames_averaged": 10,
            "conversion_gain": "LCG", "low_noise": True,
        },
        "created_at": "2026-09-20T16:49:09.770651+02:00",
        "notes": None,
        "crop_rect": [669, 168, 6065, 3760],
        "ciz nastroj": {"a": 1},          # foreign key: must survive verbatim
    }


def _item(tmp_path: Path, record: dict) -> AnnotationItem:
    tif = tmp_path / "frame001.tif"
    tif.write_bytes(b"\x00")
    sidecar = tif.with_suffix(".tif.json")
    sidecar.write_text(json.dumps(record, ensure_ascii=False),
                       encoding="utf-8")
    return AnnotationItem(image_path=tif, sidecar_path=sidecar,
                          preview_path=None, record=dict(record))


def test_save_annotation_preserves_every_other_key(tmp_path: Path) -> None:
    record = _full_record()
    item = _item(tmp_path, record)
    annotation = FrameAnnotation(
        title="Bosna po tjenisti", capture_datetime="1968:01:01 00:00:00",
        rating=4, tags=["vlak"],
        **gps_fields("49.1124306N, 9.7371244E")).model_dump(mode="json")

    save_annotation(item, annotation)

    on_disk = json.loads((tmp_path / "frame001.tif.json").read_text(
        encoding="utf-8"))
    assert on_disk["annotation"] == annotation
    untouched = {k: v for k, v in on_disk.items() if k != "annotation"}
    assert untouched == {k: v for k, v in record.items() if k != "annotation"}
    # ...and with this repo's own keys it is still a CaptureRecord the
    # capture app can load (the foreign key is nobody's model's business).
    own = {k: v for k, v in on_disk.items() if k != "ciz nastroj"}
    assert CaptureRecord.model_validate(own).annotation.rating == 4


def test_save_annotation_roundtrip_is_stable(tmp_path: Path) -> None:
    record = _full_record()
    item = _item(tmp_path, record)
    annotation = FrameAnnotation(title="A").model_dump(mode="json")
    save_annotation(item, annotation)
    first = (tmp_path / "frame001.tif.json").read_text(encoding="utf-8")
    save_annotation(item, annotation)
    assert (tmp_path / "frame001.tif.json").read_text(encoding="utf-8") \
        == first


def test_save_annotation_rejects_invalid_block(tmp_path: Path) -> None:
    """A broken annotation must not reach the disk at all."""
    item = _item(tmp_path, _full_record())
    before = (tmp_path / "frame001.tif.json").read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        save_annotation(item, {"capture_datetime": "léto 1968"})
    assert (tmp_path / "frame001.tif.json").read_text(encoding="utf-8") \
        == before


# ---------------------------------------------------------------- bulk apply

def test_build_common_patch_skips_empty_and_carries_gps_group() -> None:
    patch = build_common_patch({
        "capture_datetime": "1968:01:01 00:00:00",
        "tags": ["vlak"], "rating": 3,
        "gps_input": "49.1124306N, 9.7371244E",
        "title": "nepřenositelné", "note": "",
    })
    assert patch["capture_datetime"] == "1968:01:01 00:00:00"
    assert patch["rating"] == 3
    assert "gps_lat" in patch and patch["gps_lat_ref"] == "N"
    assert "title" not in patch and "note" not in patch


def test_apply_common_keeps_titles_and_fills_dates(tmp_path: Path) -> None:
    records = []
    for num in (1, 2, 3):
        rec = _full_record()
        rec["filename"] = rec["capture_id"] = f"frame{num:03d}"
        rec["annotation"] = FrameAnnotation(
            title=f"popisek {num}",
            **gps_fields("50.1N, 14.2E")).model_dump(mode="json")
        records.append(rec)
    items = []
    for num, rec in enumerate(records, start=1):
        tif = tmp_path / f"frame{num:03d}.tif"
        tif.write_bytes(b"\x00")
        sidecar = tif.with_suffix(".tif.json")
        sidecar.write_text(json.dumps(rec, ensure_ascii=False),
                           encoding="utf-8")
        items.append(AnnotationItem(image_path=tif, sidecar_path=sidecar,
                                    preview_path=None, record=rec))

    patch = build_common_patch({
        "capture_datetime": parse_capture_datetime("1968"),
        "gps_input": "49.1124306N, 9.7371244E",
        "rating": 5, "tags": [],
    })
    assert apply_common(items, patch) == 3

    for num, item in enumerate(items, start=1):
        on_disk = json.loads(item.sidecar_path.read_text(encoding="utf-8"))
        ann = on_disk["annotation"]
        assert ann["title"] == f"popisek {num}"        # per-frame text intact
        assert ann["capture_datetime"] == "1968:01:01 00:00:00"
        assert ann["gps_lat"] == "49.11243060"          # GPS replaced as unit
        assert ann["gps_lon_exif_dms"].endswith('" E')
        assert ann["rating"] == 5
        assert on_disk["crop_rect"] == [669, 168, 6065, 3760]
        own = {k: v for k, v in on_disk.items() if k != "ciz nastroj"}
        CaptureRecord.model_validate(own)


def test_apply_common_empty_patch_writes_nothing(tmp_path: Path) -> None:
    item = _item(tmp_path, _full_record())
    before = item.sidecar_path.read_text(encoding="utf-8")
    assert apply_common([item], build_common_patch({"gps_input": "  "})) == 0
    assert item.sidecar_path.read_text(encoding="utf-8") == before


def test_is_annotated_flag(tmp_path: Path) -> None:
    item = _item(tmp_path, _full_record())
    assert item.is_annotated is False
    save_annotation(item, FrameAnnotation().model_dump(mode="json"))
    assert item.is_annotated is False       # an all-empty block is not one
    save_annotation(item, FrameAnnotation(note="x").model_dump(mode="json"))
    assert item.is_annotated is True


# ------------------------------------------------------------------ GUI smoke

def test_gui_smoke_offscreen(tmp_path, qtbot, monkeypatch) -> None:
    """Offscreen: window builds, list fills, save writes the sidecar."""
    pytest.importorskip("pytestqt")
    from filmscan_studio.annotator.gui import MainWindow

    frames = tmp_path / "frames"
    frames.mkdir()
    _write_sidecar(frames, "frame001.tif", "scan")
    _write_sidecar(frames, "frame002.tif", "scan")

    win = MainWindow(project=load_folder(tmp_path))
    qtbot.addWidget(win)
    assert win.frame_list.count() == 2
    assert win.current is win.project.items[0]

    win.ed_title.setText("První")
    win.ed_date.setText("1968")
    win.ed_gps.setText("49.1124306N, 9.7371244E")
    win._save_current()

    on_disk = json.loads(
        (frames / "frame001.tif.json").read_text(encoding="utf-8"))
    ann = on_disk["annotation"]
    assert ann["title"] == "První"
    assert ann["capture_datetime"] == "1968:01:01 00:00:00"
    assert ann["gps_lat"] == "49.11243060"

    # Bad date must not write and must not crash.
    win.ed_date.setText("léto 1968")
    import filmscan_studio.annotator.gui as gui_mod
    called = {"n": 0}

    def fake_critical(*a, **k):
        called["n"] += 1

    monkeypatch.setattr(gui_mod.QMessageBox, "critical",
                        staticmethod(fake_critical))
    win._save_current()
    assert called["n"] == 1
    on_disk = json.loads(
        (frames / "frame001.tif.json").read_text(encoding="utf-8"))
    assert on_disk["annotation"]["capture_datetime"] == "1968:01:01 00:00:00"
