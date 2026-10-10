# ADR 007：任务控制以账本 fencing 为准，暂停不停止 Job

API 暂停在单个短事务更新状态/epoch、event/outbox/checkpoint；不依赖进程内 asyncio 状态作为事实源。
本进程取消模型 coroutine 只用于加速，跨副本旧 Worker 在 guard/checkpoint/dispatch 被 PG fencing 拒绝。
模型循环继续复用上游 AgentRunner Hook，不改 loop/runner。Job Scheduler 可领取 PAUSED 的既有 Job，
仅查询/记录外部事实，不能新派发 Job 或唤醒模型。恢复保留 Run 和原预算，先对账再继续。

控制用 state_version 防止过时 UI 应用操作；补充输入使用 Task/client_request_id 内容 hash 去重。
输入是 NOTE，只进入私有有界上下文，不作为 Evidence，不授予权限、不更改断言或 goal。
记录 projection receipt，不把模型尚未响应误说成信息已采纳。

所有模型/工具调用仍受原输入预算，暂停也不停止总 deadline 或审批 expiry。
继续采用固定 fixture 的执行合同；完整 provider/transcript 恢复与主动 WAITING_INPUT 不在本次范围。
