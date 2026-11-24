"""End-to-end tests for the mask submission and gap query API."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _post(client, **overrides):
    payload = {
        "date": "2026-11-23",
        "scene_id": "S1",
        "origin_x": 0,
        "origin_y": 0,
        "band": "red",
        "mask": [
            [1, 1, 0, 0],
            [1, 0, 0, 0],
        ],
    }
    payload.update(overrides)
    return client.post("/masks", json=payload)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_submit_red_then_query_classifies_red_only_and_both_missing(client):
    r = _post(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["valid_cells"] == 3
    # Only cells whose validity bit for this scene changed are written:
    # the three 1-cells create three coverage rows.
    assert body["coverage_cells_updated"] == 3

    r = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 4, "height": 2},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["complete_cells"] == 0
    assert data["gap_cells"] == 8
    assert data["category_counts"]["red_only"] == 3
    assert data["category_counts"]["both_missing"] == 5

    # Row 0: red-only run x=0..1, then both-missing run x=2..3
    gaps = data["gaps"]
    assert gaps[0]["y"] == 0
    assert gaps[0]["category"] == "red_only"
    assert (gaps[0]["x_start"], gaps[0]["x_end"], gaps[0]["length"]) == (0, 1, 2)
    assert gaps[1]["category"] == "both_missing"
    assert (gaps[1]["x_start"], gaps[1]["x_end"]) == (2, 3)
    # Row 1: red-only single cell at x=0, then both-missing x=1..3
    assert gaps[2]["y"] == 1 and gaps[2]["category"] == "red_only"
    assert gaps[3]["category"] == "both_missing"
    assert (gaps[3]["x_start"], gaps[3]["x_end"], gaps[3]["length"]) == (1, 3, 3)


def test_same_scene_red_and_nir_marks_complete_cells(client):
    assert _post(client, band="red", mask=[[1, 1], [0, 1]]).status_code == 200
    assert _post(client, band="nir", mask=[[1, 0], [0, 1]]).status_code == 200

    r = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 2, "height": 2},
    )
    data = r.json()
    # Complete where both bands valid in S1: (0,0) and (1,1).
    assert data["complete_cells"] == 2
    assert data["gap_cells"] == 2
    counts = data["category_counts"]
    assert counts["red_only"] == 1   # cell (1,0): red only
    assert counts["nir_only"] == 0
    assert counts["both_missing"] == 1  # cell (0,1)
    assert counts["different_scenes"] == 0


def test_different_scenes_supply_bands_at_same_cell(client):
    # S1 covers origin (0,0); S2 is shifted and overlaps at cell (1,1).
    assert _post(client, scene_id="S1", band="red",
                 mask=[[1, 1], [1, 1]]).status_code == 200
    assert _post(client, scene_id="S2", origin_x=1, origin_y=1,
                 band="nir", mask=[[1]]).status_code == 200

    r = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 2, "height": 2},
    )
    data = r.json()
    assert data["complete_cells"] == 0
    assert data["category_counts"]["different_scenes"] == 1
    assert data["category_counts"]["red_only"] == 3

    diff = [g for g in data["gaps"] if g["category"] == "different_scenes"]
    assert len(diff) == 1
    assert (diff[0]["y"], diff[0]["x_start"], diff[0]["x_end"]) == (1, 1, 1)
    assert "不同景" in diff[0]["category_label"]


def test_complete_when_any_scene_supplies_both_bands(client):
    # S1: red only everywhere; S2 supplies both bands at (0,0) -> complete.
    assert _post(client, scene_id="S1", band="red",
                 mask=[[1, 1]]).status_code == 200
    assert _post(client, scene_id="S2", band="red",
                 mask=[[1, 0]]).status_code == 200
    assert _post(client, scene_id="S2", band="nir",
                 mask=[[1, 0]]).status_code == 200

    r = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 2, "height": 1},
    )
    data = r.json()
    assert data["complete_cells"] == 1
    assert data["category_counts"]["red_only"] == 1


def test_correction_recomputes_only_this_scene(client):
    # Two overlapping scenes, then correct S1's red mask.
    assert _post(client, scene_id="S1", band="red",
                 mask=[[1, 1]]).status_code == 200
    assert _post(client, scene_id="S2", origin_x=1, origin_y=0,
                 band="nir", mask=[[1]]).status_code == 200
    # Cell (1,0): S1 red + S2 nir -> different_scenes before correction.

    assert _post(client, scene_id="S1", band="red",
                 mask=[[1, 0]]).status_code == 200

    r = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 2, "height": 1},
    )
    data = r.json()
    # (0,0): red only; (1,0): S1 red withdrawn, S2 nir remains -> nir_only.
    assert data["category_counts"]["red_only"] == 1
    assert data["category_counts"]["nir_only"] == 1
    assert data["category_counts"]["different_scenes"] == 0

    # The source mask read-back reflects the correction.
    info = client.get("/scenes/2026-11-23/S1").json()
    assert info["width"] == 2 and info["height"] == 1
    red_band = next(b for b in info["bands"] if b["band"] == "red")
    assert red_band["valid_cells"] == 1


def test_geometry_conflict_is_rejected(client):
    assert _post(client).status_code == 200
    r = _post(client, origin_x=5, origin_y=5)
    assert r.status_code == 409
    assert "fixed" in r.json()["detail"]

    # Size mismatch is also rejected.
    r = _post(client, mask=[[1, 1, 1]])
    assert r.status_code == 409


def test_failed_submission_keeps_previous_result(client):
    assert _post(client, band="red", mask=[[1, 1]]).status_code == 200

    # Oversized mask (>256) fails validation and must not change anything.
    big = [[1] * 257]
    r = _post(client, band="nir", mask=big)
    assert r.status_code == 422

    r = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 2, "height": 1},
    )
    data = r.json()
    assert data["complete_cells"] == 0
    assert data["category_counts"]["red_only"] == 2


def test_invalid_mask_shapes_rejected(client):
    # Empty mask.
    r = _post(client, mask=[])
    assert r.status_code == 422
    # Ragged rows.
    r = _post(client, mask=[[1, 0], [1]])
    assert r.status_code == 422


def test_query_rectangle_limits_enforced(client):
    r = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 257, "height": 1},
    )
    assert r.status_code == 422


def test_rectangle_partially_outside_coverage_treated_as_both_missing(client):
    assert _post(client, mask=[[1]]).status_code == 200
    r = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 3, "height": 1},
    )
    data = r.json()
    assert data["total_cells"] == 3
    assert data["category_counts"]["red_only"] == 1
    assert data["category_counts"]["both_missing"] == 2


def test_dates_are_isolated(client):
    assert _post(client, date="2026-11-23", mask=[[1]]).status_code == 200
    r = client.get(
        "/gaps",
        params={"date": "2026-11-24", "origin_x": 0, "origin_y": 0,
                "width": 1, "height": 1},
    )
    data = r.json()
    assert data["gap_cells"] == 1
    assert data["category_counts"]["both_missing"] == 1


def test_scene_endpoint_404(client):
    r = client.get("/scenes/2026-11-23/nope")
    assert r.status_code == 404


def test_max_256_mask_accepted(client):
    mask = [[0] * 256 for _ in range(256)]
    mask[0][0] = 1
    r = _post(client, mask=mask)
    assert r.status_code == 200, r.text
    assert r.json()["valid_cells"] == 1
    # Only the single valid cell changes coverage, not all 256*256 cells.
    assert r.json()["coverage_cells_updated"] == 1


def test_coverage_rows_never_exceed_rectangle_cells(client, monkeypatch):
    """Storage invariant backing the query read bound: one coverage row per
    cell at most, so a rectangle scan returns at most width*height rows."""
    from app import config
    import sqlite3

    # Two overlapping scenes contribute to the same cells; they must share
    # rows rather than create one row per (cell, scene).
    assert _post(client, scene_id="S1", band="red",
                 mask=[[1, 1], [1, 1]]).status_code == 200
    assert _post(client, scene_id="S2", origin_x=1, origin_y=1,
                 band="nir", mask=[[1]]).status_code == 200

    conn = sqlite3.connect(str(config.DEFAULT_DB_PATH))
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM coverage_v2 WHERE date = '2026-11-23'"
        ).fetchone()[0]
        in_rect = conn.execute(
            "SELECT COUNT(*) FROM coverage_v2 "
            "WHERE date = '2026-11-23' AND x BETWEEN 0 AND 1 AND y BETWEEN 0 AND 1"
        ).fetchone()[0]
        # The per-cell payload is counters only: no scene-id list columns.
        columns = {
            r[1] for r in conn.execute("PRAGMA table_info(coverage_v2)")
        }
    finally:
        conn.close()

    assert columns == {"date", "x", "y", "red_cnt", "nir_cnt", "both_cnt",
                       "updated_at"}
    rectangle_cells = 2 * 2
    assert total <= rectangle_cells
    assert in_rect <= rectangle_cells


def test_many_overlapping_scenes_keep_coverage_to_one_counter_row_per_cell(client):
    """同一天叠了很多景：覆盖行数只随像元数增长，与景数无关。"""
    from app import config
    import sqlite3

    n_scenes = 50
    for i in range(n_scenes):
        assert _post(
            client, scene_id=f"S{i:03d}", band="red",
            mask=[[1, 1], [1, 1]],
        ).status_code == 200
    # The last overlapping scene also supplies NIR, making every cell
    # complete via that one same-scene red+nir pair.
    assert _post(
        client, scene_id=f"S{n_scenes - 1:03d}", band="nir",
        mask=[[1, 1], [1, 1]],
    ).status_code == 200

    r = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 2, "height": 2},
    )
    data = r.json()
    assert data["complete_cells"] == 4
    assert data["gap_cells"] == 0

    conn = sqlite3.connect(str(config.DEFAULT_DB_PATH))
    try:
        # Still four rows for four cells despite 50 stacked scenes.
        rows = conn.execute(
            "SELECT x, y, red_cnt, nir_cnt, both_cnt FROM coverage_v2 "
            "WHERE date = '2026-11-23' ORDER BY y, x"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 4
    assert all(r[2] == n_scenes for r in rows)   # red_cnt
    assert all(r[3] == 1 for r in rows)          # nir_cnt
    assert all(r[4] == 1 for r in rows)          # both_cnt

    # Re-submitting an unchanged mask changes no validity bit: zero writes.
    r = _post(client, scene_id="S000", band="red",
              mask=[[1, 1], [1, 1]])
    assert r.status_code == 200
    assert r.json()["coverage_cells_updated"] == 0
    # Query result is unchanged.
    assert client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 2, "height": 2},
    ).json()["complete_cells"] == 4


def test_last_same_scene_band_pair_failure_keeps_other_scene_contributions(client):
    """最后一对同景双波段逐步失效：计数只在本景有效位变化的格上更新，
    其他景的贡献原样保留。"""
    # S1 supplies both bands on a 1x2 row -> both cells complete.
    assert _post(client, scene_id="S1", band="red",
                 mask=[[1, 1]]).status_code == 200
    assert _post(client, scene_id="S1", band="nir",
                 mask=[[1, 1]]).status_code == 200
    # S2 overlaps at cell x=1 with red only; S3 overlaps at x=0 with nir only.
    assert _post(client, scene_id="S2", origin_x=1, origin_y=0,
                 band="red", mask=[[1]]).status_code == 200
    assert _post(client, scene_id="S3", origin_x=0, origin_y=0,
                 band="nir", mask=[[1]]).status_code == 200

    data = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 2, "height": 1},
    ).json()
    assert data["complete_cells"] == 2

    # Withdraw S1's NIR everywhere: the last same-scene pair fails. Both
    # cells lose both_cnt/nir contributions from S1; other scenes remain.
    r = _post(client, scene_id="S1", band="nir", mask=[[0, 0]])
    assert r.status_code == 200
    assert r.json()["coverage_cells_updated"] == 2  # both cells changed

    data = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 2, "height": 1},
    ).json()
    assert data["complete_cells"] == 0
    # x=0: S1 red + S3 nir from different scenes; x=1: red only (S1+S2).
    assert data["category_counts"]["different_scenes"] == 1
    assert data["category_counts"]["red_only"] == 1
    diff = [g for g in data["gaps"] if g["category"] == "different_scenes"]
    assert (diff[0]["y"], diff[0]["x_start"], diff[0]["x_end"]) == (0, 0, 0)

    # Withdraw S1's red as well: x=0 loses every contribution (row deleted),
    # x=1 still has S2's red. Only the two cells whose S1 red bit changes
    # are written.
    r = _post(client, scene_id="S1", band="red", mask=[[0, 0]])
    assert r.status_code == 200
    assert r.json()["coverage_cells_updated"] == 2

    data = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 2, "height": 1},
    ).json()
    # x=0 keeps S3's nir only; x=1 keeps S2's red only. No row is deleted
    # yet because every cell still has some scene's contribution.
    assert data["category_counts"] == {
        "red_only": 1,
        "nir_only": 1,
        "different_scenes": 0,
        "both_missing": 0,
    }

    # Withdrawing the last remaining contribution at x=0 (S3's nir) removes
    # the coverage row entirely; the cell falls back to both-missing.
    r = client.post(
        "/masks",
        json={
            "date": "2026-11-23",
            "scene_id": "S3",
            "origin_x": 0,
            "origin_y": 0,
            "band": "nir",
            "mask": [[0]],
        },
    )
    assert r.status_code == 200
    assert r.json()["coverage_cells_updated"] == 1

    data = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 2, "height": 1},
    ).json()
    assert data["category_counts"] == {
        "red_only": 1,
        "nir_only": 0,
        "different_scenes": 0,
        "both_missing": 1,
    }


LEGACY_V1_SCHEMA = """
CREATE TABLE scenes (
    date TEXT NOT NULL, scene_id TEXT NOT NULL,
    origin_x INTEGER NOT NULL, origin_y INTEGER NOT NULL,
    width INTEGER NOT NULL, height INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (date, scene_id)
);
CREATE TABLE band_masks (
    date TEXT NOT NULL, scene_id TEXT NOT NULL,
    band TEXT NOT NULL, mask BLOB NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (date, scene_id, band)
);
CREATE TABLE coverage (
    date TEXT NOT NULL, x INTEGER NOT NULL, y INTEGER NOT NULL,
    red_scenes TEXT NOT NULL, nir_scenes TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (date, x, y)
) WITHOUT ROWID;
"""


def test_legacy_v1_volume_upgrades_and_remains_queryable_and_correctable(
    monkeypatch,
):
    """旧 SQLite 卷（coverage 表存景号 JSON 数组）启动时就地升级，
    升级后历史掩膜可继续查询和更正。"""
    import importlib
    import sqlite3
    import tempfile
    from pathlib import Path

    tmpdir = tempfile.mkdtemp(prefix="gapcheck-legacy-")
    db_path = Path(tmpdir) / "legacy.db"
    monkeypatch.setenv("GAPCHECK_DB_PATH", str(db_path))

    from app import config, db, main
    importlib.reload(config)
    importlib.reload(db)
    importlib.reload(main)

    # Hand-build a v1 volume exactly like the previous release wrote it.
    raw = sqlite3.connect(str(db_path))
    try:
        raw.executescript(LEGACY_V1_SCHEMA)
        raw.execute(
            "INSERT INTO scenes (date, scene_id, origin_x, origin_y, width, height) "
            "VALUES ('2026-11-20', 'S1', 0, 0, 2, 1)"
        )
        raw.execute(
            "INSERT INTO scenes (date, scene_id, origin_x, origin_y, width, height) "
            "VALUES ('2026-11-20', 'S2', 1, 0, 1, 1)"
        )
        raw.execute(
            "INSERT INTO band_masks (date, scene_id, band, mask) "
            "VALUES ('2026-11-20', 'S1', 'red', ?)",
            (b"\x01\x01",),
        )
        raw.execute(
            "INSERT INTO band_masks (date, scene_id, band, mask) "
            "VALUES ('2026-11-20', 'S1', 'nir', ?)",
            (b"\x01\x01",),
        )
        raw.execute(
            "INSERT INTO band_masks (date, scene_id, band, mask) "
            "VALUES ('2026-11-20', 'S2', 'nir', ?)",
            (b"\x01",),
        )
        # x=0: S1 red+nir (complete). x=1: S1 red, nir from S1 and S2.
        raw.execute(
            "INSERT INTO coverage (date, x, y, red_scenes, nir_scenes) "
            "VALUES ('2026-11-20', 0, 0, '[\"S1\"]', '[\"S1\"]')"
        )
        raw.execute(
            "INSERT INTO coverage (date, x, y, red_scenes, nir_scenes) "
            "VALUES ('2026-11-20', 1, 0, '[\"S1\"]', '[\"S1\",\"S2\"]')"
        )
        raw.commit()
    finally:
        raw.close()

    # Startup performs the in-place upgrade inside one transaction.
    db.init_db()
    check = sqlite3.connect(str(db_path))
    try:
        tables = {
            r[0] for r in check.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "coverage" not in tables
        assert "coverage_v2" in tables
        counters = check.execute(
            "SELECT x, red_cnt, nir_cnt, both_cnt FROM coverage_v2 "
            "WHERE date = '2026-11-20' ORDER BY x"
        ).fetchall()
    finally:
        check.close()
    assert counters == [(0, 1, 1, 1), (1, 1, 2, 1)]

    # Historical masks stay queryable, and gaps read the upgraded coverage.
    with TestClient(main.app) as client:
        info = client.get("/scenes/2026-11-20/S1").json()
        assert info["bands"][0]["band"] == "nir"
        assert info["bands"][0]["valid_cells"] == 2

        data = client.get(
            "/gaps",
            params={"date": "2026-11-20", "origin_x": 0, "origin_y": 0,
                    "width": 2, "height": 1},
        ).json()
        assert data["complete_cells"] == 2

        # Correct S1's NIR on the upgraded volume: both cells lose S1's nir;
        # the same-scene pair fails at x=0 (red only), x=1 falls back to the
        # different-scene NIR supplied by S2.
        r = client.post(
            "/masks",
            json={
                "date": "2026-11-20",
                "scene_id": "S1",
                "origin_x": 0,
                "origin_y": 0,
                "band": "nir",
                "mask": [[0, 0]],
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["coverage_cells_updated"] == 2

        data = client.get(
            "/gaps",
            params={"date": "2026-11-20", "origin_x": 0, "origin_y": 0,
                    "width": 2, "height": 1},
        ).json()
        assert data["category_counts"]["red_only"] == 1
        assert data["category_counts"]["different_scenes"] == 1


def test_committed_data_survives_restart_and_is_queryable(monkeypatch):
    """重启后沿用同一 SQLite 卷，无需任何处理即可直接查。"""
    import importlib
    import tempfile
    from pathlib import Path

    tmpdir = tempfile.mkdtemp(prefix="gapcheck-restart-")
    db_path = Path(tmpdir) / "restart.db"
    monkeypatch.setenv("GAPCHECK_DB_PATH", str(db_path))

    from app import config, db, main
    importlib.reload(config)
    importlib.reload(db)
    importlib.reload(main)

    with TestClient(main.app) as first:
        assert _post(first, band="red", mask=[[1, 0]]).status_code == 200
        assert _post(first, band="nir", mask=[[1, 0]]).status_code == 200

    # Simulate a process restart against the same volume: lifespan re-runs
    # init_db (no-op for an up-to-date schema) and queries keep working.
    with TestClient(main.app) as second:
        data = second.get(
            "/gaps",
            params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                    "width": 2, "height": 1},
        ).json()
        assert data["complete_cells"] == 1
        assert data["category_counts"]["both_missing"] == 1
        # New submissions after restart work as usual.
        assert _post(second, band="red", mask=[[1, 1]]).status_code == 200
