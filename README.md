# 晶圆量测设备标定参数服务（wafer-calibration-service）

按**半开批次区间** `[start, end)` 管理量测设备的标定参数。每次发布生成设备级递增修订；
新区间替换上一修订的重叠部分，保留左右残段并合并内容相同的相邻段；所有历史修订不可变、可复算。

## 运行

```bash
# 启动 db + app + verify：构建镜像、跑构建检查与全部测试，verify 执行一次后按结果退出
docker compose up --build --exit-code-from verify

# 只启动服务（宿主机端口可配置，默认 8080）
APP_HOST_PORT=9090 docker compose up --build app

docker compose down -v   # 清理
```

`verify` 服务在 `app` 健康检查后启动，执行 `compileall` 构建检查 + `pytest`
（纯逻辑单元测试 + 针对运行中服务的 HTTP 集成测试），以测试退出码结束。
`app` 在数据库就绪、建表完成前不接收任何请求（lifespan 就绪门 + `/health` 健康检查）。

## API

### 发布标定 `POST /v1/devices/{device_id}/calibrations`

```json
{
  "operation_id": "op-2026-0001",
  "seen_revision": 3,
  "interval": { "start": 100, "end": 200 },
  "content": { "offset": 1.25, "gain": 0.98 }
}
```

- `201`：成功，返回 `{device_id, operation_id, revision, interval, content}`。
- `200`：同 `operation_id` 同参数重试，返回**首次结果**，不产生新修订。
- 错误（稳定 `error.code`）：`INVALID_INTERVAL`(400)、`INVALID_REQUEST`(400)、
  `OPERATION_CONFLICT`(409，同操作标识异参复用)、`STALE_REVISION`(409，所见修订过期)、
  `DATABASE_UNAVAILABLE`(503)。

### 查询生效内容 `GET /v1/devices/{device_id}/calibrations/effective?batch=150&revision=3`

`revision` 可省略（默认当前修订）。返回当时**唯一生效**的
`{revision, interval, content}`。错误：`DEVICE_NOT_FOUND`、`REVISION_NOT_FOUND`、
`BATCH_NOT_COVERED`（未覆盖批次，均为 404）。

### 其他

- `GET /health`：健康路径，数据库可达时 `200 {"status":"ok"}`。
- `GET /v1/devices/{device_id}`：设备当前修订号。

## 并发与一致性设计

- 发布在设备行上 `SELECT ... FOR UPDATE` 串行化：相同所见修订的并发发布只有一个成功，
  其余在锁等待后看到修订已前进而得到 `STALE_REVISION`。
- `(device_id, operation_id)` 唯一；请求指纹（设备/操作/所见修订/区间/内容）一致才重放，
  否则 `OPERATION_CONFLICT`。幂等检查先于修订检查，迟到的重试返回首次结果而非报错。
- 段行不可变：被取代的段 `to_rev = 新修订` 关闭，残段/新段 `from_rev = 新修订` 插入；
  修订 `r` 的生效图恒为 `from_rev <= r AND (to_rev IS NULL OR to_rev > r)`，历史永远可复算。
- 一次发布的全部写入在单个事务内完成，失败整体回滚，不会留下部分切分。

## 结构

```
app/domain.py    纯函数：区间替换、残段保留、相邻同内容合并
app/service.py   事务性发布/查询（乐观并发、幂等、生命周期段存储）
app/db.py        连接池、就绪等待、建表
app/main.py      FastAPI 路由、健康检查、稳定错误映射
tests/           单元测试（切分/合并）+ HTTP 集成测试（冒烟/历史/幂等/并发/原子性）
Dockerfile       runtime 与 verify 两个构建目标
docker-compose.yml  db(postgres:16) + app + verify
```
