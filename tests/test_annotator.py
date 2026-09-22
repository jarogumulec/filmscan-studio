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
    migrate_camera_split,
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


# --------------------------------------------------------- camera split migration

def _camera_session(tmp_path: Path, camera: str,
                    already_split: bool = False) -> Path:
    """Skutečná K16O01/06 situace: volné `film.camera` napříč sidecary."""
    frames = tmp_path / "frames"
    frames.mkdir()
    film = {"schema_version": 3, "film_id": "K16_X", "camera": camera}
    if already_split:
        film.update(camera_make="ERNST LEITZ WETZLAR GMBH",
                    camera_model="Leica R4s MOD.2")
    for num in (1, 2):
        _write_sidecar(frames, f"frame{num:03d}.tif", "scan", film=film)
    return frames


def test_migrate_camera_split_leica_once(tmp_path: Path) -> None:
    """Povel 2026-09-22: „tuto sadu už v jsonech rozděl dle návrhu" —
    'ERNST LEITZ WETZLAR GMBH Leica R4s MOD.2' musi splitnout na entire
    uppercase run + zbytek, zapsat do každého sidecaru."""
    frames = _camera_session(tmp_path,
                             "ERNST LEITZ WETZLAR GMBH Leica R4s MOD.2")
    proj = load_folder(tmp_path)
    assert migrate_camera_split(proj) == 2
    for num in (1, 2):
        film = json.loads((frames / f"frame{num:03d}.tif.json")
                          .read_text(encoding="utf-8"))["film"]
        assert film["camera_make"] == "ERNST LEITZ WETZLAR GMBH"
        assert film["camera_model"] == "Leica R4s MOD.2"
        assert film["camera"] == "ERNST LEITZ WETZLAR GMBH Leica R4s MOD.2"
    # druhý průchod už nic nepřepíše (idempotentní — operátor mohl ručně fixnout)
    assert migrate_camera_split(load_folder(tmp_path)) == 0


def test_migrate_camera_split_never_touches_manual_fix(tmp_path: Path) -> None:
    """Sidecar s make/model už rozděleným (ručně) se NESMÍ přepsat heuristikou."""
    frames = _camera_session(tmp_path, "Zenit E", already_split=True)
    proj = load_folder(tmp_path)
    assert migrate_camera_split(proj) == 0
    film = json.loads((frames / "frame001.tif.json")
                      .read_text(encoding="utf-8"))["film"]
    assert film["camera_model"] == "Leica R4s MOD.2"   # ruční zápis přežil


def test_migrate_camera_split_syncs_project_json(tmp_path: Path) -> None:
    frames = _camera_session(tmp_path, "Nikon FM2")
    (tmp_path / "project.json").write_text(json.dumps(
        {"film": {"film_id": "K16_X", "camera": "Nikon FM2"}}),
        encoding="utf-8")
    proj = load_folder(tmp_path)
    assert migrate_camera_split(proj) == 2
    film = json.loads((tmp_path / "project.json")
                      .read_text(encoding="utf-8"))["film"]
    assert (film["camera_make"], film["camera_model"]) == ("Nikon", "FM2")


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
    assert win.grid.count() == 2
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


# ------------------------------------------------- sequential time (2026-09-21)

def _three_items(tmp_path):
    items = []
    for num in (1, 2, 3):
        rec = _full_record()
        rec["filename"] = rec["capture_id"] = f"frame{num:03d}"
        tif = tmp_path / f"frame{num:03d}.tif"
        tif.write_bytes(b"\x00")
        sidecar = tif.with_suffix(".tif.json")
        sidecar.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        items.append(AnnotationItem(image_path=tif, sidecar_path=sidecar,
                                    preview_path=None, record=rec))
    return items


def test_date_only_bulk_numbers_minutes(tmp_path) -> None:
    """A bare date means "unknown time": frames get 00:01, 00:02, 00:03 … so
    a photo manager cannot shuffle the roll (operator order 2026-09-21)."""
    items = _three_items(tmp_path)
    patch = build_common_patch({"capture_datetime": "1.1.2026"})
    assert patch["_sequential_time"] is True
    assert apply_common(items, patch) == 3
    stamps = []
    for it in items:
        ann = json.loads(it.sidecar_path.read_text())["annotation"]
        stamps.append(ann["capture_datetime"])
    assert stamps == ["2026:01:01 00:01:00", "2026:01:01 00:02:00",
                      "2026:01:01 00:03:00"]


