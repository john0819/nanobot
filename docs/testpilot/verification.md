# TestPilot 验证记录

## 第四批：四个关联模块

日期：2026-10-07。交付 Async Runtime Yield、独立 Job Scheduler、持久 Progress Guard 与 SSE。

| 检查 | 实际结果 |
|---|---|
| TestPilot 全集（PG/Docker，无 skip） | **80 passed**，19.80 秒 |
| 最近上游 Runner/QA 兼容集 | **66 passed**，0.50 秒 |
| Ruff / strict BasedPyright | All checks passed / 0 errors |
| 原数据追加迁移 | 新增 002，001 字节/校验和保留，既有数据不重建 |
| 真实 Docker pause + 只有一个 Agent Worker | 首任务等待、租约为空、模型预算保持 1；第二任务可先完成 |
| 同轮调用协议 | PENDING 前所有 tool_call_id/result 已配对，tools_completed checkpoint 无遗失项 |
| Scheduler 取消 / deadline / heartbeat | 不发模型请求，paused Job 解除 pause 后停止并确认终态；不把等待当执行通过 |
| 轮询退避截止 | next_check_at/next_wakeup_at 不超出原 Job/Task deadline |
| Waiting API 重启 | 恢复同 external ID、Docker Job 数量=1 |
| Progress Guard | 连续重复/A-B 震荡；PG 跨 owner 保留；真实 Agent 停滞保留已执行统计并 NEEDS_REVIEW |
| SSE | id 重连续读、不重复；断线不取消；撤销已连接权限；future/过期 cursor 409 resync；心跳不占 seq |
| 原 SIGKILL smoke 在新状态机上重跑 | DISPATCHING 中断后同 Job 对账，外部数量=1，report validated、3 PASS / 1 FAIL |

此次新增 14 项测试（观测级 Guard 4 项、PG/Docker/SSE 合同 10 项），与原 66 项一起通过。
全部验证由脚本 Provider 控制 Runtime，真实 SQL/Docker/HTTP pytest 均实际执行；不表述为真实模型准确率。

### 真实 API + 独立 Scheduler 两进程验证

执行 `scripts/testpilot_async_smoke.py`：API 以 --no-scheduler --workers 1 运行，两条任务均进入外部等待，
这时还没有 Scheduler 进程；模型预算均为 1。SSE 断线没有取消任务。
另起 `nanobot testpilot scheduler` 后，按 seq=6 重連 SSE，收到不重复的后续事件和最终 report.validated/task.completed。
实测：

- `task_0d9064f62f744086a23d551b9fd5b491`：3 PASS / 1 FAIL，quality_verdict=FAIL。
- `task_f233225e9d10463abde9ac0254be9b5e`：4 PASS，quality_verdict=PASS。
- 每个 operation 在 Docker server 的 Job 数量 **1**；最终累计主循环模型轮数均 **3**。
- 两进程均已停止，自己的已提交 Job 已清理；PG 数据/本地报告保留。

结果位于 `.local/testpilot-async-smoke/session_*/summary.json`。
重跑 SIGKILL 的结果为 `task_f02f89f98b204a5cb704209ed0e4aa0c`，epoch 1→2，同一 external ID，累计 3 轮、无重复 Job。
心跳/权限撤销、窗口 resync 和暂停 Job 的等待测试在 pytest 中验证，不从两个 CLI smoke 推断这些保证。

### 当前边界

已补齐等待释放、独立 Scheduler 和 SSE 的首个可运行实现；没有声称整个 M2/M4/M5 已完成。
外部 outbox relay、S3/GC、企业 OIDC/审批、复杂 Plan/Context/Memory/RAG 工具治理、真实模型 held-out 评测仍在后续。
Progress Guard 是有界的工具观测指纹规则，不具备完整业务语义判断或反思重规划。
当前 fixture 范围和四轮主循环预算不变，不扩大为生成代码或任意企业系统执行。

## 第三批：PostgreSQL 账本、租约与原 Job 恢复

日期：2026-10-07。独立 `testpilot-ledger-dev` PostgreSQL 17.11 数据库，随机开发凭证只保存在
Git 忽略、0600 的本地环境文件；没有连接或改写 qa-kb-service 数据库。

