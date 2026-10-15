# 2026-10-15 企业 Adapter 验证

分支 `feat/testpilot-evidence-gate`；本地 Python 3.13.4、PG 17.11、Docker Desktop。
Runner 使用已存在的不可变 image ID；未把可变 tag 当准入身份。

## 实际结果

| 检查 | 结果 |
|---|---|
| `pytest tests/testpilot -q`，启用真实 PG/Docker | **119 passed，36.88s** |
| 最近 AgentRunner/Tool/Hook/RAG Skill/QA evaluator 兼容集 | **66 passed，0.63s** |
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

## 用户补充凭证后的真实模型端到端

用户随后提供项目 `.env`，加载 `DEEPSEEK_API_KEY` 后实际连接
`https://api.deepseek.com`，模型为 `deepseek-flash`。凭证不进入参数、报告或 Git。

| 真实模型路径 | 实际结果 |
|---|---|
| nanobot → 工具 → 真实 HTTP gateway pytest → JUnit → Evidence Gate，健康 | 4 PASS，报告有效，quality PASS，2 模型轮 |
| 同链路，非幂等写错误重试缺陷 | 3 PASS / 1 FAIL，报告有效，quality FAIL，2 模型轮；未误报成功 |
| 持久 nanobot → 审核 Git commit → 独立 HTTP Runner，Job 创建后 kill/restart | 最终 1 PASS、报告有效、原 Job 数量 1、7 个产物 hash 核对通过 |

此次真实模型共 6 次小量任务尝试，4 次报告验收成功、2 次在定位期间被闸门拒绝；没有批量模型评测。
详细无密钥记录见 [真实模型验收 JSON](results/20261015-deepseek-smoke.json)。

- 一次模型错误终止只保留了校验缺口，原错误未持久化，无法事后证明具体 Provider 错误原因；
  已增加私有终止诊断和公共安全 stop reason，不把错误原文发布出去。
- 一次模型把 TEST_EXECUTED 值写成解释文字，闸门正确拒绝；补充 schema 和系统提示中的布尔类型要求。
- 同时补齐 provider 私有 continuation 反序列化、兼容检查及 pending 结果同步，
  新增确定性恢复测试。最终真实持久链路在这些修改后通过。
- 测试/执行 verdict 与报告有效性分别校验，不通过修改断言、忽略失败或放松闸门来得到 PASS。

这是少量 smoke，不证明所有随机模型输出都有效。RAG 不在本次真实模型链路范围内。
当前代码缺口仍见 [交付审计](completion-audit.md)，没有把这些结果当成 TRD 全量完成。

GitHub workflow 增加独立 Runner 真进程 smoke，发布无密钥 `result.json`；
不会上传本轮生成的私有 settings、token 或原始 transcript。
