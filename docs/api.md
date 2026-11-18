# API 使用

基础路径 `/v2`；完整字段、分页参数及响应模型见运行服务 `/docs`。所有业务和管理 API 均要求身份认证。

独立前端的页面、权限、轮询与错误处理约定见[前端接入契约](frontend-integration.md)。可用 `enterprise-ai export-openapi` 离线导出同版本业务接口 schema，无需数据库或配置；成功响应按各接口直接返回，不提供统一 `{code,data}` 包装。

## 认证

二选一，不能同时发送：

```text
x-api-key: <当前身份的平台 Key>
Authorization: Bearer <已明确绑定的 OIDC JWT>
```

`GET /v2/session` 返回当前身份范围。管理员管理项目与资源；服务身份受所属项目、Grant 和任务 owner 限制。JWT 中的 roles/groups 不授予管理员权限。

错误使用 `{ "error": { "code": "...", "message": "...", "request_id": "..." } }`。请求响应中的 `X-Request-ID` 由服务生成；接入与 execute 请求可带可选 `X-Correlation-ID`（不透明关联标识，不是身份或幂等键），响应会回显。已受理请求可通过 `/v2/projects/{project}/requests/{request_id}` 或 `/tasks/{task_id}/requests` 查询元数据；读权限仍按关联 Task 检查。排障保留 ID，不要上传凭据、请求原文或客户结果。

## 能力与 Prompt

Agent 的模型策略显式使用 `interaction: "agent_turn"`。`request_profile` 默认为 `openai_chat`，保持标准 OpenAI-compatible Chat Completions 请求；对 OpenRouter 可显式选择 `openrouter_non_reasoning`，发送 `reasoning: {"enabled": false, "exclude": true}`，参数语义见[供应商说明](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)。该选择属于固定版本的策略定义，不按域名推断，也不接受任意请求参数透传。当前 Agent 不支持推理内容往返：若供应商仍返回非空推理内容，响应被拒绝但已知 Usage 仍留证。该 profile 不保证所有模型均支持关闭推理，需按选定模型实际验证。

以下路径以 `/v2/projects/{project}` 为前缀：

| 操作 | 方法与路径 |
|---|---|
| 创建/查询能力 | POST / GET `/capabilities` |
| 创建/查询版本 | POST / GET `/capabilities/{key}/versions` |
| 发布版本 | POST `/capabilities/{key}/versions/{version}/publish` |
| 切换默认版本 | POST `/capabilities/{key}/versions/{version}/set-default` |
| 渲染 Prompt | POST `/prompts/{key}/versions/{version}/render` |

Prompt 使用 `${variable}`，不执行 Python 表达式。能力 Schema 使用 JSON Schema Draft 2020-12，在隔离验证器中校验；只支持自包含 Schema 与本地引用，不检索外部引用。已发布固定图 Workflow 可通过 `GET /capabilities/{key}/versions/{version}/plan` 获取从固定 DSL 即时编译的静态图元数据；该计划不是持久化发布制品、执行授权或任意代码，Worker 执行前仍会编译并进行运行时验证。发布能力不等于任务已执行。

Workflow 定义可显式声明 `definition_version: "workflow.v1"`，省略时仍固定表示 V1。发布和执行使用同一语义契约；当前节点 `max_attempts` 只支持 `1`，不能配置尚未实现的原地重试。计划响应包含 `compiler_profile`、`interpreter_profile` 和 `checkpoint_profile`，供说明语义及检查点兼容性，不是运行授权或可由前端选择的执行引擎。

同仓库业务包也可发布 `workflow.code.v1`，固定经审核的实现 Key/版本/来源摘要和活动资源依赖。本次尚未发布的契约细化使用 `CodeActivity` / `activities`，不是新协议版本或已发布定义的迁移；固定图 DSL 的 `nodes` 不变。代码流程的 `/plan` 响应为 `CodeWorkflowPlanRecord`，含 `control_flow: "code"`、实现引用、`activities` 和 `max_parallelism`，不含 Python 代码或虚构的静态 DAG；DSL 仍返回原 `WorkflowPlanRecord`。

