# 身份、审批、对象存储、Plan 与知识库

本增量通过 `nanobot testpilot serve --durable` 运行，沿用 PG 事实源和独立 Job Scheduler。
配置的 `testpilot` 节点见 [示例](enterprise-settings.example.json)，可用 `--settings` 单独传入，
`--config` 仍用于选择真实模型。不带 `--config` 使用确定性 Provider，不能把其结果当自主性评测。

## 启动与控制台

```bash
nanobot testpilot migrate
nanobot testpilot serve --durable --runner-image "$TESTPILOT_RUNNER_IMAGE" \
  --settings /path/to/private-settings.json --config /path/to/model-config.json
```

控制台：`http://127.0.0.1:8920/console`。提供任务创建/列表/状态、Plan、审批、SSE 时间线、取消、
验证报告和 hash 证据下载。Token 只保存在页面内存；所有数据接口独立鉴权。
审核人能读取待审批请求，但默认不能读取其他人的原始执行日志或知识内容。
CLI 当前仍绑定 loopback，企业接入需要 TLS ingress、网络准入与运维身份管理。

独立 Scheduler 必须使用相同 `--settings`（特别是 S3 配置）：

```bash
nanobot testpilot scheduler --runner-image "$TESTPILOT_RUNNER_IMAGE" --settings /path/to/private-settings.json
```

## 身份和审批

- JWT 固定 RS256、配置 issuer/audience/JWKS，校验 exp/iat/nbf/sub/tenant/jti。
  不访问 token 中的 jku/x5u，不接受模型/任务 JSON 指定主体。
- claims：`sub`、`tenant_id`、`projects`、`roles`（executor/reviewer/viewer/admin）。
  当前 ledger 固定 `novax-demo` tenant 和 `gateway-fixture` project；其他 scope 被拒绝。
- `development=false` 要求 JWT + S3；随机静态 Token 仅用于开发。
- 创建请求的 `require_approval=true` 或配置策略要求审批时，任务进入 WAITING_APPROVAL。
  此时不调用模型、不创建执行 Job。审批等待不消耗执行 deadline。
- 独立 reviewer 提交 APPROVE/DENY 和请求 hash；hash 绑定 Task/Run/目标/goal/mode/策略/Runner 约束。
  15 分钟过期；Scheduler 将未决请求转 NEEDS_REVIEW。派发时重新验证并在同一事务消费批准。
- `JWTIdentity.replace_keys` / `revoked` 提供运行内轮换/撤销能力；当前 CLI 从受信本地 JWKS 启动。
  OIDC discovery、自动 JWKS 刷新、跨副本撤销与登录重定向尚未实现，不能称为完整 OIDC 产品。

## S3 证据

对象键为 `tenant/project/task/sha256`。条件 PUT 防止覆盖；写后读取验证才登记证据。
每次报告/产物读取都验证远端 hash；远端故障返回失败，不使用本地缓存冒充远端事实。
本地缓存只是 Runner/JUnit 分析投影。生产权限仍需最小权限 bucket 凭据和加密/保留策略。
本轮真实存储使用 Garage v2.4.1；适配器为标准 boto3 S3 API，不依赖 Garage 管理接口。
PG 与对象存储不能跨库原子提交，上传成功而 PG 失败会留下未引用对象；自动 GC/retention 尚未实现。

## Plan、Context 与 RAG

非默认 goal 或要求知识的任务启用 propose_plan/read_artifact/get_case_result。
Plan 最多 8 步、3 次版本修改，只允许固定 action、最多一个 fixture Job，不能扩大权限或改断言。
ContextBlock 标记 trust/source/pinned/priority；保护任务事实，按 provider 实际窗口预留输出和安全余量，
全消息/tool schema 超预算时停止，不裁断 tool_call/result 协议。
知识观察最多约 2000 tokens，完整响应入对象存储；通过已登记 hash 分页读取。
尚未实现大任务历史压缩、检索贡献消融或通用工具候选路由。

RAG 使用 `qa-kb-service` 的认证 Streamable HTTP MCP，只暴露 `search`。
`knowledgeActorTokens` 显式映射任务 actor 到其 KB token，不接受模型传 token/ACL/URL。
tenant/actor 不匹配直接拒绝；返回内容始终不受信；来源 URL 只作引用，不被 Agent 自动请求。
要求知识的任务没有授权引用时不能派发 Job。知识证据保留 document/version/line/citation 和降级状态，
执行结论仍由真实 JUnit + 固定独立断言决定。
生产 OAuth token exchange/刷新尚未实现，配置映射适用于受控验收和显式服务端凭据管理。

## 可重复验收与批量评测

先准备 PG、S3 环境变量（从私有文件加载，不要提交），构建 Runner：

```bash
docker build -f runner/Dockerfile -t testpilot-runner:dev .
cd ../qa-kb-service
.venv/bin/alembic upgrade head
.venv/bin/qakb offline-sync --source mock-data/corpus --provider fake
cd ../nanobot
.venv/bin/python scripts/testpilot_governed_smoke.py --kb-project ../qa-kb-service
```

脚本启动临时本地 IdP **测试替身**（JWT 签名 + RFC7662 introspection）、真实 RAG MCP、nanobot API、
真实隔离 Runner；验证审批前零模型调用/零 Job、角色拒绝、独立批准、Plan、引用、JUnit 和远端证据。
RAG fake embedding/rerank 仅用于工程集成，不能代表真实检索质量。
设置文件 0600，结束删除其中的 S3/KB 密钥；汇总结果不包含令牌或私有 provider 推理。
测试语料 ACL 采用 `role:qa`，该脚本的 KB tenant-required 关闭；生产多租户需要按 RAG 文档配置 tenant-qualified groups。

少量真实模型验收（两任务）与可选批量命令：

```bash
.venv/bin/python scripts/testpilot_governed_smoke.py --kb-project ../qa-kb-service --config /path/to/model-config.json
# 手动运行；20 个任务，会产生真实模型费用，本轮没有运行
.venv/bin/python scripts/testpilot_governed_smoke.py --kb-project ../qa-kb-service --config /path/to/model-config.json --repeats 10
```

结果：`.local/testpilot-governed-smoke/<session>/summary.json`，含任务结果、实际轮数、引用数、耗时。
`--mode healthy` 可限制单场景。当前只覆盖两个固定业务场景；80 场景 held-out、消融、业务审查和 10 并发压测仍需建设。