def test_explicit_time_bulk_is_exact(tmp_path) -> None:
    items = _three_items(tmp_path)
    patch = build_common_patch({"capture_datetime": "1.1.2026 14:30"})
    assert "_sequential_time" not in patch
    apply_common(items, patch)
    for it in items:
        ann = json.loads(it.sidecar_path.read_text())["annotation"]
        assert ann["capture_datetime"] == "2026:01:01 14:30:00"


def test_second_date_resets_the_minute_counter(tmp_path) -> None:
    """A roll with two dates: each bulk apply starts its own numbering."""
    items = _three_items(tmp_path)
    apply_common(items[:2], build_common_patch({"capture_datetime": "1.1.1968"}))
    apply_common(items[2:], build_common_patch({"capture_datetime": "2.1.1968"}))
    first = json.loads(items[0].sidecar_path.read_text())["annotation"]
    second = json.loads(items[1].sidecar_path.read_text())["annotation"]
    third = json.loads(items[2].sidecar_path.read_text())["annotation"]
    assert first["capture_datetime"] == "1968:01:01 00:01:00"
    assert second["capture_datetime"] == "1968:01:01 00:02:00"
    assert third["capture_datetime"] == "1968:01:02 00:01:00"


# ------------------------------------------- title/note bulk transfer + rotation

def test_title_note_transfer_only_when_opted_in(tmp_path) -> None:
    items = _three_items(tmp_path)
    form = {"title": "Výlet", "note": "komentář", "capture_datetime": ""}
    # default: per-frame labels are untouchable by the bulk action
    assert "title" not in build_common_patch(form)
    assert "note" not in build_common_patch(form)
    # opt-in: they travel
    patch = build_common_patch(form, transfer={"title": True, "note": True})
    assert patch["title"] == "Výlet" and patch["note"] == "komentář"
    apply_common(items, patch)
    for it in items:
        ann = json.loads(it.sidecar_path.read_text())["annotation"]
        assert ann["title"] == "Výlet" and ann["note"] == "komentář"


def test_rotation_degrees_roundtrip(tmp_path) -> None:
    item = _item(tmp_path, _full_record())
    save_annotation(item, FrameAnnotation(rotation_degrees=270)
                    .model_dump(mode="json"))
    on_disk = json.loads(item.sidecar_path.read_text(encoding="utf-8"))
    assert on_disk["annotation"]["rotation_degrees"] == 270
    assert CaptureRecord.model_validate(
        {k: v for k, v in on_disk.items() if k != "ciz nastroj"}
    ).annotation.rotation_degrees == 270
    with pytest.raises(ValueError):
        FrameAnnotation(rotation_degrees=100)


# ------------------------------------------------- acquisition + film editing

def test_save_acquisition_typed_merge(tmp_path) -> None:
    from filmscan_studio.annotator.store import save_acquisition

    item = _item(tmp_path, _full_record())
    save_acquisition(item, {"camera": "ATR2600M", "exposure_time": "0.011111",
                            "camera_serial": "SN123", "capture_date": ""})
    on_disk = json.loads(item.sidecar_path.read_text(encoding="utf-8"))
    acq = on_disk["acquisition"]
    assert acq["exposure_time"] == pytest.approx(0.011111)  # typed on disk
    assert CaptureRecord.model_validate(
        {k: v for k, v in on_disk.items() if k != "ciz nastroj"}
    ).acquisition.exposure_time == pytest.approx(0.011111)
    assert acq["camera_serial"] == "SN123"
    assert acq["conversion_gain"] == "LCG"          # untouched keys survive
    # garbage number rejected BEFORE writing
    with pytest.raises(ValueError):
        save_acquisition(item, {"exposure_time": "půl dvanácté"})


