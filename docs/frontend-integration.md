# 前端接入契约

本文面向独立开发前端的同事，说明当前后端能提供什么、页面如何调用，以及暂不能假定具备的能力。不限定 React/Vue、组件库、路由或前端仓库结构；字段以同一版本的 OpenAPI 为准，业务调用示例见 [API 使用](api.md)。

## 1. 分工和契约来源

- 前端负责页面、交互、表单提示、请求状态、取消请求等待及缓存管理；后端负责认证、授权、版本发布校验、任务执行、并发与结果权威。
- `interfaces/http/console/` 是参考客户端，不是正式前端的组件库或必须复制的产品设计。正式页面只依赖 `/v2`，不要依赖控制台 DOM、数据库字段或 Python 私有方法。
- 默认后端不挂载 `/` 的控制台跳转或 `/console/` 静态资源。仅作开发参考时，可显式使用 `serve --reference-console`；Session 和所有业务接口均不依赖它。
- 同一任务的执行状态、Callback 交付、评测结果和业务采纳分别展示，不合并成一个“成功”字段。
- OpenAPI 使用 HTTP 方法和路径派生 `operationId`，不依赖 Python 函数名。修改路径、枚举、响应或状态语义时，前后端须同步更新契约，不能仅靠前端兼容猜测。
- 联调以明确的后端提交和对应 schema 为基准，不能只看 OpenAPI `info.version` 的 API 大版本。后端交付接口、类型和语义说明；前端交付页面实现。在约定契约一致的前提下，不要求两边共享源码或使用同一构建制品。
- 缺少响应字段、权限元数据或查询能力时，由后端补齐契约并说明边界；不要要求前端连库、调用参考页面内部实现，或通过本地 mock 填补真实后端能力。

无需数据库、配置文件、管理员 Key、Node 或模型服务即可导出接口定义：

```bash
mkdir -p .local
enterprise-ai export-openapi > .local/openapi.json
# 未安装命令行入口时：
python -m interfaces.cli.platform export-openapi > .local/openapi.json
```

后端运行时也提供 `/openapi.json` 与 `/docs`。离线导出与运行服务共用业务路由、认证声明和响应模型；离线文档不包含静态控制台及健康探针。导出不是启动一个绕过认证的模拟后端，也不提供伪造执行结果。

前端可据此生成类型或构建合成 mock，但 mock 不代表后端已执行、权限已通过或模型实际可用。当前 Session、项目/身份/Key/Grant、能力/版本、Prompt 渲染、Task、取消、事件、执行树与任务交付等主要链路已有响应模型；部分资源管理/运营接口仍使用通用 JSON 响应，不能把空 schema 当作“接口没有数据”或“可提交任意字段”。这些页面联调前应补齐对应响应契约。

## 2. 地址、代理和认证

### 地址

推荐使用可配置的 API base 和同源 `/v2` 路径。独立前端开发服务器可代理 `/v2` 至后端，保留路径、请求体、认证头及响应头，不依赖后端提供前端路由回退。

当前后端**不配置跨源 CORS 放行，也不使用 Cookie 登录**。不同端口也属于跨源；直接从另一个源发请求会失败。不要用 `no-cors`、`*` 或转发管理员凭据到任意地址来绕过。确需直连跨源时，先约定明确 Origin、允许的请求头和暴露的响应头，再由后端提供配置。

### 认证与身份

每次业务请求二选一，不能同时发送：

```http
x-api-key: <当前平台身份的 Key>
```

或：

```http
Authorization: Bearer <已由后端明确绑定的 OIDC JWT>
```

`GET /v2/session` 返回：

```json
{
  "name": "service:example-identity",
  "is_admin": false,
  "project_key": "demo",
  "identity_id": "example-identity"
}
```

- 管理员的 `project_key` / `identity_id` 可为 `null`；项目列表 `/v2/projects` 仅管理员可读。服务身份使用 Session 中的项目，不要先调用管理员项目列表作为进入系统的前提。
- Session 不是完整权限矩阵，`is_admin: false` 也不表示没有操作权限。当前没有面向前端的有效 Grant 汇总接口；按钮可先按功能组织并正确处理 403，精确按钮级权限需再约定读取契约，不能从 JWT roles/groups 或页面可见性推断授权。
- 当前没有用户名密码登录、平台签发的访问/刷新令牌、完整用户/成员 RBAC 或通用退出接口。页面退出是清除本页凭据、敏感数据和请求；不等于撤销平台 Key。撤销 Key 是管理员操作。
- OIDC 主体绑定的是服务身份，不是完整的企业用户权限模型。多个业务用户共享同一服务身份时会共享 Task owner 范围；BFF 不能只转发同一 Key 就宣称实现了用户隔离。
- 不将平台 Key、JWT、模型供应商 Key 或 Callback 签名密钥写入前端源码、构建环境公共变量、URL、日志、埋点或持久浏览器缓存。受控原型手工输入凭据时仅存页面内存；正式登录/BFF 方案另行确定。

