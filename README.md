# 波段查缺 API（FastAPI + SQLite）

植被指数（如 NDVI）要求**同一景**的红光（red）与近红外（nir）在同一网格像元上
都有效。本服务接收各景各波段的 0/1 有效掩膜，维护每像元的已提交覆盖，并按日期与
网格矩形返回连续缺口，供数据员补报。

## 启动

### Docker Compose（推荐）

```bash
docker compose up -d --build
```

- 服务：<http://localhost:8000>
- Swagger 交互文档：<http://localhost:8000/docs>（可直接在页面上提交掩膜与查缺）
- OpenAPI：<http://localhost:8000/openapi.json>
- SQLite 数据持久化在命名卷 `gapcheck-data`（容器内 `/data/gapcheck.db`）

### 本地直接运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 数据库路径可用环境变量覆盖：
# GAPCHECK_DB_PATH=/tmp/gapcheck.db uvicorn app.main:app
```

## API

### `POST /masks` — 提交 / 更正某景某波段掩膜

请求体（JSON）：

| 字段 | 说明 |
| --- | --- |
| `date` | UTC 日期 `YYYY-MM-DD` |
| `scene_id` | 景号 |
| `origin_x` / `origin_y` | 景网格左上角在全局网格中的坐标 |
| `band` | `red`（红光）或 `nir`（近红外），两波段可分别提交、分别更正 |
| `mask` | 行优先二维 0/1 数组，宽、高各不超过 256 格，行长度必须一致；1=有效 |

规则：

- **同景的网格位置与尺寸固定**：首次提交注册几何；之后同景号/同日期再提交若
  原点或宽高不一致，返回 `409`，不写入任何数据。
- 更正只回读**本景**的两份源掩膜（待更正波段的旧掩膜 + 另一波段掩膜），且只
  重写**本景范围内、本景有效位发生变化**的格；其他景的贡献保存在每像元计数器
  中，原样保留，无需回读。掩膜内容完全没变的重复提交不写任何覆盖行。
- 整个提交在一个 SQLite 事务（`BEGIN IMMEDIATE`）内完成，任何失败都回滚，
  已提交结果仍可查询。

### `GET /gaps` — 按日期与矩形查缺

查询参数：`date`、`origin_x`、`origin_y`、`width`、`height`
（矩形宽、高各不超过 256 格）。

判定：任一景的红光与近红外在某格都有效（两景号集合有交集），该格即**齐全**。
其余格子按缺口类别，在每一行内把连续同类格子合并成段返回：

| category | 含义 |
| --- | --- |
| `red_only` | 仅红光有效，缺近红外 |
| `nir_only` | 仅近红外有效，缺红光 |
| `different_scenes` | 两波段都有，但分属不同景，没有同一景的双波段 |
| `both_missing` | 红光与近红外均缺报 |

返回含每类计数、齐全格数与逐行连续缺口段（`y`、`x_start`、`x_end`、`length`）。

### `GET /scenes/{date}/{scene_id}` — 回读某景几何与已提交波段信息

## 数据模型与关键保证

SQLite 三张表（见 `app/db.py`）：

- `scenes`：每景固定几何（日期、景号、原点、宽高）。
- `band_masks`：每景每波段一份源掩膜 BLOB（行优先 0/1），更正按波段覆盖。
- `coverage_v2`：每像元一行（`date,x,y` 主键，`WITHOUT ROWID`），载荷为三个
  小整数计数器，**不再保存景号列表**：
  - `red_cnt`：该格红光有效的景数；
  - `nir_cnt`：该格近红外有效的景数；
  - `both_cnt`：该格**本景红光与近红外都有效**的景数（即让该格“齐全”的景数）。

  行大小与重叠景数量完全无关：同一天叠再多景，每像元仍只有一行固定大小的
  计数记录。

对应需求的落点：

1. **查缺读取量只随像元数增长，不读景号列表或源掩膜** — `/gaps` 只查
   `coverage_v2` 表，单条带索引的范围扫描
   （`x BETWEEN ... AND y BETWEEN ...`，主键即 `(date,x,y)`），每像元至多一行、
   每行三个整数；无覆盖的像元按“两波段都缺”处理，不产生额外记录。WAL 模式下
   读不阻塞写，一条查询对应一个已提交快照。
2. **只有同一景双波段都有效才算齐全** — 齐全判定为 `both_cnt > 0`；红光与近红
   外分别由不同景提供时计入 `different_scenes`，响应字段与逐行分段规则不变。
3. **更正只更新本景有效位发生变化的格** — 提交时逐格对比本景更正前后的
   red / nir / 同景双有效三个位，位不变的格完全不写；有变化的格按增量
   （+1/-1）调整三个计数器，其他景的贡献不被读取也不被重写。三个计数都归零的
   格删除覆盖行。
4. **掩膜和覆盖一同生效，失败仍可查更正前结果** — 单事务提交（源掩膜 upsert
   与覆盖回写在同一 `BEGIN IMMEDIATE` 事务内），异常即回滚。
5. **沿用原 SQLite 卷，旧掩膜升级后可继续查询和更正，重启可直接查** — 启动时
   在单事务内把旧版 `coverage`（景号 JSON 数组）按“数组长度 / 两数组交集大小”
   就地换算成三个计数器并替换为 `coverage_v2`，`scenes` 与 `band_masks` 不动；
   版本记录在 `PRAGMA user_version`，升级幂等。重启时对已是新版的卷只做
   `CREATE TABLE IF NOT EXISTS`，无需迁移即可查询。
6. **尺寸限制** — 掩膜与查询矩形的宽、高均在 Pydantic / 查询校验中限制为
   1–256。

## 测试

```bash
python -m pytest tests/ -q
```

覆盖：四类缺口分类、行内连续段合并、同景双波段齐全、跨景不同景判定、多景任一
齐全、更正只影响本景且只写变化格、重复提交零写入、几何冲突 409、失败回滚后原
结果可查、256 上限、日期隔离、大量重叠景下覆盖行数仍等于像元数、最后一对同景
波段逐步失效、旧版 SQLite 卷就地升级后可查可更正、重启沿用原卷直接可查等
19 个用例。

另有独立端到端自测（真实拉起 uvicorn，使用临时数据库，不影响命名卷）：

```bash
python -I selftest/selftest_e2e.py
```

## 目录结构

```
app/
  config.py    # 环境配置与 256 上限
  db.py        # SQLite 连接、建表、旧卷升级、事务边界
  models.py    # 请求/响应模型与缺口类别
  services.py  # 掩膜提交更正、覆盖增量维护、查缺核心逻辑
  main.py      # FastAPI 路由（/docs）
tests/         # pytest 端到端用例
selftest/      # 独立 uvicorn 端到端自测脚本
Dockerfile
docker-compose.yml
requirements.txt
```