| 检查 | 实际结果 |
|---|---|
| TestPilot 全集，实际 PG + Docker（无 skip） | **66 passed**，7.49 秒 |
| 最近 Runner/QA 兼容集 | **66 passed** |
| 扩展、测试、故障脚本 Ruff / strict BasedPyright | All checks passed / 0 errors |
| SQL migration 实际应用与重复调用 | 幂等成功，校验和锁定，复合 FK 拒绝错 run/task 关系 |
| request_dedup 与 quota 并发事务 | 5 个同请求只产生一个 task；不同内容 409；跨 actor/tenant 不能读取 |
| SKIP LOCKED / 过期租约 / CAS | 同 task 两个竞争 Worker 只一个领取；旧 epoch 禁止 intent/result/checkpoint/budget/finish |
| 任务模型主循环预算跨恢复 | 使用量保留，超过 4 轮被拒绝 |
| 丢 ACK / result 未提交 / 原 Job 已完成 | 原 Job 对账成功；external ID 相同；Docker server Job 数量=1 |
| 未知写、Docker Job 不存在 | 不创建新 Job；UNKNOWN；取消也不能伪报已清理 |
| 真实 Docker mode 与目标不同 | 拒绝采纳结果，UNKNOWN；没有“环境一致”的自报假证据 |
| API 重新启动 | 原 task/key/report/artifacts 可查询；篡改已发布证据后 report 409 |
| PG 不可用 | HTTP 准入 503；正常启动在 readiness 校验阶段失败，无内存降级 |
| wheel build | 包含 `testpilot/storage/001_ledger.sql` 和 PG adapter（WebUI build 跳过） |

本批 13 个新测试与前批 53 个测试合并。基础设施合同由脚本 Provider 驱动，Docker HTTP Gateway、
真实 pytest、JUnit、PG 事务均真实执行；本批没有新增真实 LLM 质量评测结果。
前批 DeepSeek smoke 记录保留，不能把此类 fault test 当成模型自主规划评测。

### 实际进程 kill/restart

运行 `scripts/testpilot_crash_smoke.py`，真实启动 `python -m nanobot testpilot serve --durable`，
观察到 Docker Job 后 SIGKILL，确认 PG operation 尚为 **DISPATCHING**，再启动另一 nanobot 服务。
实测结果：

- `task_id=task_8b380d97cb3d492cac387d301ac56578`
- `operation_id=op_3e2b23316e8c4c29a94f852b953601f6`
- `external_run_id=59e66fd687a360cabfe8563bfa8335382976756729477cf2f2db1bc1b456e7ec`
- 新旧报告指向同一 Docker Job；Docker server 按 operation label 统计 **1 个**，不是只数 Agent trace。
- lease_epoch **1 → 2**，累计主循环模型预算 **3/4**，重新请求原 key 返回原 task。
- 恢复后 report_validated=true、quality_verdict=FAIL，可信统计 **3 PASS / 1 FAIL / 0 SKIP / 0 not_run**。

完整本地 JSON 结果位于 `.local/testpilot-crash-smoke/session_*/summary.json`，对应前后启动日志同目录。
脚本关闭服务并清理自己的已提交 Job；PG 数据保留供检查。

### 范围与未完成项

已验证固定 suite 的安全恢复首个闭环，未完成整个 M2/F01–F05/F11–F13 矩阵。
恢复对齐原 target 和可信 operation 事实，重新建立 bounded fragment；未恢复完整 Plan/Provider continuation。
四轮计数约束主循环，Provider 内部有限重试/长度恢复的实际 API 请求数和 token/费用预算待 M4。
长 Job 当前占用 Worker 等待槽，尚未实现独立 Scheduler/Reconciler 和资源等待释放。
PG outbox 原子写入，但外部 relay/SSE 不在本批范围。
产物仍是同一持久文件目录，Docker Job 需同一 daemon；S3、多机、OIDC、GC 与真实企业 Adapter 待后续。

## 第二批：Task API、HTTP fixture、独立隔离 Runner

日期：2026-10-06。实际启动 Docker Desktop，并通过真实 `python -m nanobot testpilot serve`
进程接受任务，非仅在测试中调用 Wrapper。

