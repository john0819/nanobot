# ADR 003：PG 账本 + 保留 Job 的固定目标恢复

状态：采纳。日期：2026-10-07。

原 API 的 task/key 只在内存，原 Runner `--rm` 且临时目录生命周期随进程结束，不能满足恢复。
新增明确 `--durable` 模式，不改变旧内存演示路径，也不把 JSON 文件包装成数据库事实源。

PG 保存 Task/Run、请求幂等、Operation/Attempt、模型主循环预算、事件/checkpoint/outbox；
使用短事务、复合外键与每次 worker 写入的 owner/epoch/lease 检查。原 AgentRunner 不改动；
用 fail-closed before-iteration guard 和 checkpoint callback 承接预算与持久化，工具发起另有独立账本校验。
结果先发布不可变 fsync/hash 产物，再登记 PG；PG 失败不发布可信证据。

此阶段直接使用 typed Psycopg async adapter 和校验和锁定的原生 SQL migration，避免为一个控制面增量
同时引入 ORM 与全套迁移依赖。它是 TRD SQLAlchemy/Alembic 选型的阶段性取舍；正式复杂 schema 升级时
评估迁移到 Alembic，保留 SQL 约束和测试，不能改写已发布的迁移。

使用固定 Docker name 作为此适配器的 operation 外部键，保留容器和独立输出目录。
恢复查询原 Job：已终态收集相同 JUnit/日志；不对已存在容器执行 start，不对未知/缺失 Job重新 run。
fencing 不能撤回已经在途的 Docker 请求，名字唯一和保守 UNKNOWN 处理补齐其边界。
可能发出的 Job 不存在时也不能证明取消完成：保留清理义务并 NEEDS_REVIEW。

只恢复目标与已验证外部事实，重建有限模型片段；旧 checkpoint 保留作审计，不强行重放未配对工具消息
或过期 Provider continuation。Task 级迭代预算不随新片段重置。模型配置全量冻结与 Plan/完整上下文恢复待补。
outbox 事务已落地，但尚无外部投递 relay；API 从 durable events 分页读取，不冒称 SSE 已实现。

当前约束：同一 Docker daemon、同一持久产物根目录、固定受审 suite、开发身份、单项目。
真实 SIGKILL 重启和 Docker 外部 Job 数量断言证明当前固定恢复路径，不代表通用 exactly-once 或完整企业可靠性。
后续依赖顺序：异步 Job Scheduler/等待释放 → 完整 Job/Artifact/Evidence schema 与 S3 → 剩余故障矩阵 → 企业身份与真实目标。