def test_save_film_updates_every_sidecar_and_project_json(tmp_path) -> None:
    from filmscan_studio.annotator.store import save_film

    frames = tmp_path / "frames"
    frames.mkdir()
    for num in (1, 2):
        _write_sidecar(frames, f"frame{num:03d}.tif", "scan")
    (tmp_path / "project.json").write_text(json.dumps(
        {"schema_version": 1, "film": {"film_id": "K16_X"}}), encoding="utf-8")
    proj = load_folder(tmp_path)

    n = save_film(proj, {"camera": "Nikon FM2", "shooting_lens": "Nikkor 50/2",
                         "development_start": "15.7.2026", "notes": "x"})
    assert n == 2
    for it in proj.items:
        film = json.loads(it.sidecar_path.read_text(encoding="utf-8"))["film"]
        assert film["camera"] == "Nikon FM2"
        assert film["shooting_lens"] == "Nikkor 50/2"
        assert film["film_id"] == "K16_X"           # never overwritten
    proj_json = json.loads((tmp_path / "project.json").read_text())
    assert proj_json["film"]["shooting_lens"] == "Nikkor 50/2"


def test_gui_bulk_and_panels_offscreen(tmp_path, qtbot, monkeypatch) -> None:
    """New evening widgets exist and drive the store: transfer checkboxes,
    rotation radios, acquisition + film panels."""
    pytest.importorskip("pytestqt")
    import filmscan_studio.annotator.gui as gui_mod
    from filmscan_studio.annotator.gui import MainWindow

    frames = tmp_path / "frames"
    frames.mkdir()
    _write_sidecar(frames, "frame001.tif", "scan")
    _write_sidecar(frames, "frame002.tif", "scan")
    (tmp_path / "project.json").write_text(
        json.dumps({"film": {"film_id": "K16_X"}}), encoding="utf-8")

    win = MainWindow(project=load_folder(tmp_path))
    qtbot.addWidget(win)

    # rotation radio writes through save
    win.rot_group.button(90).setChecked(True)
    win.ed_date.setText("1968")
    win._save_current()
    ann = json.loads((frames / "frame001.tif.json").read_text())["annotation"]
    assert ann["rotation_degrees"] == 90
    assert ann["capture_datetime"] == "1968:01:01 00:00:00"   # single save: exact

    # list row gained the date + rotation symbols
    assert "📅" in win.grid.item(0).text()
    assert "⟳" in win.grid.item(0).text()

    # transfer checkboxes drive the bulk patch
    win.ed_title.setText("Spolecny")
    win.grid.selectAll()
    win._apply_to_selected()      # no transfer -> titles of frame002 untouched
    ann2 = json.loads((frames / "frame002.tif.json").read_text())["annotation"]
    assert ann2["title"] == ""
    win.chk_transfer_title.setChecked(True)
    win.ed_title.setText("Spolecny")   # re-type: the apply reload re-flects disk
    win.grid.selectAll()
    win._apply_to_selected()
    ann2 = json.loads((frames / "frame002.tif.json").read_text())["annotation"]
    assert ann2["title"] == "Spolecny"

    # acquisition panel prefilled and saving
    assert win.ed_acq["exposure_time"].text() == "0.011"
    win.ed_acq["camera_serial"].setText("SN-42")
    win._save_acquisition()
    acq = json.loads((frames / "frame001.tif.json").read_text())["acquisition"]
    assert acq["camera_serial"] == "SN-42"

    # shot box (2026-09-22: camera/lens/ISO pulled out of the film block;
    # výrok 22:30: make+model místo volného „camera"): edit once -> every
    # sidecar, through the same save_film path.
    assert "camera" not in win.ed_film and "camera" not in win.ed_shot
    assert "camera_make" in win.ed_shot and "camera_model" in win.ed_shot
    win.ed_shot["camera_make"].setText("Nikon")
    win.ed_shot["camera_model"].setText("FM2")
    win.ed_shot["shooting_lens"].setText("Nikkor 50/2")
    win.ed_shot["film_iso"].setText("100")
    win._save_shot()
    for num in (1, 2):
        film = json.loads((frames / f"frame{num:03d}.tif.json").read_text())["film"]
        assert film["camera_make"] == "Nikon"
        assert film["camera_model"] == "FM2"
        assert film["camera"] == "Nikon FM2"   # lidsky souhrn stays in sync
        assert film["shooting_lens"] == "Nikkor 50/2"
        assert film["film_iso"] == "100"


