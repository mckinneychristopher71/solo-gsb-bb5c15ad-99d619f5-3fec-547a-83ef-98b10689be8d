# 冷链温度合规审计 API

基于 **FastAPI + SQLite** 的冷链运输温度合规审计服务。客户端按批次提交带
ISO 8601 时间戳的温度读数与允许区间，服务端：

1. 按时间排序读数；
2. 用**滞回阈值（hysteresis）**识别连续超温事件，避免临界值附近抖动产生碎片；
3. 将超过采样间隔上限的**缺测**切成独立片段；
4. 返回时间轴上每段的起止时间、峰值、缺测标记与是否合规；
5. 可对报告做**封存（seal）**：按固定字段顺序与 UTC 格式生成 canonical JSON
   并固化 SHA-256 摘要，之后随时通过只读校验接口发现报告是否被改动。

默认零配置：SQLite 文件库、WAL 模式、启动自动建表，**无需任何第三方账号**。

---

## 目录结构

```
app/
  main.py       # FastAPI 入口、路由、启动初始化（lifespan 建表/迁移）
  audit.py      # 审计核心引擎：滞回状态机 + 缺测切片
  seal.py       # 报告封存：canonical JSON（固定字段顺序、UTC）与 SHA-256
  schemas.py    # 请求/响应模型与参数校验
  crud.py       # 批次写入、报告查询、封存与校验
  database.py   # sqlite3 连接、建表脚本、WAL/外键、封存列幂等迁移
  config.py     # 环境变量配置
tests/          # pytest 引擎与 API 测试
Dockerfile
docker-compose.yml
requirements.txt
```

## 审计规则

### 允许区间与滞回状态机

每批提交 `low_limit`、`high_limit`（°C）与 `hysteresis`（°C，默认 0.5）。
服务端维护三态状态机：`NORMAL` / `HIGH_EXCURSION` / `LOW_EXCURSION`。

| 当前状态 | 读数条件 | 新状态 |
| --- | --- | --- |
| 正常 | `t > high_limit` | 高温越限 |
| 正常 | `t < low_limit` | 低温越限 |
| 正常 | `low_limit ≤ t ≤ high_limit` | 正常（边界值算正常） |
| 高温越限 | `t ≤ high_limit - hysteresis` | 解除，回正常 |
| 低温越限 | `t ≥ low_limit + hysteresis` | 解除，回正常 |

滞回带的意义：温度在允许边界附近来回抖动时（例如上限 8°C，读数在
7.6 ↔ 8.4 之间），只有回落到 **7.5°C** 以下才解除事件，整段抖动仍归并为
**一个**连续超温片段，不会产生大量碎片。解除后同一读数可立即反向进入另一
越限态。

约束：`low_limit < high_limit`，且 `hysteresis < (high_limit - low_limit) / 2`。

### 缺测切片

读数按时间排序后，若相邻读数间隔**严格大于** `max_interval_seconds`
（默认 900 秒 / 15 分钟；等于上限不算缺测），则在两条读数之间切出一个
独立的 `missing` 片段：

- 起止时间为前后两条读数的时间戳；
- `sample_count = 0`，无峰值；
- 缺测两侧的越限事件**互不合并**，状态机在缺测后复位。

### 片段与合规判定

整个批次时间轴被完整切分为相邻片段，类型为：

- `normal` —— 区间内正常观测，`compliant = true`
- `high_excursion` —— 连续高温越限，`compliant = false`
- `low_excursion` —— 连续低温越限，`compliant = false`
- `missing` —— 采样间隔超限的缺测，`missing = true`、`compliant = false`

批次整体 `compliant` 当且仅当**所有片段**合规。

每个片段包含：`start_time` / `end_time` / `duration_seconds`、`sample_count`、
`peak_temperature_c` 与 `peak_at`（高温事件峰值为最高温、低温事件为最低温；
正常/缺测片段为 null）、`max_temperature_c` / `min_temperature_c`、
`missing`、`compliant`。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

- 服务地址：http://localhost:8000
- 交互式文档（Swagger UI）：http://localhost:8000/docs
- ReDoc：http://localhost:8000/redoc
- SQLite 数据持久化在命名卷 `coldchain-data`（容器内 `/data/coldchain.db`）

容器启动时由应用 lifespan 自动完成建库建表，可直接调用接口。

## 本地运行

需要 Python 3.11+。

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 数据库默认 /data/coldchain.db，可用环境变量改到有权限的目录：
COLDCHAIN_DB_PATH=./data/coldchain.db uvicorn app.main:app
```

## 接口

### `POST /batches` —— 提交批次并完成审计

请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `external_ref` | string，可选 | 业务批次号，唯一；重复提交返回 `409`。省略则自动生成 UUID |
| `low_limit` / `high_limit` | number | 允许温度区间（°C），边界值视为正常 |
| `hysteresis` | number，可选 | 滞回阈值，默认 `0.5` |
| `max_interval_seconds` | number，可选 | 采样间隔上限，默认 `900`；严格超过切缺测片段 |
| `readings` | 非空数组 | `{ "timestamp": ISO8601, "temperature": °C }`，顺序随意，服务端排序 |

时间戳建议带时区偏移（如 `+08:00`）；不带时区按 UTC 处理。响应统一输出 UTC
（`Z` 后缀）。

```bash
curl -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{
    "external_ref": "TRUCK-A-001",
    "low_limit": 2.0,
    "high_limit": 8.0,
    "hysteresis": 0.5,
    "max_interval_seconds": 900,
    "readings": [
      {"timestamp": "2026-09-22T08:00:00Z", "temperature": 5.0},
      {"timestamp": "2026-09-22T08:10:00Z", "temperature": 8.4},
      {"timestamp": "2026-09-22T08:20:00Z", "temperature": 7.6},
      {"timestamp": "2026-09-22T08:30:00Z", "temperature": 9.1},
      {"timestamp": "2026-09-22T09:30:00Z", "temperature": 5.0},
      {"timestamp": "2026-09-22T09:40:00Z", "temperature": 1.2}
    ]
  }'
