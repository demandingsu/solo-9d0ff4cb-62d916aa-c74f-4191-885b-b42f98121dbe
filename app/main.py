"""FastAPI 入口：预报/实测 CSV 导入、回测样本数据集生成与查询。"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Body, FastAPI, HTTPException, Path
from pydantic import BaseModel, Field

from . import db, logic

# ---------------------------------------------------------------- 示例（同时用于 /docs 与 README）

FORECAST_CSV_EXAMPLE = """station,target_hour,issued_at,irradiance
ST01,2026-03-01T00:00:00Z,2026-02-27T08:00:00Z,500.0
ST01,2026-03-01T00:00:00Z,2026-02-28T06:00:00Z,550.0
ST01,2026-03-01T01:00:00Z,2026-02-28T00:30:00Z,510.0
ST01,2026-03-01T03:00:00Z,2026-02-27T09:00:00Z,520.0
"""

ACTUAL_CSV_EXAMPLE = """station,target_hour,energy
ST01,2026-03-01T00:00:00Z,48.2
ST01,2026-03-01T01:00:00Z,49.1
ST01,2026-03-01T02:00:00Z,47.5
"""

REVISION_SET_CSV_EXAMPLE = """station,target_hour,revision,action,energy
ST01,2026-03-01T00:00:00Z,1,set,50.5
"""

REVISION_WITHDRAW_CSV_EXAMPLE = """station,target_hour,revision,action,energy
ST01,2026-03-01T01:00:00Z,1,withdraw,
"""

REVISION_RESTORE_CSV_EXAMPLE = """station,target_hour,revision,action,energy
ST01,2026-03-01T01:00:00Z,2,set,49.4
"""

GENERATE_EXAMPLE = {
    "station": "ST01",
    "start_hour": "2026-03-01T00:00:00Z",
    "end_hour": "2026-03-01T03:00:00Z",
    "lead_hours": 24,
}

API_DESCRIPTION = """
光伏回测样本服务：把历史辐照预报与实测发电量配成回测样本，**保证事后发布的预报不会进入样本**。

## 使用流程

1. `POST /imports/forecasts` 导入预报 CSV（场站、目标 UTC 小时、发布 UTC 时刻、预测辐照）。
2. `POST /imports/actuals` 导入实测 CSV（场站、目标小时、电量）。
3. `POST /datasets/{数据集号}/generate` 指定场站、目标小时区间、提前小时数生成配对结果并保存。
4. `GET /datasets/{数据集号}` 随时读回已保存的结果。

## 配对与迟到预报规则

对每个目标小时，发布截止时刻 = `目标小时 - lead_hours`。
只采用 `issued_at <= 截止时刻` 的预报，取其中**最新**一条；晚于截止时刻发布的预报视为**迟到预报**，不参与配对。
每行结果都带回 `cutoff_issued_at`（截止时刻）与实际采用的 `issued_at`（来源时刻），迟到预报可在导入后通过对比发现。

## 缺配原因（missing_reason）

| 代码 | 含义 |
|---|---|
| `no_forecast` | 截止时刻前无已发布预报（或全部迟到） |
| `no_actual` | 无实测电量 |
| `no_forecast+no_actual` | 两者均缺 |
| `null` | 配对成功 |

## 导入幂等与整批拒收

- 同键同值重复导入：幂等跳过（`skipped_duplicates`）。
- 同键异值（批内或与本库已有数据冲突）：**整批拒收**（409），一行都不会写入。
- 行格式错误：整批拒收（422），并逐行列出原因。

## 实测修订（计量复核）

普通导入的实测视为 **0 号修订**。`POST /imports/actual-revisions` 按 场站+目标小时+修订号 提交修订：

- `action=set`：把该小时实测修订为新电量（`energy` 必填）；
- `action=withdraw`：撤回该小时实测（`energy` 留空），该小时退出后续回测，
  新生成的数据集中 `energy=null`、`missing_reason=no_actual`、`actual_revision` 为撤回修订号。

规则：

- 每小时只应用**更高**修订号；撤回后提交更高号 `set` 可**恢复**该小时实测。
- 已处理修订**原样重投**：幂等跳过（`skipped_duplicates`），状态不变。
- **旧号首次送达**（修订号 ≤ 该小时当前最大号且从未记录过）：整批拒收（409）。
- **同号异内容**（批内或与库中记录冲突）：整批拒收（409）。
- 普通实测导入（0 号）**不得覆盖已修订的小时，也不得复活已撤回记录**：冲突整批拒收（409）；
  与已存 0 号记录同值的重投仍幂等跳过。
