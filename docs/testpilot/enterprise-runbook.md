# Git / 独立 Runner / OIDC / Trace

当前支持审核过的 Python pytest suite；不自动执行模型生成代码。Task API 和 Runner
共用 PG 账本，分别使用不同输出根目录，通过 HTTP 传输并 hash 验证执行产物。
只有 Runner 进程需要 Docker 权限。API 中的 Worker 与 Scheduler 仍可分别运行。

## 启动与验收

沿用 [PG Runbook](postgres-runbook.md) 准备数据库、不可变 Runner 镜像 ID 和环境变量。
对现有数据库执行 `nanobot testpilot migrate`，迁移 checksum 不允许修改。

```bash
# 独立进程、真实 Git blob、Job 创建后 kill/restart Runner、产物核对；默认不产生模型费用
.venv/bin/python scripts/testpilot_enterprise_smoke.py
# 追加 10 并发真实任务验收；4 个持久 Runner 槽位，无付费模型
.venv/bin/python scripts/testpilot_enterprise_smoke.py --load10
```

脚本在 `.local/testpilot-enterprise-smoke/session_*/result.json` 留下无密钥结果，
私有配置权限 0600；退出时终止自己启动的进程并移除自己创建的临时执行 Job。
最后停止本轮使用的 PG/S3 容器，保留数据卷。不要停止无关容器。

审核 manifest 示例（SHA/hash 替换为实际 Git blob 的值）：

```json
{
  "commit_sha": "<40位真实Git提交SHA>",
  "files": {"tests/test_gateway.py": "<64位blob内容SHA256>"},
  "test_paths": ["tests/test_gateway.py"],
  "expected_cases": ["tests.test_gateway::test_write_no_retry"],
  "profile_version": "git-pytest-v1"
}
```

`expected_cases` 使用 JUnit `classname::name`，包含参数化 ID；与 JUnit 不符会被闸门拒绝。
manifest 仅声明人工/独立流程审核的文件，不代表全仓库和 diff 覆盖。
最多 64 个文件、每 blob 4 MiB、总导出 16 MiB、200 个 planned case。
只读取真实提交的 regular blob，拒绝 symlink/submodule、目录逃逸、hash 漂移。
工作树改动不影响执行。依赖须已存在于固定镜像，容器不联网安装。

nanobot 配置的 `testpilot` 部分（配置文件不得提交密钥）：

```json
{
  "tenantId": "novax-demo",
  "development": true,
  "gitRepository": "/operator/approved/repo",
  "gitManifest": "/operator/approved/manifest.json",
  "runnerEndpoint": "http://127.0.0.1:8930",
  "runnerServiceToken": "<随机至少32字符服务令牌>",
  "runnerMaxJobs": 4
}
```

```bash
nanobot testpilot runner --runner-image "$TESTPILOT_RUNNER_IMAGE" --settings /private/settings.json --output .local/runner
nanobot testpilot serve --durable --runner-image "$TESTPILOT_RUNNER_IMAGE" --settings /private/settings.json --output .local/api
# 可另运行无模型 Scheduler；API 使用 --no-scheduler
nanobot testpilot scheduler --runner-image "$TESTPILOT_RUNNER_IMAGE" --settings /private/settings.json --output .local/api
```

服务默认只绑定回环地址。远程生产部署需要 HTTPS 反向代理和独立服务身份；
`development=false` 拒绝非 HTTPS 和私网/回环出站，不能用开发开关当生产网络策略。
一个部署绑定一个 tenant 和当前 `gateway-fixture` 项目；尚未提供通用多项目路由。
所有实例须共享该部署的相同 manifest/config；更换后旧 Run 会拒绝快照不兼容。

## 身份和 GitLab

`oidcDiscovery=true`、`jwtIssuer`、`jwtAudience` 开启服务端 discovery/JWKS 验证，
与 `jwksFile` 固定密钥二选一。只接受当前 JWT 算法/claim 合同。
`oidcJwksTtl` 默认 300 秒，未知 key 刷新有 5 秒冷却。
配置 `oidcIntrospectionUrl/oidcClientId/oidcClientSecret` 后，每个鉴权请求重新检查 active、
身份和角色，失败关闭；未配 introspection 时撤销受 JWT 到期/JWKS TTL 限制。
尚未实现浏览器 authorization-code/PKCE 登录和 refresh token session。

`gitlabEndpoint/gitlabProjectId/gitlabToken/gitlabJobId` 可开启只读 GitLab 工具。
project/job 由操作员指定，模型不能指定 URL、token、项目或 Job。commit/Job 必须匹配准入 SHA。
diff 为一个有界 API 页，显式标记覆盖不完整；原响应为私有 hash 产物。
CI success 只是历史背景，不能直接变成本次测试 PASS。尚不触发/取消 GitLab pipeline。

## 恢复与观测

提交 HTTP 超时/丢 ACK 后只按 Operation 查询，不重新提交。Runner PG 去重、外部 Docker
唯一名称及 epoch CAS 共同避免重复执行。UNKNOWN 占用槽位，不假定它没有消耗外部资源。
独立 Runner 每次行动前重查 PG、取消和原始 60 秒 Job/120 秒 Task deadline。
Docker Job 创建前的不可判定崩溃不能凭空恢复成成功；保留 UNKNOWN，截止清理无法确认则人工处理。

Checkpoint v2 保存完整 message/tool_call/result、Runtime 指纹与私有 provider state。
恢复按 tool_call_id 补齐 read-only/显式幂等动作；其他未决写拒绝自动重放。
当前通过完整消息协议重建模型输入，不回放 provider opaque continuation。
超过预算只移除完整低优先级工具轮，保留负面/部分证据和用户输入；保护事实超预算则停止。
原始 checkpoint 始终是私有不可变内容寻址产物，不进入公共报告/下载接口。

`GET /v1/tasks/{id}/traces` 只允许任务所有者，记录模型轮/工具耗时和 token 数，
不保存提示词、参数、模型原文。`GET /metrics` 需要 reviewer/admin，输出当前部署作用域
的低基数 Prometheus gauge。当前还没有 OTLP 导出及跨服务完整 span。

```bash
nanobot testpilot verify-artifacts TASK_ID ACTOR --output .local/api --settings /private/settings.json
```

核对命令对登记的原文/JUnit/日志/知识/集成产物重新读回并验证 hash，缺失返回 exit 2；
S3 不可用不降级到本地缓存。用于恢复后的核查，**不是数据库备份命令**。
自动 GC、孤儿扫描和完整备份恢复演练尚未实现；当前保留原对象，不自动删除历史证据。
