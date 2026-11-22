"""Business logic: boundary replacement, count reports, YoY comparison."""
from __future__ import annotations

import json
import sqlite3

from . import database as db
from .geometry import Rect, assert_no_overlap, canonical_geometry, validate_rect
from .overlay import build_components
from .schemas import BlockOut, BlockRef, ComparisonOut, CountReportResult, GroupOut


class ServiceError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

def list_blocks(conn: sqlite3.Connection, year: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM blocks WHERE year=? ORDER BY block_no", (year,)
    ).fetchall()


def row_to_block_out(row: sqlite3.Row) -> BlockOut:
    return BlockOut(
        year=row["year"],
        block_no=row["block_no"],
        x1=row["x1"],
        y1=row["y1"],
        x2=row["x2"],
        y2=row["y2"],
        boundary_rev=row["boundary_rev"],
        households=row["households"],
    )


def _load_year(conn: sqlite3.Connection, year: int) -> dict[str, sqlite3.Row]:
    return {row["block_no"]: row for row in list_blocks(conn, year)}


# --------------------------------------------------------------------------- #
# Boundary replacement (full-year correction)
# --------------------------------------------------------------------------- #

def replace_boundaries(
    conn: sqlite3.Connection, year: int, incoming: list
) -> dict:
    # All validation happens before any write so failures keep old data.
    seen: set[str] = set()
    new_rects: dict[str, Rect] = {}
    for item in incoming:
        no = item.block_no.strip()
        if no in seen:
            raise ServiceError(422, f"块编号 {no} 在本次上传中重复")
        seen.add(no)
        new_rects[no] = validate_rect(
            item.x1, item.y1, item.x2, item.y2, no
        )
    assert_no_overlap(new_rects)

    with db.transaction(conn):
        existing = _load_year(conn, year)
        max_rev = max(
            (r["boundary_rev"] for r in existing.values()), default=0
        )
        next_rev = max_rev + 1

        retained: list[str] = []
        reset: list[str] = []
        removed = sorted(set(existing) - set(new_rects))
        insert_rows: list[tuple] = []

        for no, rect in new_rects.items():
            old = existing.get(no)
            if old is not None and (
                old["x1"], old["y1"], old["x2"], old["y2"]
            ) == (rect.x1, rect.y1, rect.x2, rect.y2):
                # Same number, unchanged boundary: keep revision and counts.
                retained.append(no)
                insert_rows.append(
                    (
                        year, no, rect.x1, rect.y1, rect.x2, rect.y2,
                        old["boundary_rev"], old["households"],
                    )
                )
            else:
                # New block or changed boundary: fresh revision, counts void.
                reset.append(no)
                insert_rows.append(
                    (
                        year, no, rect.x1, rect.y1, rect.x2, rect.y2,
                        next_rev, None,
                    )
                )

        conn.execute("DELETE FROM blocks WHERE year=?", (year,))
        conn.executemany(
            """INSERT INTO blocks
               (year, block_no, x1, y1, x2, y2, boundary_rev, households)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            insert_rows,
        )
        state_id = db.bump_state(
            conn,
            "boundary_replace",
            json.dumps(
                {
                    "year": year,
                    "retained": sorted(retained),
                    "reset": sorted(reset),
                    "removed": removed,
                },
                ensure_ascii=False,
            ),
        )

    rows = list_blocks(conn, year)
    return {
        "state_id": state_id,
        "year": year,
        "block_count": len(rows),
        "retained_blocks": sorted(retained),
        "reset_blocks": sorted(reset),
        "removed_blocks": removed,
        "blocks": [row_to_block_out(r) for r in rows],
    }


# --------------------------------------------------------------------------- #
# Household count report
# --------------------------------------------------------------------------- #

def report_count(
    conn: sqlite3.Connection,
    year: int,
    block_no: str,
    households: int,
    boundary_rev: int,
) -> dict:
    with db.transaction(conn):
        row = conn.execute(
            "SELECT * FROM blocks WHERE year=? AND block_no=?",
            (year, block_no),
        ).fetchone()
        if row is None:
            raise ServiceError(404, f"{year} 年不存在块 {block_no}")
        if row["boundary_rev"] != boundary_rev:
            raise ServiceError(
                409,
                f"修订号不匹配：块 {block_no} 现行边界修订号为 "
                f"{row['boundary_rev']}，收到 {boundary_rev}；"
                f"请按现行边界重新补报",
            )
        conn.execute(
            "UPDATE blocks SET households=? WHERE year=? AND block_no=?",
            (households, year, block_no),
        )
        state_id = db.bump_state(
            conn,
            "count_report",
            json.dumps(
                {"year": year, "block_no": block_no, "households": households},
                ensure_ascii=False,
            ),
        )
        new_row = conn.execute(
            "SELECT * FROM blocks WHERE year=? AND block_no=?",
            (year, block_no),
        ).fetchone()

    return {"state_id": state_id, "block": row_to_block_out(new_row)}


# --------------------------------------------------------------------------- #
# YoY comparison
# --------------------------------------------------------------------------- #

def _rect_of(row: sqlite3.Row) -> Rect:
    return Rect(row["x1"], row["y1"], row["x2"], row["y2"])


def _cache_key(
    year_a: int, geo_a: str, year_b: int, geo_b: str
) -> str:
    return f"{year_a}::{geo_a}||{year_b}::{geo_b}"


def _block_ref(row: sqlite3.Row) -> BlockRef:
    rect = _rect_of(row)
    return BlockRef(
        year=row["year"],
        block_no=row["block_no"],
        boundary_rev=row["boundary_rev"],
        households=row["households"],
        area=rect.area,
    )


def run_comparison(
    conn: sqlite3.Connection, year_a: int, year_b: int
) -> ComparisonOut:
    # One snapshot transaction: every read and the stored snapshot share the
    # same committed state. Reports submitted meanwhile land in later states.
    with db.transaction(conn):
        state_id = db.current_state_id(conn)
        rows_a = _load_year(conn, year_a)
        rows_b = _load_year(conn, year_b)

        rects_a = {no: _rect_of(r) for no, r in rows_a.items()}
        rects_b = {no: _rect_of(r) for no, r in rows_b.items()}
        geo_a = canonical_geometry(
            [(no, r) for no, r in rects_a.items()]
        )
        geo_b = canonical_geometry(
            [(no, r) for no, r in rects_b.items()]
        )
        key = _cache_key(year_a, geo_a, year_b, geo_b)

        cached = conn.execute(
            "SELECT payload FROM geometry_match_cache WHERE cache_key=?",
            (key,),
        ).fetchone()
        reused = cached is not None
        if cached:
            components = json.loads(cached["payload"])
        else:
            comps = build_components(rects_a, rects_b)
            components = [
                {
                    "a_blocks": c.a_blocks,
                    "b_blocks": c.b_blocks,
                    "a_area": c.a_area,
                    "b_area": c.b_area,
                    "intersections": c.intersections,
                }
                for c in comps
            ]
            conn.execute(
                """INSERT INTO geometry_match_cache(cache_key, year_a, year_b, payload)
                   VALUES(?, ?, ?, ?)
                   ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload""",
                (key, year_a, year_b, json.dumps(components, ensure_ascii=False)),
            )

        groups: list[GroupOut] = []
        all_complete = True

        for comp in components:
            a_refs = [_block_ref(rows_a[no]) for no in comp["a_blocks"]]
            b_refs = [_block_ref(rows_b[no]) for no in comp["b_blocks"]]
            geometry_matched = bool(
                comp["a_blocks"]
                and comp["b_blocks"]
                and comp["a_area"] == comp["b_area"]
            )

            if not geometry_matched:
                all_complete = False
                if not comp["b_blocks"]:
                    reason = (
                        f"{year_a} 年块 {comp['a_blocks']} 的覆盖范围"
                        f"在 {year_b} 年无对应块"
                    )
                elif not comp["a_blocks"]:
                    reason = (
                        f"{year_b} 年块 {comp['b_blocks']} 的覆盖范围"
                        f"在 {year_a} 年无对应块"
                    )
                else:
                    reason = (
                        f"块组合覆盖范围不一致：{year_a} 年面积 "
                        f"{comp['a_area']}，{year_b} 年面积 "
                        f"{comp['b_area']}，存在仅一侧覆盖的区域"
                    )
                groups.append(
                    GroupOut(
                        status="range_mismatch",
                        year_a_blocks=a_refs,
                        year_b_blocks=b_refs,
                        households_a_total=None,
                        households_b_total=None,
                        difference_b_minus_a=None,
                        missing=[],
                        reason=reason,
                    )
                )
                continue

            missing = [
                ref
                for ref in (*a_refs, *b_refs)
                if ref.households is None
            ]
            if missing:
                all_complete = False
                nos = ", ".join(
                    f"{ref.year}年块{ref.block_no}(rev{ref.boundary_rev})"
                    for ref in missing
                )
                groups.append(
                    GroupOut(
                        status="missing_counts",
                        year_a_blocks=a_refs,
                        year_b_blocks=b_refs,
                        households_a_total=None,
                        households_b_total=None,
                        difference_b_minus_a=None,
                        missing=missing,
                        reason=f"组内住户数未报齐，缺数块：{nos}",
                    )
                )
                continue

            total_a = sum(ref.households for ref in a_refs)
            total_b = sum(ref.households for ref in b_refs)
            groups.append(
                GroupOut(
                    status="matched",
                    year_a_blocks=a_refs,
                    year_b_blocks=b_refs,
                    households_a_total=total_a,
                    households_b_total=total_b,
                    difference_b_minus_a=total_b - total_a,
                    missing=[],
                    reason=None,
                )
            )

        payload = {
            "state_id": state_id,
            "year_a": year_a,
            "year_b": year_b,
            "reused_range_result": reused,
            "all_complete": all_complete,
            "groups": [g.model_dump() for g in groups],
        }
        cur = conn.execute(
            """INSERT INTO comparisons
               (state_id, year_a, year_b, reused_range, payload)
               VALUES(?, ?, ?, ?, ?)""",
            (
                state_id,
                year_a,
                year_b,
                int(reused),
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        comparison_id = cur.lastrowid

    return ComparisonOut(
        comparison_id=comparison_id,
        state_id=state_id,
        year_a=year_a,
        year_b=year_b,
        reused_range_result=reused,
        all_complete=all_complete,
        groups=groups,
    )


def get_comparison(conn: sqlite3.Connection, comparison_id: int) -> dict | None:
    row = conn.execute(
        "SELECT payload FROM comparisons WHERE comparison_id=?",
        (comparison_id,),
    ).fetchone()
    return json.loads(row["payload"]) if row else None


def get_state(conn: sqlite3.Connection) -> dict:
    state_id = db.current_state_id(conn)
    history = conn.execute(
        "SELECT state_id, change, detail, created_at FROM state_log ORDER BY state_id"
    ).fetchall()
    return {
        "state_id": state_id,
        "history": [dict(r) for r in history],
    }