| 检查 | 实际结果 |
|---|---|
| 新增/原 TestPilot 全集，设置实际 Runner image ID | **53 passed**（3.92 秒，无 skip） |
| 最近 Runner/QA 兼容集 | **66 passed** |
| 扩展/Runner/脚本/CLI 改动 Ruff | All checks passed |
| `basedpyright testpilot` | 0 errors / 0 warnings |
| `nanobot testpilot serve --help` | 新命令加载成功 |
| 实际 nanobot 进程，脚本 Provider + 容器 HTTP pytest | healthy 4 PASS；seeded defect 3 PASS / 1 FAIL；两报告均 validated |
| 实际 nanobot 进程，现有 **deepseek-flash** + 容器 HTTP pytest | healthy 4 PASS；seeded defect 3 PASS / 1 FAIL；两报告均 validated |
| HTTP 未认证、重复请求、报告/产物获取、任务所有权 | 401/幂等回放/限定读取/跨主体 404 验证通过 |
| 真实容器隔离探针 | UID 10001、只读 rootfs、零 CapEff、NoNewPrivs=1、无外网路由、无 Docker socket/模型 Key |
| 真实运行容器取消 | 移除后向 Docker 确认不存在；同 operation 后续重发被拒绝 |
| 并发取消 | 两请求等待同一次清理，完成前不提前报告 CANCELLED |
| Image/source hash 不匹配 | Runner 拒绝，无 JUnit，不可支持执行通过 |

真实模型此次只有正常/缺陷 **2 条 smoke 任务**。没有固定 held-out 模型集、多次采样或
规划效果统计，因此不能据此称自主规划准确率/生产稳定性已达到 TRD 目标。
此次也没有把 RAG 接进 Task API 的工具目录；已有 RAG 接入不变。

验证中遇到宿主 HTTP fixture 一次 30 秒超时；日志为空、没有 JUnit，已被闸门拒绝。
固定回环 HTTP 现显式使用 `ProxyHandler({})`，避免系统代理影响。
修正后最近 HTTP/Task API 14 项通过，全 TestPilot 53 项通过；原因属于修复推断，未对挂起进程做堆栈取证。

Runner 实际使用镜像 ID：
`sha256:77812e8b5f77a423f33c86549679c4886f11a6f95a412e108f56451a987b46a6`（本机架构）。
基础镜像多架构 digest 与依赖在 `runner/Dockerfile` 锁定；不同架构/build attestation 下 ID 可以不同，
执行时总是从本机构建结果提取不可变 ID，不硬编码此运行记录。

复验命令：

```bash
docker build -f runner/Dockerfile -t testpilot-runner:dev .
export TESTPILOT_RUNNER_IMAGE="$(docker image inspect --format '{{.Id}}' testpilot-runner:dev)"
.venv/bin/python -m pytest tests/testpilot -q
.venv/bin/python scripts/testpilot_live_smoke.py
# 真实模型所需环境变量已配置时：
.venv/bin/python scripts/testpilot_live_smoke.py --config .local/config.json --output .local/testpilot-live-model
```

默认脚本 Provider 的真实服务结果保存于 `.local/testpilot-live-smoke/summary.json`；
真实 DeepSeek 结果位于 `.local/testpilot-live-model/summary.json`。
两次服务都已停止，每个对应 Runner operation 均无残留容器。
服务为有界单进程开发部署，task/key 查询状态不跨进程保留；PG 恢复、OIDC 和企业目标仍未实现。

## 第一批：证据闸门与受信宿主 fixture

日期：2026-10-06。环境：macOS、Python 3.13.4；精确依赖见 `upstream.lock`。
使用脚本 Provider 和真实 pytest，未调用真实 LLM、企业 API 或在线 RAG。

## 已通过的检查

| 检查 | 实际结果 |
|---|---|
| `.venv/bin/python -m pytest tests/testpilot -q` | 40 passed |
| Runner core/tool execution/hooks + QA Skill + QA evaluator 最近兼容集 | 66 passed |
| `.venv/bin/ruff check testpilot tests/testpilot` | All checks passed |
| `.venv/bin/basedpyright testpilot` | 0 errors / 0 warnings |
| `.venv/bin/python -m pip check` | No broken requirements found |
| `git diff --check` | 通过 |
| Python wheel 构建（跳过 WebUI 构建，使用本机缓存 Hatchling） | `nanobot_ai-0.3.5-py3-none-any.whl`，含 13 个 testpilot 文件 |
| 从 wheel 解包目录导入并执行健康 fixture | exit 0；真实 4 PASS；report_validated=true |

