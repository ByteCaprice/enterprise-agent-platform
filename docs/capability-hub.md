# 业务能力 Hub：Agent 与代码式 Workflow

`capability_hub/` 是同仓库的业务开发区。平台核心负责执行与治理，业务包维护自己的 Agent、Python 流程、处理步骤、只读工具、Schema 和 Prompt。不是把业务代码上传到数据库，也不要求每个 Agent 单独部署服务。

## 代码放哪里

```text
capability_hub/
├── catalog.py                 # 显式注册；不扫描目录自动加载
└── remitter_search/
    ├── agent.py               # Pi Agent 的资源与限制
    ├── workflow.py            # Python 业务编排
    ├── steps.py               # 确定性处理与结果校验
    ├── tools.py               # 供 Agent/Workflow 使用的只读操作
    ├── schemas.py             # 输入输出契约
    └── ...                    # Prompt、业务包及来源清单
```

按业务能力而非执行技术分目录；一个业务包可以同时包含 Agent 和 Workflow。新增物流或合规能力时创建相邻业务包，不把具体业务分支加入通用 Executor、Broker 或 Pi runner。没有真实业务系统接口或批准的政策时，不提供假查询、模拟审批或合规认证结果。

## 两种 Workflow

- `workflow.v1`：现有固定图 DSL，规则/模型/只读工具/Agent 节点。
- `workflow.code.v1`：显式安装的 async Python 流程，发布时固定实现 Key、版本、来源摘要及允许调用的活动（activity）。Python 决定阶段、循环、顺序和条件分支，平台记录实际调用及其层级证据。

本文描述尚未发布的 `workflow.code.v1` 活动契约细化：`CodeWorkflowDefinition.activities` 使用 `CodeActivity`，不再用 `CodeWorkflowNode` / `nodes` 描述代码流程。它不是新版本协议，也不是已发布流程的迁移或新的执行引擎；固定图 DSL 的 `nodes` 不受影响。以下是编写契约；单个示例的链路验证不代表整个平台已经生产就绪。

业务流程可以这样编写（活动必须与发布声明一致）：

```python
async def run(ctx, values):
    prepared = (await ctx.call("normalize", values)).require_value()
    reply = (await ctx.call("search", prepared)).require_value()
    checked = await ctx.call("validate", {"source": prepared, "reply": reply})
    return checked.require_value()
```

`ctx.call` 按声明的 `kind` 分派：`step` 调用本业务包显式安装的确定性处理函数；`agent`、`tool`、`llm` 复用已有执行适配器和 Gateway；`workflow` 调用固定版本的子代码流程。所有 Agent 仍共用 Pi 循环，Model/Tool Gateway 不变；业务定制优先放在工具、确定性步骤和流程组合，不复制运行循环。

### 活动声明与 Context

每个 `CodeWorkflowDefinition` 声明 1–99 个 Key 唯一的活动，`max_parallelism` 默认为 4，允许 1–16。声明是资源和调用预算的边界，**不是静态 DAG**，不决定执行顺序，也不表示每个活动只调用一次。

| `CodeActivity` 字段 | 契约 |
|---|---|
| `key` / `kind` | 唯一 Key；`kind` 为 `step`、`agent`、`tool`、`llm` 或 `workflow` |
| `capability` | 除 `step` 外必须固定能力 Key/版本；`step` 不携带该字段，使用安装的处理函数 |
| `prompt` | 仅 `llm` 可声明固定版本的 Prompt |
| `required` | 默认 `true`；当前流程 frame 内至少有一次非 skipped 调用 |
| `max_calls` | 默认 1，允许 1–1000；限制每个 frame 内该活动的调用次数 |
| `timeout_seconds` | 单次调用期限，只能缩短继承的剩余执行期限 |
| `on_unavailable` | `fail`（默认）或 `continue`；后者只接受下述明确允许的 unavailable 证据 |

发布声明只含固定资源引用和元数据，不含 Python 路径、源码或表达式。`workflow-result` 由平台保留作为结果凭据，不是业务活动 Key。

| Context 接口 | 返回与用途 |
|---|---|
| `await ctx.call(activity, values, *, key="once")` | 一次调用，返回 `ActivityOutcome` |
| `ctx.plan(activity, values, *, key="once")` | 创建不可变 `ActivityCall`，不启动工作 |
| `await ctx.parallel(mapping, concurrency=...)` | 接受名称到 `ActivityCall` 的映射，返回同名到 outcome 的字典 |
| `await ctx.map(activity, list_of_dicts, key=None, concurrency=...)` | 返回按输入顺序排列的 outcome 列表；`key` 可省略或提供 callable 生成调用 Key |
| `await ctx.skip(activity, *, key="once")` | 只允许显式跳过可选活动；返回 skipped outcome 并记录证据 |

