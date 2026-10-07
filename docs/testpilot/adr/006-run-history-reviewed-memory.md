# ADR 006：新 Run 重新验证与独立审核的历史经验

Task 的预算和事件序号跨 Run；Run 的结果、Plan、知识和 Progress 不混合。
第一版显式 rerun 选择新 Run，目标不变，表达新的重新验证，不提供同 Run 多 suite/manifest 复跑。
终态存入不可变 run_results，迁移保留既有终态；未知 Operation 先对账，再允许新执行。
请求去重、任务锁、版本、配额、新 Run 与审批/事件处于同一 PG 事务，审批不重置预算或 deadline。

Memory 接收可信来源的失败观察，不接收自动根因。候选明确个人/共享范围，经独立 reviewer 确认后才能读。
引用 run/evidence，30 天 TTL，状态变更追加审计；使用前检查时效、权限和来源产物。
同 suite 可用于不同环境，因此 matching 同时包含 commit、环境快照和 suite，避免旧失败污染健康环境。
历史观察不能产生当前测试 VERIFIED 声明。

通过 Tool/RunControls/Adapter 扩展，不改上游 loop/runner，不共享跨任务 USER/MEMORY 文件。
固定项目下的真实 Docker/HTTP 验证证明工程隔离和协议正确，尚不证明企业适配或经验质量提升。
