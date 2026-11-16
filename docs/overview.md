# 功能、技术栈与架构

> **产品状态：生产级框架原型（production-level framework prototype），尚未达到生产就绪。** 平台已交付一组可运行的执行、治理与交付基础，但完整产品能力、组织策略和生产验收仍未完成。本文介绍面向使用者的能力与边界；仓库公开/私有内容边界见[文档政策](repository-policy.md)。

## 当前技术栈

| 部分 | 当前实现 | 边界 |
|---|---|---|
| 服务语言与 API | Python 3.11+、FastAPI、Pydantic / pydantic-settings、Uvicorn | REST 接入；OpenAPI 位于 `/docs` |
| 参考控制台 | 同源静态 HTML、CSS 和 JavaScript | 仅 `serve --reference-console` 时挂载；正式前端独立开发，仅依赖 `/v2` 契约 |
| 数据与迁移 | PostgreSQL、SQLAlchemy、psycopg2、Alembic | 平台独立 schema 和 `infra/platform_migrations/`；任务队列、租约、事件及 outbox 存于数据库 |
| Agent | 平台 Runtime 端口及封闭的显式适配器注册表；当前内置 Pi，Node 22.19+ | Pi 依赖由 `framework/agent_runtime/pi/node/package-lock.json` 固定；无 Agent 会话恢复 |
| 外部连接 | HTTPX、OpenAI 兼容模型接口、受审核的 MCP JSON 接入、受限只读工具 | 实际调用受平台授权和资源配置约束；不是任意网络或脚本执行 |
| 契约与身份 | JSON Schema、API Key、服务身份和显式 OIDC JWT 绑定 | 用户/成员 RBAC 与组织身份映射尚未完成 |

当前采用模块化服务加独立 Worker 的逻辑划分，不要求 Redis、消息队列或对象存储。技术栈描述以 `pyproject.toml`、锁文件和实际代码为准；上述边界不表示全部生产能力已完成。

## 核心概念

| 概念 | 用途 |
|---|---|
| Project / ServiceIdentity / Grant | 确定资源归属、调用身份和允许的动作 |
| Capability / Version | 管理能力定义、Schema、固定依赖和不可变制品 |
| Task / TaskAttempt | 管理任务准入、执行租约、取消及最终结果 |
| Workflow / Agent | 分别承担确定性编排和有界推理/工具选择 |
| Model / Tool Gateway | 在实际调用位置执行资源授权、传输与用量留证 |
| TaskEvent / CallbackDelivery | 分别承担任务时间线和独立外部通知交付 |

## 模块

| 目录 | 职责 |
|---|---|
| `domain/` | 能力、任务、权限、Workflow、模型/工具及运营的类型契约 |
| `orchestration/` | 发布、任务用例、执行、网关授权与质量用例 |
| `capability_hub/` | 同仓库业务 Agent、Python Workflow、自定义步骤、只读工具和发布包 |
| `framework/` | 无外部 I/O 的规则、Prompt 模板、图编译，以及显式运行适配器与工具目录 |
| `infra/` | 数据库、仓储、传输与迁移 |
| `interfaces/` | HTTP、控制台、CLI 与 Worker 装配 |
| `tests/` | 隔离数据及本地替身验证 |

平台入口为 `interfaces/cli/platform.py` 和 `interfaces/http/platform_server.py`。`interfaces/runtime.py` 独立装配配置、存储和身份服务并管理进程资源；HTTP 只是其适配器，Worker、健康查询与配置检查不创建 FastAPI 应用。持久化使用 `infra/platform_store.py`。能力定义是受校验的数据，不是任意 Python 模块导入入口。

业务代码以显式注册的 `capability_hub/` 包与平台核心同仓库维护，具体见[业务能力 Hub](capability-hub.md)。`workflow.code.v1` 的尚未发布细化以 `CodeActivity` / `activities` 声明可调用资源和预算，允许 async Python 组合确定性步骤与 Agent/工具/模型及固定版本子代码流程；不是静态 DAG、新协议版本或已发布流程迁移。平台核心不导入具体业务包；仅进程入口安装所选业务实现。当前 `remitter_search` 样板只处理调用方提供的候选名称，不预置业务系统、组织政策或真实业务数据；业务规则与证据契约仍归业务包。

