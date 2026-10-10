# 暂停、恢复和补充输入

运行 `nanobot testpilot migrate` 应用追加迁移 006，再启动 durable API/独立 Scheduler。
CLI、身份、对象存储配置沿用已有 Runbook。控制台 `/console` 增加暂停、恢复、提交输入及处理记录。

## 暂停与恢复

`POST /v1/tasks/{id}/pause` 和 `/resume`：

```json
{"expected_state_version": 12}
```

从最新任务快照取得 state_version。owner/executor 可操作，202 表示控制已持久化；状态/版本/预算/目标冲突 409。
重复控制旧版本不会再次应用，刷新后再决定操作。终态与 CANCELLING 不接受暂停。

暂停事务把 Task 置 PAUSED，清除租约并递增 epoch，追加 event/checkpoint。
原 Worker 不能保存新结果或启动新副作用，本进程额外取消对应模型 coroutine 加速生效。
已经获准的请求在其他副本可能结束，发起新工具前和真实 Job 派发前仍要通过 PG 租约验证。
这不是停止已发出的外部 Job，也不是把模型请求费用撤销。

已派发 Job 继续由独立 Scheduler 查询；即便 API 重启，结果可以入账。
结果入账后保持 PAUSED，模型轮次不增加；不会因 Job 成功自动继续 Agent。
恢复先对账未决操作，已有成功结果只回放。Run、目标、suite、已用轮次、审批与 deadline 不重置。
可用 Runner/目标快照改变时拒绝自动恢复。普通暂停不创建新 Run、不发起显式复跑。

取消仍会请求停止 Job；暂停不会。暂停期间总 Task/Job deadline 继续计时。
无未决 Job 的已过期任务由 Scheduler 转 NEEDS_REVIEW；有 Job 则沿用截止清理/UNKNOWN 规则。
审批等待暂停仍遵守审批 expiry；恢复不会绕过独立审批。

## 私有补充信息

`POST /v1/tasks/{id}/inputs`：

```json
{"client_request_id": "followup-1", "text": "Please inspect the retry assertions using the admitted scope"}
```

新输入 202，同 Task/Run 同 ID 同内容重放 200；改内容或跨 Run 复用该 ID 返回 409。
仅 owner/executor 能提交；owner 查询 `GET /inputs` 获取原文和 consumed_at。
普通事件只包含 ID/kind，不含原文；其他主体无法读取或提交。
每 Run 最多 8 条、每条 2000 字符，超过数量 429。额外目标、权限、预算字段 422。
终态/CANCELLING 不接收新消息；对既有输入重放不产生新行动。

在下一次获准模型调用前，Adapter 将新增输入按 ID 投影为 UNTRUSTED_USER_NOTE。
每个执行片段内不重复插入，恢复片段会重新读取该 Run 已登记的说明，标记不会重复产生。
全消息加工具 schema 必须通过实际模型 Context budget，过长说明不会通过裁掉工具协议强行送出。
consumed_at 表示“已加入获准请求的上下文”，不是模型一定采纳或一定已成功回应。
说明不能改变当前目标、授权、断言和账本；报告仍由当前执行证据验证。

目前没有模型主动 request_input 工具、WAITING_INPUT 状态或同轮强制重写已发出的请求。
企业 Git/CI Adapter、完整 OIDC、版本化完整消息恢复等仍是独立后续工作。

## 验证

```bash
.venv/bin/python scripts/testpilot_controls_smoke.py
.venv/bin/pytest tests/testpilot/test_task_controls.py -q
```

环境需 TESTPILOT_DATABASE_URL 和固定 Runner 镜像 testpilot-runner:dev。
smoke 使用免费 scripted Provider，实际启动/重启 nanobot API 和单独 Scheduler：
暂停→输入去重→API 重启→同 Job 对账且零新增模型调用→恢复同 Run→真实 3 PASS/1 FAIL。
输出到 `.local/testpilot-controls-smoke/<session>/summary.json`；结束停止临时进程并移除该测试 Job，保留账本与证据。
