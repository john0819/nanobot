# ADR 005：服务端身份、一次审批与远端证据

新增能力继续放在独立 `testpilot`，不改 nanobot loop/runner。共享任务模式要求 PG、JWT 与 S3。
静态开发身份与局部文件模式继续支持已有确定性验证。

JWT 的权威来自受信配置的签名键与 claims，审批权来自 reviewer 角色；模型不能提供权限。
approval.request_hash 固定执行意图，独立审核人批准后，由 operation 派发事务重新验证并消费。
批准前不开模型/Job，防止实际执行早于治理。Plan 是受限可审查建议，不是权限授予。

S3 上传读回在 DB 证据登记之前；API 总是读远端再验证 hash。跨库失败允许未引用对象，但不允许
不完整证据发布。缓存不承担远端失效回退。Garage 为可运行的 S3 兼容验证后端；MinIO 镜像拉取在本地失败，
未把没有跑起来的后端记为已验证。存储 ACL/GC/retention 仍属于部署后续工作。

RAG 不复制 retrieval pipeline；使用已有 qa-kb-service 认证 MCP 的只读 search。
知识身份由服务端显式 actor-token 映射决定，完整引用持久化并标记不受信。
真实模型发现的 provider continuation 对象通过上游 `to_private_record` 序列化，不采用通用字符串转换。
模型完整消息仅包裹一个 JSON code fence 时移除围栏，仍经过原严格候选 schema/证据验证；不抽取正文子串或修正声明。

明确边界：固定 gateway-fixture，受信本地 JWKS，CLI loopback，没有企业 Git/CI Adapter、OIDC discovery、
自动凭据刷新、治理 Memory 或完整 held-out 评测。此次增量是 M3/M4/M5 的可运行子集。
