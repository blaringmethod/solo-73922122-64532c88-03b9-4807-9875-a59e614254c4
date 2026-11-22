# 社区改界后住户数同比 API（FastAPI + SQLite）

按同一米制整数坐标范围比较两年统计块的住户数。每年至多 100 个带编号的
整数矩形块，同年块不得重叠；系统按**最细整块组合**匹配两年覆盖范围，组内
住户数齐全时输出两侧块号、合计与差额，否则列出相关块和原因。

## 1. 启动

```bash
docker compose up --build
# API:   http://localhost:8000
# 文档:  http://localhost:8000/docs   （Swagger，可直接演示）
# 数据:  命名卷 yoy-data 中的 SQLite 文件，容器重建不丢数据
```

本地无 Docker 时：

```bash
python3 -m pip install -r requirements.txt
DB_PATH=./households.db SEED_DEMO=1 \
  python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

`SEED_DEMO=1`（默认）在空库时写入演示数据（2024/2025 两年，含一块拆分与
一处缺数）；`SEED_DEMO=0` 表示空库启动。

## 2. 核心规则与实现

### 2.1 几何与"最细整块组合"

* 矩形为半开整数盒 `[x1,x2) × [y1,y2)`，要求 `x2>x1、y2>y1`，坐标整数；
  仅边相接（零面积相交）不算重叠、也不构成覆盖对应。
* 同年块两两做正面积相交校验，重叠或重号、超 100 块一律 422 拒绝。
* 两年匹配：对两年块建**相交二分图**（有正面积相交即连边），其每个连通
  分量就是最细整块组合——
  * 不同分量间无任何共享覆盖，整块匹配不可能跨分量合并；
  * 同一分量内的块经共享面积传递相连，任何整块匹配只要含其中一块就必须
    含全部分量成员，因此分量不可再细分。
* 分量两侧面积相等（同年块互不重叠，等价于两侧单元格完全互覆）才算范围
  一致；否则标 `range_mismatch` 并给出原因（单侧独有 / 面积不等）。

### 2.2 改界（整份更正某年边界表）

`PUT /boundaries` 全量替换某年边界，所有校验（重号、非法矩形、同年重叠、
数量上限）在写库前完成，失败整体回滚、原数据不变。对每个上传块：

* 同号且四条边界完全未变 → 沿用原 `boundary_rev` 与原住户数（`retained_blocks`）；
* 新块或同号但边界变化 → 分配新修订号 `max(旧修订号)+1`，住户数清空
  （`reset_blocks`）；
* 本次未出现的旧块 → 删除（`removed_blocks`）。

### 2.3 补报住户数

`POST /counts` 必须携带块的**现行边界修订号**：

* 块不存在 → 404；修订号与现行值不符 → 409（提示现行修订号）；
* 匹配则写入/更新住户数，每次成功提交推进 `state_id`；
* 边界比对期间可随时补报：同比在单个 `BEGIN IMMEDIATE` 事务内读快照，
  补报是独立短事务，两者互不阻塞到数据不一致（SQLite WAL + busy_timeout）。

### 2.4 同比与状态

* `GET /yoy/compare?year_a=&year_b=` 在一个事务内读取当前已提交状态，
  结果作为不可变快照写入 `comparisons`，可用
  `GET /yoy/comparisons/{id}` 取回；补报/改界只影响之后的新同比。
* 范围比对结果按两年边界的几何指纹缓存于 `geometry_match_cache`。只改
  住户数时指纹不变，响应中 `reused_range_result=true`；任何边界变化都会
  使缓存未命中并重算。
* 组状态：
  * `matched`：范围一致且住户数齐全 → 给两侧块号、`households_*_total`、
    `difference_b_minus_a`（报告年 − 基期年）；
  * `missing_counts`：范围一致但有块未报数 → 列出 `missing` 块及现行修订号；
  * `range_mismatch`：范围对不上 → 列出两侧相关块与原因，不给合计。
* 所有写操作失败即回滚，保留原数据（见测试 `test_failed_write_preserves_original_data`）。

## 3. /docs 演示流程（启动后按序在 Swagger 执行）

演示数据：2024 年 `A=[0,10)×[0,10)`（120 户）、`B=[10,20)×[0,10)`（80 户）；
2025 年 `A` 拆为 `A1=[0,6)×[0,10)`（**缺数**）与 `A2=[6,10)×[0,10)`（45 户），
`B` 不变（75 户）。

1. `GET /yoy/compare?year_a=2024&year_b=2025`
   → A/A1/A2 组 `missing_counts`（缺 A1）；B 组 `matched`，差额 −5。
2. `POST /counts`，body：
   `{"year":2025,"block_no":"A1","households":85,"boundary_rev":1}`
   → 提交成功，`state_id` 前进。
3. 再次 `GET /yoy/compare`
   → A 组变 `matched`（120 vs 130，差额 +10），`reused_range_result=true`。
4. `PUT /boundaries` 整份更正 2025（把 A2 缩小为 `[6,9)×[0,10)`，其余照旧）
   → `retained_blocks=["A1","B"]`、`reset_blocks=["A2"]`，A2 计数清空、
   修订号变为 2。
5. 用旧修订号给 A2 补报（`boundary_rev=1`）→ **409**；改用 `boundary_rev=2`
   → 成功。
6. 再次同比 → A 组 `range_mismatch`（面积 100 vs 90），且
   `reused_range_result=false`（边界已变，缓存失效）。
7. `GET /yoy/comparisons/2` → 第 2 次同比的历史快照内容不变。
8. `GET /state` → 查看每次提交的 `state_id`、变更类型与明细。

附：上传互相重叠的块（例如 A2 用 `[6,12)` 与 B 重叠）会被 422 拒绝且旧
数据保留；上传 101 个块同样 422。

## 4. API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| PUT | `/boundaries` | 整份上传/更正某年边界（≤100 块），返回保留/重置/删除明细 |
| POST | `/counts` | 补报/修订住户数，须匹配现行边界修订号 |
| GET | `/blocks/{year}` | 查看某年块边界、修订号、住户数 |
| GET | `/yoy/compare` | 执行同比并保存不可变快照 |
| GET | `/yoy/comparisons/{id}` | 读取历史同比快照 |
| GET | `/state` | 当前 state_id 与提交历史 |
| GET | `/health` | 健康检查 |

## 5. 项目结构与测试

```
app/
  geometry.py    # 整数矩形、面积/相交、同年重叠校验、几何指纹
  overlay.py     # 相交二分图连通分量（最细整块组合）
  database.py    # SQLite schema、WAL、事务与 state_id
  schemas.py     # Pydantic 模型与 100 块上限
  services.py    # 改界、补报、同比、缓存、快照
  main.py        # FastAPI 路由、异常映射、演示种子
tests/           # 19 个 pytest 用例
```

```bash
python3 -m pytest -q
```

覆盖：几何相交/边相接、非法矩形与重叠拒绝、重号与 100 块上限、拆分块最
细分量、缺数/齐全/差额、范围不一致原因、改界保留与重置、删除块、修订号
409/404、范围缓存复用与失效、快照不可变、写失败回滚保留原数据、空年份。

## 6. 待完成事项 / 说明

* 交付包含 `Dockerfile` 与 `docker-compose.yml`；当前开发环境未安装
  Docker，容器构建未在本机执行，已通过 uvicorn 实跑 + 全量 pytest 验证，
  上环境后建议先跑 `docker compose config` 与 `docker compose up --build`。
* 坐标与块数按题设约束（每年 ≤100 块）实现，两两相交 O(n²) 在该规模下
  足够；如未来放宽块数，可换成扫描线/栅格索引。
* 鉴权未在需求范围内，未实现；生产部署建议放在网关之后并限制写接口。