代码流程声明 1–99 个活动，`kind` 为 `step|agent|tool|llm|workflow`；除 step 外必须固定能力版本，只有 llm 可声明 Prompt。每项声明 `required`、`max_calls`（1–1000）、`timeout_seconds` 与 `on_unavailable: "fail"|"continue"`；定义级 `max_parallelism` 默认 4、上限 16。实际阶段、分支及重复调用由 Python 决定，公共接口统一为 `ctx.call`，有界并发使用 `ctx.parallel` / `ctx.map`。未调用的可选活动没有隐式 skipped 记录，声明数量不是执行节点数。

Task 提交和查询入口不变。代码节点通过 `parent_node_id` 与 `scope` 表达层级；scope 包含流程 `workflow_key` / `version` / `content_hash`，调用还含 `activity_key` / `invocation_hash`。流程 frame 为 `result`，实际调用为 `step|agent|tool|llm|workflow`；节点终态 `unavailable` 附安全 `error_code`，表示部分证据不可用，不是 Task 状态、空业务结果或业务“clear”。continue 仅适用于白名单内已安全收敛的模型/只读工具失败，不能吞掉授权、租约、取消、账务、配置或未知检查点故障；未知外部结果不自动重放。

子流程初期只接受已发布的固定版本代码 Workflow；`workflow:invoke` 在调用处重检并向后代继承。全部调用共享根 Task / Attempt / 租约和只能缩短的期限，不创建子 Task，代码 Workflow 不支持恢复、持久等待或审批。完整 Context、预算和完成校验契约见[业务能力 Hub](capability-hub.md)；单个示例验证不代表整个平台生产就绪。

模型响应即使因格式、策略或内容被拒绝，已知的安全 Usage、实际模型、HTTP 状态及供应商请求 ID 仍会保存；它们不表示答案成功或费用已经核实。completion 的固定候选 fallback 仅适用于有明确持久证据的完整 429 拒绝且未报告 Usage；网络故障、超时、一般 5xx 或格式无效不自动重放，Agent turn 不自动重试。

模型传输只协商 identity、gzip 或 deflate 单层编码，并检查压缩结束标记；截断或不支持的编码不视为完整响应。重复 JSON 字段和非有限数字不能作为接纳答案或授予 fallback 的依据。完整响应的安全元数据在连接清理前提取，清理失败仍不代表答案成功。

Skill 是版本化的静态任务指引：创建 `kind: "skill"` 的能力并发布 `definition: {"instructions": "..."}`。Agent 定义用 `skills: [{"key": "...", "version": "..."}]` 固定引用，同时将相同版本列入 `dependencies`；每个 Agent 最多使用 8 个 Skill。服务身份必须具有对应的 `skill:use` Grant，平台在每次模型调用前及响应后重新检查。Skill 不支持脚本或可执行路径，也不授予任何工具权限；工具仍须单独授权。Skill 指引可能影响模型输出，但不是安全隔离机制。

MCP 工具也可通过 Agent 的 `tools` 固定引用，但连接与 Schema pin 由 Worker 审核目录配置，不接受 Task 指定 endpoint。Pi 负责循环和工具选择，MCP 的实际连接、授权与留证仍经 Tool Gateway。完整配置示例及当前协议限制见 [Skills、MCP 与 Pi](agent-skills-mcp.md)。

本地 Skill Markdown 可用 `skill-export` 预览、`skill-publish` 发布，支持 `SKILL.md` frontmatter 与普通 Markdown 正文；导入不执行脚本或自动授予工具。MCP 公开服务可省略 `credential_ref`；无远程输出 Schema 的文本工具可显式使用 `result_mode: "text"`，包装为 `{"text": "..."}`，默认结构化模式不变。

