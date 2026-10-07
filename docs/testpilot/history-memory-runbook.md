# 复跑、历史报告与审核 Memory

本批追加迁移 `005_run_history_memory.sql`，既有迁移不变。启动前运行 `nanobot testpilot migrate`。
沿用 PG API、相同身份和 S3 配置；`/console` 增加执行历史、复跑、经验候选和审核入口。

## 显式复跑

`POST /v1/tasks/{id}/rerun`，executor 只能复跑自己的任务；携带 Idempotency-Key：

```json
{"expected_state_version": 12, "reason": "Check reproducibility of the observed failure"}
```

state_version 从最新任务快照读取。首次请求 202，同请求重放 200，payload/版本/状态冲突 409。
`requested_run_id` 始终标识此请求创建的 Run；Task 可能已前进，不能把重放当新执行。

- 原任务必须终态；未决 DISPATCHING/PENDING/UNKNOWN Operation 禁止复跑。
- 本版复跑是新 Run 的显式重新验证：目标/模式/范围一致，重新建立 Plan、Progress 和知识引用。
  新 Operation/Job 不复用旧结果；普通 tool 重复调用只回放当前 Run，不隐式复跑。
- 同时只有一个 active Run；并发相同 key 只创建一次。原轮次上限、已用轮次和执行 deadline 保留，
  不重置预算。不足时 409；扩大预算需新 Task，目前没有 budget amendment。
- 原策略要求审批时，新 Run 必须重新批准，旧 hash 不能复用；再次批准不延长已激活 Task deadline。
- 旧报告进入 run_results，PG 拒绝 UPDATE；首轮失败不会被之后的结果覆盖。
  新报告只计算当前 Run 的 4 个用例，不把复跑累计成 8 个。

`GET /v1/tasks/{id}/runs` 返回序号、active、状态与 verdict；
`GET /v1/tasks/{id}/runs/{run_id}/report` 返回历史报告，读前重新验证远端 hash；
`GET /v1/tasks/{id}/artifacts/{hash}?run_id={run_id}` 下载该历史报告登记的证据。
所有路径按 Task owner 和 tenant/project 授权，ID/cursor 不授予权限。

## 经验候选与审核

`POST /v1/tasks/{id}/memory` 由来源任务 owner/executor 提议：

```json
{"source_run_id": "run_<32 hex>", "case_id": "<report.findings 中已验证的失败 case>", "shared": false}
```

初始支持 CONFIRMED_FAILURE_PATTERN 的历史失败观察：value 由服务端从有效失败报告和 ExecutionRecord
派生，保留“根因尚未确认”。不接收任意 value、凭证、断言修改或自动诊断。无效/无失败来源返回 409。
同来源 case 重放候选，改变分享范围冲突；默认个人 scope，shared=true 明确申请审核后项目内共享。

`GET /v1/memory` 查看自己的记录和有效共享记录；`?review=true` 仅 reviewer 查看候选队列。
`POST /v1/memory/{id}/decision` 需要独立 reviewer 与当前版本：

```json
{"expected_version": 1, "decision": "CONFIRM"}
```

CONFIRM 前重查来源远端证据；REVOKE 记录新版本。申请人不能自行确认；版本冲突 409。
`GET /v1/memory/{id}/events` 查看 scoped 版本审计。状态为 CANDIDATE/CONFIRMED/REVOKED/EXPIRED。
默认 30 天 TTL，检索实时过滤时效；独立 Scheduler 追加 EXPIRED 版本/审计，无模型调用。

## Agent 如何使用

启用 Plan 的任务获得只读 search_memory，不能选择 tenant、用户、目标或 SQL。
查询匹配 tenant/project/user、commit/environment/suite，仅返回 CONFIRMED 且未过期记录，最多 8 条约 2000 tokens。
使用前再读来源产物，丢失/篡改来源不进入上下文；读前后重查状态/版本，记录私有 memory_read checkpoint。
历史观察带 current_execution_evidence=false，不代替当前 Job/JUnit。证据闸门只接受当前 ExecutionRecord。
没有启用上游共享文件 profile 自动写入。

未实现 TEAM_PREFERENCE/PROJECT_CONVENTION、语义检索、跨环境迁移、自动根因确认、多证据归纳或价值消融。
经验的业务贡献需后续同预算对照评测，本轮不能声称改善率。

## 验证

```bash
# 免费 scripted Provider；真实 nanobot / PG / JWT 审批 / RAG MCP / S3 / Docker
.venv/bin/python scripts/testpilot_governed_smoke.py --kb-project ../qa-kb-service --exercise-history-memory
.venv/bin/pytest tests/testpilot/test_history_memory.py -q
```

要求治理 Runbook 中的 PG/S3 环境和 RAG 索引已准备。Task 创建时设 24 轮上限（复跑不扩大），
完成正常/缺陷两任务，再复跑缺陷。summary.json 记录重新审批、旧报告保留、实际 Memory 读取、撤销和总轮次。
付费 --config 与批量参数均是手动选择，本轮没有新增真实模型调用。