def test_empty_numeric_boxes_keep_stored_values(tmp_path) -> None:
    """Blank copy_number / pushed_stops boxes must not erase the stored
    number (they are non-optional model fields) — and blank optional ones
    (gain) may unset."""
    from filmscan_studio.annotator.store import save_acquisition

    item = _item(tmp_path, _full_record())
    save_acquisition(item, {"copy_number": "", "gain": "",
                            "camera_serial": "SN-1"})
    on_disk = json.loads(item.sidecar_path.read_text(encoding="utf-8"))
    assert on_disk["acquisition"]["copy_number"] == 1    # kept, not None
    assert on_disk["acquisition"]["gain"] is None        # optional: unset ok


def test_film_empty_pushed_stops_keeps_value(tmp_path) -> None:
    from filmscan_studio.annotator.store import save_film

    frames = tmp_path / "frames"
    frames.mkdir()
    _write_sidecar(frames, "frame001.tif", "scan")
    proj = load_folder(tmp_path)
    assert save_film(proj, {"pushed_stops": "", "camera": "FM2"}) == 1
    film = json.loads((frames / "frame001.tif.json").read_text())["film"]
    assert film["pushed_stops"] == 0.0                   # untouched
    assert film["camera"] == "FM2"
    with pytest.raises(ValueError):
        save_film(proj, {"pushed_stops": "hodne"})


def test_gui_thumbnail_grid_and_space_fullscreen(tmp_path, qtbot) -> None:
    """Grid replaces the list; Space (or dbl-click) opens full screen, Space
    again closes it (operator's order 2026-09-21 night, Bridge style)."""
    pytest.importorskip("pytestqt")
    from filmscan_studio.annotator.gui import MainWindow

    frames = tmp_path / "frames"
    frames.mkdir()
    _write_sidecar(frames, "frame001.tif", "scan")
    _write_sidecar(frames, "frame002.tif", "scan")

    win = MainWindow(project=load_folder(tmp_path))
    qtbot.addWidget(win)
    assert win.grid.count() == 2
    assert win.current_row == 0

    # space in the grid -> fullscreen viewer with the current frame
    win.toggle_fullscreen()
    assert win._fs.isVisible()
    # space again -> back to the grid
    win.toggle_fullscreen()
    assert not win._fs.isVisible()

    # Esc closes it too (the viewer owns the key while shown)
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    win.toggle_fullscreen()
    win._fs.keyPressEvent(
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                  Qt.KeyboardModifier.NoModifier))
    assert not win._fs.isVisible()


# ------------------------------------------- Film start -> automatické datum

@pytest.mark.parametrize("raw,expected", [
    ("8.11.2026 Vacation 2026", "2026:11:08 00:00:00"),   # date + note (K16O01)
    ("15.7.2026", "2026:07:15 00:00:00"),              # plain (K16O04)
    ("2026-07-12", "2026:07:12 00:00:00"),             # ISO spelling
    ("1. 2. 1968 poznámka", "1968:02:01 00:00:00"),    # spaced
])
def test_parse_film_start_date_readable(raw: str, expected: str) -> None:
    from filmscan_studio.annotator.store import parse_film_start_date

    assert parse_film_start_date(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "cca 2/2017", "cca 2015", "?2025", "asi 12/25", "1968",
    "léto 1968", "31.2.2026",                          # impossible day
])
def test_parse_film_start_date_unreadable(raw: str) -> None:
    """„pokud tam je něco jako cca 2015 tak to nejde“ — the operator fills
    those by hand; an approximate date must not masquerade as an exact one."""
    from filmscan_studio.annotator.store import parse_film_start_date

    assert parse_film_start_date(raw) == ""


def _project_with_film_start(tmp_path, start):
    frames = tmp_path / "frames"
    frames.mkdir()
    for num in (1, 2, 3):
        _write_sidecar(frames, f"frame{num:03d}.tif", "scan")
    if start is not None:
        for p in frames.glob("*.json"):
            rec = json.loads(p.read_text())
            rec["film"]["development_start"] = start
            p.write_text(json.dumps(rec), encoding="utf-8")
    return load_folder(tmp_path)


def test_auto_date_stamps_undated_frames(tmp_path) -> None:
    from filmscan_studio.annotator.store import auto_date_frames

    proj = _project_with_film_start(tmp_path, "8.11.2026 Vacation 2026")
    count, date = auto_date_frames(proj)
    assert (count, date) == (3, "2026:11:08 00:00:00")
    stamps = [json.loads(i.sidecar_path.read_text())["annotation"]
              ["capture_datetime"] for i in proj.items]
    assert stamps == ["2026:11:08 00:01:00", "2026:11:08 00:02:00",
                      "2026:11:08 00:03:00"]     # sequential, sortable order