评测运行均评分完成后，可通过 `GET /v2/projects/{project}/quality/runs/{left_id}/compare/{right_id}` 比较同一能力在同一数据集版本上的结果。接口单独返回技术执行成功数和评测规则通过数及逐样本状态，不返回样本输入；它们不等于业务采纳率或真实准确率。不同数据集或尚未完成评分的运行不能比较。

## 第一个任务：纯规则 Workflow

在隔离试用环境中，以管理员身份依次执行以下操作。示例只做字段映射，不访问模型、外部工具或业务系统。每个请求都要带前述认证头。

### 1. 创建项目和能力

`POST /v2/projects`：

```json
{"key": "demo", "name": "Demo"}
```

`POST /v2/projects/demo/capabilities`：

```json
{"key": "echo-workflow", "kind": "workflow", "name": "Echo Workflow"}
```

项目或能力已存在时直接复用；不要为绕过冲突而更改已有制品。

### 2. 创建固定版本

`POST /v2/projects/demo/capabilities/echo-workflow/versions`：

```json
{
  "version": "1",
  "input_schema": {
    "type": "object",
    "properties": {"message": {"type": "string"}},
    "required": ["message"],
    "additionalProperties": false
  },
  "output_schema": {
    "type": "object",
    "properties": {"message": {"type": "string"}},
    "required": ["message"],
    "additionalProperties": false
  },
  "definition": {
    "nodes": [{
      "key": "echo",
      "kind": "rule",
      "rule": {"operation": "project"},
      "input_mapping": {
        "message": {"source": "input", "pointer": "/message"}
      }
    }],
    "output_mapping": {
      "message": {"source": "node", "node": "echo", "pointer": "/message"}
    }
  }
}
```

版本不可覆盖；变更定义时创建新版本。

### 3. 发布

发送 `POST /v2/projects/demo/capabilities/echo-workflow/versions/1/publish`，不需要请求体。发布后才能提交任务。

### 4. 提交 Task

```http
POST /v2/projects/demo/tasks
x-api-key: <当前身份的平台 Key>
Idempotency-Key: example-request-001
Content-Type: application/json

{
  "capability_key": "echo-workflow",
  "version": "1",
  "business_id": "case-example-001",
  "customer_id": "customer-example-001",
  "input": {"message": "hello"}
}
```

需要事先创建并发布对应版本，具备 `task:submit`。`business_id` 和 `customer_id` 均可选，是调用方提供的关联标识，不是认证或客户访问授权；读取任务仍受项目、身份和能力授权约束。它们包含在幂等指纹内，重试同一请求时须保持不变，相同键异内容返回冲突。不要在标识中放入密钥或敏感明文。queued/claimed 不代表成功，HTTP 不启动 Worker。

`queued` 不表示任何 Worker 都能执行：领取前会匹配固定制品所需的解释器、检查点、节点适配器和 Agent Runtime profile。不兼容的 Worker 不消耗 Attempt；任务等待适合的部署，不能仅为结束排队而自动换 Runtime 或放宽授权。该变化不新增 Task 状态或客户端选择执行器的参数。

任务列表可按 `business_id` 或 `customer_id` 精确过滤；过滤不会扩大调用者可见范围。这只是任务关联查询，不等于客户档案或客户级权限体系。

管理员可通过 `/v2/operations/concurrency` 设置全局运行 Task 上限，通过 `/v2/projects/{project}/capabilities/{key}/concurrency` 设置能力上限；项目上限仍由 `/operations/limits` 设置。请求体为 `{"max_running": 10}`，`null` 表示未设置上限，`0` 暂停新的相应执行。三种上限由数据库跨 Worker 协同；下调不会中断已运行任务。Agent 能力上限同时计入根 Agent Task 与 Workflow 中正在运行的该 Agent；嵌入节点在容量不足时等待，其等待受原 Task 租约、取消和超时约束。全局上限计 Task，不重复计入内部节点。

