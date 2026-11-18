# 部署与运维

> **状态：生产级框架原型，尚未达到生产就绪。** 本文描述通用运行边界，不是生产部署认证或完整运维手册。公开/私有内容边界见[文档政策](repository-policy.md)。

## 运行条件与配置

- Python3.11+、PostgreSQL，以及管理员批准的独立 schema。
- Agent 执行需要 Node22.19+ 和锁定的 Pi npm 依赖。
- 一个配置目录只用于一个部署环境，不能把开发配置直接复制为生产配置。

应用要求独立 PostgreSQL schema，拒绝 public/系统 schema。复制审核过的[空白配置模板](../docker/platform/app.env.example)，在仓库外填写为专用配置目录的 `.env`。不要提交或挂载包含明文管理员 Key、客户材料和模型秘密的整个工作目录。

```bash
enterprise-ai check-release
enterprise-ai check-config --config-dir /path/to/platform-config
```

数据库、模型及 Callback 凭据通过明确的私有配置/环境引用提供，不能嵌入能力定义。模板不是可直接生产运行的部署配置。

生产模式要求 `DATABASE__SSLMODE=verify-full` 和单个显式 TCP `DATABASE__HOST`；不允许 Unix socket 或主机回退列表。应用、迁移和备份都执行此配置检查。挂载受信任的 CA 证书并通过 `DATABASE__SSLROOTCERT` 指定，或使用 libpq 的默认信任根文件；连接时校验数据库证书与主机名。试用模式保留本地开发选项。

## 初始化与升级

平台迁移目录为 `infra/platform_migrations/`，初始化 baseline 为 `0001_platform`，固定 SQL 位于 `versions/schema.sql`。只在获批的空隔离 schema 中初始化；应用启动不会创建表、导入业务 seed 或自动修改 schema。

源码中的 schema head 为尚未发布的 `0003_business_code`。增量 `0002_outbound_evidence` 在模型调用记录上增加安全 outbound evidence；本次细化直接在未发布的 `0003_business_code` 中扩展节点 kind/status、scope 和父级约束，并允许 `workflow:invoke` 资源授权，支持代码活动调用层级及 unavailable 证据。冻结的 `0001_platform` / `0002_outbound_evidence` 不变，不创建业务能力、修改已有定义或重放任务，也不表示已验证或升级整个运行环境。运行对应应用前仍须按授权显式升级；缺少历史证据的调用不会获得 fallback 许可。所有增量都不修改 baseline，也不自动运行。降级删除调用或执行证据须另行授权，不能通过 downgrade 丢弃。

首次初始化仅能由管理员在获批空 schema 执行。仓库提供 `scripts/bootstrap_platform_sit.py` 测试初始化工具，默认预检，显式 `--apply` 才创建；它仅用于隔离测试环境，不是生产自动部署入口。

已有平台先停止写入与所有 Worker、排空运行任务，并备份/演练，再执行：

```bash
enterprise-ai migrate --config-dir /path/to/platform-config
# 审核预检结果后才应用：
enterprise-ai migrate --config-dir /path/to/platform-config --apply
```

不要重跑 bootstrap 覆盖已有 schema，也不要通过 stamp 把不匹配的库标记为当前版本。未知 revision 明确拒绝；初始化 baseline 不提供破坏性 downgrade。后续已交付结构变更使用增量迁移，schema 退役须单独授权。

## 进程

按需要分别运行，使用进程管理器管理其生命周期：

| 进程 | 职责 | 启用条件 |
|---|---|---|
| API | 认证、接入与查询 | 必需，不执行任务；参考控制台仅在显式启用时挂载 |
| Workflow Worker | 规则/模型/只读工具 Workflow | 需要执行任务时启用 |
| Agent-capable Worker | 根 Agent 和内嵌 Agent | 显式选择执行角色、配置依赖并授权 |
| Callback Worker | 终态通知投递 | 配置 Callback 后单独启用 |
| Recovery Worker | 收敛过期租约及禁用项目的排队任务 | 独立运行，不重新执行任务 |