## 3. 页面和接口的对应关系

项目资源以下均以前缀 `/v2/projects/{project}` 表示；`{project}` 是选定项目，不是任意 JWT claim。

| 页面 / 交互 | 接口 | 当前权限与交互边界 |
|---|---|---|
| 当前身份 | `GET /v2/session` | 认证后可读；切换凭据时重新读取 |
| 项目、服务身份、Key、Grant | `/v2/projects` 及 `/service-identities/...` | 管理员；Key 签发响应中的 `token` 只返回一次，不能假设列表可再次读取 |
| 能力列表和版本详情 | `/capabilities`、`/capabilities/{key}/versions` | `capability:read`；列表权限与 `task:submit` 分开 |
| 新建能力 / 草稿版本 | `POST /capabilities`、`POST /capabilities/{key}/versions` | `capability:write`；没有草稿覆盖保存接口，修改需新版本 |
| 发布 / 设置默认版本 | `POST .../{version}/publish`、`POST .../{version}/set-default` | `capability:publish`；发布不执行 Task，也不授权模型/工具 |
| Prompt 预览 | `POST /prompts/{key}/versions/{version}/render` | `prompt:render`；只渲染已发布的固定版本，不是模型试运行 |
| Workflow 计划 / 代码活动声明 | `GET /capabilities/{key}/versions/{version}/plan` | `capability:read`；DSL 返回静态图，代码流程返回活动元数据，不是执行树或可编辑保存模型 |
| 提交 Task | `POST /tasks` 或 `POST /tasks/execute` | `task:submit`；sync 还要求 `task:read` |
| Task 列表、详情、结果和证据 | `/tasks`、`/tasks/{id}/...` | `task:read` + 项目/能力/owner 限制 |
| 请求取消 | `POST /tasks/{id}/cancel` | `task:cancel`；取消意图不等于已停下或已远端回滚 |
| 恢复评估 / 显式恢复 | `GET .../recovery`、`POST .../recover` | 读取及新 Task 提交权限；只支持符合条件的纯规则 Workflow |
| Task 业务反馈 | `GET / POST .../feedback` | `task:read` 加对应 `feedback:read` / `feedback:write` |
| 人工质检 | `GET / POST .../reviews` | 读取需 `task:read`，提交还需 `quality:review` |
| 模型、工具资源及运营配置 | `/model-providers`、`/tool-bindings`、`/operations/...` | 多数管理操作要求管理员；质量、记忆等按各自授权，不按菜单名称推断 |

Task 详情是安全元数据，**不返回原始输入供前端自动重建表单**；结果通过 `/result` 按需读取。不要预设存在“编辑 Task 输入”“原地重跑”“Agent 会话继续”接口。

Key 签发使用 `IssuedServiceKeyResponse`（含一次性 `token`），查询/撤销使用 `ServiceKeyResponse`（不含 `token` 和 `digest`）。撤销通过 `revoked` 标记，过期单独看 `expires_at`，不要假设存在统一 `status` 字段。Grant 重复创建返回已有记录，不签发新的 Key；Key 签发本身没有客户端幂等键，响应丢失后不要自动重复签发，应由管理员核对、撤销或明确重新创建。

能力定义中的 `definition` 随 `kind` 不同而不同，当前 OpenAPI 中它是 JSON 对象，不是已穷尽的可视化编辑器 schema。第一阶段可使用明确 kind 的 JSON/表单编辑，再约定图编辑器；后端契约分别在 `domain/capability.py`、`domain/workflow.py`、`domain/platform_agent.py` 和 `domain/platform_tool.py`。前端不能自行发明等待、审批、写工具、脚本路径或恢复节点。

Workflow V1 可声明 `definition_version: "workflow.v1"`，省略也只表示 V1；节点 `max_attempts` 当前只能为 `1`。不支持的重试在定义校验阶段拒绝。静态计划中的 `compiler_profile`、`interpreter_profile`、`checkpoint_profile` 是后端兼容元数据，前端按字符串展示/保留，不把它们作为切换执行器或允许重放的参数。

`workflow.code.v1` 的计划返回另一类型 `CodeWorkflowPlanRecord`：`control_flow: "code"`，含固定实现引用、`activities` 和 `max_parallelism`。这是尚未发布的活动契约细化，不是新协议版本或已发布定义迁移，也不改变 DSL 的 `nodes`。活动声明包含 `kind`、固定资源、`required`、`max_calls`、`timeout_seconds` 和 `on_unavailable`；Python 控制阶段、实际次序、分支及有界并发，不能按声明数组顺序绘制确定的连线，或把一个活动画成必然只有一次调用。

