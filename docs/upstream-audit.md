# nanobot 上游审计（M0）

审计日期：2026-10-06。只使用用户提供的 checkout，没有更新上游。

## 版本与改动

- Fork：`john0819/nanobot`；开始开发时分支 `feat/qa-knowledge-mcp`。
- Fork 基线：`23f1a0844599ae8c11b33f2190caa6d63cb044ee`。
- 首个本地 QA 接入提交 `326b826f` 的父提交：`ecf6519ef1fc1f8af30d593ebbcc6f6b15a28c52`。
  本地没有 `upstream/main` remote-tracking ref，不能声称已确认与在线 main 的 merge-base。
- 已有五个 fork 提交包含 MCP preset/集成、DeepSeek RAG 接入、QA Skill、Agent 评估以及忽略面试文档。
- 上游 MIT 许可证、`LICENSE` 与 `THIRD_PARTY_NOTICES.md` 保留。
- 精确版本见根目录 `upstream.lock`。Python 3.13 实际环境版本快照见
  `requirements-testpilot-py313.lock`；这不是所有平台的解析锁，也未声称进行了干净环境重装验证。

## 当前签名与扩展点

| 实际源码 | 已有能力 | 本次使用/边界 |
|---|---|---|
| `nanobot/agent/runner.py` `AgentRunner.run(spec: AgentRunSpec)` | 真正的模型/工具循环、迭代限制、消息配对 | 由 `testpilot/nanobot_adapter.py` 唯一适配，不复制 Loop |
| `AgentRunSpec.runtime: LLMRuntime` | Provider/模型/生成设置的冻结快照 | `LLMRuntime.capture`，不使用过期的 `provider=` 入参 |
| `AgentRunSpec.tools: ToolRegistry` | 本次运行的工具定义与调用 | 每任务新建注册表，只有无参数固定 fixture 工具 |
| `AgentRunSpec.consolidate_history` | 上下文治理必须提供的回调 | 明确传 `retain_raw_history`；短片段仅返回 None，使用原始历史 fallback |
| `AgentRunSpec.checkpoint_callback` | 可接收执行边界 checkpoint | 尚未接持久库；不能用它声称已实现 crash recovery |
| `AgentHook`、`AgentHook(reraise=False)` | 运行与迭代/输出等生命周期 hook | 不能把默认观测 hook 当唯一发布闸门 |
| `AgentRunSpec.events: EventSink` | 事件 sink | 不是 durable outbox/SSE 重放 |
| `Tool.parameters/execute(**kwargs)`、`ToolRegistry` | Schema 校验、工具结果、并发 | Tool 自身也拒绝额外参数，防直接调用绕过 Schema |
| `nanobot/agent/tools/mcp.py` | MCP 生命周期、OAuth、远程工具 | 已有 RAG 继续复用；不重复实现检索 |

首次集成测试发现 `consolidate_history` 在实际 Runner 初始化时是必需项，虽然 dataclass
字段可为 None。这已在 adapter 明确处理，并由真实 Runner 合同测试覆盖。

## 发布边界与补丁

本次没有修改 `nanobot/agent/loop.py`、`runner.py`、Hook 或 ToolRegistry。
`AgentRunResult.final_content` 只作为私有报告候选输入；`run_task()` 返回的唯一结果是
`build_report()` 生成的可信结构化报告或 NEEDS_REVIEW 部分报告。
没有自由发送、任意文件写、shell 或 MCP 工具可绕过此任务目录的发布边界。

这只保证 **TestPilot adapter 路径**，并未给原 nanobot 通用聊天全局安装企业治理。
下一阶段引入 MCP/CI/子任务时，需要统一 dispatcher 和权限/operation 账本，逐条证明不可绕过。

## 验证入口

```bash
.venv/bin/python -m pytest tests/testpilot -q
.venv/bin/python -m pytest tests/agent/test_runner_core.py tests/agent/test_runner_tool_execution.py tests/agent/test_runner_hooks.py tests/agent/test_builtin_qa_knowledge_skill.py tests/evals/test_qa_agent_evaluator.py -q
.venv/bin/ruff check testpilot tests/testpilot
.venv/bin/basedpyright testpilot
```

实际结果与已有基线问题记录在 [验证记录](testpilot/verification.md)，不把设计目标写成实测效果。