```bash
enterprise-ai serve --config-dir /path/to/platform-config
enterprise-ai worker --config-dir /path/to/platform-config
# 根 Agent 和内嵌 Agent 均显式启用：
enterprise-ai worker --executors workflow,agent --workflow-agents --config-dir /path/to/platform-config
# 在上述角色基础上，显式安装审核过的业务包：
enterprise-ai worker --executors workflow,agent --workflow-agents --hub-bundles remitter-search --config-dir /path/to/platform-config
# 独立通知投递：
enterprise-ai callback-worker --config-dir /path/to/platform-config
enterprise-ai recovery-worker --config-dir /path/to/platform-config
```

API 不自动启动 Worker。API 与 Task Worker 启动时会进行本地 Schema 验证后端握手；该握手不调用 Provider、模型、工具或数据库，也不是按请求探测外部服务。Pi Agent 需要 Node22.19+ 以及已安装的锁定 npm 依赖；`enterprise-ai check-release --pi` 检查本地 Node/Pi 握手，不调用模型或外部服务。需要 MCP 时显式提供经审核的目录，并先用 `check-mcp-catalog` 校验。

MCP 目录、Tool binding、Agent 的 tools/skills 引用和对应 Grant 是分别配置的环节；文件校验不会探测远程服务或自动授予工具。当前只支持受控 Streamable HTTP JSON 只读工具，不是任意 MCP 服务即插即用。详见 [Skills、MCP 与 Pi 配置](agent-skills-mcp.md)。

Task Worker 从实际安装的节点适配器和已检查 Runtime 形成不可变能力清单，领取前核对固定制品及相关依赖要求的语义 profile，而非只看执行角色。根 Agent 和内嵌 Agent 都需要匹配的 Runtime；当前 V1 只定义 `pi → enterprise-ai.pi.v1`。不兼容或制品完整性异常的任务仍排队，不消耗 Attempt，不自动安装代码或切换 Runtime。部署需覆盖实际能力需求；健康心跳中的能力清单只用于观察，不是执行授权。工具 binding/handler、Provider 和凭据仍在实际调用时检查。

`--hub-bundles` 仅从同发行物的显式业务目录安装代码，不读取数据库中的模块路径。代码流程/工具还要求实现 Key、版本、来源摘要完全相同；发布新业务版本不让旧 Worker 自动兼容。源码变化后重新发布并部署对应实现，不通过线上热改文件切换历史版本。具体命令与发布/授权边界见[业务能力 Hub](capability-hub.md)。

## 制品与容器

源码构建使用 `python -m scripts.build_platform_release --out-dir /path/to/new-release-directory`，输出 wheel、SHA256 manifest 和构建日志；目录须不存在。该脚本使用独立的临时构建目录并检查制品，避免本地构建缓存混入。wheel 包含平台源码、控制台、初始化 SQL 和 Pi 适配器，不包含业务样本、node_modules、私有配置、内部文档或本地 demo 报告。

独立镜像和 Compose 分别位于 [Dockerfile](../docker/platform/Dockerfile) 与 [compose.yaml](../docker/platform/compose.yaml)。Compose 不建库、不迁移、不导入 seed；发布时固定镜像 digest 并提供 `PLATFORM_IMAGE`、`PLATFORM_CONFIG_DIR`。模型与通知秘密分别通过对应 Worker 的私有环境文件注入。

```bash
docker build -f docker/platform/Dockerfile -t enterprise-ai:reviewed-release .
export PLATFORM_IMAGE=enterprise-ai:reviewed-release
export PLATFORM_CONFIG_DIR=/path/to/platform-config
docker compose -f docker/platform/compose.yaml config --quiet
docker compose -f docker/platform/compose.yaml up -d api workflow recovery
```

