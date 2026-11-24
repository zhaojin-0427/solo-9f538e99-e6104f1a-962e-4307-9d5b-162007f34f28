"""SQLite persistence for band masks and committed coverage.

Three tables are used:

* ``scenes``      - one row per (UTC date, scene id); grid origin and size of a
                    scene are immutable once first registered.
* ``band_masks``  - the source 0/1 validity masks submitted per scene / band.
                    Re-submitting a band only overwrites that band's mask.
* ``coverage_v2`` - one committed row per (date, grid cell), holding three
                    small counters instead of scene-id lists:

                    - ``red_cnt``: number of scenes whose red band is valid
                      at this cell;
                    - ``nir_cnt``: number of scenes whose NIR band is valid
                      at this cell;
                    - ``both_cnt``: number of scenes whose *own* red and NIR
                      masks are both valid at this cell (i.e. scenes that
                      make the cell complete for an NDVI-like product).

                    With this layout a gap query reads one fixed-size row per
                    rectangle cell at most; its read volume grows with the
                    number of cells only and never with the number of scenes
                    stacked on the same date. Neither scene-id lists nor the
                    source masks are touched on the query path.

A legacy ``coverage`` table (the previous schema, with JSON scene-id arrays)
is upgraded in place on startup, so existing SQLite volumes keep working:
the historical masks remain queryable and correctable after the upgrade.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import DEFAULT_DB_PATH

# Bumped when the coverage layout changed from JSON scene-id arrays (v1) to
# per-cell counters (v2). Kept in SQLite's PRAGMA user_version.
SCHEMA_VERSION = 2

# NOTE: no PRAGMA statements here and never execute this via ``executescript``
# -- that helper issues an implicit COMMIT, which would break the single
# ``BEGIN IMMEDIATE`` transaction guarding startup upgrades. Each statement is
# run separately inside the caller's transaction instead.
SCHEMA_STATEMENTS = [
    """
CREATE TABLE IF NOT EXISTS scenes (
    date       TEXT NOT NULL,          -- UTC date, ISO 8601 YYYY-MM-DD
    scene_id   TEXT NOT NULL,
    origin_x   INTEGER NOT NULL,       -- grid coordinate of the upper-left cell
    origin_y   INTEGER NOT NULL,
    width      INTEGER NOT NULL,       -- fixed grid width  of this scene
    height     INTEGER NOT NULL,       -- fixed grid height of this scene
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (date, scene_id),
    CHECK (width  > 0),
    CHECK (height > 0)
)
""",
    """
CREATE TABLE IF NOT EXISTS band_masks (
    date       TEXT NOT NULL,
    scene_id   TEXT NOT NULL,
    band       TEXT NOT NULL CHECK (band IN ('red', 'nir')),
    mask       BLOB NOT NULL,          -- width*height bytes, row-major, 0/1
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (date, scene_id, band),
    FOREIGN KEY (date, scene_id) REFERENCES scenes (date, scene_id)
)
""",
    # WITHOUT ROWID keyed by cell: point / range lookups by (date, x, y) and
    # rectangle scans by (date, y, x) both use the primary-key index. The row
    # payload is three small counters, so its size is independent of how many
    # scenes overlap the cell.
    """
CREATE TABLE IF NOT EXISTS coverage_v2 (
    date        TEXT NOT NULL,
    x           INTEGER NOT NULL,
    y           INTEGER NOT NULL,
    red_cnt     INTEGER NOT NULL DEFAULT 0 CHECK (red_cnt >= 0),
    nir_cnt     INTEGER NOT NULL DEFAULT 0 CHECK (nir_cnt >= 0),
    both_cnt    INTEGER NOT NULL DEFAULT 0 CHECK (both_cnt >= 0),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (date, x, y)
) WITHOUT ROWID
""",
]

# Derives the v2 counters from one legacy v1 coverage row. A scene id appears
# in both JSON arrays iff that scene's own red and NIR were both valid there,
# so the intersection size is the historical both-scene count.
_MIGRATE_ROW = """
SELECT
    date, x, y,
    json_array_length(red_scenes) AS red_cnt,
    json_array_length(nir_scenes) AS nir_cnt,
    (
        SELECT COUNT(*)
        FROM json_each(red_scenes) AS r
        WHERE EXISTS (
            SELECT 1 FROM json_each(nir_scenes) AS n WHERE n.value = r.value
        )
    ) AS both_cnt
FROM coverage
"""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with the pragmas required by the service."""
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL is set per database on first connection; it persists on the file.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    # Readers never block on the writer and always see a single committed
    # snapshot for the duration of their statement.
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone() is not None


def _upgrade_from_v1(conn: sqlite3.Connection) -> None:
    """Convert the legacy JSON-array coverage table into v2 counters.

    Runs inside the caller's transaction. Old volumes only carry ``coverage``
    plus the unchanged ``scenes`` / ``band_masks`` tables, so the upgrade does
    not touch any source mask; after it the volume behaves exactly like one
    created by the current version.
    """
    conn.execute(
        "INSERT INTO coverage_v2 (date, x, y, red_cnt, nir_cnt, both_cnt) "
        + _MIGRATE_ROW
    )
    conn.execute("DROP TABLE coverage")


def init_db(db_path: Path | str | None = None) -> None:
    """Create the schema if missing and upgrade legacy volumes in place.

    The whole startup migration is one transaction: on failure nothing is
    changed and the previous committed state stays queryable.
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            legacy_present = _has_table(conn, "coverage")
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version < SCHEMA_VERSION:
                if legacy_present:
                    _upgrade_from_v1(conn)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


@contextmanager
def session(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """Transaction boundary: commit on success, roll back on failure."""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
