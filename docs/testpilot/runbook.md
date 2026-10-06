# TestPilot 启动、验证与清理

## 本地 Task API

前置：Python 开发依赖包含 `.[api,dev]`，Docker daemon 在线。Runner 镜像只含受审固定 fixture。
Dockerfile 使用锁定 Python base digest 和精确 pytest 依赖；特定 CPU 架构产生各自的不可变 image ID。

```bash
docker build -f runner/Dockerfile -t testpilot-runner:dev .
export TESTPILOT_API_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(40))')"
testpilot_image=$(docker image inspect --format '{{.Id}}' testpilot-runner:dev)
.venv/bin/nanobot testpilot serve --runner-image "$testpilot_image" --port 8920
```

只监听 `127.0.0.1`。开发 Token 至少 32 字符，不写入仓库，不通过 URL query 传递。
加 `--config .local/config.json` 复用既有真实模型配置；相关 API Key 必须已在环境中。
配置启动时校验，每任务建立新 Provider/工具作用域。默认脚本 Provider 不调用付费模型。

提交、查询与报告：

```bash
curl -s http://127.0.0.1:8920/health/live
curl -s -H "Authorization: Bearer $TESTPILOT_API_TOKEN" \
  -H 'Idempotency-Key: gateway-smoke-1' -H 'Content-Type: application/json' \
  -d '{"mode":"retry-write-bug"}' http://127.0.0.1:8920/v1/tasks
# 用上一步返回的 task_id 替换下面的 task_xxx。
curl -s -H "Authorization: Bearer $TESTPILOT_API_TOKEN" http://127.0.0.1:8920/v1/tasks/task_xxx
curl -s -H "Authorization: Bearer $TESTPILOT_API_TOKEN" http://127.0.0.1:8920/v1/tasks/task_xxx/report
```

Task 输入只有固定 fixture mode。模型、请求方均不能填 tenant/principal、代码、URL、镜像或宿主挂载。
创建 202；同 key 同请求 200 回放；不同内容 409；未授权 401；不可见资源 404；契约错误 422；容量满 429。
报告未就绪返回 409。完整 [OpenAPI 3.1](openapi.json) 可在带认证的 `/openapi.json` 查询。

获取 `/v1/tasks/<task_id>/artifacts/<hash>` 时检查所有权、报告引用和内容 hash；不能读任意文件。
`POST /v1/tasks/<task_id>/cancel` 等待清理结束后返回 CANCELLED；清理无法确认时 NEEDS_REVIEW。
并发取消不会再中断同一个清理过程。取消/未决操作不自动再发；有意复跑要新建 task/key。

## 一键实际启动验证

```bash
.venv/bin/python scripts/testpilot_live_smoke.py
# 真实模型配置已解析所需环境变量时：
.venv/bin/python scripts/testpilot_live_smoke.py --config .local/config.json --output .local/testpilot-live-model
```

脚本调用真实 `python -m nanobot testpilot serve`，选择空闲回环端口、生成进程内 Token，
验证正常/缺陷两任务、401、幂等、报告与产物，并确认每 operation 无残留容器。
保存 `summary.json`、`nanobot.log` 与 tasks 下的 `operation.json/execution.json/report.json/report.md/artifacts/`。
脚本结束会 SIGTERM 服务并等待清理；不保留后台监听进程。

## 隔离边界

Agent 控制进程通过 Docker CLI 启动固定 job，容器里没有 Docker socket。
运行参数：UID 10001、根文件系统只读、capabilities 全移除、no-new-privileges、network none、
256MB、1 CPU、64 PIDs。仅 mount 当前 job 的空产物目录；tmpfs 32MB/noexec/nosuid。
Gateway、Upstream、pytest 都在该 job 内，HTTP 只用容器回环地址，不接企业网络。
Runner entrypoint 比对请求中的 suite_hash 与 image-owned source/oracle bytes，不一致就拒绝执行。
这种 Runner 只支持固定受审 suite，不能接受生成代码或企业凭证。开放这些能力前需审批/profile/出站治理。

## 停止与未决操作

前台服务 Ctrl-C 正常停止，活跃 job 会被取消并确认移除。runner deadline 60 秒，任务 deadline 90 秒。
清理错误或服务不可用会保守返回 NEEDS_REVIEW；不会假称容器已停止或重新执行同 operation。
可用状态中的 operation_id，或输出目录的 operation.json，定位 `testpilot.operation` label：

```bash
docker container ls -a --filter label=testpilot.operation=op_xxx
# 确认是自己的 TestPilot job 后，只清理该 job；不要全局 prune。
docker rm -f testpilot-op_xxx
```

当前任务 API 是单进程开发服务，重启会丢失任务查询和请求幂等映射；本地报告不等于持久任务事实源。
企业部署前必须实现 PG 账本/租约/outbox/恢复对账，不能直接将该进程扩成多副本。
