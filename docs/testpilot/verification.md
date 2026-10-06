# TestPilot 验证记录

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