阶段间依赖由 Python 表达：可以先 `await ctx.parallel(...)`，检查各 outcome，再按业务规则进入下一阶段；有界重复使用 `ctx.map` 或带不同 Key 的串行 `ctx.call`。并发必须使用框架拥有的结构化子任务，受 `max_parallelism` 和请求的 `concurrency` 约束，并在退出或失败时取消、等待回收。不要对公共 `ctx.call` 使用裸 `asyncio.gather`、`create_task` 或遗留后台任务。

`ActivityOutcome.status` 为 `succeeded`、`unavailable` 或 `skipped`，与业务值分离。只有 succeeded 携带业务字典；unavailable 携带安全 `error_code`，不是 `{}`、空业务结果或业务上的“clear”。`.require_value()` 返回成功字典，否则抛出异常；需要部分证据的流程应显式检查 status，而不是把缺少结果当作否定结论。

### 有界执行契约

- 每次实际调用都有独立身份，由流程 scope、活动 Key 和调用 Key 的哈希共同确定；默认 Key 为 `once`，同一 frame 内重复调用同一活动必须使用不同 Key。Key 是证据身份，不是重试或幂等复用许可。
- 活动的 `max_calls` 按 frame 计数；嵌套 frame 深度上限为 8，单个 Attempt 的全部节点总量上限为 1000，不能通过子流程绕过总预算。
- `required` 活动至少执行一次非 skipped 调用；每次实际调用都必须成功，或满足 `on_unavailable: "continue"` 的明确允许条件。一次成功不能掩盖另一次不允许的失败。可选活动只有显式 `ctx.skip` 才记录 skipped；从未调用的可选活动不生成隐式记录。
- continue 只适用于明确白名单内、调用证据与结算已安全收敛的模型或只读工具失败。认证、Grant、租约、取消、账务、配置和未知检查点故障始终致命，即使业务代码捕获异常也不能成功提交。结果未知的只读外部操作可以保留为 unavailable 证据，但绝不因此获得自动重放许可。
- 输入输出是有界、脱离业务对象的 JSON。全部后代共享根 Task / Attempt / 租约及继承的执行期限，不创建子 Task 或新租约；节点期限不能延长根期限。流程结束后不能再使用 Context 启动工作。
- 首阶段子流程只支持已发布、固定版本的代码 Workflow，不接受任意 Python 函数或 DSL 子流程。`workflow:invoke` 在调用处重新检查，且其授权约束沿后代继承；嵌套不能扩大模型、工具、Agent 或 Workflow 权限。
- 结果先校验、再写入保留凭据；Task 成功提交时重新核对层级、所有实际调用、终态子 Agent 和结果摘要。节点和 Pi 会话不拥有最终 Task 状态。
- 不支持代码 Workflow 的检查点恢复、未知外部调用自动重试、持久等待或审批。不能因为代码是确定性控制流就推断外部操作可安全重放。

业务步骤只获得项目、提交主体、Task/Attempt/节点标识，不获得数据库、租约凭据或管理员能力。外部访问通过受治理的工具；不要在流程或步骤中直接调用模型 SDK、数据库或网络。业务代码是部署方审核的可信代码，**不是 Python 沙箱**；接口限制不能阻止恶意源码自行导入网络库。

## 源码、发布版本与执行安装

三者职责不同：

1. Git 中的业务包是开发来源；显式来源清单计算包内审核文件的摘要。
2. 发布把 Schema、Prompt、资源依赖及实现引用保存为不可覆盖的能力版本。
3. Worker 显式安装指定业务包；领取前要求运行语义以及全部相关实现角色、Key/版本/摘要精确匹配。Workflow 实现与工具 handler 分开匹配，不能用同名/同摘要的另一类代码冒充。缺少或摘要不同的代码不会消耗 Attempt。

来源摘要与 interpreter/profile 分开：profile 表示语义 ABI，不表示源码相同。来源清单只覆盖明确列入的本地包文件，不是 Python 解释器、第三方依赖、环境、加载字节码和整个镜像的完整构建证明。新增共享业务模块时须纳入清单和发行物审核；受信任装配还须保证函数来自所声明的实现。部署文件应只读，不依赖运行中修改源码实现热更新。

修改业务源码或 Prompt 后重新计算摘要并创建新版本；不要用相同 Key/版本替换已发布内容。安装新代码、发布能力、启用资源和授予调用权限是分别进行的操作，任何一个都不自动授权其他操作。

## 查看、发布与运行

离线检查不连接数据库、模型或工具：

```bash
enterprise-ai hub-list
enterprise-ai hub-export --bundle remitter-search \
  --policy-key approved-agent-policy --policy-version 1
```

先在目标项目显式创建并发布 `interaction: "agent_turn"` 的模型策略，再以管理员身份发布业务包。管理员 Key 从指定环境变量读取，不放在命令参数或业务源码中：

```bash
enterprise-ai hub-publish --bundle remitter-search \
  --policy-key approved-agent-policy --policy-version 1 \
  --project demo --config-dir /path/to/platform-config \
  --api-key-env PLATFORM_API_KEY
```

