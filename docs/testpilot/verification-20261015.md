# 2026-10-15 企业 Adapter 验证

分支 `feat/testpilot-evidence-gate`；本地 Python 3.13.4、PG 17.11、Docker Desktop。
Runner 使用已存在的不可变 image ID；未把可变 tag 当准入身份。

## 实际结果

| 检查 | 结果 |
|---|---|
| `pytest tests/testpilot -q`，启用真实 PG/Docker | **118 passed，37.58s** |
| 最近 AgentRunner/Tool/Hook/RAG Skill/QA evaluator 兼容集 | **66 passed，0.48s** |
| `ruff check testpilot runner tests/testpilot scripts/testpilot_enterprise_smoke.py nanobot/config/schema.py` | 通过 |
| `basedpyright testpilot` | 0 errors / 0 warnings |
| `git diff --check` | 通过 |
| 真 nanobot API + 独立 Runner + 审核 Git commit pytest | 报告通过；脏工作树未执行 |
| 外部 Job 创建后 kill/restart Runner | 原 Job 对账恢复，外部数量 1 |
| 产物核对 CLI | 6 个登记产物读回/hash 验证通过 |
| 10 并发 API 任务 | 10 个报告通过，10 个唯一 Operation，每个外部 Job 数量 1 |
| 并发准入/总耗时 | 83.73 ms / 10.64 s |

10 并发为短时确定性 Provider + 真实 PG/Docker/HTTP，不是模型质量评测或生产容量保证。
OIDC/GitLab 测试使用同协议 MockTransport；OIDC JWT 使用真 RSA 签名；不是公司服务联调。

复现：

```bash
# 从本地私有环境加载 TESTPILOT_DATABASE_URL，不打印连接串
export TESTPILOT_RUNNER_IMAGE="$(docker image inspect --format '{{.Id}}' testpilot-runner:dev)"
.venv/bin/pytest tests/testpilot -q
.venv/bin/python scripts/testpilot_enterprise_smoke.py --load10
```

兼容集：`tests/agent/test_runner_core.py`、`test_runner_tool_execution.py`、
`test_runner_hooks.py`、`test_builtin_qa_knowledge_skill.py`、`tests/evals/test_qa_agent_evaluator.py`。
没有重跑或宣称既有全仓库失败已修复，旧基线限制仍见 [此前验证记录](verification.md)。

## 验证发现并修复

- 独立 Runner 短暂不可达不再立刻结束任务：保留固定 Operation/Job ID，有限查询恢复，禁止重发。
- 只有确认真实外部 Job 已创建后，重启测试才执行 kill，避免把“创建前未知窗口”伪装成可恢复成功。
- manifest planned case 改用 JUnit `classname::name`；集成测试检查 Evidence Gate，不能只看 pytest exit 0。
- 日志 artifact 上限 64 MiB，XML parser 仍独立限制 4 MiB；10 MiB 原日志不整段进入模型。
- metrics 未认证 401、普通 executor 403、reviewer 200，未降低权限让测试通过。

## 未执行的验收

本轮尝试启动少量真实模型 smoke，但进程在加载配置时即退出：`DEEPSEEK_API_KEY` 未设置。
没有模型请求、没有新增批量调用费用，也没有把历史模型 smoke 当作本轮通过结果。
当前代码缺口见 [交付审计](completion-audit.md)，仍未达到 TRD 最终完成定义。

GitHub workflow 增加独立 Runner 真进程 smoke，发布无密钥 `result.json`；
不会上传本轮生成的私有 settings、token 或原始 transcript。
