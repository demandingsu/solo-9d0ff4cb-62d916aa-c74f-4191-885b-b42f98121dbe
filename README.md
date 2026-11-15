# 光伏回测样本服务

把历史辐照预报与实测发电量配成回测样本的 API 服务（FastAPI + SQLite）。
核心保证：**事后发布（迟到）的预报不会进入样本**；生成结果来自同一个已提交快照；
同数据集号重试幂等；导入同键异值整批拒收；**实测修订按修订号生效，被撤回的错误实测退出后续回测**。

## 快速开始

```bash
docker compose up --build -d
curl http://localhost:8000/health
# 交互式文档（含迟到预报、修订撤回/恢复/迟到重投、重试、冲突示例，可直接在页面里执行）
open http://localhost:8000/docs
```

可选：再启动第二个共享同一 SQLite 数据卷的服务进程，验证多进程交错下的快照隔离：

```bash
docker compose --profile multi up -d   # api:8000 与 api2:8001 共用 /data/app.db
```

本地开发（不用 Docker）：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest                                  # 29 个端到端测试
uvicorn app.main:app --reload           # 数据库默认写到 ./data/app.db
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/imports/forecasts` | 导入预报 CSV（`text/csv` 请求体） |
| POST | `/imports/actuals` | 导入实测 CSV（视为 0 号修订） |
| POST | `/imports/actual-revisions` | 导入实测修订 CSV（提交新电量 / 撤回） |
| POST | `/datasets/{数据集号}/generate` | 生成回测样本并保存 |
| GET | `/datasets/{数据集号}` | 读回已保存结果 |
| GET | `/datasets` | 列出全部数据集 |
| GET | `/health` | 健康检查 |

## CSV 格式

预报（业务键 = 场站+目标小时+发布时刻）：

```csv
station,target_hour,issued_at,irradiance
ST01,2026-03-01T00:00:00Z,2026-02-27T08:00:00Z,500.0
ST01,2026-03-01T00:00:00Z,2026-02-28T06:00:00Z,550.0
```

实测（业务键 = 场站+目标小时）：

```csv
station,target_hour,energy
ST01,2026-03-01T00:00:00Z,48.2
```

- 时间按 UTC 解析，支持 `Z` 后缀、`+00:00`、空格分隔、裸时间（视为 UTC）；目标小时必须整点。
- 表头支持中英文别名（如 `场站,目标UTC小时,发布UTC时刻,预测辐照` / `电量`），可带 BOM。
- **同键同值**：幂等跳过（响应里的 `skipped_duplicates`）。
- **同键异值**（批内或与库中已有数据冲突）：**整批拒收**，返回 409 与冲突明细，一行都不写入。
- 行格式错误：整批拒收，返回 422 与逐行错误。

## 实测修订（计量复核）

数据员收到计量复核单后，用修订接口让错误实测退出后续回测。普通导入的实测视为 **0 号修订**，
修订 CSV 按 场站+目标小时+修订号（≥1）提交，`action` 为 `set`（提交新电量）或 `withdraw`（撤回）：

```csv
station,target_hour,revision,action,energy
ST01,2026-03-01T00:00:00Z,1,set,50.5
ST01,2026-03-01T01:00:00Z,1,withdraw,
```

- `set`：`energy` 必填；`withdraw`：`energy` 必须留空。动作也接受中文别名（`修正`/`撤回` 等）。
- **每小时只应用更高修订号**：撤回后提交更高号 `set` 即可恢复该小时实测。
- **已处理修订原样重投**：幂等跳过（`skipped_duplicates`），状态不变 —— 迟到重投安全。
- **旧号首次送达**（修订号 ≤ 该小时当前最大号且从未记录过）：整批拒收（409，`stale_revision`）。
- **同号异内容**（批内或与库中记录冲突）：整批拒收（409，`same_revision_different_content`）。
- **批内任何冲突整批回滚**：一行都不会写入。
- **普通实测导入（0 号）不得覆盖已修订的小时，也不得复活已撤回记录**：
  冲突整批拒收（409，`hour_already_revised`）；与已存 0 号记录同值的重投仍幂等跳过，且不影响修订生效值。
- 被撤回的小时退出后续回测：新生成的数据集中 `energy=null`、`missing_reason=no_actual`。
- 数据集每行带回 `actual_revision`：`0`=普通导入实测，`≥1`=生效修订号，`null`=无实测。
- 修订记录与当前生效实测在**同一个写事务**内生效；失败整体回滚，不留不一致状态。

## 配对规则与迟到预报

生成参数：`station`、`start_hour`、`end_hour`（均含）、`lead_hours`（提前小时数）。

对每个目标小时：

```
发布截止时刻 cutoff = target_hour - lead_hours
采用 issued_at <= cutoff 的预报中最新的一条；issued_at > cutoff 的即迟到预报，不参与配对
实测取当前生效值：普通导入（0 号）或被该小时最高号修订覆盖（set 换新电量 / withdraw 视为无实测）
```

结果每行带回 `cutoff_issued_at` 与实际采用的 `issued_at`（来源时刻）。
以上面的示例数据、`lead_hours=24` 为例：目标小时 `2026-03-01T00:00:00Z` 的截止时刻是
`2026-02-28T00:00:00Z`，`550.0` 那条发布于 `2026-02-28T06:00:00Z`，属于迟到预报，
配对结果采用 `500.0`。

缺配原因 `missing_reason`：`no_forecast`（截止前无已发布预报或全部迟到）、
`no_actual`（无实测）、`no_forecast+no_actual`、`null`（已配对），
另有中文字段 `missing_reason_cn`。

## 数据集号语义

- **同号同参数重试**：直接读回首次保存的结果（`replayed=true`），即使期间又导入了新数据。
- **同号异参数**：409，响应带回已存参数与请求参数，不覆盖。

## 一致性与原子性

- 导入与生成都在 `BEGIN IMMEDIATE` 写事务中执行：事务开始即持有写锁，
  其他进程/连接的写入只能等待（`busy_timeout`）或超时失败（映射为 503 可重试），
  因此生成读到的永远是**同一个已提交快照**，不会混入交错导入的半新数据。
- 任一环节失败即整体回滚：导入不会写半批，生成不会留下半份结果（有对应测试）。
- SQLite 开启 WAL，查询（GET）与写入互不阻塞。

## 端到端示例（curl）

```bash
# 1. 导入预报（含一条迟到预报）
curl -X POST http://localhost:8000/imports/forecasts \
  -H 'Content-Type: text/csv' --data-binary @- <<'CSV'
