# ADR 002：固定目标 Task API 与独立容器 Runner

状态：采纳。日期：2026-10-06。

## 决策

从仅宿主固定 fixture 增量到实际 nanobot CLI 启动的 Task API，加每 job 独立容器 Runner。
继续复用 AgentRunner，CLI 只新增 `testpilot` 子命令注册。业务 API 不调用原 nanobot 通用工具目录，
模型只能调用固定 fixture 工具。Provider 复用 nanobot 既有配置工厂；API/Runner 依赖窄 FixtureExecutor 协议。

选择 aiohttp 作为 **开发 API**：已有 `.[api]` 依赖，不为当前固定纵切增加另一套服务依赖。
完整企业 TaskService 仍按 TRD 演进；其 Task/授权/Runner 协议保持独立。
没有用 SQLite 或文件账本替代 TRD 的 PG 事实源。当前 task/key 状态是有界进程内开发状态，
明确禁止共享部署/多副本或 crash recovery 声明。

Docker 权限只在本机控制进程；job 内无 socket、host shell 或长期 secret。
容器使用固定不可变 image ID 和 source hash、只读 rootfs、非 root、零 capabilities、无外网与资源限额。
被测 Gateway/Upstream 在 job 内通过真实 HTTP 交互，因此网络隔离不会阻断本地 oracle。
镜像 build context 采用专用 allowlist，仅送入固定源码和 runner entrypoint，不包含 .local/.env/项目密钥。

重复工具调用回放同 operation；派发未决则禁止自动再发。取消强制移除对应容器，再向 Docker server
确认不存在才返回 CANCELLED。并发取消和服务 shutdown 不重复 cancel 正在清理的 worker。
这是当前进程内的可靠边界；持久 Job identity/UNKNOWN 查询和进程 crash 后处理留给 M2。

## 取舍与后续

此增量可以真实启动 nanobot、真实模型选择工具、隔离执行和发布报告；开发 Token 不是 OIDC。
目标是 worktree snapshot，不声称已核验企业 PR 的 commit 内容。
后续先把 task/operation/job 状态迁到 PostgreSQL，完成 kill/ACK 丢失/fencing 故障矩阵，再接企业目标与 RAG。
只读 RAG 的上线事实与本次测试执行事实继续分开，不用知识引用证明 Job 成功。