执行观察仍通过既有 Task 接口：节点使用 `parent_node_id` 和 `scope` 区分流程 frame 与实际调用。scope 包含 `workflow_key`、`version`、`content_hash`，调用还含 `activity_key` / `invocation_hash`；这些 hash 按不透明字符串处理。frame 类型为 `result`，调用类型为 `step|agent|tool|llm|workflow`，不是子 Task。节点终态 `unavailable` 应独立展示安全错误码和部分证据，不表示新增 Task 状态、业务“clear”或可自动重试。skipped 只表示显式跳过；从未调用的可选活动没有记录，不应在客户端补造 skipped 节点。

Task 成功仍依赖后端核对全部层级、终态子 Agent 与结果凭据；前端不能根据局部 succeeded/unavailable 自行推断最终结果。前端不上传源码、安装业务包或选择 Python 函数，也不把声明转换为审批、持久等待或恢复编辑器。详细边界见[业务能力 Hub](capability-hub.md)；后端示例验证不能代替正式前端的联调和验收。

## 4. Task 的交互状态机

### 提交与幂等

一次用户确认的业务提交对应一个 `Idempotency-Key`，修改输入或明确创建另一个 Task 才使用新键。同一次提交超时、断网或重复点击时保留原键和原始载荷，不能自动换键重发。

| 操作 | HTTP 响应 | 页面应如何判断 |
|---|---|---|
| `POST /tasks` | 新 Task 为 202；相同键同内容复用为 200 | 返回 `TaskRecord`，按 `status` 显示；200 可能仍是 queued/running |
| `POST /tasks/execute` async | 202 | 返回 `{task,request,result,wait_timed_out}`；不启动额外执行 |
| `POST /tasks/execute` sync | 终态及结果读取完成为 200；等待超时为 202 | `wait_timed_out: true` 只表示停止等待，不是执行失败；`result` 在这里是结果记录，不是裸业务 JSON |
| `GET /tasks/{id}/result` | 可读时 200 | 先读 `status`，再取业务 `result`；失败或未完成时业务结果可为 `null` |
| `POST .../cancel` | 排队或已终态通常为 200；运行中保存意图为 202 | 返回 `{task,cancellation}`；继续查询实际状态 |
| `POST .../recover` | 新恢复 Task 为 202；同请求键复用为 200 | 跳转到新 Task；原 Task 和原结果不变 |

所有成功体均按接口直接返回，没有统一的 `{code,data}` 包装。`Location` 指向后续查询地址，`X-Request-ID` 是服务生成的请求编号；可选 `X-Correlation-ID` 用于关联，不是幂等键或授权依据。

统一请求层需要保留 HTTP 状态和响应头，不能只返回 JSON 正文而丢失 `Location`、`X-Request-ID`、`Retry-After`；头名称大小写不敏感。删除 Grant 等接口的 204 响应没有正文，不应调用 `response.json()`。

`sync.wait_seconds` 大于 0、最多 30 秒，省略时为 10 秒；HTTP 客户端等待上限应留出网络余量。浏览器 `AbortController` 或关闭页面只停止本地等待，不会取消 Task。超时/5xx/响应解析失败可能发生在服务已接受操作之后，应先查询，只有有明确幂等契约的操作才以原键和原载荷重试。

### 展示、轮询和取消

Task 只有 `queued`、`running`、`succeeded`、`failed`、`timed_out`、`cancelled`。`claimed` 不是公开 Task 状态。`succeeded` 表示技术结果通过校验并提交，不等于业务采纳、评测通过或通知送达。

Worker 领取前还要匹配固定制品所需的解释器、检查点、节点适配器和 Agent Runtime profile。不匹配时任务仍为 `queued`，不会仅因某个 Worker 缺少适配器就消耗 Attempt；前端不把排队改称执行失败，也不自动改版本、Runtime 或重提新任务。

- 可见的 queued/running 详情页可每 2–3 秒轮询状态和新增事件；避免并发重叠请求。列表按需刷新，不为所有行分别轮询详情。
- 页面隐藏、切换 Task/项目、退出或认证失效时停止轮询；丢弃旧选择/旧身份的迟到响应，不能覆盖当前页面。
- Task 到终态后停止执行状态轮询。Callback 仍可能 pending/leased，需要单独刷新，不以 Task 终态判断交付完成。
- 事件用 `after_sequence` 续读，按 `(task_id,sequence)` 去重，使用 `next_after` 推进；`has_more` 为真时继续分页，不能用本地数组长度作为服务端序号。
- 事件及执行树只含元数据，不是逐 token 流、SSE 或 WebSocket。执行树按 `kind/id` 与 `parent` 重建；分页未加载的父级不能当作证据损坏。
- 取消状态为 `requested` / `observed` / `acknowledged` / `unconfirmed`。202 或 observed 不能显示成“已取消”；unconfirmed 应提示未确认。最终以 Task 状态与取消回执共同说明，不能声称远端已回滚。