发布只创建/复用内容一致的能力与版本，不覆盖冲突、不切换已有默认版本（首次发布沿用平台默认版本设置）、不安装代码、不创建 Provider、工具 binding 或 Grant。整包发布不是一个跨资源事务；预检已知冲突后按依赖顺序提交，失败可能留下已发布的前序资源，修正原因后重跑会复用一致内容。

工具 binding 由管理员另行审核并通过 `/v2/projects/{project}/tool-bindings` 创建。代码工具使用 `tool.code.v1`，固定实现 Key/版本/摘要；binding 的 handler Key/版本必须与其一致，实际调用时还会核对已安装实现。发布工具定义不代表 binding 已配置或主体已获 `tool:invoke`。

显式启动业务 Worker：

```bash
enterprise-ai worker --config-dir /path/to/platform-config \
  --executors workflow,agent --workflow-agents \
  --hub-bundles remitter-search
```

所有 Agent 仍使用共享 Pi 适配器，要求锁定的 Node/Pi 依赖。未指定 `--hub-bundles` 的 Worker 不安装业务包；API 进程不运行它们。选中业务包也不会自动启用 Agent 执行角色或内嵌 Agent，需明确提供相应参数。

业务系统仍通过已有 Task API 指定能力 Key 与固定版本提交，不直接导入 Hub 或共享数据库。服务身份仍需对应的 `task:submit`、`task:read`、`workflow:invoke`、`agent:invoke`、`model:invoke`、`tool:invoke` 等授权。实际要求以调用到的资源为准。

## 新增自己的业务包

1. 新建 `capability_hub/<业务名>/`，按需拆分 `agent.py`、`workflow.py`、`steps.py`、`tools.py`、`schemas.py` 和 Prompt。可参考 `remitter_search/bundle.py`，不要复制平台的 Broker、Gateway 或 Pi 循环。
2. 用 `SourceManifest` 明确列入全部业务源码/Prompt 依赖文件，生成固定 `CodeReference`；用 `InstalledCodeWorkflow` 注册 async 流程及 step 活动处理函数，用 `InstalledReadonlyTool` 注册只读工具。Workflow 实现和工具实现仍是独立角色，不能因来源文件相同而互相替代。
3. 发布包用 `CapabilityBundle` / `BundleCapability` 提供能力定义和固定依赖；模型策略由调用方显式指定，不把某个环境的 Provider、端点或秘密写进模板。
4. 在 `capability_hub/catalog.py` 的 `builtin_catalog()` 中显式加入业务描述符，并核对发行物资产清单。不要添加目录扫描、数据库 Python 路径导入或业务源码自动执行。
5. 离线检查、发布到目标项目、配置所需 binding/Grant，再在 Worker 上显式选择该业务包。业务源码变更创建新版本；旧任务仍需要匹配其原实现的部署。

参考包接受 `query`、`candidates`（每项 `id`、`name`、可选 `aliases`）及 `limit`，只对调用方给出的候选进行名称相似度搜索。`remitter-search-agent` 可单独作为 Agent Task 调用，其输出仍是模型建议；需要确定性候选归属验证时调用 `remitter-search`，它增加标准化、分支和代码校验。该包不连接实际汇款人目录，也不作身份核验、制裁筛查或合规结论。

`weather-query` 提供真实外部数据示例：并行调用城市天气子流程，再使用共享 Pi Agent 解释供应商数据。发布、安装、数据来源与请求示例见 [Weather query demo](weather-query-demo.md)。

既有业务实现可用于理解编排需求，但不自动构成迁移、移植或扩展范围。业务规则、证据要求和输出解释属于业务包；平台核心只提供通用活动执行与治理，不内置业务政策。

## 观察与存储边界

既有 `/tasks/{id}/nodes`、`/agent-executions`、`/model-calls`、`/tool-calls`、`/events` 和执行树继续使用。节点 API 通过 `parent_node_id` 表达层级，通过 `scope` 记录 `workflow_key`、`version`、`content_hash` 及调用的 `activity_key` / `invocation_hash`。流程 frame 使用 `kind: "result"`，实际调用使用 `step`、`agent`、`tool`、`llm` 或 `workflow`。节点终态 `unavailable` 保留部分证据，不是新增 Task 状态或业务结论；完成提交仍核对层级、终态 Agent 与结果凭据。观察接口不返回源码或处理函数。

`GET /capabilities/{key}/versions/{version}/plan` 对代码流程返回 `control_flow: "code"`、实现引用、`activities` 和 `max_parallelism`，不伪造 Python 条件分支的静态 DAG。前端不能据声明顺序推断执行顺序，也不能把声明数量当作实际调用数量。

尚未发布的 `0003_business_code` 扩展节点 kind/status、scope 和父级约束，并允许 `workflow:invoke` 资源授权；冻结的 `0001_platform` / `0002_outbound_evidence` 不变，不修改已有能力或业务数据。应用不会自动迁移；发布前仍须针对目标环境显式升级和验收。只读工具的限制继续成立；业务写操作需另行定义授权、幂等、结果确认和失败处置契约。
