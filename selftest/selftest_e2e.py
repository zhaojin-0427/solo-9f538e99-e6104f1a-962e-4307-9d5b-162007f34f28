"""Standalone end-to-end self-test for the gap-check service.

Runs the real uvicorn server against an isolated temp SQLite volume (never
the default /data path) and exercises:

1. Many overlapping scenes on one date: /gaps reads one counter row per
   cell regardless of scene count; response shape / run rules are unchanged.
2. The last same-scene red+nir pair failing band by band, including the
   coverage-row deletion when every scene withdraws from a cell.
3. Correction touches only cells whose own validity bits change.
4. Restart with the same volume keeps everything directly queryable.

Run with the project virtualenv:
    .venv-tmp/venv/bin/python -I selftest/selftest_e2e.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8000"


def _request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def _wait_ready(proc: subprocess.Popen) -> None:
    deadline = time.time() + 20
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("server exited early")
        try:
            status, body = _request("GET", "/health")
            if status == 200 and body == {"status": "ok"}:
                return
        except OSError:
            pass
        time.sleep(0.2)
    raise RuntimeError("server did not become ready")


def _mask_submit(date: str, scene: str, ox: int, oy: int, band: str,
                 mask: list[list[int]]) -> dict:
    status, body = _request(
        "POST",
        "/masks",
        {
            "date": date,
            "scene_id": scene,
            "origin_x": ox,
            "origin_y": oy,
            "band": band,
            "mask": mask,
        },
    )
    assert status == 200, body
    return body


def _gaps(date: str, w: int, h: int, ox: int = 0, oy: int = 0) -> dict:
    qs = f"date={date}&origin_x={ox}&origin_y={oy}&width={w}&height={h}"
    status, body = _request("GET", f"/gaps?{qs}")
    assert status == 200, body
    return body


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="gapcheck-e2e-")
    db_path = Path(tmpdir) / "e2e.db"
    env = {
        **os.environ,
        "GAPCHECK_DB_PATH": str(db_path),
        "PYTHONPATH": str(REPO_ROOT),
    }
    proc = subprocess.Popen(
        [sys.executable, "-I", "-m", "uvicorn",
         "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_ready(proc)
        date = "2026-11-24"

        # ---- 1. many overlapping scenes --------------------------------
        n = 100
        for i in range(n):
            _mask_submit(date, f"S{i:04d}", 0, 0, "red",
                         [[1, 1, 1], [1, 1, 1]])
        # Last scene also supplies nir -> all 6 cells complete.
        _mask_submit(date, f"S{n - 1:04d}", 0, 0, "nir",
                     [[1, 1, 1], [1, 1, 1]])

        result = _gaps(date, 3, 2)
        assert result["total_cells"] == 6
        assert result["complete_cells"] == 6
        assert result["gap_cells"] == 0
        assert result["gaps"] == []
        # Response keeps the original fields and run schema.
        assert set(result) == {
            "date", "origin_x", "origin_y", "width", "height",
            "total_cells", "complete_cells", "gap_cells",
            "category_counts", "gaps",
        }

        # Storage: exactly one row per cell; row width independent of n.
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT red_cnt, nir_cnt, both_cnt FROM coverage_v2 "
                f"WHERE date = '{date}'"
            ).fetchall()
            cols = [r[1] for r in conn.execute("PRAGMA table_info(coverage_v2)")]
        finally:
            conn.close()
        assert len(rows) == 6, rows
        assert all(r == (n, 1, 1) for r in rows), rows
        assert cols == ["date", "x", "y", "red_cnt", "nir_cnt",
                        "both_cnt", "updated_at"], cols
        print(f"[ok] {n} overlapping scenes -> 6 counter rows {rows[0]}")

        # Idempotent re-submission writes nothing.
        body = _mask_submit(date, "S0000", 0, 0, "red",
                            [[1, 1, 1], [1, 1, 1]])
        assert body["coverage_cells_updated"] == 0, body
        print("[ok] unchanged re-submission updates 0 coverage cells")

        # ---- 2. last same-scene band pair fails ------------------------
        # S0099 currently provides both bands everywhere. Withdraw its nir.
        body = _mask_submit(date, f"S{n - 1:04d}", 0, 0, "nir",
                            [[0, 0, 0], [0, 0, 0]])
        assert body["coverage_cells_updated"] == 6, body
        result = _gaps(date, 3, 2)
        assert result["complete_cells"] == 0
        assert result["category_counts"]["red_only"] == 6, result
        print("[ok] same-scene pair withdrawn -> all 6 cells red_only")

        # Withdraw S0099's red as well: every cell still has 99 other red
        # scenes, so rows stay; all remain red_only.
        body = _mask_submit(date, f"S{n - 1:04d}", 0, 0, "red",
                            [[0, 0, 0], [0, 0, 0]])
        assert body["coverage_cells_updated"] == 6, body
        conn = sqlite3.connect(str(db_path))
        try:
            left = conn.execute(
                "SELECT MIN(red_cnt), MAX(red_cnt) FROM coverage_v2 "
                f"WHERE date = '{date}'"
            ).fetchone()
        finally:
            conn.close()
        assert left == (n - 1, n - 1), left
        print(f"[ok] scene red withdrawn; other scenes preserved: red_cnt={left[0]}")

        # Withdraw every remaining scene from one cell -> row deleted.
        for i in range(n - 1):
            _mask_submit(date, f"S{i:04d}", 0, 0, "red",
                         [[0, 1, 1], [1, 1, 1]])
        result = _gaps(date, 3, 2)
        assert result["category_counts"]["both_missing"] == 1, result
        assert result["category_counts"]["red_only"] == 5, result
        # Runs: row 0 begins with a both_missing single-cell run.
        first_run = result["gaps"][0]
        assert (first_run["y"], first_run["x_start"], first_run["x_end"],
                first_run["category"]) == (0, 0, 0, "both_missing")
        conn = sqlite3.connect(str(db_path))
        try:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM coverage_v2 WHERE date = ? AND x = 0 AND y = 0",
                (date,),
            ).fetchone()[0]
        finally:
            conn.close()
        assert cnt == 0
        print("[ok] cell with no contributions: coverage row deleted -> both_missing")

        # ---- 3. same-scene completeness rule on a fresh date -----------
        d2 = "2026-11-25"
        _mask_submit(d2, "A", 0, 0, "red", [[1, 1]])
        _mask_submit(d2, "A", 0, 0, "nir", [[1, 0]])
        _mask_submit(d2, "B", 1, 0, "nir", [[1]])  # different scene at x=1
        result = _gaps(d2, 2, 1)
        assert result["complete_cells"] == 1
        assert result["category_counts"]["red_only"] == 0
        assert result["category_counts"]["different_scenes"] == 1, result
        print("[ok] same-scene red+nir required for complete; cross-scene classified")

        # ---- 4. restart against the same volume ------------------------
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    proc = subprocess.Popen(
        [sys.executable, "-I", "-m", "uvicorn",
         "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_ready(proc)
        result = _gaps(date, 3, 2)
        assert result["category_counts"]["both_missing"] == 1
        assert result["category_counts"]["red_only"] == 5
        result2 = _gaps("2026-11-25", 2, 1)
        assert result2["complete_cells"] == 1
        assert result2["category_counts"]["different_scenes"] == 1
        print("[ok] after restart: same volume queryable without any action")
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    print("ALL E2E CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
