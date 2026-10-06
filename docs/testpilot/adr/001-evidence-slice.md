# ADR 001：先形成 nanobot 证据发布纵切

状态：采纳。日期：2026-10-06。

## 问题

已有 RAG 可以支持规范问答，但不能证明测试已经执行，也不能防止模型用错误数字、旧目标
或“全部通过”的自由文本形成可信测试报告。TRD 很大，需要先交付可运行、可故障验证的增量。

## 决策

复用上游 AgentRunner，业务放独立 testpilot 包。首个增量实现不可变合同、固定 suite 执行、
内容寻址产物、JUnit 统计、声明校验和最终发布闸门。模型候选只允许四类声明：TEST_EXECUTED、
TEST_COUNTS、ALL_PASSED、CASE_FAILED；未知声明和任意叙述不能进入正式报告。

执行事实来自宿主 executor 的 ExecutionRecord，不接受模型提交 manifest/路径/目标。
目标绑定 tenant/project、fork 基线 SHA、工作树源码与断言 hash、环境快照与 run。
`source_revision_kind=WORKTREE_SNAPSHOT` 明确表示本地源码快照；不能拿它证明某个 PR commit。
Counters 由 parser 按 case 计算；unexpected/duplicate case、歧义 outcome、聚合数字冲突均拒绝。
缺失 case 记 not_run，任务降级；skipped 不纳入 executed，不可支持全部通过。

每实例只有一个 operation，重复调用回放相同 run；这是进程内去重，不是持久幂等或 exactly-once。
测试失败是被测行为 FAIL，不等于工具执行失败；报告可以有效地完成并记录 FAIL。
虚假声明阻断时保留真实失败统计，返回 NEEDS_REVIEW 部分报告，绝不修改 oracle。

## 执行与信任边界

固定 fixture 在隔离临时工作目录使用 `python -I -m pytest`，插件自动发现关闭，不继承模型密钥。
无模型生成代码、任意命令、用户目标 URL、宿主文件参数。仍然是宿主上的受信固定源码执行，
没有 OS sandbox/网络隔离，不能在此开放生成代码或企业凭证。
M1 完整企业纵切必须把它替换为独立隔离 Runner 服务、HTTP gateway fixture 和任务 API。

脚本 Provider 验证工具协议和 Runtime 合同，pytest/JUnit 则是真执行。
二者均不证明真实 LLM 自主规划效果；该评测在独立 frozen/held-out 任务集上完成。

## 后续

先补独立 Runner 合同/隔离和 Task API，再接 PostgreSQL operation/attempt/job、租约、checkpoint、
outbox 与 UNKNOWN 对账。之后把 qa-kb-service 接入 task-scoped 只读工具；RAG 引用可支撑规范，
不能替代执行证据。保留已有 RAG 接入，避免两个仓库同时无关改动。