前端与后端通过 HTTP 契约协作，不共享数据库或运行组件。`interfaces/http/api_contract.py` 统一业务路由及 OpenAPI；Session API 与参考控制台资源分离。离线 schema 导出不初始化存储，具体分工与交互边界见[前端接入契约](frontend-integration.md)。

这些目录按职责分层；平台由显式入口装配，数据库由独立初始化 baseline 管理。后续扩展方向见[建设路线图](roadmap.md)。

### 用例与执行边界

框架按“制品定义 → 固定版本使用 → 有权执行 → 独立观察/交付”组织，不围绕一个通用 Manager 或某种 Agent SDK 组织所有功能。

| 职责 | 代码入口 | 不负责的事情 |
|---|---|---|
| 能力编写与发布 | `orchestration/capability_service.py` | 不驱动 Runtime，不承担 Prompt 渲染或执行查询 |
| 定义解释与依赖角色 | `domain/capability_definitions.py` | 不查库、不选择供应商、不授权或安装代码 |
| 已发布制品读取 | `orchestration/published_capabilities.py`、`domain/capability.py` 的 `CapabilityReader` | 不发布、不追随默认版本、不授权、不探测运行资源 |
| Prompt 渲染 / Workflow 计划查询 | `orchestration/prompt_renderer.py`、`orchestration/workflow_plan_query.py` | 不创建 Task，不调用模型，不要求依赖发布用例 |
| 接入事务 | `domain/task_admission.py`、`infra/store/repository/admission.py` | 不运行 Schema 子进程、不执行模型/工具、不向应用暴露 Session |
| Task 接入与查询 | `orchestration/task_service.py`、`infra/store/repository/task.py` | 不领取或完成执行，不把模型、工具状态当最终结果 |
| Task 调度与执行所有权 | `orchestration/platform_worker.py`、`infra/store/repository/task_queue.py` | 不承担 HTTP 装配、制品发布或最终成功规则 |
| Task 完成规则与原子提交 | `domain/task_completion.py`、`infra/store/repository/task_completion.py` | 纯规则不读取 ORM；提交适配器不重新执行业务或决定交付成功 |
| Workflow / Agent 执行 | 对应 Executor、Broker 和显式适配器目录 | 不建立新的根任务、租约或主体 |
| 模型/工具治理与调用 | `orchestration/model_policy_resolver.py` 及各 Gateway | 不授予发布定义安装代码、替换端点或透传密钥的权力 |
| 执行观察、质量、反馈与交付 | 对应查询和运营用例、独立 Callback 生命周期 | 不反向修改已提交 Task 结果 |

已发布制品读取先核对项目、Key、资源类型、能力 ID、固定版本、发布状态和内容哈希，再返回脱离仓储对象的副本。`CapabilityVersionRecord.verify_integrity()` / `require_published()` 是共享领域规则，执行组件不再借用发布服务的私有方法。制品有效不等于当前允许调用：读取制品后的实时授权、端点启用、预算及适配器可用性仍由具体用例处理，不能缓存为永久通行证。

定义解释是封闭的、带版本的领域契约。编写、发布、Workflow 计划、执行和恢复使用相同的语义检查与依赖角色，不能分别维护一套“发布可接受”和“执行可理解”的规则。Workflow V1 可显式写 `definition_version: "workflow.v1"`，省略时固定表示 V1，而不是跟随最新解释器。当前不支持的节点重试在定义校验时拒绝，不等到 Worker 领取后失败。发布前在锁外完成 Schema 和依赖语义检查，提交时重新核对根制品及全部直接依赖的 ID、版本、种类、发布状态与内容哈希；发布审计记录解释器 profile 和依赖哈希。profile 表示语义兼容边界，不等于完整的源码构建摘要。

依赖关系是图，而不是要求所有功能依次穿过的调用链：`interfaces` 负责装配，`orchestration` 组织用例；`framework` 的纯算法/运行组件与 `infra` 的存储/传输各自依赖 `domain`，二者不互相导入。外部适配通过明确端口和安装目录接入，不用包名字符串或动态导入作为扩展契约。Task、Gateway 与质量应用服务不再操作 ORM/SQL 或传递原始事务；其余部分运营用例仍直接组织存储事务，后续按实际职责收敛，不构成通用执行内核或额外领域状态机。

## 执行链路