station,target_hour,issued_at,irradiance
ST01,2026-03-01T00:00:00Z,2026-02-27T08:00:00Z,500.0
ST01,2026-03-01T00:00:00Z,2026-02-28T06:00:00Z,550.0
ST01,2026-03-01T01:00:00Z,2026-02-28T00:30:00Z,510.0
ST01,2026-03-01T03:00:00Z,2026-02-27T09:00:00Z,520.0
CSV

# 2. 导入实测（03 时故意缺测）
curl -X POST http://localhost:8000/imports/actuals \
  -H 'Content-Type: text/csv' --data-binary @- <<'CSV'
station,target_hour,energy
ST01,2026-03-01T00:00:00Z,48.2
ST01,2026-03-01T01:00:00Z,49.1
ST01,2026-03-01T02:00:00Z,47.5
CSV

# 3. 生成：00/01 时配对，02 时 no_forecast，03 时 no_actual
curl -X POST http://localhost:8000/datasets/demo-20260301/generate \
  -H 'Content-Type: application/json' \
  -d '{"station":"ST01","start_hour":"2026-03-01T00:00:00Z","end_hour":"2026-03-01T03:00:00Z","lead_hours":24}'

# 4. 同号同参数重试 → 读回原结果（replayed=true）
curl -X POST http://localhost:8000/datasets/demo-20260301/generate \
  -H 'Content-Type: application/json' \
  -d '{"station":"ST01","start_hour":"2026-03-01T00:00:00Z","end_hour":"2026-03-01T03:00:00Z","lead_hours":24}'

# 5. 同号异参数 → 409 冲突
curl -X POST http://localhost:8000/datasets/demo-20260301/generate \
  -H 'Content-Type: application/json' \
  -d '{"station":"ST01","start_hour":"2026-03-01T00:00:00Z","end_hour":"2026-03-01T03:00:00Z","lead_hours":12}'

# 6. 计量复核：1 号修订把 00 时实测改为 50.5
curl -X POST http://localhost:8000/imports/actual-revisions \
  -H 'Content-Type: text/csv' --data-binary @- <<'CSV'
station,target_hour,revision,action,energy
ST01,2026-03-01T00:00:00Z,1,set,50.5
CSV

# 7. 撤回 01 时实测（该小时退出后续回测：新数据集 no_actual）
curl -X POST http://localhost:8000/imports/actual-revisions \
  -H 'Content-Type: text/csv' --data-binary @- <<'CSV'
station,target_hour,revision,action,energy
ST01,2026-03-01T01:00:00Z,1,withdraw,
CSV

# 8. 更高修订恢复 01 时实测为 49.4
curl -X POST http://localhost:8000/imports/actual-revisions \
  -H 'Content-Type: text/csv' --data-binary @- <<'CSV'
station,target_hour,revision,action,energy
ST01,2026-03-01T01:00:00Z,2,set,49.4
CSV

# 9. 迟到重投第 6 步的修订：原样重投幂等（skipped_duplicates=1）；
#    同号异内容或旧号首次送达则整批 409
curl -X POST http://localhost:8000/imports/actual-revisions \
  -H 'Content-Type: text/csv' --data-binary @- <<'CSV'
station,target_hour,revision,action,energy
ST01,2026-03-01T00:00:00Z,1,set,50.5
CSV

# 10. 新数据集按当前状态配对（00 时 50.5、01 时 49.4）；旧数据集仍是原快照
curl -X POST http://localhost:8000/datasets/after-review/generate \
  -H 'Content-Type: application/json' \
  -d '{"station":"ST01","start_hour":"2026-03-01T00:00:00Z","end_hour":"2026-03-01T03:00:00Z","lead_hours":24}'
```

## 项目结构

```
app/
  main.py    # FastAPI 路由、/docs 示例、事务边界（导入/修订/生成）
  logic.py   # 时间规范化、CSV 解析、修订解析、生效实测与配对逻辑
  db.py      # SQLite 连接（WAL、busy_timeout）、建表与老库迁移
tests/
  test_api.py  # 29 个端到端测试（含修订、快照隔离、失败回滚）
Dockerfile
docker-compose.yml
```