先检查配置再启动。Compose 的 Recovery Worker 独立于执行 Worker 运行，用于在执行 Worker 全部停止时继续收敛失租任务；它不重新执行任务，也不判断未知外部操作是否成功。Agent 和 Callback 服务使用独立的 `agents` / `callbacks` profile，按需显式开启；不要仅为让任务结束而给所有 Worker 配置全量凭据或权限。Recovery Worker 也须纳入进程状态监控。

API 默认仅绑定宿主 loopback。应用 ingress 配置提供单进程请求体/头/目标大小、读写超时和并发边界；这些是传输资源限制，不是租户配额、Task deadline 或多实例共享限流。应用内身份限流在认证之后执行，不能保护认证入口。生产部署必须由受信任的边缘层在认证前实施跨实例共享限流、请求大小限制和入口 TLS，并限制对 API 进程的直接访问。秘密管理、备份保护和网络出口仍由部署设施提供。

## 环境策略

运行模式与策略文件由部署方显式配置；可参考通用的[部署策略模板](../docker/platform/deployment-policy.example.json)及[应用配置模板](../docker/platform/app.env.example)，逐项审核并填入适用于自身环境的值。模板只是结构示例，不是预置部署配置；配置通过校验不代表组织身份、财务、留存、备份或边缘策略已落实。平台不会据此自动开通用户、推断价格或物理删除客户数据。

## 健康与故障

- `/livez` 检查 API 响应；`/readyz` 检查数据库版本和必要表，不代表 Worker 正常消费。
- `enterprise-ai health --project <project> --config-dir ...` 返回运行汇总；退出0表示ok，1表示attention，2表示查询故障。初始化失败另会非零退出，监控也须捕获。
- 健康数据包含队列等待、缺失执行角色、失租、取消未确认、unknown费用和Callback死信，可供已有监控采集，不默认向外部告警平台发送数据。
- 项目健康接口与 CLI 的 `queue_readiness` 仅读取启用项目最早入队的 32 个任务（同一入队时间按 ID 排序），最多查找 128 次固定制品，并使用最多 1000 个新鲜且标记为运行中的 Worker 心跳。同一 Worker 必须同时覆盖执行类型、所有精确 profile 和按流程/工具角色区分的代码实现 Key/版本/来源摘要。响应只列出有问题的任务 ID、原因与采样统计，不返回清单、定义或任务输入。`queue_truncated`、`worker_list_truncated` 表示观察不完整，不能由 `ok` 推断整条队列就绪。
- `QUEUED_TASK_NO_COMPATIBLE_WORKER` 表示已解析需求但未观察到匹配 Worker；`QUEUED_TASK_REQUIREMENTS_UNAVAILABLE` 表示固定制品缺失、无效或无法解析；`QUEUED_TASK_COMPATIBILITY_UNKNOWN` 表示没有观察到匹配，但可能有未列出的 Worker、相关 Worker 尚无有效清单（如旧版本或清单超过 128 项），或已达到制品查找上限。告警计数仅限样本。即使观察到匹配也不代表可立即领取，不检查并发配额、实时授权、绑定或供应商状态。
- 新 Worker 启动时在现有心跳 JSON 中写入内部清单，不需要迁移。滚动更新时先更新 API/CLI 健康读取端，再重启 Task Worker；新版读取端兼容旧心跳，并只在 `executors` 返回执行类型。
- Worker 的 DB 心跳只作观察，不替代 TaskAttempt 租约。DB/观察故障时停止进程，由 supervisor 重启；不在原进程盲目重试未知事务或业务调用。
- SIGTERM 停止新领取，允许有界在途执行收尾；不能回收时保留结果未知，不伪造取消确认。
- 排障使用 X-Request-ID 和安全元数据，不打开原始凭据/客户输入输出日志。

源码提供显式 schema 备份工具 `scripts/backup_platform.py`，默认只预检，加 `--apply --out <新文件>` 才运行 pg_dump。备份含客户数据，须受控加密和存储；失败残留文件不能用于恢复。恢复只能在获批独立空数据库中演练，禁止自动覆盖在线 schema，也不能通过恢复备份安全重放未知副作用。