```text
业务调用 / 控制台
  → 身份、项目与资源授权
  → 固定能力版本和幂等 Task + GatewayRequest 原子接入
  → 独立 Worker 领取 TaskAttempt 租约
  → Workflow 或已安装的 Agent Runtime（当前为 Pi）
  → Model Gateway / 只读 Tool Gateway
  → 结果校验、事件与用量记账
  → 独立 Callback outbox 投递
```

只有通过身份与资源授权的接入才会被接受；Task 与 GatewayRequest 元数据在同一事务中创建，拒绝的请求不会留下已接受的 GatewayRequest 记录。GatewayRequest 记录接入操作及其 request/correlation 元数据，并关联 Task；它不是 Task，也不授予执行权限。Task 是执行状态与最终结果的权威，CallbackDelivery 有独立交付状态。已发布的不可变制品包含 Workflow 定义及其内容哈希，不持久化编译计划。固定图 DSL 在计划查询时或执行前编译静态图；代码流程只提供实现引用与活动声明，不生成 Python 控制流的静态 DAG。这不是任意代码入口，运行时仍需验证；能力版本及其依赖保持固定。

- 能力版本发布后不可覆盖，依赖必须固定到同项目的已发布版本。
- Task/Attempt 管理领取、租约、取消与最终结果；AgentExecution/Step 记录从属执行证据。
- 全局、项目与能力并发策略在数据库中协同准入；Agent 能力额度由根任务和内嵌执行共享。未配置上限不意味着生产默认容量，须由运营方明确设置。
- 固定图 `workflow.v1` 支持映射、条件和规则/模型/只读工具/显式 Agent 节点；仅纯规则节点支持有界并行，外部 I/O 顺序执行。Schema 按 JSON Schema Draft 2020-12 校验，在隔离验证器中执行，禁止检索外部 Schema/资源。Runtime 和只读工具使用显式封闭目录；MCP 仅接受经审核的部署目录。
- 代码 Workflow 的活动契约由 Python 决定阶段与分支，通过 `ctx.call`、`ctx.parallel` / `ctx.map` 进行带独立哈希身份的有界调用，不使用裸 gather。1–99 项声明限制每个 frame 的调用预算，`max_parallelism` 默认 4、上限 16；深度上限 8，单 Attempt 总节点上限 1000。所有后代共享根 Task / Attempt / 租约和继承期限，子流程只调用已发布固定版本的代码 Workflow，并重检、继承 `workflow:invoke`。
- 代码流程共享节点账本，通过父级和 scope 记录 `result` frame 与 `step|agent|tool|llm|workflow` 调用。必需活动至少有一次非 skipped 调用，每次实际调用须成功或明确允许 unavailable；未调用的可选活动不生成 skipped 记录。unavailable 是部分证据状态，不是业务否定值；授权、租约、取消、账务、配置及未知检查点故障不能被 continue 吞掉。完成时核对层级、终态子 Agent 与结果摘要；代码流程不恢复，未知外部调用不自动重放。单个示例的链路验证不代表整个平台已经生产就绪。
- 代码工具和流程的来源 Key/版本/摘要与语义 profile 分别匹配，Workflow 与工具安装角色分开，不接受任意导入路径；全部 Agent 仍使用 Pi，Model/Tool Gateway 不变。
- 根 Agent 和 Workflow 内嵌 Agent 均需显式安装执行支持；内嵌 Agent 还需要独立 `agent:invoke` 授权。
- Agent 定义通过运行时标识选择由 Worker 显式装配的可信适配器；只有版本化语义要求与实际 profile 匹配时才领取，不动态导入用户指定代码或回退到其他框架。当前 V1 运行契约为 `pi → enterprise-ai.pi.v1`；未知运行时 Key 不能靠同名注册自动成为兼容实现。事件由平台 Broker 记录，取消通过协作式终止处理，暂不支持会话恢复。
- Skill 是不可变、版本固定的静态指引，使用前检查独立 Grant；不运行脚本，也不能扩大 Agent 的工具权限。指引内容会进入模型上下文，不能代替后端授权或业务规则。
- 业务反馈独立于 Task 状态和人工质检：记录采纳、修改后采纳或未采纳，并以提交人维度追加修订；技术成功不等于业务采纳或质量正确。
- 完成评分的评测可按相同数据集版本和能力比较不同版本；运行、评分和比较前校验数据集内容哈希，不将规则通过数称作真实准确率。
- Worker 从实际安装目录形成不可变能力清单，领取前匹配 Workflow 解释器/检查点、所需节点适配器以及根/内嵌 Agent Runtime 的精确语义 profile。匹配沿固定版本的实际引用检查相关依赖，而不是只看根类型或 `workflow_agents` 开关；无关的声明依赖不会安装运行能力。缺少、不支持或完整性异常的制品不消耗 Attempt，仍保持排队供合适的部署或运营调查处理；扫描继续寻找后面的兼容工作。
- 结果未知的外部调用不自动重放。恢复入口只允许显式创建带来源的纯规则 Workflow 新 Task；相同版本与输入指纹下，可复用原任务已完成的规则检查点，并记录来源节点。
- Workflow 静态计划同时说明 compiler、interpreter 和 checkpoint profile；节点源指纹包含解释器及检查点兼容标识，审计也记录它们。旧指纹不会因内容版本相同而被新解释器自动复用；纯规则恢复可以重新计算，外部 I/O 不因兼容失败而重放。