```

上例的片段序列（7.6°C 高于解除线 7.5°C，不切断高温事件）：

```
normal → high_excursion(峰值 9.1°C，3 个样本) → missing(3600s) → normal → low_excursion(1.2°C)
```

### `GET /batches/{batch_ref}/report` —— 查询审计报告

`batch_ref` 可以是批次 `id` 或 `external_ref`；不存在返回 `404`。

```bash
curl http://localhost:8000/batches/TRUCK-A-001/report
```

### `POST /batches/{batch_ref}/seal` —— 封存报告摘要（幂等）

对整份审计报告（批次字段 + 全部片段）生成 **canonical JSON** 并计算
**SHA-256** 摘要，首次调用时把摘要与 `sealed_at`（UTC）固化到批次记录中。

canonical JSON 规则：

- 顶层与每个片段都使用**固定字段顺序**（即报告响应中的字段顺序，
  `segments` 排最后；片段为 `index, type, start_time, end_time,
  duration_seconds, sample_count, peak_temperature_c, peak_at,
  max_temperature_c, min_temperature_c, missing, compliant`）；
- 所有时间戳归一化为 **UTC、`Z` 后缀**的 ISO 8601；
- 紧凑序列化（无多余空白、`ensure_ascii=False`、不按键名排序）。

**重复调用完全幂等**：已封存的批次不会重算或覆盖，返回与首次相同的
`digest` / `sealed_digest` / `sealed_at`（响应状态码仍为 `200`）。
批次不存在返回 `404`。

```bash
curl -X POST http://localhost:8000/batches/TRUCK-A-001/seal
```

```json
{
  "id": "…",
  "external_ref": "TRUCK-A-001",
  "sealed": true,
  "sealed_at": "2026-09-22T09:40:00.123456Z",
  "digest": "5b747c6d…7c0d22ec",
  "sealed_digest": "5b747c6d…7c0d22ec"
}
```

### `GET /batches/{batch_ref}/verify` —— 校验封存摘要（只读）

按当前持久化数据**重新计算**报告摘要，与封存时固化的摘要比对：

| 字段 | 含义 |
| --- | --- |
| `digest` | 当前报告的 SHA-256 摘要 |
| `sealed_digest` | 封存时固化的摘要；批次尚未封存时为 `null` |
| `sealed_at` | 封存时间（UTC）；尚未封存时为 `null` |
| `valid` | `sealed_digest` 存在且与 `digest` 一致时为 `true`；不一致或未封存为 `false` |

校验接口**只读**：即使当前数据与封存摘要不一致（`valid=false`），也不会修改
任何审计数据或封存记录；此时再次 `POST …/seal` 仍返回最初固化的摘要。

```bash
curl http://localhost:8000/batches/TRUCK-A-001/verify
```

### `GET /batches` —— 批次列表（摘要）

查询参数：`compliant=true|false`（可选过滤）、`limit`（1–200，默认 50）、
`offset`（默认 0）。

### `GET /health` —— 健康检查

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `COLDCHAIN_DB_PATH` | `/data/coldchain.db` | SQLite 文件路径，目录自动创建 |
| `COLDCHAIN_DEFAULT_HYSTERESIS` | `0.5` | 请求未指定时的默认滞回阈值（°C） |
| `COLDCHAIN_DEFAULT_MAX_INTERVAL` | `900` | 请求未指定时的默认采样间隔上限（秒） |
| `COLDCHAIN_MAX_READINGS` | `100000` | 单批读数上限 |

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

测试覆盖：排序、滞回不分裂抖动事件、边界严格比较、缺测独立切片
（含“等于上限不算缺测”）、高/低温峰值、单读数片段、API 提交/报告/重复 409/
404/422/列表过滤与时区归一化；封存侧覆盖 canonical JSON 的固定字段顺序、
UTC 归一、摘要随数据变化、封存幂等（重复调用 sealed_at/digest 不变）、
未封存与篡改后 `valid=false` 且校验不回写数据、未知批次 404。

## 设计说明

- **写时审计**：提交时一次性计算并持久化片段，报告查询为纯读取，结果可追溯。
- **封存只读校验**：`sealed_at`/`sealed_digest` 仅在首次 `seal` 时写入
  （`UPDATE … WHERE sealed_digest IS NULL`，并发下也不会重复固化）；`verify`
  只做读取与重算，摘要不一致时返回 `valid=false` 但绝不改动审计数据。
- **版本兼容**：启动 lifespan 自动幂等建表，并为旧版本库 `ALTER TABLE`
  补齐 `sealed_at` / `sealed_digest` 两列（旧批次默认未封存）。
- **SQLite**：标准库 `sqlite3`，开启 WAL（并发读不阻塞写）与外键级联删除；
  批次、读数、片段三张表并建立索引。
- **纯函数引擎**：`app/audit.py` 不依赖 Web/DB 框架，输入排序后的读数，
  输出片段列表，便于独立测试与复用。
- **不做读数去重**：相同时间戳按提交顺序稳定排列、全部保留。
