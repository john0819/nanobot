# 主动补问、报告修复与缺陷草稿

本批实现四项：主动补充输入、单次报告修复、失败草稿/独立审核、三层准入配额。
执行 `nanobot testpilot migrate` 应用 checksum 锁定的 009 迁移后再启动服务。

## 主动补充输入

持久任务的模型可调用 `request_input(question, reason)`，reason 为
`MISSING_CONTEXT / ASSERTION_SOURCE / ENVIRONMENT / KNOWLEDGE_ACCESS`。
该动作必须独占工具轮。完整调用/结果 checkpoint 保存后，宿主再暂停任务并释放租约。

数据库复用 `PAUSED` 状态，API 通过 `waiting_reason=INPUT_REQUIRED` 区别用户手动暂停；
`input_request` 返回给任务所有者，公共事件只含 reason，不含问题或回答原文。
这是等待输入的实现语义，没有另加一个数据库 WAITING_INPUT 状态。

1. 所有者通过原 `/v1/tasks/{id}/inputs` 提交新信息。
2. 使用最新 `state_version` 调用 `/resume`。缺少问题产生后的新输入时返回 409。
3. 恢复原 Run、预算、deadline 和目标；新信息以不可信用户说明加入上下文。

回答不能替换测试断言、扩大目标、增加权限或重置预算。等待仍消耗原始绝对 deadline。
暂停期间没有模型轮询；已在途 Job 的对账沿用现有 Scheduler。

## 单次报告修复

执行证据完整、模型正常结束，但候选 JSON/目标关联/声明校验失败时，允许修复一次。
`task_runs.report_repairs` 在 fenced PG 事务中原子预留，重启不能重新领取。

修复仍计入原任务模型轮数和 deadline，只提供校验缺口，不修改测试结果或放松 Evidence Gate。
修复运行不注册任何工具；私有 checkpoint 保存 repair 标记，恢复后也不能重新取得执行工具。
修复失败保留 NEEDS_REVIEW。执行缺失、目标/产物证据错误、provider 错误不触发这条修复路径。

## 缺陷草稿和内容审核

```http
POST /v1/tasks/{task_id}/defects
{"run_id":"run_...","case_id":"实际已验证失败ID"}

GET /v1/defects
GET /v1/defects?review=true

POST /v1/defects/{defect_id}/decision
{"expected_version":1,"content_hash":"草稿返回的SHA256","decision":"APPROVE"}
```

创建需要 executor，查看审核队列/决策需要 reviewer 或 admin。
服务端从不可变、已验证的 FAIL 报告派生内容，绑定 Run/target/report hash/证据/产物引用。
只记录“观察到用例失败”，不推断根因、不确认产品 Bug；调用方不能提交任意草稿正文。

同 Run/Case 只生成一个草稿；正文不可变，审核绑定 hash 和版本。
创建与批准前重新读回证据；自己不能批准自己的草稿，即使同时拥有 reviewer 角色。
审核变更和审计事件在同一事务中保存；并发/过期版本/篡改 hash 均拒绝。

控制台已增加生成草稿、本人列表、审核队列及批准/拒绝按钮。
**APPROVED 仅表示本地内容审核，不代表已提交 GitLab/Jira；外部提交和对账尚未实现。**

## 准入配额

配置项 `testpilot.maxActiveTasks / maxUserActiveTasks / maxGlobalActiveTasks` 默认分别为 10/10/1000。
分别约束当前 tenant/project、当前用户、共享 PG 中全部租户的活动任务数。
新建和复跑统一在全局/项目 advisory lock 下检查并写入，配额满时返回 429。
相同请求的幂等回放不再占用配额。PAUSED、待审批、外部等待和取消中任务也占用容量。
跨进程应使用相同部署配额策略；这里还不是 token/费用预算或全局 Docker 资源调度。

## 本次真实验证

```bash
# 私有环境先加载 TESTPILOT_DATABASE_URL 和 DEEPSEEK_API_KEY；只运行一条付费任务
.venv/bin/python scripts/testpilot_business_smoke.py --config .local/config.json
```

脚本启动真实 nanobot，使用临时 RSA/JWKS 身份，验收补问/拒绝无输入恢复/回答后续跑、
真实 pytest 3 PASS / 1 FAIL、失败草稿、拒绝自审及独立身份批准，外部 Job 数量为 1。
最终成功任务模型共 4 轮，首轮后等待输入；没有向外部平台提交缺陷。

真实验收发现 provider 的 governed 输入投影会重放旧工具结果，导致 DeepSeek 拒绝重复 call_id。
修复位于 provider 状态拥有者：只转发当前 raw delta 中尚未 pending 的工具结果；
补充 pending/已消费结果两种边界回归，没有更换 ID 或重新执行来绕过错误。

最终 TestPilot **127 passed / 41.90s**；扩展兼容集含 provider 状态测试 **78 passed / 0.53s**。
lint、TestPilot strict typecheck、控制台 JavaScript 语法检查通过。
源数据见 [真实模型业务链路结果](results/20261015-business-smoke.json)。