## 共享执行契约

执行授权位于 `orchestration/execution_authorization.py`，独立于 Worker 调度器。Workflow、Agent、模型与工具网关使用同一套实时项目/主体/资源检查，不通过导入 Worker 获取权限能力。管理员按当前主体定向查询，服务身份按当前 Grant 查询；子 Agent 仍继承根 Task 的可信身份，不获得新的主体或项目。

`domain/execution_context.py` 定义运行期的执行时间预算。Worker 为一次执行建立绝对单调时钟期限；Workflow 节点、Agent、模型轮次和工具调用只能缩短剩余期限，不能在嵌套或 fallback 时重新获得完整的父级时长。实际传输 timeout 也受剩余时间限制；Agent 的资源解析、Prompt 组装与容量等待计入自身期限。

时间预算不进入业务请求、幂等指纹或持久结果，也不是数据库租约或费用预算。执行过期会阻止启动新的外部工作和接受迟到结果，但已知 Usage 结算、取消确认等收尾仍按有效租约完成，不能因时间预算耗尽而丢弃证据。单调时钟期限不能跨进程恢复，也不代表能强制撤销远端请求；未知外部结果仍不自动重放。

不能回收执行资源的错误属于共享执行契约，由拥有执行权的 Worker 停止领取并交给 supervisor 处置；Workflow 和 Agent 不自行创建第二套任务状态机。

### 执行端口与事务所有权

Task 接入先冻结请求和固定制品快照，在项目写锁外执行有界输入校验，再通过 `store.admission.unit_of_work(project, owner)` 提交作用域命令。最终事务先检查幂等复用，再核对校验快照、制品完整性、恢复来源及配额；即使准备期间校验失败，也先确认是否已有并发请求受理了相同内容。相同已受理请求不依赖当前验证器或目标制品重新通过校验；异内容继续冲突。GatewayRequest 和 Task 共用此事务；评测的全部 case Task 与 run 也一次提交，不允许半批次受理。

质量模块的纯数据集完整性、评分与比较规则在 `domain/quality.py`，持久查询/保存归 `infra/store/repository/quality.py`。Schema 评分在写锁外完成，保存时重新核对运行、数据集和任务证据；准备期间发生变化则返回冲突，调用方可重新评分，不拿旧快照覆盖新证据。评分仍不是业务结果正确性认证。

`domain/task_worker.py` 的 `WorkerServices` 明确提供领取/恢复、取消控制、完成提交、实时授权和有界 Schema 校验端口。Worker 不持有整个 PlatformStore，不负责创建授权服务或选择基础设施实现；`interfaces/worker/platform.py` 安装实际适配器。接入/查询、调度/租约恢复、取消和完成提交分别由 `tasks`、`task_queue`、`task_control`、`task_completion` 适配器承担；它们操作同一个 Task/Attempt 权威，不是四套状态机。这样可独立理解调度/取消逻辑，不需要依赖完整控制面存储。

执行组件同样不持有 PlatformStore：`domain/execution_ports.py` 定义只读制品/授权/校验资源、模型调用账本、Agent 轮次/工具账本、Agent 生命周期及 Workflow 检查点端口。模型 Gateway 组合共享策略解析器，不再继承它；Broker 直接接收 Prompt 渲染与 Agent 生命周期，不穿透 Gateway 访问仓储。实际端口由 Worker 入口安装，同一进程共用资源范围及 fenced 账本；这些是依赖边界，不是给插件增加数据库权限的机制。

