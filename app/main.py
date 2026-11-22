"""FastAPI application: YoY household-count comparison API.

演示脚本（配合 Swagger /docs 逐步操作）见 README.md「演示流程」。
启动时若库为空且 SEED_DEMO=1（默认），会写入两年演示边界与部分住户数。
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlite3 import Connection

from . import database as db
from . import services
from .geometry import GeometryError
from .schemas import (
    BoundaryUpload,
    BoundaryUploadResult,
    BlockOut,
    ComparisonOut,
    CountReport,
    CountReportResult,
    StateOut,
)

DEMO_YEAR_A = 2024
DEMO_YEAR_B = 2025


def get_conn() -> Connection:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def _seed_demo(conn: Connection) -> None:
    """Idempotent demo data: two years, one split block, one missing count."""
    existing = conn.execute("SELECT COUNT(*) AS n FROM blocks").fetchone()["n"]
    if existing:
        return

    with db.transaction(conn):
        # 2024: A=[0,10)x[0,10), B=[10,20)x[0,10).
        conn.executemany(
            """INSERT INTO blocks
               (year, block_no, x1, y1, x2, y2, boundary_rev, households)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
            [
                (DEMO_YEAR_A, "A", 0, 0, 10, 10, 120),
                (DEMO_YEAR_A, "B", 10, 0, 20, 10, 80),
            ],
        )
        # 2025: old block A is split into A1 + A2 (same footprint); B stays.
        conn.executemany(
            """INSERT INTO blocks
               (year, block_no, x1, y1, x2, y2, boundary_rev, households)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
            [
                (DEMO_YEAR_B, "A1", 0, 0, 6, 10, None),
                (DEMO_YEAR_B, "A2", 6, 0, 10, 10, 45),
                (DEMO_YEAR_B, "B", 10, 0, 20, 10, 75),
            ],
        )
        db.bump_state(conn, "seed", "demo boundaries and partial counts")


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect()
    try:
        db.init_db(conn)
        if os.environ.get("SEED_DEMO", "1") == "1":
            _seed_demo(conn)
    finally:
        conn.close()
    yield


app = FastAPI(
    title="社区改界后住户数同比 API",
    version="1.0.0",
    description=(
        "## 业务规则\n"
        "- 两年各至多 100 个带编号统计块，同年块为整数矩形且不得重叠；\n"
        "- 按**最细整块组合**比较两年覆盖范围（相交二分图的连通分量）；\n"
        "- 仅当组内全部块住户数齐全时，才给出两侧块号、合计与差额；\n"
        "- 范围对不上或缺数时，返回相关块号与原因；\n"
        "- 可整份更正某年边界：同号且边界未变的块沿用原修订号与住户数，"
        "边界变化的块产生新修订号并清空计数；\n"
        "- 补报住户数必须携带块的**现行边界修订号**，不匹配返回 409；\n"
        "- 每次同比读取同一已提交状态并保存快照；只改住户数时复用范围"
        "比对结果（`reused_range_result=true`）；写失败回滚，原数据保留。\n\n"
        "## 演示流程（先调 /yoy/compare 观察缺数与改界，再补报、再同比）\n"
        "1. `GET /yoy/compare?year_a=2024&year_b=2025`：A 被拆成 A1/A2，"
        "A1 缺住户数，该组为 `missing_counts`；\n"
        "2. `POST /counts` 补报 2025 年 A1（boundary_rev=1, households=85）；\n"
        "3. 再次同比：该组变为 `matched`，且 `reused_range_result=true`；\n"
        "4. `PUT /boundaries` 整份更正 2025 年边界（例如移动 A2），"
        "响应中 `reset_blocks` 显示被清空计数的块；\n"
        "5. 用旧修订号补报会得到 409，改用新修订号后恢复；\n"
        "6. `GET /state` 查看每次提交对应的 state_id 与变更明细。"
    ),
    lifespan=lifespan,
)


@app.put(
    "/boundaries",
    response_model=BoundaryUploadResult,
    summary="整份上传/更正某年边界表（至多 100 块）",
)
def put_boundaries(
    payload: BoundaryUpload, conn: Connection = Depends(get_conn)
) -> BoundaryUploadResult:
    try:
        result = services.replace_boundaries(conn, payload.year, payload.blocks)
    except services.ServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    except GeometryError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return BoundaryUploadResult(**result)


@app.post(
    "/counts",
    response_model=CountReportResult,
    summary="补报/修订某块住户数（须匹配现行边界修订号）",
)
def post_count(
    payload: CountReport, conn: Connection = Depends(get_conn)
) -> CountReportResult:
    try:
        result = services.report_count(
            conn,
            payload.year,
            payload.block_no.strip(),
            payload.households,
            payload.boundary_rev,
        )
    except services.ServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return CountReportResult(**result)


@app.get(
    "/blocks/{year}",
    response_model=list[BlockOut],
    summary="查看某年全部块（边界、修订号、住户数）",
)
def get_blocks(year: int, conn: Connection = Depends(get_conn)) -> list[BlockOut]:
    return [services.row_to_block_out(r) for r in services.list_blocks(conn, year)]


@app.get(
    "/yoy/compare",
    response_model=ComparisonOut,
    summary="按当前已提交状态执行两年同比并保存快照",
)
def yoy_compare(
    year_a: int = Query(..., ge=1900, le=2999, description="基期年"),
    year_b: int = Query(..., ge=1900, le=2999, description="报告年"),
    conn: Connection = Depends(get_conn),
) -> ComparisonOut:
    return services.run_comparison(conn, year_a, year_b)


@app.get(
    "/yoy/comparisons/{comparison_id}",
    summary="读取历史同比快照（不可变）",
)
def get_yoy(comparison_id: int, conn: Connection = Depends(get_conn)) -> dict:
    payload = services.get_comparison(conn, comparison_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="同比快照不存在")
    return payload


@app.get("/state", response_model=StateOut, summary="当前已提交状态与变更历史")
def get_state(conn: Connection = Depends(get_conn)) -> StateOut:
    return StateOut(**services.get_state(conn))


@app.get("/health", summary="健康检查")
def health() -> dict:
    return {"status": "ok"}
