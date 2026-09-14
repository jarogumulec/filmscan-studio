"""SQLite catalog.

The database mirrors the JSON sidecars rather than replacing them. Both are
written because they serve different purposes: the sidecar travels with the file
and survives the database being lost, while the database is what makes a session
queryable ("every frame of HP5_001 shot at f/8", "all dark frames usable at this
shutter speed").

Schema is versioned so a project archived today can be migrated in five years.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from filmscan_studio.core.models import (
    AcquisitionMetadata,
    CaptureRecord,
    FilmMetadata,
    FrameKind,
    to_json_dict,
)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    version     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS films (
    film_id         TEXT PRIMARY KEY,
    label           TEXT NOT NULL,
    metadata        TEXT NOT NULL,          -- FilmMetadata as JSON
    created_at      TEXT NOT NULL,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS captures (
    capture_id      TEXT PRIMARY KEY,
    film_id         TEXT NOT NULL REFERENCES films(film_id),
    frame_number    INTEGER,
    kind            TEXT NOT NULL,          -- scan | dark | flat
    filename        TEXT NOT NULL,
    file_format     TEXT NOT NULL,
    width           INTEGER,
    height          INTEGER,
    black_level     REAL,
    white_level     REAL,
    shutter         REAL,
    iso             INTEGER,
    f_number        REAL,
    captured_at     TEXT,
    metadata        TEXT NOT NULL,          -- full CaptureRecord as JSON
    created_at      TEXT NOT NULL,
    UNIQUE (film_id, filename)
);

CREATE INDEX IF NOT EXISTS idx_captures_film ON captures(film_id);
CREATE INDEX IF NOT EXISTS idx_captures_kind ON captures(kind);
CREATE INDEX IF NOT EXISTS idx_captures_frame ON captures(film_id, frame_number);
"""


class Catalog:
    """Project database.

    Kept deliberately thin: no ORM, so the schema stays readable to anyone
    inspecting an archived project with the ``sqlite3`` CLI, which matters for a
    project whose stated purpose includes archival durability.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            row = conn.execute("SELECT version FROM schema_meta").fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,))
            elif row["version"] != SCHEMA_VERSION:
                raise RuntimeError(
                    f"catalog {self.path} is schema v{row['version']}, "
                    f"this build expects v{SCHEMA_VERSION}"
                )

    def upsert_film(self, film: FilmMetadata, notes: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO films(film_id, label, metadata, created_at, notes)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(film_id) DO UPDATE SET
                    label = excluded.label,
                    metadata = excluded.metadata,
                    notes = COALESCE(excluded.notes, films.notes)
                """,
                (
                    film.film_id,
                    film.label(),
                    json.dumps(to_json_dict(film)),
                    datetime.now().astimezone().isoformat(),
                    notes,
                ),
            )

    def get_film(self, film_id: str) -> FilmMetadata | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT metadata FROM films WHERE film_id = ?", (film_id,)
            ).fetchone()
        return FilmMetadata.model_validate_json(row["metadata"]) if row else None

    def films(self) -> list[FilmMetadata]:
        with self._connect() as conn:
            rows = conn.execute("SELECT metadata FROM films ORDER BY created_at").fetchall()
        return [FilmMetadata.model_validate_json(r["metadata"]) for r in rows]

    def add_capture(self, record: CaptureRecord) -> None:
        acq = record.acquisition
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO captures(
                    capture_id, film_id, frame_number, kind, filename, file_format,
                    width, height, black_level, white_level, shutter, iso, f_number,
                    captured_at, metadata, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(capture_id) DO UPDATE SET
                    frame_number = excluded.frame_number,
                    kind = excluded.kind,
                    metadata = excluded.metadata
                """,
                (
                    record.capture_id,
                    record.film_id,
                    record.frame_number,
                    record.kind.value,
                    record.filename,
                    record.file_format.value,
                    record.width,
                    record.height,
                    record.black_level,
                    record.white_level,
                    acq.exposure_time,
                    acq.iso,
                    acq.f_number,
                    acq.capture_date.isoformat() if acq.capture_date else None,
                    json.dumps(to_json_dict(record)),
                    record.created_at.isoformat(),
                ),
            )

    def captures(
        self,
        film_id: str | None = None,
        kind: FrameKind | None = None,
    ) -> list[CaptureRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if film_id:
            clauses.append("film_id = ?")
            params.append(film_id)
        if kind:
            clauses.append("kind = ?")
            params.append(kind.value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT metadata FROM captures {where} ORDER BY created_at", params
            ).fetchall()
        return [CaptureRecord.model_validate_json(r["metadata"]) for r in rows]

    def next_frame_number(self, film_id: str) -> int:
        """Highest existing scan number plus one.

        Frames are numbered by the operator's film advance, not by capture order,
        so this is a suggestion the session can override rather than an
        authoritative counter.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(frame_number) AS n FROM captures WHERE film_id = ? AND kind = ?",
                (film_id, FrameKind.SCAN.value),
            ).fetchone()
        return (row["n"] or 0) + 1

    def export_json(self, destination: str | Path) -> Path:
        """Dump the whole catalog to a single human-readable JSON file.

        Part of the project's "open workflow" requirement: an archive must be
        inspectable without this application, and a plain JSON dump outlives any
        Python library.
        """
        dest = Path(destination)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "exported_at": datetime.now().astimezone().isoformat(),
            "films": [to_json_dict(f) for f in self.films()],
            "captures": [to_json_dict(c) for c in self.captures()],
        }
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return dest

    def __repr__(self) -> str:
        return f"Catalog({str(self.path)!r})"
