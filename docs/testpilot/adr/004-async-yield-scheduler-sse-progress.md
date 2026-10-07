# ADR 004：协议完整的宿主等待与独立 Job 调度

状态：采纳。日期：2026-10-07。

前批恢复可以保证同 Job，但在 Agent Worker 里轮询 60 秒，会占据模型执行槽。
本批拆成 submit/poll：工具返回 typed PENDING，先保存所有配对结果，after_iteration 才退出到宿主。
原 ToolRegistry 和工具执行层会捕获普通 Exception，故不在工具内部以异常承担唯一等待控制。
无需修改/复制 AgentRunner；适配层用实际 after_iteration 与 checkpoint callback 承接此边界。

Worker 原子登记 external_jobs/WAITING_EXTERNAL 后释放租约，Scheduler 独立领取 RECONCILING，
每轮只 query 原 Job，running 则带有界 backoff 再等待，终态才 result+requeue。
取消/截止使用独立控制预算，stop 已绑定的 Job 后确认终态；不再依赖 Agent 正在等待的 coroutine。
检查 Docker ID、image/label/mode/hash，external ID 不允许随恢复变化。所有状态推进仍需有效 epoch。

新 002 迁移只追加状态、调度字段、外部 Job 表和进展字段，不改写 001。
进程内 Scheduler 是默认便捷部署，独立 CLI 允许分开运行；二者共用同一套租约竞争和接口，不创建第二个事实源。

SSE 用 durable events 的 seq，而不是广播内存；反复授权、无 seq 心跳、明确 resync、断线不取消。
源事件已通过公共 payload 投影，checkpoint 原文不公开。外部 outbox relay 不在此批伪装为完成。
Progress Guard 只保存有界指纹，不引入第二个 Agent Loop；简单重复/A-B 停滞规则可复现验证，复杂自主规划后续再评测。

当前边界：固定受审 suite、local bearer、同 daemon/根目录；尚无 OIDC/S3、完整 Plan、知识工具治理和通用执行审批。