`POST /v2/projects/{project}/tasks/execute` 是统一调用入口，请求体为 `{"task": {…任务字段…}, "mode": "async"}`；`mode: "sync"` 时可指定 `wait_seconds`（大于 0、不超过 30，默认 10）。同步模式要求提交及读取权限，只等待同一个持久 Task。所有响应路径（异步立即返回、同步完成或等待超时）都包含 `task` 与 `request` 元数据；同步完成时还包含 `result`，等待超时返回 202 和 `wait_timed_out: true`。超时不会取消或丢弃任务。响应提供 `X-Request-ID`、可选 `X-Correlation-ID` 回显及 `Location` 供后续查询。同步/异步是交付偏好，不改变任务幂等指纹，也不会启动额外执行。

### 5. 执行并读取结果

在此示例的独立环境中运行一次 Worker：

```bash
enterprise-ai worker --once --config-dir /path/to/platform-config
```

从提交响应保留 `id` 和 `Location`，调用 `GET /v2/projects/demo/tasks/{task_id}/result`。成功时状态为 `succeeded`，`result` 为 `{"message":"hello"}`；未执行时仍可能为 queued/running。已有其他排队任务的环境中，单次 Worker 不保证领取本例任务，需按返回任务 ID 查询实际状态。

业务系统接入时改用有明确 `task:submit` / `task:read` Grant 的服务身份，不向调用方分发管理员 Key。

## 任务查询与取消

以 `/v2/projects/{project}/tasks/{task_id}` 为前缀：

| 方法与后缀 | 用途 |
|---|---|
| GET 基础路径 / `/result` | 任务状态 / 受授权最终结果 |
| GET `/attempts`、`/nodes` | Attempt 与 Workflow 节点证据 |
| GET `/model-calls`、`/tool-calls` | 调用元数据与用量 |
| GET `/agent-executions` | Agent 从属执行记录 |
| GET `/attempts/{attempt_id}/execution-tree` | 分页执行树 |
| GET `/events` | 按序号续读持久事件 |
| GET `/requests` | 分页读取已受理接入记录，包括幂等复用请求 |
| GET `/deliveries` | 分页读取该 Task 的 Callback 投递元数据，与 Task 状态独立 |
| POST `/cancel`、GET `/cancellation` | 取消意图与确认状态 |
| GET `/recovery` | 恢复资格元数据评估；不是执行保证，也不创建 Task |
| POST `/recover` | 幂等创建带来源的新 Task（仅支持符合条件的纯规则 Workflow） |
| POST / GET `/reviews` | 追加/读取人工质检 |

恢复评估只返回版本、内容哈希和资格原因等元数据；资格不保证运行时校验或检查点可用。实际恢复通过 POST 创建一个带来源的新 Task，不改变原结果；仅在固定版本、输入和上游指纹匹配时复用已完成的纯规则检查点。不自动重放模型、工具或 Agent。评估、读取、取消和恢复分别受授权约束。

| Task 状态 | 含义 |
|---|---|
| queued | 已接入，等待具备对应执行能力的 Worker |
| running | 有正在执行的 Attempt，不保证外部调用已完成 |
| succeeded | 结果通过校验并已提交 |
| failed / timed_out | 执行失败或超时，需检查安全错误码和调用证据 |
| cancelled | 平台确认执行停止；不能据此推断外部业务回滚 |

### 业务反馈

`GET /feedback` 和 `POST /feedback` 分别要求 `task:read` 以及对应的 `feedback:read`、`feedback:write` Grant，遵守现有任务 owner 与项目隔离。当前列表最多返回 200 条。提交示例：

```json
{
  "request_key": "feedback-example-1",
  "expected_revision": 0,
  "adoption": "modified",
  "final_result": {"decision": "adjusted"},
  "reason": "Example correction"
}
```