## 5. 分页和基础数据约定

当前存在三种明确不同的集合，前端 API 层应分别建模，**不要假定所有列表都有 total/page/page_size**：

| 类型 | 示例 | 返回形状与行为 |
|---|---|---|
| 数组 | Task、能力、版本、Attempt、节点列表 | `[...]`，没有总数或下一页；只有部分集合提供 `limit`，Attempt/节点接口不接受该参数；数量不能用作全项目 KPI |
| 元数据游标页 | 执行树、Agent execution/step、Task requests/deliveries | `{items,next_cursor,read_time,consistency}`；默认 100，最多 200；`next_cursor: null` 为本次续读结束 |
| 运营游标页 | `/operations/pages/{collection}` | `{items,next_cursor,limit}`；默认 50，最多 200；reviews 需 task_id，memory 需 capability/subject |

事件是第四种**增量序号流**：`{items,head_sequence,next_after,has_more,history_complete}`，不是 cursor page。切换项目、任务、Attempt、过滤条件或身份时重置相应位置。游标是不透明的查询位置，不要解析、拼接或跨集合复用；每一页重新授权，跨请求不是同一数据库快照。刷新投递状态时从第一页重读，不能只读后续页期待旧记录重新出现。

ID、版本和 hash 按不透明字符串处理，不转为数字。时间为带时区的 ISO 8601 字符串，展示时再转换本地时区。费用使用整数 `microunits`，不要用浮点数累计；金额、币种、未知费用和预留占额分别处理。`null` 与 `0` 不互换，例如并发上限 `null` 表示未设置，`0` 表示暂停新执行。查询 `business_id` / `customer_id` 是精确匹配，不是全文搜索或客户级授权。

## 6. 错误、空态和敏感数据

错误形状统一为：

```json
{"error":{"code":"FORBIDDEN","message":"Resource access denied","request_id":"example-request-id"}}
```

不要按英文 message 做业务分支，也不要按 FastAPI 默认 `detail[]` 解析 422；后端故意不回显拒绝的客户输入或字段原文。表单可做本地校验，但最终以服务端响应为准。网关、代理或网络错误可能不是此 JSON 形状，客户端仍需安全降级。

| 响应 | 前端处理 |
|---|---|
| 401 | 清除本页身份及敏感缓存，停止后台请求，提示重新认证 |
| 403 | 提示无权限或当前资源不可访问；不要换身份或换项目重试 |
| 404 | 不存在或对当前 owner 不可见，不以此泄露其他用户的任务存在性 |
| 409 | 幂等键异内容、版本冲突、修订冲突等；重新查询，不能自动覆盖或换键提交 |
| 422 | 载荷、状态组合或游标无效；修正请求，不循环重试 |
| 429 / 503 | 展示限流/暂不可用，读取 `Retry-After`（有时提供）；GET 可有界退避，写请求仍按各自幂等契约处理 |
| 413 / 414 / 431 / 415 | 请求体、URL、头或编码超限；当前不接受压缩请求体 |
| 5xx、网络超时或中断 | 不等于写操作未生效；保留提交上下文和可用的关联 ID，先查状态 |

空数组、403、尚未执行、没有已启用回调端点、暂时不可用应分别显示；不要用“暂无数据”掩盖所有错误。结果正文、Prompt、反馈原因、修订结果及一次性 token 不进入日志、埋点或错误上报。纯文本安全渲染，不将模型输出当作可信 HTML。

## 7. 与前端同事需要确认的事项

下列是尚需共同决定的产品/接入选择，不是已实现接口：

1. 正式前端独立仓库、路由前缀和 API base；开发代理还是明确 Origin 的跨源访问。
2. 受控试用是否手工输入 Key，还是接企业登录/BFF；具体用户映射与授权模型不能由前端自行推导。
3. 第一阶段页面范围：建议先做项目选择、能力/版本、新 Task、任务详情/执行树，再推进资源与运营配置。
4. 能力编辑先使用 JSON/表单还是可视化图；需要后端另提供哪些 kind 的编辑 schema。
5. 精确按钮权限、完整历史列表、总数/运营聚合、搜索、字段级错误和 SSE 等新增能力的真实需求；当前不要用前端计算或 mock 冒充已具备。

对接顺序建议为：同版本 OpenAPI → Session 与权限边界 → [纯规则 Workflow](api.md#第一个任务纯规则-workflow) 完整闭环 → 取消/冲突/断网空态 → 按范围接入模型、工具和运营页面。接口调整需同步本文、类型定义和实际响应，不能只修改参考控制台。
