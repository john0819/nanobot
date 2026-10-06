# TestPilot 验证记录

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
