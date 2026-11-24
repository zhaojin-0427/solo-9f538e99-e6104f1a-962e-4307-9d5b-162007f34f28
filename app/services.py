"""Core service logic: mask submission / correction and gap queries.

Design guarantees (see README for the full rationale):

* A submission runs inside one transaction. On any failure nothing is
  committed, so operators can still query the previous committed result.
* Re-submitting a band only reads *that scene's* two source masks and only
  rewrites coverage rows inside *that scene's* grid extent whose validity
  bits for this scene actually change. Other scenes' contributions live in
  the per-cell counters and are preserved without being read.
* Queries never read per-scene masks or scene-id lists: they read the
  committed ``coverage_v2`` table only, at most one fixed-size row per
  rectangle cell, so the read volume grows with the number of cells only.
"""

from __future__ import annotations

import sqlite3
from datetime import date as date_type

from .models import (
    GAP_BOTH_MISSING,
    GAP_CATEGORY_LABELS,
    GAP_DIFFERENT_SCENES,
    GAP_NIR_ONLY,
    GAP_RED_ONLY,
    GapRun,
    MaskSubmission,
)


class GeometryConflictError(Exception):
    """A re-submitted scene uses a different origin or size."""


def _flatten(mask: list[list[int]]) -> bytes:
    return bytes(v for row in mask for v in row)


def _bit(mask: bytes | None, index: int) -> bool:
    """Validity bit of a scene's own mask at a flat cell index."""
    return mask is not None and mask[index] == 1


def submit_mask(conn: sqlite3.Connection, sub: MaskSubmission) -> dict:
    """Insert/correct one band mask and recompute this scene's coverage.

    Only coverage cells inside the submitted scene's extent are considered,
    and among them only the cells where this scene's own validity bits
    (red / nir / both-valid) actually change are written. Other scenes'
    contributions are kept in the per-cell counters and are never rewritten.
    """
    date_str = sub.date.isoformat()
    flat = _flatten(sub.mask)
    valid_cells = sum(flat)

    # 1. Register the scene, or verify that the fixed geometry still matches.
    row = conn.execute(
        "SELECT origin_x, origin_y, width, height FROM scenes "
        "WHERE date = ? AND scene_id = ?",
        (date_str, sub.scene_id),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO scenes (date, scene_id, origin_x, origin_y, width, height) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                date_str,
                sub.scene_id,
                sub.origin_x,
                sub.origin_y,
                sub.width,
                sub.height,
            ),
        )
    elif (row["origin_x"], row["origin_y"], row["width"], row["height"]) != (
        sub.origin_x,
        sub.origin_y,
        sub.width,
        sub.height,
    ):
        raise GeometryConflictError(
            f"scene {sub.scene_id} on {date_str} is already registered with origin "
            f"({row['origin_x']}, {row['origin_y']}) and size "
            f"{row['width']}x{row['height']}; a scene's grid position and size are fixed"
        )

    # 2. Read this scene's own current masks: the submitted band's old mask
    #    (to diff against the new one) and the other band's mask (its
    #    contribution at each cell is unchanged but participates in the
    #    same-scene both-valid bit). No masks of any other scene are read.
    own_rows = conn.execute(
        "SELECT band, mask FROM band_masks "
        "WHERE date = ? AND scene_id = ? AND band IN ('red', 'nir')",
        (date_str, sub.scene_id),
    ).fetchall()
    own_masks = {r["band"]: r["mask"] for r in own_rows}
    old_own: bytes | None = own_masks.get(sub.band)
    other_band = "nir" if sub.band == "red" else "red"
    other: bytes | None = own_masks.get(other_band)

    # 3. Upsert the source mask for the submitted band.
    conn.execute(
        "INSERT INTO band_masks (date, scene_id, band, mask) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (date, scene_id, band) DO UPDATE SET "
        "mask = excluded.mask, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
        (date_str, sub.scene_id, sub.band, flat),
    )

    # 4. Load the per-cell counters inside this scene's extent in one indexed
    #    range scan. Rows are fixed-size counters; scene ids are not stored.
    ox, oy, w, h = sub.origin_x, sub.origin_y, sub.width, sub.height
    existing: dict[tuple[int, int], sqlite3.Row] = {
        (r["x"], r["y"]): r
        for r in conn.execute(
            "SELECT x, y, red_cnt, nir_cnt, both_cnt FROM coverage_v2 "
            "WHERE date = ? AND x BETWEEN ? AND ? AND y BETWEEN ? AND ?",
            (date_str, ox, ox + w - 1, oy, oy + h - 1),
        )
    }

    touched = 0
    for iy in range(h):
        for ix in range(w):
            idx = iy * w + ix
            x, y = ox + ix, oy + iy

            # This scene's own validity bits before and after the correction.
            if sub.band == "red":
                old_red = _bit(old_own, idx)
                new_red = flat[idx] == 1
                old_nir = new_nir = _bit(other, idx)
            else:
                old_nir = _bit(old_own, idx)
                new_nir = flat[idx] == 1
                old_red = new_red = _bit(other, idx)
            old_both = old_red and old_nir
            new_both = new_red and new_nir

            # Correction only changes cells where this scene's validity
            # bits actually move; identical re-submissions write nothing.
            if (old_red, old_nir, old_both) == (new_red, new_nir, new_both):
                continue

            d_red = int(new_red) - int(old_red)
            d_nir = int(new_nir) - int(old_nir)
            d_both = int(new_both) - int(old_both)

            saved = existing.get((x, y))
            red_cnt = (saved["red_cnt"] if saved is not None else 0) + d_red
            nir_cnt = (saved["nir_cnt"] if saved is not None else 0) + d_nir
            both_cnt = (saved["both_cnt"] if saved is not None else 0) + d_both

            if red_cnt == 0 and nir_cnt == 0 and both_cnt == 0:
                # No scene contributes anything here anymore.
                conn.execute(
                    "DELETE FROM coverage_v2 WHERE date = ? AND x = ? AND y = ?",
                    (date_str, x, y),
                )
            else:
                conn.execute(
                    "INSERT INTO coverage_v2 "
                    "(date, x, y, red_cnt, nir_cnt, both_cnt) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (date, x, y) DO UPDATE SET "
                    "red_cnt = excluded.red_cnt, "
                    "nir_cnt = excluded.nir_cnt, "
                    "both_cnt = excluded.both_cnt, "
                    "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
                    (date_str, x, y, red_cnt, nir_cnt, both_cnt),
                )
            touched += 1

    return {
        "date": sub.date,
        "scene_id": sub.scene_id,
        "band": sub.band,
        "origin_x": ox,
        "origin_y": oy,
        "width": w,
        "height": h,
        "valid_cells": valid_cells,
        "coverage_cells_updated": touched,
    }