兼容集命令：

```bash
.venv/bin/python -m pytest tests/agent/test_runner_core.py tests/agent/test_runner_tool_execution.py tests/agent/test_runner_hooks.py tests/agent/test_builtin_qa_knowledge_skill.py tests/evals/test_qa_agent_evaluator.py -q
```

测试覆盖：真实 AgentRunner 工具往返、固定 oracle 检出 seeded defect、同 Agent 重复调用回放、
执行前 source drift 拒绝、未执行却报告成功、错 tenant/project/commit/env/suite/run、错误引用、
重复声明、产物篡改、非终态/错误退出、nested JUnit、重复/未知 case、缺项/跳过、XML DTD、
大小限制、模型候选格式/大小、symlink 与路径逃逸。没有声称覆盖完整 TRD 故障矩阵。

## 真实演示结果

| 模式 | planned | PASS | FAIL | SKIP / not_run | 结论 | 闸门 | CLI exit |
|---|---:|---:|---:|---:|---|---|---:|
| healthy | 4 | 4 | 0 | 0 / 0 | PASS | 通过 | 0 |
| retry-write-bug | 4 | 3 | 1 | 0 / 0 | FAIL | 通过，发布有效失败报告 | 0 |
| retry-write-bug + claim-all-passed | 4 | 3 | 1 | 0 / 0 | FAIL | 拒绝；NEEDS_REVIEW | 2 |

写重试 defect 模式只改变被测实现的开关，`POST [503,200]` 的独立预期始终是 `[503]`。
失败不是伪造 pass rate，也没有由模型放宽断言。
模型假成功被阻断时仍显示真实 3 PASS / 1 FAIL，不能通过删除反证“修正”报告。

可重跑命令：

```bash
.venv/bin/python -m testpilot --mode healthy
.venv/bin/python -m testpilot --mode retry-write-bug
.venv/bin/python -m testpilot --mode retry-write-bug --claim-all-passed
```

CLI 输出实际报告位置；原始 JUnit、stdout、执行记录和模板报告保存在 `.local/testpilot/`。
这些数据是本地合同/执行验证，不是生产上线数据或真实 LLM 的准确率。

## 扩大验证的限制

扩大运行了 `tests/agent` + QA evaluator，实际 **1742 passed / 19 failed / 2 errors**
（51.32 秒），未全绿；当前尚不能声称全仓回归通过。

- `tests/agent/test_codex_context_pressure.py` 的三项失败：实际 compact 后输入估算
  `195598 > 190784`，触发已有保护。使用 `git archive HEAD` 解出 **未改动基线**，在同一环境
  单独跑该文件仍为 **3 failed / 12 passed**，已确认与此次 testpilot 增量无关。
- MCP reconnect 两项 setup error：当前执行沙箱禁止绑定本地 socket，报
  `PermissionError: Operation not permitted`。并未把未执行的测试记为通过。
- 其他既有 Agent 测试失败涉及 summary、ephemeral Hook、checkpoint 和 session/SDK 模型设置；本次未逐项归因，
  不能断言全部都是环境问题。未修改这些上游模块来掩盖失败。
- 全仓 `.venv/bin/basedpyright` 为 1576 errors，包含未安装的可选渠道/Provider SDK
  （Telegram、WeCom、WhatsApp、Azure、Bedrock、Langfuse 等）及后续 unknown type。
  本次独立 `testpilot` strict check 为 0 errors；未安装所有平台原生渠道依赖，也未降低 strict 级别。

GitHub 增加独立 `TestPilot evidence gate` workflow，运行新增测试、最近兼容集与三个演示，
上传执行产物。配置已写入；尚未推送，因此没有远端 Actions 成功记录。
原上游 workflow 保留。全仓基线问题需要另开增量调查，避免与当前 Evidence Gate PR 混合。