`adoption` 可为 `adopted`、`modified`、`not_adopted`；只有 `modified` 携带 `final_result`。同一提交人对同一 Task 的反馈追加修订，`expected_revision` 防止并发覆盖；相同请求键和内容重试返回原记录，内容不同则冲突。只能对终态任务反馈，只有成功任务可标记为采纳。反馈不修改任务结果或人工质检，多个提交人之间的最终采纳与聚合口径尚未定义。`final_result` 与 `reason` 可能含敏感信息，需按部署方数据策略管理。

## 配置与运营

同样使用项目路径前缀：

| 路径 | 用途 |
|---|---|
| `/service-identities` | 身份、Key 和资源 Grant，管理端操作 |
| `/model-providers`、`/tool-bindings` | 审核的模型与只读工具资源 |
| `/operations/limits` | 项目配额与活动预算 |
| `/v2/projects/{project}/operations/retention-inventory?before=<RFC3339>` | 管理员只读留存盘点；返回观测计数，不判断可删除性，也不执行删除 |
| `/operations/budgets`、`/operations/charges` | 显式预算周期、费用及未知费用核对 |
| `/operations/callbacks`、`/operations/deliveries` | 签名终态通知端点及投递状态 |
| `/operations/health` | 管理员可读运行健康与告警；`queue_readiness` 对排队任务抽样检查 Worker 兼容性，不授予执行权。见[健康与故障](deployment.md#健康与故障) |
| `/quality/datasets`、`/quality/runs` | 不可变评测集、批次及显式评分 |
| `/memory`、`/memory/{capability}/{subject}` | 当前身份的记忆修订与读取 |
| `/oidc/bindings`、`/oidc/unbind` | 主体与现有服务身份的明确绑定/撤销 |

OIDC Provider 的管理入口为 `/v2/oidc/providers`。具体 HTTP 方法和载荷以 OpenAPI 为准，不能按此表推断任意集合都支持修改或删除。

`GET /operations/pages/{collection}?limit=50&cursor=...` 返回 `{items,next_cursor,limit}`。集合包括 budgets、charges、callbacks、deliveries、datasets、evaluations、reviews、memory、oidc-bindings；每页重新授权，cursor 不携带授权，live 查询不是快照。reviews 需要 task_id，memory 需要 capability/subject。

## Callback 接收

通知只含 delivery_id、project_key、task_id、sequence、event_type、status。接收方校验时间窗和 `X-Enterprise-AI-Signature`，签名为 `sha256=` 加 HMAC-SHA256(secret, timestamp + `.` + 原始 body bytes) 的十六进制值。仅 2xx 确认投递；必须按 delivery_id 持久去重，不重跑 Task。

### 投递排障

`GET /v2/projects/{project}/tasks/{task_id}/deliveries?limit=50&cursor=...` 返回 `{items,next_cursor,read_time,consistency}`，每页最多 200 条，按固定的事件序号和 delivery ID 排序。每页均重新检查 `task:read`、能力 Grant 和 Task owner；管理员可跨 owner 查询。游标绑定项目、Task 和读取范围，不是授权凭证，也不是跨请求快照。

只返回端点 Key、事件序号、投递状态、尝试次数、调度时间、最近 HTTP 状态与安全错误码；不包含通知正文、端点 URL、凭据引用或租约字段。`pending` 等待调度，`leased` 表示 Worker 持有投递租约，`delivered` 表示收到 2xx 确认，`dead` 表示当前投递流程终止。没有记录可能是尚未终态或当时未启用回调端点；不能据此判断漏投。

尝试次数包含可能未收到确认的发送；`http_status: null` 不等于接收方一定没有收到。`next_attempt_at` 仅作调度观测，不能承诺执行时间；终止状态下不代表还会重试。Task 成功、通知送达和业务采纳分别判断。可选参考控制台的任务详情提供按需读取的接入请求与交付面板；查看状态不会触发重发或执行。
