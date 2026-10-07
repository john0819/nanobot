# PostgreSQL 持久执行、恢复与对账

## 启动

安装 `.[testpilot,dev]`，准备 Docker daemon、独立数据库与固定 Runner 镜像。
数据库与 qa-kb-service 完全分离，迁移只写 `testpilot` schema，不读取 RAG 业务数据。

```bash
export TESTPILOT_POSTGRES_PASSWORD="$(.venv/bin/python -c 'import secrets; print(secrets.token_hex(24))')"
docker compose -f testpilot-compose.yml up -d
export TESTPILOT_DATABASE_URL="postgresql://testpilot:$TESTPILOT_POSTGRES_PASSWORD@127.0.0.1:8943/testpilot"
.venv/bin/nanobot testpilot migrate
docker build -f runner/Dockerfile -t testpilot-runner:dev .
testpilot_image=$(docker image inspect --format '{{.Id}}' testpilot-runner:dev)
export TESTPILOT_API_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(40))')"
.venv/bin/nanobot testpilot serve --durable --runner-image "$testpilot_image" --output .local/testpilot-pg
```

随机开发凭证不提交到 Git；重启数据库必须继续使用首次创建 volume 时的密码，换环境变量不会修改 PG 账号密码。
服务仍只监听回环地址，Bearer 是开发身份，不是企业 OIDC。要使用真实模型可加既有 `--config`。
不传 `--durable` 保留旧内存演示模式；传入后 PG 不可用会阻断启动/准入/工具发起，不偷偷回退。

迁移是明确的控制命令，启动不自动执行 DDL。`001_ledger.sql` 与校验和保存于 migrations 表；
再次执行幂等，改写已应用文件会被拒绝。未来结构变化必须新增迁移和迁移器版本，不能直接编辑发布后的 001。

## 可验证的语义

- Create：请求去重、Task/Run、created event、outbox 同事务；202 后事实源在 PG。
- Claim：短事务 SKIP LOCKED，设置 owner/until，递增 epoch；事务外调用模型和工具。
- Intent：lease/CAS 校验后 operation DISPATCHING、attempt、dispatch event/checkpoint 同事务，然后发起 Docker。
- Result：先将产物以 hash 写入并 fsync，再提交 operation result、attempt、event/checkpoint；失败只会留下孤儿产物。
- Publish：报告目标/run 校验、报告和 report.validated/task.completed 事件同事务。读取报告时再次检查产物完整性。
- 恢复：领取过期租约，先回放/对账原 operation，再建立新的有限 AgentRunner 片段；不重新 start 已有 Docker 容器。
- 租约失效：旧 Worker 不能提交结果/报告、checkpoint、预算或新的操作。已在途副作用仍由原 Job 对账解决。

固定逻辑 operation 在同 run 下唯一；每次显式新 task/key 会产生新 run/operation。
内部 `run_id` 属于 Task Run，`external_run_id` 是 Docker 的真实 Job ID；二者在报告中分别展示。
Docker Job 使用稳定名称 `testpilot-<operation_id>`。名字唯一和 PG 操作账本共同保护此固定适配器；
Docker API 不是通用持久幂等服务，不能据此声称任意 CI/缺陷平台 exactly-once。

`PREPARED` 允许第一次 invoke；`DISPATCHING/UNKNOWN` 只能查询原 Job；缺失 Job/daemon 不可用/环境或 hash 不匹配
进入 UNKNOWN、NEEDS_REVIEW。即使目标参数完全相同也不能自动创建新 Job。
对账验证 Docker 实际 image、operation label、mode 和 suite hash，不能仅信任任务自报目标。

## 查询与取消

沿用 [Task API](openapi-postgres.json)。新增 `GET /health/ready` 与 scoped
`GET /v1/tasks/<task_id>/events?after=<event_seq>`，返回最多 100 条按序持久事件。
这是分页事件读取，不是 SSE；outbox 已原子写入，但外部 relay 尚未实现。
Checkpoint 含私有消息/Provider 数据，不通过公共事件接口展示。

取消首先持久化 CANCELLING，返回 202；Worker 停止匹配 Job，向 Docker 确认终态后才记录 CANCELLED。
可能发出但原 Job 不存在时，不能排除迟到创建，返回 NEEDS_REVIEW/CANCELLATION_UNCONFIRMED；
该义务留在操作账本中，不能伪称清理成功。迟到 unknown 不覆盖已有成功结果，取消后的任务不会重新 COMPLETED。

## 实际进程故障验证

```bash
export TESTPILOT_RUNNER_IMAGE="$testpilot_image"
.venv/bin/python -m pytest tests/testpilot -q
.venv/bin/python scripts/testpilot_crash_smoke.py
```

脚本启动真实 nanobot 持久服务，观察到真实 Docker Job 后 SIGKILL；重启到同一个 PG 和产物根目录，
等待新 epoch 领取原 task，校验同 external ID、Docker 端 Job 数量为 1、原请求重放与真实失败统计。
输出 `.local/testpilot-crash-smoke/session_*/summary.json` 和前后两段本地进程日志。
脚本用 3 秒租约加快确定性故障验证；正常服务默认 30 秒。脚本 Provider 验证 Runtime 合同，不是模型质量评测。

## 停止与清理

正常 SIGTERM 暂停 Worker、释放其有效租约，不取消已经发出的 Job；重新启动会对账继续。
SIGKILL 留下租约，到期后新 Worker 接管。恢复需同一产物目录、同一 Docker daemon 和原镜像/源码快照。
换目标或镜像不能静默复用旧结果，会 NEEDS_REVIEW；跨机器 S3/Runner 服务仍在后续阶段。

持久模式保留停止的容器作为 Job 查询记录，不采用 `--rm`；它们无运行 CPU，但需纳入后续 GC。
确认结果已持久化、没有 UNKNOWN/清理待办后，按明确的 operation 名删除该容器；不要全局 prune。
验证脚本与集成测试会清理自己已知的 Job。Compose `stop` 保留 PG volume；不要把 `down -v` 当例行清理。

## 当前范围

模型预算为跨恢复最多 4 个主循环迭代，执行片段启动前预留/计费，失败可能保守占用一轮。
Provider 内部有限重试/length recovery 的物理请求数、token/费用预算尚未纳入此计数；属于 M4。
任务截止按原创建时间计算 120 秒，恢复不重置；对账/取消有独立的有界控制等待。
恢复重建原目标和可信 operation 事实，保留原 checkpoint 供审计；尚未重放完整 Plan/Provider continuation 状态。
长 Job 当前仍占 Worker 等待槽；独立 Scheduler、外部等待释放、OIDC/S3、artifact/evidence 独立表、90 天 GC、
全部故障矩阵及企业 Git/CI Adapter 尚未完成，不能标记完整 M2/M3 达成。