def _classify(red_cnt: int, nir_cnt: int, both_cnt: int) -> str | None:
    """Return the gap category, or None when the cell is complete.

    Complete means at least one scene's own red and NIR are both valid at
    this cell, i.e. ``both_cnt > 0``.
    """
    if both_cnt > 0:
        return None
    if red_cnt > 0 and nir_cnt > 0:
        return GAP_DIFFERENT_SCENES
    if red_cnt > 0:
        return GAP_RED_ONLY
    if nir_cnt > 0:
        return GAP_NIR_ONLY
    return GAP_BOTH_MISSING


def query_gaps(
    conn: sqlite3.Connection,
    query_date: date_type,
    origin_x: int,
    origin_y: int,
    width: int,
    height: int,
) -> dict:
    """Find gap cells in a rectangle and merge same-category runs per row.

    A single indexed range read returns at most ``width * height`` coverage
    rows, each a fixed-size counter tuple. Masks and scene-id lists are
    never read here.
    """
    date_str = query_date.isoformat()
    rows = conn.execute(
        "SELECT x, y, red_cnt, nir_cnt, both_cnt FROM coverage_v2 "
        "WHERE date = ? AND x BETWEEN ? AND ? AND y BETWEEN ? AND ?",
        (date_str, origin_x, origin_x + width - 1, origin_y, origin_y + height - 1),
    ).fetchall()
    by_cell: dict[tuple[int, int], tuple[int, int, int]] = {
        (r["x"], r["y"]): (r["red_cnt"], r["nir_cnt"], r["both_cnt"]) for r in rows
    }

    gaps: list[GapRun] = []
    category_counts = {
        GAP_RED_ONLY: 0,
        GAP_NIR_ONLY: 0,
        GAP_DIFFERENT_SCENES: 0,
        GAP_BOTH_MISSING: 0,
    }
    complete = 0
    gap_total = 0

    for iy in range(height):
        y = origin_y + iy
        run_category: str | None = None
        run_start = origin_x

        def _flush(end_x: int) -> None:
            nonlocal run_category, run_start
            if run_category is not None:
                gaps.append(
                    GapRun(
                        y=y,
                        x_start=run_start,
                        x_end=end_x,
                        category=run_category,  # type: ignore[arg-type]
                        category_label=GAP_CATEGORY_LABELS[run_category],
                        length=end_x - run_start + 1,
                    )
                )
            run_category = None

        for ix in range(width):
            x = origin_x + ix
            red_cnt, nir_cnt, both_cnt = by_cell.get((x, y), (0, 0, 0))
            category = _classify(red_cnt, nir_cnt, both_cnt)
            if category is None:
                _flush(x - 1)
                complete += 1
                continue
            gap_total += 1
            category_counts[category] += 1
            if run_category is None:
                run_category = category
                run_start = x
            elif category != run_category:
                _flush(x - 1)
                run_category = category
                run_start = x
        _flush(origin_x + width - 1)

    return {
        "date": query_date,
        "origin_x": origin_x,
        "origin_y": origin_y,
        "width": width,
        "height": height,
        "total_cells": width * height,
        "complete_cells": complete,
        "gap_cells": gap_total,
        "category_counts": category_counts,
        "gaps": gaps,
    }


def get_scene(
    conn: sqlite3.Connection, query_date: date_type, scene_id: str
) -> dict | None:
    date_str = query_date.isoformat()
    scene = conn.execute(
        "SELECT origin_x, origin_y, width, height FROM scenes "
        "WHERE date = ? AND scene_id = ?",
        (date_str, scene_id),
    ).fetchone()
    if scene is None:
        return None
    bands = [
        {
            "band": r["band"],
            "valid_cells": sum(r["mask"]),
            "updated_at": r["updated_at"],
        }
        for r in conn.execute(
            "SELECT band, mask, updated_at FROM band_masks "
            "WHERE date = ? AND scene_id = ? ORDER BY band",
            (date_str, scene_id),
        )
    ]
    return {
        "date": query_date,
        "scene_id": scene_id,
        "origin_x": scene["origin_x"],
        "origin_y": scene["origin_y"],
        "width": scene["width"],
        "height": scene["height"],
        "bands": bands,
    }
