"""端到端测试：导入、实测修订、配对、幂等、冲突、快照隔离、失败回滚。"""
import json
import os
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, logic  # noqa: E402
from app.main import (  # noqa: E402
    ACTUAL_CSV_EXAMPLE,
    FORECAST_CSV_EXAMPLE,
    GENERATE_EXAMPLE,
    REVISION_RESTORE_CSV_EXAMPLE,
    REVISION_SET_CSV_EXAMPLE,
    REVISION_WITHDRAW_CSV_EXAMPLE,
    app,
)

CSV_HEADERS = {"Content-Type": "text/csv"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test.db")
    monkeypatch.setenv("APP_DB_PATH", db_file)
    logic.SNAPSHOT_TEST_HOOK = None
    with TestClient(app) as client:
        yield client, db_file
    logic.SNAPSHOT_TEST_HOOK = None


def import_demo(client):
    r = client.post("/imports/forecasts", content=FORECAST_CSV_EXAMPLE, headers=CSV_HEADERS)
    assert r.status_code == 200
    r = client.post("/imports/actuals", content=ACTUAL_CSV_EXAMPLE, headers=CSV_HEADERS)
    assert r.status_code == 200


def table_counts(db_file):
    conn = db.connect(db_file)
    try:
        return {
            t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            for t in ("forecasts", "actuals", "actual_revisions", "datasets", "dataset_rows")
        }
    finally:
        conn.close()


def dataset_row(client, dataset_id, target_hour):
    body = client.get(f"/datasets/{dataset_id}").json()
    return [r for r in body["rows"] if r["target_hour"] == target_hour][0]


# ---------------------------------------------------------------- 导入

def test_import_and_idempotent_reimport(env):
    client, _ = env
    r = client.post("/imports/forecasts", content=FORECAST_CSV_EXAMPLE, headers=CSV_HEADERS)
    assert r.status_code == 200
    assert r.json() == {"inserted": 4, "skipped_duplicates": 0, "accepted_rows": 4}
    # 同键同值重导：幂等跳过
    r = client.post("/imports/forecasts", content=FORECAST_CSV_EXAMPLE, headers=CSV_HEADERS)
    assert r.status_code == 200
    assert r.json() == {"inserted": 0, "skipped_duplicates": 4, "accepted_rows": 4}


def test_import_conflict_against_db_rejects_whole_batch(env):
    client, db_file = env
    import_demo(client)
    batch = (
        "station,target_hour,issued_at,irradiance\n"
        "ST01,2026-03-01T00:00:00Z,2026-02-27T08:00:00Z,999.0\n"  # 同键异值
        "ST01,2026-03-01T05:00:00Z,2026-02-27T08:00:00Z,123.0\n"  # 正常行
    )
    r = client.post("/imports/forecasts", content=batch, headers=CSV_HEADERS)
    assert r.status_code == 409
    assert r.json()["detail"]["conflicts"][0]["db_value"] == 500.0
    # 整批拒收：正常行也没有写入
    assert table_counts(db_file)["forecasts"] == 4


def test_import_conflict_within_batch_rejected(env):
    client, db_file = env
    batch = (
        "station,target_hour,energy\n"
        "ST01,2026-03-01T00:00:00Z,48.2\n"
        "ST01,2026-03-01T00:00:00Z,48.9\n"
    )
    r = client.post("/imports/actuals", content=batch, headers=CSV_HEADERS)
    assert r.status_code == 409
    assert table_counts(db_file)["actuals"] == 0


def test_import_invalid_rows_rejected_with_details(env):
    client, db_file = env
    batch = (
        "station,target_hour,issued_at,irradiance\n"
        "ST01,2026-03-01T00:30:00Z,2026-02-27T08:00:00Z,500.0\n"  # 目标小时未整点
        "ST01,2026-03-01T01:00:00Z,not-a-time,510.0\n"            # 发布时间非法
        "ST01,2026-03-01T02:00:00Z,2026-02-27T08:00:00Z,abc\n"    # 辐照非数值
    )
    r = client.post("/imports/forecasts", content=batch, headers=CSV_HEADERS)
    assert r.status_code == 422
    assert len(r.json()["detail"]["errors"]) == 3
    assert table_counts(db_file)["forecasts"] == 0


def test_import_chinese_headers(env):
    client, db_file = env
    batch = "场站,目标UTC小时,发布UTC时刻,预测辐照\nST09,2026-03-01T00:00:00Z,2026-02-27T08:00:00Z,321.5\n"
    r = client.post("/imports/forecasts", content=batch, headers=CSV_HEADERS)
    assert r.status_code == 200
    assert r.json()["inserted"] == 1


# ---------------------------------------------------------------- 配对

def test_pairing_latest_before_cutoff_and_missing_reasons(env):
    client, _ = env
    import_demo(client)
    r = client.post("/datasets/demo/generate", json=GENERATE_EXAMPLE)
    assert r.status_code == 200
    body = r.json()
    assert body["replayed"] is False
    assert body["summary"] == {
        "total_hours": 4, "paired": 2,
        "missing_forecast": 1, "missing_actual": 1, "missing_both": 0,
    }
    rows = {row["target_hour"]: row for row in body["rows"]}

    # 00 时：550.0 发布于截止时刻之后（迟到），采用 500.0
    row0 = rows["2026-03-01T00:00:00Z"]
    assert row0["cutoff_issued_at"] == "2026-02-28T00:00:00Z"
    assert row0["issued_at"] == "2026-02-27T08:00:00Z"
    assert row0["irradiance"] == 500.0
    assert row0["energy"] == 48.2
    assert row0["missing_reason"] is None

    # 01 时：00:30 发布，恰在截止 01:00 之前
    assert rows["2026-03-01T01:00:00Z"]["irradiance"] == 510.0

    # 02 时：无预报
    row2 = rows["2026-03-01T02:00:00Z"]
    assert row2["missing_reason"] == "no_forecast"
    assert row2["issued_at"] is None and row2["energy"] == 47.5

    # 03 时：无实测
    row3 = rows["2026-03-01T03:00:00Z"]
    assert row3["missing_reason"] == "no_actual"
    assert row3["irradiance"] == 520.0 and row3["energy"] is None


def test_pairing_both_missing(env):
    client, _ = env
    import_demo(client)
    params = dict(GENERATE_EXAMPLE, start_hour="2026-03-01T04:00:00Z", end_hour="2026-03-01T04:00:00Z")
    r = client.post("/datasets/both-missing/generate", json=params)
    assert r.status_code == 200
    row = r.json()["rows"][0]
    assert row["missing_reason"] == "no_forecast+no_actual"


# ---------------------------------------------------------------- 数据集号语义

def test_retry_same_params_returns_original_result(env):
    client, _ = env
    import_demo(client)
    first = client.post("/datasets/ds1/generate", json=GENERATE_EXAMPLE).json()
    assert first["replayed"] is False

    # 生成后导入会改变配对结果的新数据
    extra = "station,target_hour,issued_at,irradiance\nST01,2026-03-01T02:00:00Z,2026-02-27T10:00:00Z,999.0\n"
    assert client.post("/imports/forecasts", content=extra, headers=CSV_HEADERS).status_code == 200

    # 同号同参数重试：读回原结果
    second = client.post("/datasets/ds1/generate", json=GENERATE_EXAMPLE).json()
    assert second["replayed"] is True
    assert second["rows"] == first["rows"]
    assert second["created_at"] == first["created_at"]
    row2 = [r for r in second["rows"] if r["target_hour"] == "2026-03-01T02:00:00Z"][0]
    assert row2["missing_reason"] == "no_forecast"  # 新导入的 999.0 不影响已保存结果


def test_same_id_different_params_conflict(env):
    client, _ = env
    import_demo(client)
    assert client.post("/datasets/ds1/generate", json=GENERATE_EXAMPLE).status_code == 200
    r = client.post("/datasets/ds1/generate", json=dict(GENERATE_EXAMPLE, lead_hours=12))
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["existing_params"]["lead_hours"] == 24
    assert detail["requested_params"]["lead_hours"] == 12


def test_get_dataset_and_list(env):
    client, _ = env
    import_demo(client)
    assert client.get("/datasets/nope").status_code == 404
    client.post("/datasets/ds1/generate", json=GENERATE_EXAMPLE)
    r = client.get("/datasets/ds1")
    assert r.status_code == 200
    assert r.json()["dataset_id"] == "ds1"
    listing = client.get("/datasets").json()
    assert [d["dataset_id"] for d in listing] == ["ds1"]


def test_generate_param_validation(env):
    client, _ = env
    bad = dict(GENERATE_EXAMPLE, start_hour="2026-03-02T00:00:00Z")  # 区间倒置
    assert client.post("/datasets/d1/generate", json=bad).status_code == 422
    bad = dict(GENERATE_EXAMPLE, start_hour="2026-03-01T00:30:00Z")  # 未整点
    assert client.post("/datasets/d2/generate", json=bad).status_code == 422
    bad = dict(GENERATE_EXAMPLE, lead_hours=-1)
    assert client.post("/datasets/d3/generate", json=bad).status_code == 422


# ---------------------------------------------------------------- 快照隔离与原子性

def test_generate_reads_single_committed_snapshot(env):
    """生成事务期间，另一连接的导入无法提交；结果不含交错数据。"""
    client, db_file = env
    import_demo(client)
    state = {}

    def hook():
        # 模拟另一进程：在生成事务持有写锁期间尝试导入
        other = db.connect(db_file)
        other.execute("PRAGMA busy_timeout=300")
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute(
                "INSERT INTO forecasts VALUES ('ST01', '2026-03-01T02:00:00Z', "
                "'2026-02-27T10:00:00Z', 999.0)"
            )
            other.execute("COMMIT")
            state["committed"] = True
        except sqlite3.OperationalError as exc:
            state["error"] = str(exc)
        finally:
            other.close()

    logic.SNAPSHOT_TEST_HOOK = hook
    r = client.post("/datasets/ds1/generate", json=GENERATE_EXAMPLE)
    assert r.status_code == 200
    body = r.json()

    # 交错导入被锁挡住，未提交；生成结果完全来自导入前的已提交快照
    assert state.get("committed") is not True
    assert "locked" in state.get("error", "").lower()
    row2 = [x for x in body["rows"] if x["target_hour"] == "2026-03-01T02:00:00Z"][0]
    assert row2["missing_reason"] == "no_forecast"

    # 生成结束后，该导入可以正常提交
    logic.SNAPSHOT_TEST_HOOK = None
    extra = "station,target_hour,issued_at,irradiance\nST01,2026-03-01T02:00:00Z,2026-02-27T10:00:00Z,999.0\n"
    assert client.post("/imports/forecasts", content=extra, headers=CSV_HEADERS).status_code == 200

    # 新数据集号反映新数据；旧数据集保持原样
    newer = client.post("/datasets/ds2/generate", json=GENERATE_EXAMPLE).json()
    row2_new = [x for x in newer["rows"] if x["target_hour"] == "2026-03-01T02:00:00Z"][0]
    assert row2_new["irradiance"] == 999.0
    old = client.get("/datasets/ds1").json()
    row2_old = [x for x in old["rows"] if x["target_hour"] == "2026-03-01T02:00:00Z"][0]
    assert row2_old["missing_reason"] == "no_forecast"


def test_failed_generate_leaves_no_partial_result(env, monkeypatch):
    """提交失败时整体回滚：数据集与行都不可见，已导入数据不受影响。"""
    client, db_file = env
    import_demo(client)

    import app.main as main_module

    real_commit = main_module._commit
    calls = {"n": 0}

    def flaky_commit(conn):
        if calls["n"] == 0:
            calls["n"] += 1
            raise sqlite3.OperationalError("simulated commit failure")
        return real_commit(conn)

    monkeypatch.setattr(main_module, "_commit", flaky_commit)
    # TestClient 默认会把服务端未处理异常重新抛出；服务端此时已整体回滚
    with pytest.raises(sqlite3.OperationalError, match="simulated commit failure"):
        client.post("/datasets/dsX/generate", json=GENERATE_EXAMPLE)
    monkeypatch.undo()

    assert client.get("/datasets/dsX").status_code == 404
    counts = table_counts(db_file)
    assert counts["datasets"] == 0
    assert counts["dataset_rows"] == 0
    assert counts["forecasts"] == 4 and counts["actuals"] == 3

    # 数据库仍可用：重试成功
    r = client.post("/datasets/dsX/generate", json=GENERATE_EXAMPLE)
    assert r.status_code == 200
    assert r.json()["replayed"] is False


# ---------------------------------------------------------------- 实测修订：生效与快照

def test_revision_set_applies_to_new_datasets_old_keeps_snapshot(env):
    """set 修订后：新数据集用修订电量，旧数据集保持原快照。"""
    client, _ = env
    import_demo(client)
    assert client.post("/datasets/before/generate", json=GENERATE_EXAMPLE).status_code == 200

    r = client.post("/imports/actual-revisions", content=REVISION_SET_CSV_EXAMPLE, headers=CSV_HEADERS)
    assert r.status_code == 200
    assert r.json() == {"inserted": 1, "skipped_duplicates": 0, "accepted_rows": 1}

    assert client.post("/datasets/after/generate", json=GENERATE_EXAMPLE).status_code == 200
    row = dataset_row(client, "after", "2026-03-01T00:00:00Z")
    assert row["energy"] == 50.5
    assert row["actual_revision"] == 1
    assert row["missing_reason"] is None
    # 未修订的小时仍是 0 号（普通导入）
    assert dataset_row(client, "after", "2026-03-01T01:00:00Z")["actual_revision"] == 0

    old = dataset_row(client, "before", "2026-03-01T00:00:00Z")
    assert old["energy"] == 48.2
    assert old["actual_revision"] == 0


def test_revision_withdraw_removes_actual_from_backtest(env):
    """withdraw 后该小时退出后续回测：新数据集 no_actual。"""
    client, _ = env
    import_demo(client)
    r = client.post("/imports/actual-revisions", content=REVISION_WITHDRAW_CSV_EXAMPLE, headers=CSV_HEADERS)
    assert r.status_code == 200

    assert client.post("/datasets/wd/generate", json=GENERATE_EXAMPLE).status_code == 200
    row = dataset_row(client, "wd", "2026-03-01T01:00:00Z")
    assert row["energy"] is None
    assert row["actual_revision"] == 1
    assert row["missing_reason"] == "no_actual"
    assert row["irradiance"] == 510.0  # 预报配对不受影响


def test_revision_restore_with_higher_revision(env):
    """撤回后提交更高号 set：实测恢复。"""
    client, _ = env
    import_demo(client)
    assert client.post(
        "/imports/actual-revisions", content=REVISION_WITHDRAW_CSV_EXAMPLE, headers=CSV_HEADERS
    ).status_code == 200
    r = client.post("/imports/actual-revisions", content=REVISION_RESTORE_CSV_EXAMPLE, headers=CSV_HEADERS)
    assert r.status_code == 200
    assert r.json()["inserted"] == 1

    assert client.post("/datasets/restore/generate", json=GENERATE_EXAMPLE).status_code == 200
    row = dataset_row(client, "restore", "2026-03-01T01:00:00Z")
    assert row["energy"] == 49.4
    assert row["actual_revision"] == 2
    assert row["missing_reason"] is None


def test_revision_multiple_revisions_same_hour_one_batch(env):
    """批内同一小时的多个修订按修订号升序应用，最终生效最高号。"""
    client, db_file = env
    import_demo(client)
    batch = (
        "station,target_hour,revision,action,energy\n"
        "ST01,2026-03-01T00:00:00Z,2,withdraw,\n"
        "ST01,2026-03-01T00:00:00Z,1,set,55.0\n"
    )
    r = client.post("/imports/actual-revisions", content=batch, headers=CSV_HEADERS)
    assert r.status_code == 200
    assert r.json()["inserted"] == 2
    assert table_counts(db_file)["actual_revisions"] == 2

    assert client.post("/datasets/multi/generate", json=GENERATE_EXAMPLE).status_code == 200
    row = dataset_row(client, "multi", "2026-03-01T00:00:00Z")
    assert row["energy"] is None and row["actual_revision"] == 2
    assert row["missing_reason"] == "no_actual"


# ---------------------------------------------------------------- 实测修订：幂等与拒收

def test_revision_idempotent_replay(env):
    """已处理修订原样重投：幂等跳过，状态不变。"""
    client, db_file = env
    import_demo(client)
    assert client.post(
        "/imports/actual-revisions", content=REVISION_SET_CSV_EXAMPLE, headers=CSV_HEADERS
    ).status_code == 200
    r = client.post("/imports/actual-revisions", content=REVISION_SET_CSV_EXAMPLE, headers=CSV_HEADERS)
    assert r.status_code == 200
    assert r.json() == {"inserted": 0, "skipped_duplicates": 1, "accepted_rows": 1}
    assert table_counts(db_file)["actual_revisions"] == 1

    assert client.post("/datasets/replay/generate", json=GENERATE_EXAMPLE).status_code == 200
    assert dataset_row(client, "replay", "2026-03-01T00:00:00Z")["energy"] == 50.5


def test_revision_same_number_different_content_rejected(env):
    """同号异内容：整批 409，状态不变。"""
    client, db_file = env
    import_demo(client)
    assert client.post(
        "/imports/actual-revisions", content=REVISION_SET_CSV_EXAMPLE, headers=CSV_HEADERS
    ).status_code == 200
    different = "station,target_hour,revision,action,energy\nST01,2026-03-01T00:00:00Z,1,set,99.9\n"
    r = client.post("/imports/actual-revisions", content=different, headers=CSV_HEADERS)
    assert r.status_code == 409
    conflict = r.json()["detail"]["conflicts"][0]
    assert conflict["reason"] == "same_revision_different_content"
    assert conflict["db"] == {"action": "set", "energy": 50.5}
    assert table_counts(db_file)["actual_revisions"] == 1

    # 同号 withdraw vs 已存 set 也算异内容
    wd = "station,target_hour,revision,action,energy\nST01,2026-03-01T00:00:00Z,1,withdraw,\n"
    assert client.post("/imports/actual-revisions", content=wd, headers=CSV_HEADERS).status_code == 409


def test_revision_stale_first_delivery_rejected(env):
    """旧号首次送达：整批 409。"""
    client, db_file = env
    import_demo(client)
    rev2 = "station,target_hour,revision,action,energy\nST01,2026-03-01T00:00:00Z,2,set,51.0\n"
    assert client.post("/imports/actual-revisions", content=rev2, headers=CSV_HEADERS).status_code == 200
    rev1 = "station,target_hour,revision,action,energy\nST01,2026-03-01T00:00:00Z,1,set,50.5\n"
    r = client.post("/imports/actual-revisions", content=rev1, headers=CSV_HEADERS)
    assert r.status_code == 409
    conflict = r.json()["detail"]["conflicts"][0]
    assert conflict["reason"] == "stale_revision"
    assert conflict["current_max_revision"] == 2
    assert table_counts(db_file)["actual_revisions"] == 1


def test_revision_batch_conflict_rolls_back_whole_batch(env):
    """批内任何冲突：整批回滚，有效行也不写入。"""
    client, db_file = env
    import_demo(client)
    assert client.post(
        "/imports/actual-revisions", content=REVISION_SET_CSV_EXAMPLE, headers=CSV_HEADERS
    ).status_code == 200
    batch = (
        "station,target_hour,revision,action,energy\n"
        "ST01,2026-03-01T02:00:00Z,1,set,55.5\n"   # 有效行
        "ST01,2026-03-01T00:00:00Z,1,set,99.9\n"   # 同号异内容 → 整批拒收
    )
    r = client.post("/imports/actual-revisions", content=batch, headers=CSV_HEADERS)
    assert r.status_code == 409
    assert table_counts(db_file)["actual_revisions"] == 1  # 只有最初的修订

    assert client.post("/datasets/rollback/generate", json=GENERATE_EXAMPLE).status_code == 200
    assert dataset_row(client, "rollback", "2026-03-01T02:00:00Z")["energy"] == 47.5


def test_revision_within_batch_same_number_different_content(env):
    """批内同号异内容：409，一行不写。"""
    client, db_file = env
    import_demo(client)
    batch = (
        "station,target_hour,revision,action,energy\n"
        "ST01,2026-03-01T00:00:00Z,1,set,50.5\n"
        "ST01,2026-03-01T00:00:00Z,1,set,50.6\n"
    )
    r = client.post("/imports/actual-revisions", content=batch, headers=CSV_HEADERS)
    assert r.status_code == 409
    assert table_counts(db_file)["actual_revisions"] == 0


def test_revision_parse_errors_rejected(env):
    """行格式错误：整批 422。"""
    client, db_file = env
    import_demo(client)
    bad = (
        "station,target_hour,revision,action,energy\n"
        "ST01,2026-03-01T00:00:00Z,0,set,50.5\n"          # 修订号 0（保留给普通导入）
        "ST01,2026-03-01T00:00:00Z,abc,set,50.5\n"        # 修订号非整数
        "ST01,2026-03-01T01:00:00Z,1,withdraw,49.0\n"     # 撤回行带电量
        "ST01,2026-03-01T02:00:00Z,1,set,\n"              # set 缺电量
        "ST01,2026-03-01T02:00:00Z,1,delete,\n"           # 未知动作
    )
    r = client.post("/imports/actual-revisions", content=bad, headers=CSV_HEADERS)
    assert r.status_code == 422
    assert len(r.json()["detail"]["errors"]) == 5
    assert table_counts(db_file)["actual_revisions"] == 0


def test_revision_chinese_headers_and_action_alias(env):
    """中文表头与中文动作别名。"""
    client, _ = env
    import_demo(client)
    batch = "场站,目标UTC小时,修订号,操作,电量\nST01,2026-03-01T00:00:00Z,1,撤回,\n"
    r = client.post("/imports/actual-revisions", content=batch, headers=CSV_HEADERS)
    assert r.status_code == 200
    assert r.json()["inserted"] == 1
    assert client.post("/datasets/cn/generate", json=GENERATE_EXAMPLE).status_code == 200
    assert dataset_row(client, "cn", "2026-03-01T00:00:00Z")["missing_reason"] == "no_actual"


# ---------------------------------------------------------------- 普通导入 vs 已修订小时

def test_plain_actual_import_cannot_overwrite_revised_hour(env):
    """普通实测导入不得覆盖已修订的小时：409，生效值不变。"""
    client, db_file = env
    import_demo(client)
    # 00 时：有 0 号记录 + 1 号修订；03 时：无 0 号记录，只有 1 号修订
    assert client.post(
        "/imports/actual-revisions", content=REVISION_SET_CSV_EXAMPLE, headers=CSV_HEADERS
    ).status_code == 200
    rev03 = "station,target_hour,revision,action,energy\nST01,2026-03-01T03:00:00Z,1,set,66.0\n"
    assert client.post("/imports/actual-revisions", content=rev03, headers=CSV_HEADERS).status_code == 200

    # 无 0 号记录的小时：普通导入即“旧号首次送达”，hour_already_revised
    plain = (
        "station,target_hour,energy\n"
        "ST01,2026-03-01T03:00:00Z,77.7\n"   # 已有 1 号修订 → 拒收
        "ST01,2026-03-01T05:00:00Z,10.0\n"   # 正常行（整批回滚，不写入）
    )
    r = client.post("/imports/actuals", content=plain, headers=CSV_HEADERS)
    assert r.status_code == 409
    conflict = r.json()["detail"]["conflicts"][0]
    assert conflict["reason"] == "hour_already_revised"
    assert conflict["current_revision"] == 1
    assert conflict["effective_action"] == "set"
    assert conflict["effective_energy"] == 66.0
    # 整批回滚：正常行也没写入
    assert table_counts(db_file)["actuals"] == 3

    # 有 0 号记录的小时：异值导入同样 409（同键异值）
    changed = "station,target_hour,energy\nST01,2026-03-01T00:00:00Z,77.7\n"
    r = client.post("/imports/actuals", content=changed, headers=CSV_HEADERS)
    assert r.status_code == 409
    assert r.json()["detail"]["conflicts"][0]["reason"] == "same_key_different_value"

    # 修订生效值不受影响
    assert client.post("/datasets/guard/generate", json=GENERATE_EXAMPLE).status_code == 200
    assert dataset_row(client, "guard", "2026-03-01T00:00:00Z")["energy"] == 50.5
    assert dataset_row(client, "guard", "2026-03-01T03:00:00Z")["energy"] == 66.0


def test_plain_actual_import_cannot_resurrect_withdrawn(env):
    """普通实测导入不得复活已撤回记录。"""
    client, _ = env
    import_demo(client)
    # 01 时（有 0 号记录 49.1）与 03 时（无 0 号记录）都撤回
    batch = (
        "station,target_hour,revision,action,energy\n"
        "ST01,2026-03-01T01:00:00Z,1,withdraw,\n"
        "ST01,2026-03-01T03:00:00Z,1,withdraw,\n"
    )
    assert client.post("/imports/actual-revisions", content=batch, headers=CSV_HEADERS).status_code == 200

    # 无 0 号记录的撤回小时：普通导入 409（复活即覆盖修订状态）
    plain03 = "station,target_hour,energy\nST01,2026-03-01T03:00:00Z,60.0\n"
    r = client.post("/imports/actuals", content=plain03, headers=CSV_HEADERS)
    assert r.status_code == 409
    assert r.json()["detail"]["conflicts"][0]["effective_action"] == "withdraw"

    # 有 0 号记录的撤回小时：同值重投幂等跳过，但绝不复活
    plain01 = "station,target_hour,energy\nST01,2026-03-01T01:00:00Z,49.1\n"
    r = client.post("/imports/actuals", content=plain01, headers=CSV_HEADERS)
    assert r.status_code == 200
    assert r.json()["skipped_duplicates"] == 1

    assert client.post("/datasets/still-wd/generate", json=GENERATE_EXAMPLE).status_code == 200
    row1 = dataset_row(client, "still-wd", "2026-03-01T01:00:00Z")
    row3 = dataset_row(client, "still-wd", "2026-03-01T03:00:00Z")
    assert row1["energy"] is None and row1["missing_reason"] == "no_actual"
    assert row3["energy"] is None and row3["missing_reason"] == "no_actual"


def test_plain_actual_reimport_same_base_value_idempotent_after_revision(env):
    """0 号记录仍在：同值重投幂等跳过，异值 409；修订生效值不受影响。"""
    client, _ = env
    import_demo(client)
    assert client.post(
        "/imports/actual-revisions", content=REVISION_SET_CSV_EXAMPLE, headers=CSV_HEADERS
    ).status_code == 200
    # 与 0 号记录同值的重投：幂等
    r = client.post("/imports/actuals", content=ACTUAL_CSV_EXAMPLE, headers=CSV_HEADERS)
    assert r.status_code == 200
    assert r.json() == {"inserted": 0, "skipped_duplicates": 3, "accepted_rows": 3}
    # 与 0 号记录异值：409
    changed = "station,target_hour,energy\nST01,2026-03-01T00:00:00Z,48.9\n"
    assert client.post("/imports/actuals", content=changed, headers=CSV_HEADERS).status_code == 409

    assert client.post("/datasets/idem/generate", json=GENERATE_EXAMPLE).status_code == 200
    assert dataset_row(client, "idem", "2026-03-01T00:00:00Z")["energy"] == 50.5


# ---------------------------------------------------------------- 修订的一致性与并发

def test_revision_commit_failure_leaves_no_inconsistent_state(env, monkeypatch):
    """修订导入提交失败：整体回滚，修订记录与生效实测都保持原状，可重试。"""
    client, db_file = env
    import_demo(client)

    import app.main as main_module

    real_commit = main_module._commit
    calls = {"n": 0}

    def flaky_commit(conn):
        if calls["n"] == 0:
            calls["n"] += 1
            raise sqlite3.OperationalError("simulated commit failure")
        return real_commit(conn)

    monkeypatch.setattr(main_module, "_commit", flaky_commit)
    with pytest.raises(sqlite3.OperationalError, match="simulated commit failure"):
        client.post("/imports/actual-revisions", content=REVISION_SET_CSV_EXAMPLE, headers=CSV_HEADERS)
    monkeypatch.undo()

    # 修订记录与当前生效实测均未改变
    assert table_counts(db_file)["actual_revisions"] == 0
    assert client.post("/datasets/pre-rev/generate", json=GENERATE_EXAMPLE).status_code == 200
    assert dataset_row(client, "pre-rev", "2026-03-01T00:00:00Z")["energy"] == 48.2

    # 重试成功
    r = client.post("/imports/actual-revisions", content=REVISION_SET_CSV_EXAMPLE, headers=CSV_HEADERS)
    assert r.status_code == 200
    assert table_counts(db_file)["actual_revisions"] == 1


def test_generate_snapshot_isolation_with_interleaved_revision(env):
    """生成事务期间另一连接的修订无法提交；结果来自修订前的已提交快照。"""
    client, db_file = env
    import_demo(client)
    state = {}

    def hook():
        other = db.connect(db_file)
        other.execute("PRAGMA busy_timeout=300")
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute(
                "INSERT INTO actual_revisions VALUES "
                "('ST01', '2026-03-01T00:00:00Z', 1, 'set', 50.5, '2026-03-02T00:00:00Z')"
            )
            other.execute("COMMIT")
            state["committed"] = True
        except sqlite3.OperationalError as exc:
            state["error"] = str(exc)
        finally:
            other.close()

    logic.SNAPSHOT_TEST_HOOK = hook
    r = client.post("/datasets/iso/generate", json=GENERATE_EXAMPLE)
    assert r.status_code == 200
    row0 = [x for x in r.json()["rows"] if x["target_hour"] == "2026-03-01T00:00:00Z"][0]
    # 交错修订被锁挡住：结果仍是修订前的 48.2
    assert state.get("committed") is not True
    assert "locked" in state.get("error", "").lower()
    assert row0["energy"] == 48.2
    assert row0["actual_revision"] == 0

    # 生成结束后修订可正常提交，并对新数据集生效；旧数据集不变
    logic.SNAPSHOT_TEST_HOOK = None
    assert client.post(
        "/imports/actual-revisions", content=REVISION_SET_CSV_EXAMPLE, headers=CSV_HEADERS
    ).status_code == 200
    assert client.post("/datasets/iso2/generate", json=GENERATE_EXAMPLE).status_code == 200
    assert dataset_row(client, "iso2", "2026-03-01T00:00:00Z")["energy"] == 50.5
    assert dataset_row(client, "iso", "2026-03-01T00:00:00Z")["energy"] == 48.2