- 数据集每行带回 `actual_revision`：0=普通导入实测，≥1=生效修订号，null=无实测。
- 修订记录与当前生效实测在**同一个写事务**内生效；任一环节失败整体回滚，不留不一致状态。

## 数据集号语义

- 同一数据集号 + 相同参数重试：直接读回首次保存的结果（`replayed=true`），即使期间又有新数据导入。
- 同一数据集号 + 不同参数：409 冲突，返回已存参数与请求参数。

## 快照隔离

生成在一个 `BEGIN IMMEDIATE` 写事务内完成：事务期间其他进程的导入无法提交，
整份结果必然来自**同一个已提交快照**；任一环节失败即整体回滚，不会留下可见的半份结果。
"""

TAGS = [
    {"name": "imports", "description": "CSV 导入（整批原子，同键异值拒收）；实测修订按修订号生效"},
    {"name": "datasets", "description": "回测样本数据集：生成、幂等重试、查询"},
    {"name": "meta", "description": "服务信息"},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db_path = os.environ.get("APP_DB_PATH", os.path.join(os.getcwd(), "data", "app.db"))
    db.init_db(app.state.db_path)
    yield


app = FastAPI(
    title="光伏回测样本服务",
    version="1.0.0",
    description=API_DESCRIPTION,
    openapi_tags=TAGS,
    lifespan=lifespan,
)


# ---------------------------------------------------------------- 模型

class GenerateParams(BaseModel):
    station: str = Field(min_length=1, max_length=128, description="场站编号", examples=["ST01"])
    start_hour: str = Field(description="起始目标 UTC 小时（整点）", examples=["2026-03-01T00:00:00Z"])
    end_hour: str = Field(description="结束目标 UTC 小时（整点，含）", examples=["2026-03-01T03:00:00Z"])
    lead_hours: int = Field(ge=0, le=24 * 90, description="提前小时数：只采用 目标小时-lead_hours 之前发布的预报", examples=[24])


DATASET_ID_PATH = Path(
    pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    description="数据集号（字母数字开头，可含 . _ -）",
    examples=["demo-20260301"],
)


# ---------------------------------------------------------------- 工具

def _commit(conn: sqlite3.Connection) -> None:
    """提交当前事务。独立成函数以便测试注入提交失败。"""
    conn.execute("COMMIT")


def _locked_to_503(exc: sqlite3.OperationalError) -> None:
    if "locked" in str(exc).lower():
        raise HTTPException(status_code=503, detail={
            "message": "数据库正被其他进程写入，请稍后重试",
            "retryable": True,
        })


def _import_batch(table: str, fields: list[str], key_fields: list[str], value_field: str, rows: list[dict]):
    """单事务导入：同键同值跳过，同键异值整批回滚并返回 409。"""
    conn = db.connect(app.state.db_path)
    inserted = skipped = 0
    conflicts = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        where = " AND ".join(f"{f} = ?" for f in key_fields)
        columns = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        for row in rows:
            key = tuple(row[f] for f in key_fields)
            existing = conn.execute(f"SELECT {value_field} AS v FROM {table} WHERE {where}", key).fetchone()
            if existing is None:
                conn.execute(
                    f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
                    tuple(row[f] for f in fields),
                )
                inserted += 1
            elif existing["v"] != row[value_field]:
                conflicts.append({
                    "key": dict(zip(key_fields, key)),
                    "db_value": existing["v"],
                    "csv_value": row[value_field],
                })
            else:
                skipped += 1
        if conflicts:
            conn.execute("ROLLBACK")
            raise HTTPException(status_code=409, detail={
                "message": "同键异值，整批拒收（未写入任何行）",
                "conflicts": conflicts,
            })
        _commit(conn)
        return inserted, skipped
    except HTTPException:
        raise
    except sqlite3.OperationalError as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        _locked_to_503(exc)
        raise
    finally:
        conn.close()


def _latest_revision(conn, station: str, target_hour: str):
    """该小时库中最高号修订记录（无则 None）。调用方须处于事务中。"""
    return conn.execute(
        "SELECT revision, action, energy FROM actual_revisions "
        "WHERE station = ? AND target_hour = ? ORDER BY revision DESC LIMIT 1",
        (station, target_hour),
    ).fetchone()


def _import_actuals(rows: list[dict]):
    """普通实测导入 ≡ 投递 0 号修订。

    - 已有 0 号记录：同值幂等跳过，异值整批 409；
    - 该小时已存在修订（>=1 号）：普通导入不得覆盖已修订小时、不得复活已撤回记录，整批 409；
    - 否则写入 0 号记录。
    """
    conn = db.connect(app.state.db_path)
    inserted = skipped = 0
    conflicts = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for row in rows:
            key = (row["station"], row["target_hour"])
            existing = conn.execute(
                "SELECT energy FROM actuals WHERE station = ? AND target_hour = ?", key
            ).fetchone()
            if existing is not None:
                if existing["energy"] != row["energy"]:
                    conflicts.append({
                        "key": {"station": key[0], "target_hour": key[1]},
                        "reason": "same_key_different_value",
                        "db_value": existing["energy"],
                        "csv_value": row["energy"],
                    })
                else:
                    skipped += 1
                continue
            latest = _latest_revision(conn, *key)
            if latest is not None:
                conflicts.append({
                    "key": {"station": key[0], "target_hour": key[1]},
                    "reason": "hour_already_revised",
                    "message": "该小时已存在修订记录，普通实测导入（0 号）不得覆盖或复活，请改用 /imports/actual-revisions",
                    "csv_value": row["energy"],
                    "current_revision": latest["revision"],
                    "effective_action": latest["action"],
                    "effective_energy": latest["energy"],
                })
                continue
            conn.execute(
                "INSERT INTO actuals (station, target_hour, energy) VALUES (?, ?, ?)",
                (row["station"], row["target_hour"], row["energy"]),
            )
            inserted += 1
        if conflicts:
            conn.execute("ROLLBACK")
            raise HTTPException(status_code=409, detail={
                "message": "实测导入与已有数据冲突，整批拒收（未写入任何行）",
                "conflicts": conflicts,
            })
        _commit(conn)
        return inserted, skipped
    except HTTPException:
        raise
    except sqlite3.OperationalError as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        _locked_to_503(exc)
        raise
    finally:
        conn.close()


def _import_actual_revisions(rows: list[dict]):
    """实测修订导入：每小时只应用更高修订号。

    - 修订号已记录：内容相同幂等跳过，内容不同整批 409（同号异内容）；
    - 修订号未记录且不高于该小时当前最大号（含 0 号普通实测）：整批 409（旧号首次送达）；
    - 同一批内同一小时的多个修订按修订号升序依次应用；
    - 任何冲突整批回滚，一行都不写入。
    """
    conn = db.connect(app.state.db_path)
    inserted = skipped = 0
    conflicts = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        created_at = logic.canon(datetime.now(timezone.utc))
        # 同一小时批内多个修订按修订号升序应用
        ordered = sorted(rows, key=lambda r: (r["station"], r["target_hour"], r["revision"]))
        batch_max: dict[tuple[str, str], int] = {}
        for row in ordered:
            hour_key = (row["station"], row["target_hour"])
            existing = conn.execute(
                "SELECT action, energy FROM actual_revisions "
                "WHERE station = ? AND target_hour = ? AND revision = ?",
                (hour_key[0], hour_key[1], row["revision"]),
            ).fetchone()
            if existing is not None:
                if existing["action"] == row["action"] and existing["energy"] == row["energy"]:
                    skipped += 1  # 已处理修订原样重投：幂等
                else:
                    conflicts.append({
                        "key": {"station": hour_key[0], "target_hour": hour_key[1],
                                "revision": row["revision"]},
                        "reason": "same_revision_different_content",
                        "message": "同号异内容",
                        "db": {"action": existing["action"], "energy": existing["energy"]},
                        "csv": {"action": row["action"], "energy": row["energy"]},
                    })
                continue
            current_max = batch_max.get(hour_key, logic.BASE_REVISION - 1)
            latest = _latest_revision(conn, *hour_key)
            if latest is not None:
                current_max = max(current_max, latest["revision"])
            base_exists = conn.execute(
                "SELECT 1 FROM actuals WHERE station = ? AND target_hour = ?", hour_key
            ).fetchone()
            if base_exists is not None:
                current_max = max(current_max, logic.BASE_REVISION)
            if row["revision"] <= current_max:
                conflicts.append({
                    "key": {"station": hour_key[0], "target_hour": hour_key[1],
                            "revision": row["revision"]},
                    "reason": "stale_revision",
                    "message": "旧号首次送达：修订号不高于该小时当前最大修订号",
                    "current_max_revision": current_max,
                })
                continue
            conn.execute(
                "INSERT INTO actual_revisions (station, target_hour, revision, action, energy, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (hour_key[0], hour_key[1], row["revision"], row["action"], row["energy"], created_at),
            )
            batch_max[hour_key] = row["revision"]
            inserted += 1
        if conflicts:
            conn.execute("ROLLBACK")
            raise HTTPException(status_code=409, detail={
                "message": "修订冲突，整批拒收（未写入任何行）",
                "conflicts": conflicts,
            })
        _commit(conn)
        return inserted, skipped
    except HTTPException:
        raise
    except sqlite3.OperationalError as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        _locked_to_503(exc)
        raise
    finally:
        conn.close()


def _parse_or_reject(kind: str, parse_fn, csv_text: str):
    try:
        rows, errors, conflicts = parse_fn(csv_text)
    except logic.CsvFormatError as exc:
        raise HTTPException(status_code=422, detail={"message": str(exc)})
    if errors:
        raise HTTPException(status_code=422, detail={
            "message": f"{kind} CSV 存在错误行，整批未导入",
            "errors": errors,
        })
    if conflicts:
        raise HTTPException(status_code=409, detail={
            "message": f"{kind} CSV 批内同键异值，整批拒收（未写入任何行）",
            "conflicts": conflicts,
        })
    if not rows:
        raise HTTPException(status_code=422, detail={"message": f"{kind} CSV 无有效数据行"})
    return rows


# ---------------------------------------------------------------- 路由：meta

@app.get("/", tags=["meta"], summary="服务信息")
def root():
    return {
        "service": "光伏回测样本服务",
        "docs": "/docs",
        "endpoints": [
            "POST /imports/forecasts",
            "POST /imports/actuals",
            "POST /imports/actual-revisions",
            "POST /datasets/{dataset_id}/generate",
            "GET /datasets",
            "GET /datasets/{dataset_id}",
        ],
    }


@app.get("/health", tags=["meta"], summary="健康检查")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------- 路由：导入

@app.post(
    "/imports/forecasts",
    tags=["imports"],
    summary="导入预报 CSV",
    description="整批原子导入。同键（场站+目标小时+发布时刻）同值幂等跳过；同键异值整批拒收（409）。",
)
def import_forecasts(
    csv_text: str = Body(
        media_type="text/csv",
        openapi_examples={
            "含迟到预报的预报批次": {
                "summary": "预报导入示例（含一条迟到预报）",
                "description": (
                    "配合生成示例（lead_hours=24）：目标小时 2026-03-01T00:00:00Z 的发布截止时刻为 "
                    "2026-02-28T00:00:00Z。第二行 550.0 发布于 2026-02-28T06:00:00Z，**晚于截止时刻，"
                    "属于迟到预报，不会被采用**，配对结果将使用 500.0（2026-02-27T08:00:00Z 发布）。"
                ),
                "value": FORECAST_CSV_EXAMPLE,
            },
        },
    ),
):
    rows = _parse_or_reject("预报", logic.parse_forecast_csv, csv_text)
    inserted, skipped = _import_batch(
        "forecasts",
        ["station", "target_hour", "issued_at", "irradiance"],
        ["station", "target_hour", "issued_at"],
        "irradiance",
        rows,
    )
    return {"inserted": inserted, "skipped_duplicates": skipped, "accepted_rows": len(rows)}


@app.post(
    "/imports/actuals",
    tags=["imports"],
    summary="导入实测 CSV",
    description=(
        "整批原子导入，视为 0 号修订。同键（场站+目标小时）同值幂等跳过；同键异值整批拒收（409）。"
        "已存在修订（≥1 号）的小时：普通导入不得覆盖、不得复活已撤回记录，冲突整批拒收（409）。"
    ),
)
def import_actuals(
    csv_text: str = Body(
        media_type="text/csv",
        openapi_examples={
            "实测批次": {
                "summary": "实测导入示例",
                "description": "目标小时 2026-03-01T03:00:00Z 故意缺实测，用于演示缺配原因 no_actual。",
                "value": ACTUAL_CSV_EXAMPLE,
            },
        },
    ),
):
    rows = _parse_or_reject("实测", logic.parse_actual_csv, csv_text)
    inserted, skipped = _import_actuals(rows)
    return {"inserted": inserted, "skipped_duplicates": skipped, "accepted_rows": len(rows)}


@app.post(
    "/imports/actual-revisions",
    tags=["imports"],
    summary="导入实测修订 CSV（计量复核）",
    description=(
        "按 场站+目标小时+修订号 提交修订：`action=set` 提交新电量（energy 必填），"
        "`action=withdraw` 撤回该小时实测（energy 留空，退出后续回测）。"
        "普通导入的实测为 0 号，修订号须 ≥1。每小时只应用更高修订号；"
        "已处理修订原样重投幂等；旧号首次送达、同号异内容整批拒收（409）；"
        "批内任何冲突整批回滚。"
    ),
    responses={
        409: {"description": "同号异内容 / 旧号首次送达，整批拒收"},
        422: {"description": "行格式错误（修订号非正整数、动作非法、set 缺电量、withdraw 带电量等）"},
        503: {"description": "数据库被其他写入占用，可重试"},
    },
)
def import_actual_revisions(
    csv_text: str = Body(
        media_type="text/csv",
        openapi_examples={
            "提交修订电量": {
                "summary": "set：把 00 时实测修订为 50.5",
                "description": (
                    "计量复核发现 00 时实测 48.2 有误，以 1 号修订改为 50.5。"
                    "之后生成的数据集该小时 energy=50.5、actual_revision=1；已生成的旧数据集保持原快照。"
                ),
                "value": REVISION_SET_CSV_EXAMPLE,
            },
            "撤回错误实测": {
                "summary": "withdraw：撤回 01 时实测",
                "description": (
                    "复核确认 01 时实测不可用，提交 1 号 withdraw 撤回（energy 列留空）。"
                    "该小时退出后续回测：新生成的数据集中 energy=null、missing_reason=no_actual、"
                    "actual_revision=1。"
                ),
                "value": REVISION_WITHDRAW_CSV_EXAMPLE,
            },
            "更高修订恢复实测": {
                "summary": "restore：撤回后以更高号 set 恢复",
                "description": (
                    "在 1 号 withdraw 之后提交 2 号 set：01 时实测恢复为 49.4，"
                    "新数据集中 energy=49.4、actual_revision=2。每小时永远只生效最高号修订。"
                ),
                "value": REVISION_RESTORE_CSV_EXAMPLE,
            },
            "迟到重投（幂等）": {
                "summary": "已处理修订原样重投 → 幂等跳过",
                "description": (
                    "把“提交修订电量”的 CSV 原样再发一遍：返回 200，计入 skipped_duplicates，状态不变。"
                    "若同一修订号换上不同内容（同号异内容），或投递不高于当前最大号的修订"
                    "（旧号首次送达），则整批 409 拒收，一行都不写入。"
                ),
                "value": REVISION_SET_CSV_EXAMPLE,
            },
        },
    ),
):
    rows = _parse_or_reject("实测修订", logic.parse_actual_revision_csv, csv_text)
    inserted, skipped = _import_actual_revisions(rows)
    return {"inserted": inserted, "skipped_duplicates": skipped, "accepted_rows": len(rows)}


# ---------------------------------------------------------------- 路由：数据集

@app.post(
    "/datasets/{dataset_id}/generate",
    tags=["datasets"],
    summary="生成回测样本数据集",
    description=(
        "在单个写事务内读取同一已提交快照完成配对并保存。"
        "同号同参数重试读回原结果（replayed=true）；同号异参数返回 409。"
        "失败整体回滚，不留半份结果。"
    ),
    responses={
        409: {"description": "数据集号已存在且参数不同"},
        422: {"description": "参数非法（未整点、区间倒置、区间过长等）"},
        503: {"description": "数据库被其他写入占用，可重试"},
    },
)
def generate_dataset(
    params: GenerateParams = Body(
        openapi_examples={
            "首次生成": {
                "summary": "首次生成（配合 /imports 的示例数据）",
                "description": (
                    "预期结果：00 时配对 500.0（550.0 为迟到预报被排除）；01 时配对 510.0；"
                    "02 时缺合格预报 no_forecast；03 时缺实测 no_actual。"
                ),
                "value": GENERATE_EXAMPLE,
            },
            "同号同参数重试": {
                "summary": "同号同参数重试 → 读回原结果",
                "description": (
                    "用同一 dataset_id 再次提交完全相同的参数：即使期间又导入了新数据，"
                    "也直接读回首次保存的结果，响应中 replayed=true。"
                ),
                "value": GENERATE_EXAMPLE,
            },
            "同号异参数冲突": {
                "summary": "同号异参数 → 409 冲突",
                "description": "同一 dataset_id 换用 lead_hours=12 等任何不同参数，返回 409 及已存参数。",
                "value": {**GENERATE_EXAMPLE, "lead_hours": 12},
            },
        },
    ),
    dataset_id: str = DATASET_ID_PATH,
):
    try:
        canon_params, start_dt, end_dt = logic.normalize_generate_params(
            params.station, params.start_hour, params.end_hour, params.lead_hours
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"message": str(exc)})

    conn = db.connect(app.state.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")

        existing = conn.execute(
            "SELECT params_json, result_json FROM datasets WHERE dataset_id = ?",
            (dataset_id,),
        ).fetchone()
        if existing is not None:
            stored_params = json.loads(existing["params_json"])
            if stored_params == canon_params:
                result = json.loads(existing["result_json"])
                _commit(conn)
                result["replayed"] = True
                return result
            conn.execute("ROLLBACK")
            raise HTTPException(status_code=409, detail={
                "message": "数据集号已存在且参数不同，未覆盖",
                "dataset_id": dataset_id,
                "existing_params": stored_params,
                "requested_params": canon_params,
            })

        rows, summary = logic.build_pairs(
            conn, canon_params["station"], start_dt, end_dt, canon_params["lead_hours"]
        )

        if logic.SNAPSHOT_TEST_HOOK is not None:  # 测试钩子：验证快照隔离，生产为空
            logic.SNAPSHOT_TEST_HOOK()

        created_at = logic.canon(datetime.now(timezone.utc))
        result = {
            "dataset_id": dataset_id,
            "created_at": created_at,
            "params": canon_params,
            "summary": summary,
            "rows": rows,
        }
        conn.execute(
            "INSERT INTO datasets (dataset_id, params_json, result_json, created_at) VALUES (?, ?, ?, ?)",
            (dataset_id, json.dumps(canon_params, sort_keys=True), json.dumps(result), created_at),
        )
        conn.executemany(
            "INSERT INTO dataset_rows (dataset_id, station, target_hour, cutoff_issued_at, "
            "issued_at, irradiance, energy, actual_revision, missing_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    dataset_id, r["station"], r["target_hour"], r["cutoff_issued_at"],
                    r["issued_at"], r["irradiance"], r["energy"], r["actual_revision"],
                    r["missing_reason"],
                )
                for r in rows
            ],
        )
        _commit(conn)
        result["replayed"] = False
        return result
    except HTTPException:
        raise
    except sqlite3.OperationalError as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        _locked_to_503(exc)
        raise
    finally:
        conn.close()


@app.get("/datasets", tags=["datasets"], summary="列出已保存的数据集")
def list_datasets():
    conn = db.connect(app.state.db_path)
    try:
        rows = conn.execute(
            "SELECT dataset_id, params_json, created_at FROM datasets ORDER BY created_at, dataset_id"
        ).fetchall()
    finally:
        conn.close()
    return [
        {"dataset_id": r["dataset_id"], "params": json.loads(r["params_json"]), "created_at": r["created_at"]}
        for r in rows
    ]


@app.get(
    "/datasets/{dataset_id}",
    tags=["datasets"],
    summary="读回数据集结果",
    responses={404: {"description": "数据集不存在"}},
)
def get_dataset(dataset_id: str):
    conn = db.connect(app.state.db_path)
    try:
        row = conn.execute(
            "SELECT result_json FROM datasets WHERE dataset_id = ?", (dataset_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail={"message": f"数据集 {dataset_id!r} 不存在"})
    return json.loads(row["result_json"])