端口拆分不拆事务：Agent 轮次、模型调用和预算仍由一次原有仓储命令共同提交，Tool/Workflow 检查点也保留原有 fence。实时授权只依赖当前项目/Grant 与定向管理员读取端口，按原调用位置重新检查；有界 Schema 校验由入口注入，执行组件不自行选择基础设施。

兼容要求位于 `domain/execution_requirements.py`。profile 是精确的语义 ABI，不是实现构建摘要；自定义实现只有明确实现同一契约并声明同一 profile 才可匹配，不根据名称、版本号或“较新”推断兼容。执行器在使用处再次核对 Runtime/节点适配器 profile；供应商、工具 handler/可变 binding、授权、配额和预算仍是实时调用条件，能力清单不提前批准这些资源。

完成请求先冻结为规范 JSON 快照。完成适配器读取固定制品并关闭读取连接后，才等待隔离 Schema 校验；成功提交事务中重新核对制品 ID/版本/完整内容哈希、从属执行证据、取消意图与租约。纯领域规则负责根 Agent 输出匹配和 Workflow 节点/子 Agent 完整性，PostgreSQL 仍以最终条件写提供执行 fence。校验完成不授予永久执行权，过期或已取消的结果仍不能提交。

任务状态、终态证据收敛、预算占额关闭、任务事件及 Callback outbox 仍在同一事务提交或回滚。`execution_effects.record_task_transition` 显式组织这些操作；普通 `append_task_event` 只分配序号并追加事件，不隐式执行财务或交付逻辑。模型调用的预算预留/结算也通过明确入口组织，不再借用其他仓储的私有审计方法。这里不是异步事件总线，也不改变 Callback 独立、至少一次的交付生命周期。

### 外部模型操作证据

`domain/outbound_operation.py` 的不可变 `OutboundEvidence` 分别描述请求发送确定性、完整响应是否收到、答案是否被接纳、已报告 Usage、供应商元数据与重试许可。内容拒绝、格式无效、授权撤回或超时后丢弃答案，都不抹掉已经得到的安全 Usage/模型/请求 ID；已知证据与调用状态、账务在同一 fenced 事务保存。Usage 由供应商报告，不等于费用已经定价或核实。

重试决策读取模型调用本身的 `outbound_evidence`，不回放通用审计日志。当前只有明确完整的 HTTP 429 拒绝、未报告 Usage 且持久证据一致时，completion 策略才可能按固定候选列表 fallback；普通 5xx、网络断开、超时、格式错误或旧 `retryable` 提示都不授予自动重放许可。Agent turn 仍不自动重试。该契约不是远端 exactly-once、免费重试或业务写操作幂等保证。

## 治理与运营

- 默认拒绝匿名 API 请求；服务身份按项目、资源 Grant 和任务 owner 限定访问。
- Key 只存摘要。OIDC 使用明确安装的公开 RS256 公钥和主体绑定，不信任 JWT 中自报的管理员角色。
- 模型/工具凭据只保存引用，由运行进程从环境读取；发布定义不嵌入密钥。
- 配额控制排队与运行准入。预算按显式价格预留、按已知 Usage 结算；未知费用保留占额，实际费用可能超过预留。
- Callback 只发送终态元数据，以 HMAC 签名并独立重试，接收方按 `delivery_id` 持久去重。
- 评测使用不可变数据集、原子受理的 Task 批次及确定性评分；人工质检只追加，不改 Task 结果。
- 记忆按当前 owner 与 capability 隔离，使用不可变修订、过期时间和删除标记，不自动注入模型。

## 明确限制

授权决定能否执行；交付偏好（同步等待或异步返回）只决定如何等待同一 Task，不改变权限或执行权。Gateway 请求元数据用于关联，不代替身份、幂等键或 Task 状态。

不承诺远端调用回滚、通知 exactly-once、供应商硬停预算、同一 Task 原地重试或 Agent 会话恢复。客户留存/物理删除、组织登录和角色政策、供应商验收、业务副作用与幂等策略须由使用方明确。只读留存盘点不等于删除授权；公开 API 和状态说明见[API 使用](api.md)，部署边界见[部署与运维](deployment.md)。
