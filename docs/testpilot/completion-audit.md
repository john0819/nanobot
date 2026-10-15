# TRD 完整交付审计（2026-10-15）

**尚未实现/验收 TRD 全部内容。** 本文区分可运行代码、实际验证、剩余交付。
不能把确定性 Provider 的通过率当成模型自主性或公司团队验收。

## 本轮落地并验证

| 模块 | 已实现范围 | 验证 |
|---|---|---|
| Git source | 真实 commit、审核 blob hash/manifest、只读代码工具、工作树隔离、拒绝 symlink/逃逸 | Git 单测 + 真 Docker pytest + 报告闸门 |
| 独立 Runner | HTTP/CLI、PG 幂等、固定 Job、4 槽位、查询恢复、取消/原 deadline、hash 传输 | 并发同请求/冲突/鉴权、丢 ACK、kill/restart、外部 Job 数量=1 |
| OIDC 服务端 | discovery/JWKS TTL/轮换、刷新冷却、可选逐请求 introspection | 同协议 MockTransport + 真 RSA JWT/撤销；非公司 IdP 联调 |
| GitLab 只读 | 固定 project/commit/Job、diff 覆盖限制、CI SHA 校验、私有原文 | 同协议 MockTransport；非真实 GitLab 联调 |
| Checkpoint v2 | 完整消息/逐工具结果私有不可变产物、按 ID 恢复、拒绝 Runtime 漂移及未知写重放 | 工具轮中断恢复 + API/Job 重启 |
| 输入投影 | 按完整工具组移除、保留负面/部分事实及用户输入、预算保护 | 10 MiB 日志/hash、大历史、孤立/重复工具结果测试 |
| 观测 | PG 模型轮/工具耗时/token Trace，所有者 traces、审核角色 Prometheus gauge | API 鉴权及字段白名单 |
| 产物核查 | 同事务登记 JUnit/日志/transcript/知识/集成引用，CLI 读回 hash | 真进程核对 6 个产物；非备份/GC |

既有能力保留：持久 Task/Run/Operation/Attempt、租约 fencing、审批、证据闸门、S3、
RAG、Memory、暂停/恢复/私有输入、SSE 和控制台。

## 实际运行记录

- 最新 TestPilot 完整回归 127 passed（41.90 秒）；扩展上游兼容集含 provider 状态测试 78 passed，未声称全仓库测试通过。
- 真 nanobot CLI API + 独立 Runner，外部 Job 创建后 kill/restart，原 Job 恢复、报告通过，数量为 1。
- 10 并发：10 个报告通过、10 个唯一外部 Job；最近一轮准入 83.73 ms、全部完成 10.64 秒。
  这是本机短时 smoke，不是可用性 SLO/持续压测。
- 用户补充本地 `.env` 凭证后，已使用 DeepSeek `deepseek-flash` 实际验收健康/缺陷两条：
  4 PASS 与 3 PASS / 1 FAIL，均正确通过报告校验。真实持久 Runner 恢复结果见本轮验证记录。
  首次缺少环境变量的启动失败和后续失败尝试均保留，没有批量付费评测。
- 新批次已实现主动补问、单次无执行工具报告修复、不可变失败草稿/独立审核、用户/项目/全局准入配额。
  真实 DeepSeek 完成补问恢复→3 PASS / 1 FAIL→草稿独立批准，Job 数量=1；见 [业务闭环验证](business-workflow.md)。

## 仍需实现，不能打勾

1. 完整业务工具：受审核 CasePlan/测试代码生成、断言来源校验、prepare suite、request_id 日志查询，
   同 Run 多 suite/有限重试；当前只执行已审核 suite。
2. 缺陷外部提交及其幂等/对账；草稿与具体内容独立审核已实现。
3. 统一版本化工具目录、熔断及工具候选召回；主动补问/报告修复已实现，等待语义为 PAUSED + INPUT_REQUIRED。
4. task/tool/模型 token/费用总预算与细粒度资源配额；用户/项目/全局活动任务配额、模型轮数、deadline 和 Runner 槽位已实现。
5. 浏览器 OIDC PKCE/session、通用多项目路由及真实 IdP/GitLab 联调。
6. 独立 API/Worker/Runner/S3 的完整服务 Compose；当前 Compose 仍为 PG 开发配置。
7. 产物保留策略、并发安全 GC/孤儿扫描、数据库及对象存储备份恢复演练。
8. OTLP 导出和跨服务 span/告警；当前为 PG Trace/Prometheus 拉取接口。
9. 80 条分层 dev/held-out 业务样例、真实模型 evaluator/消融、人工报告审查。
10. F01–F20 全矩阵仍未全覆盖。动态工具召回/SubAgent 默认关闭，没有假称已证明收益。

## 故障测试索引（范围受限）

| TRD | 当前证据/限制 |
|---|---|
| F01 | HTTP timeout/大小限制、Job/Task deadline；完整只读重试/熔断待交付 |
| F02/F03 | `test_durable_recovery` 丢 ACK/UNKNOWN；`test_enterprise_adapters` 独立 Runner 外部数量核对 |
| F04/F05 | crash smoke、独立 Runner kill/restart、PG epoch；非所有精确崩溃窗口 |
| F06 | `test_transcript` 10 MiB 原日志/工具组预算；GitLab diff 明示覆盖不完整 |
| F07 | `test_progress`、`test_async_modules` 重复/震荡终止 |
| F08/F09 | `test_evidence`、`test_nanobot_integration`、错误 env/目标/hash/计数拒绝 |
| F10 | 既有容器取消/Scheduler deadline；独立 HTTP Runner 完整取消故障矩阵待补 |
| F11/F12 | 无 callback endpoint；上传与 PG 引用分离，但自动孤儿 GC 尚缺 |
| F13 | PG unavailable 503 与 dispatch 前 fence；真实断网/恢复窗口待扩 |
| F14/F15 | 审批 payload/suite hash、独立操作隔离；共享企业 fixture 锁未交付 |
| F16/F17 | Runtime/tool/prompt 指纹、固定 SHA、无模型 URL/凭证；动态 catalog 未交付 |
| F18 | 新 Run/旧失败保留/Memory 测试；同 Run 子 suite 重试待实现 |
| F19 | P2 SubAgent 关闭，无失联子任务验收 |
| F20 | SSE replay/disconnect/revocation/resync、OIDC introspection 单测；非真实 IdP SSE 联调 |

公司凭证、公司部署、持续可用性和实际团队审查不能用本地契约测试替代。
启动及边界见 [企业 Adapter Runbook](enterprise-runbook.md)。