def test_auto_date_skips_unreadable_film_start(tmp_path) -> None:
    from filmscan_studio.annotator.store import auto_date_frames

    proj = _project_with_film_start(tmp_path, "cca 2/2017")
    assert auto_date_frames(proj) == (0, "")
    for item in proj.items:
        ann = json.loads(item.sidecar_path.read_text()).get("annotation")
        assert ann is None or not ann.get("capture_datetime")


def test_auto_date_never_overwrites_an_existing_date(tmp_path) -> None:
    from filmscan_studio.annotator.store import auto_date_frames

    proj = _project_with_film_start(tmp_path, "15.7.2026")
    save_annotation(proj.items[0],
                    FrameAnnotation(capture_datetime="1968:08:12 14:30:00")
                    .model_dump(mode="json"))
    count, _ = auto_date_frames(proj)
    assert count == 2                              # frame 1 keeps its date
    first = json.loads(proj.items[0].sidecar_path.read_text())["annotation"]
    assert first["capture_datetime"] == "1968:08:12 14:30:00"
    # rerun: everything is dated, nothing left to stamp
    assert auto_date_frames(proj)[0] == 0


# ------------------------------------------------ immediate thumbnail rotation

def test_rotation_radio_rotates_thumb_before_save(tmp_path, qtbot,
                                                  monkeypatch) -> None:
    """„klidnu otočit 90° ať se otočí i náhledový thumbnail“ (2026-09-22) —
    the grid follows the radio immediately, not only after Uložit."""
    pytest.importorskip("pytestqt")
    from filmscan_studio.annotator.gui import MainWindow

    frames = tmp_path / "frames"
    frames.mkdir()
    _write_sidecar(frames, "frame001.tif", "scan")
    _write_sidecar(frames, "frame002.tif", "scan")
    win = MainWindow(project=load_folder(tmp_path))
    qtbot.addWidget(win)

    assert win._rotation_for(0) == 0
    win.rot_group.button(90).setChecked(True)
    assert win._rotation_for(0) == 90              # form wins, unsaved
    # the saved sidecar is still at 0 — the radio alone changes nothing yet
    ann = (json.loads((frames / "frame001.tif.json").read_text())
           .get("annotation") or {})
    assert ann.get("rotation_degrees", 0) == 0
    # and the *other* rows keep reading their own sidecars
    assert win._rotation_for(1) == 0


# ------------------------------------------------------------- empty state

def test_empty_app_shows_placeholder_not_dead_splitter(tmp_path,
                                                       qtbot) -> None:
    """An app with no folder shows the big-button page, and opens the last
    folder again on demand (operator 2026-09-22: půlka okna mrtvá)."""
    pytest.importorskip("pytestqt")
    from PySide6.QtCore import QSettings

    from filmscan_studio.annotator.gui import MainWindow
    QSettings("FilmscanStudio", "filmscan-annotate").setValue(
        "last_folder", "")
    try:
        win = MainWindow()
        qtbot.addWidget(win)
        assert win.stack.currentIndex() == 0       # empty page
        assert not win.btn_reopen.isEnabled()      # nothing remembered yet

        frames = tmp_path / "frames"
        frames.mkdir()
        _write_sidecar(frames, "frame001.tif", "scan")
        win.set_project(load_folder(tmp_path))
        assert win.stack.currentIndex() == 1       # working view
        assert QSettings("FilmscanStudio", "filmscan-annotate").value(
            "last_folder") == str(tmp_path)

        # a folder that opens but holds no scans must NOT swap the page
        empty = tmp_path / "empty"
        (empty / "frames").mkdir(parents=True)
        assert win._has_project(load_folder(empty)) is False

        win._show_empty(True)
        assert win.stack.currentIndex() == 0
        assert str(tmp_path) in win.lbl_last.text()
        win._reopen_last()
        assert win.stack.currentIndex() == 1
    finally:
        QSettings("FilmscanStudio", "filmscan-annotate").remove(
            "last_folder")
