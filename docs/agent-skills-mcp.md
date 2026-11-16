# Agent 的 Skills、MCP 与 Pi

两者已经接入，但**不是将 Pi coding-agent CLI 或任意插件完整嵌入平台**。平台使用 `pi-agent-core` 驱动 Agent 循环，并保留模型、工具、授权、期限和执行记录的控制权。

```text
Agent 固定版本
  ├── instruction_prompt + skills → 平台解析、检查 skill:use → Pi 的系统上下文
  └── tools → 平台已批准工具目录 → Pi 决定调用
                                  → Broker → Tool Gateway
                                    ├── 安装的 Python 只读工具
                                    └── MCP 适配器 → 经审核的远程 MCP Tool
```

Pi 不持有模型或 MCP 的凭据，也不能自行安装工具、改变连接或扩大授权。Skill 是行为指引；工具请求才是需要执行和留证的外部操作。

## 当前支持范围

| 能力 | 已支持 | 尚未支持 |
|---|---|---|
| Skill | 本地 `SKILL.md` 单文件导入、发布固定版本；最多 8 项；启动时全文加入上下文；每次模型调用前、响应后和最终交付前重检授权 | 名称/描述路由、按需加载、附属文件读取、自动运行脚本 |
| MCP Tool | 项目绑定的审核目录；固定工具及 Schema；可选 Bearer 凭据；结构化结果或显式纯文本包装；受治理的只读调用 | 任意服务器即插即用、自动授予所有远程工具、stdio、SSE、OAuth 交互、resources/prompts、sampling、服务器发起请求或 MCP Task |
| Pi | 接收平台提供的系统上下文和工具描述，驱动模型—工具—模型循环 | 自动读取工作站的 Pi/Claude/OpenCode 配置、Skill 目录、MCP 连接或凭据 |

MCP 当前协议 profile 是 `streamable_http_json` / `2025-06-18`。服务器须支持 JSON 响应；默认使用固定 Schema 的对象型 `structuredContent`，无输出 Schema 的纯文本工具可显式选择下述 `result_mode: "text"`。图片或 SSE 响应尚不支持。只读是部署方审核承诺，不能仅凭远程 `readOnlyHint` 推断可信。

此前 weather-query 示例使用的是普通 HTTPS 工具，不是 MCP；其真实验收不能代替 MCP/Skill 联合链路验收。

## 1. 发布 Skill

最简单的方式是准备一个自包含的 `SKILL.md`：

```markdown
---
name: weather-report-guide
description: 查询并说明天气结果时使用
---

需要天气资料时使用 lookup_weather 工具。
只根据工具结果回答，保留资料时间；没有数据时明确说明，不要编造。
```

```bash
# 离线预览：不连接数据库或模型
enterprise-ai skill-export --skill-file /path/to/weather-report-guide/SKILL.md

# 发布到已有项目，随后可由 Agent 的 skills 引用
enterprise-ai skill-publish --skill-file /path/to/weather-report-guide \
  --skill-version 1 --project demo --config-dir /private/platform-config \
  --api-key-env PLATFORM_API_KEY
```

`--skill-file` 可指向 Markdown 文件或包含 `SKILL.md` 的目录；`--skill-key` 可覆盖 frontmatter 的 name，`--skill-version` 默认为 `1`。无 frontmatter 的普通 Markdown 也可导入，但须显式提供 `--skill-key`。导入仅读取该文件正文；不会扫描其他目录、读取附属文件、运行脚本或按 `allowed-tools` 自动授权。name/description 保存为能力元数据，正文保持为已发布的指引。只支持现有 32,000 字符的 Skill 正文限制。

也可继续通过 Capability API 创建 `kind: "skill"`，例如 `weather-report-guide`，再创建并发布版本 `1`：

```json
{
  "version": "1",
  "definition": {
    "instructions": "需要天气资料时使用 lookup_weather 工具。只根据工具结果回答，保留资料时间；没有数据时明确说明，不要编造。"
  },
  "dependencies": []
}
```

这是静态指引，不会自动创建 `lookup_weather` 工具或获得任何权限。Skill 更新须发布新版本，再由 Agent 新版本显式引用。

## 2. 部署 MCP Tool

下面只演示配置结构；`mcp.example.com` 是占位地址，**不是已验证的天气服务**。管理员须核对真实服务器支持的协议、工具名及完整 Schema。

将审核目录保存在部署私有配置中，不提交真实端点或凭据：

```json
{
  "connections": [{
    "project_key": "demo",
    "key": "weather-service",
    "endpoint": "https://mcp.example.com/mcp",
    "credential_ref": "weather-mcp-key",
    "transport": "streamable_http_json",
    "protocol_version": "2025-06-18"
  }],
  "tools": [{
    "project_key": "demo",
    "connection_key": "weather-service",
    "handler_key": "mcp.weather",
    "remote_tool": "weather",
    "operation_type": "read_only",
    "input_schema": {
      "type": "object",
      "properties": {"city": {"type": "string"}},
      "required": ["city"],
      "additionalProperties": false
    },
    "output_schema": {
      "type": "object",
      "properties": {"forecast": {"type": "string"}},
      "required": ["forecast"],
      "additionalProperties": false
    }
  }]
}
```

目录不包含密钥值。需要鉴权时，凭据由 Worker 的 `EnvironmentCredentialResolver` 按项目和引用解析；可用 `infra.model_credentials.credential_variable_name("demo", "weather-mcp-key")` 获取环境变量名，再由秘密管理设施注入。**公开、无鉴权的 MCP 服务直接省略 `credential_ref`**，此时不会读取凭据或发送 Authorization 头。若已配置凭据引用却找不到其值，仍然报错，不会自动退回无鉴权模式。

若远程工具没有 `outputSchema`，仅通过 `content` 返回文本，可在该 tool pin 上配置：

```json
{
  "result_mode": "text",
  "output_schema": {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": false
  }
}
```

这是 tool pin 的字段片段，需与其余 `project_key`、`connection_key`、工具名及输入 Schema 一起配置。平台把文本块以换行连接为 `{"text": "..."}`，不会猜测或解析文本里的 JSON；平台 Tool 能力的输出 Schema 也使用这个包装 Schema。错误结果、空 content、非文本内容仍报错。默认 `result_mode: "structured"` 行为不变。

```bash
enterprise-ai check-mcp-catalog --mcp-catalog /private/mcp-catalog.json
```

此命令只校验文件，并输出每项的 `handler_key` / `handler_version`；**不连接服务器，不代表已通过连通性或远程 Schema 检查**。handler version 是连接和工具配置的摘要，不是 MCP 服务器的版本号。目录配置变化后，需要更新相应 binding；不能沿用旧摘要冒充兼容。

管理员创建项目内的 Tool binding：

```json
{
  "key": "weather-mcp-binding",
  "name": "Approved MCP weather",
  "handler_key": "mcp.weather",
  "handler_version": "REPLACE_WITH_CHECK_MCP_CATALOG_HANDLER_VERSION",
  "operation_type": "read_only",
  "max_timeout_seconds": 20
}
```

将版本占位符替换为命令输出的完整摘要。然后创建、发布 `kind: "tool"`、Key 为 `weather-lookup` 的能力，版本内容使用与审核目录**完全一致**的 `input_schema` / `output_schema`，定义为：

```json
{
  "definition_version": "tool.v1",
  "binding_key": "weather-mcp-binding",
  "operation_type": "read_only",
  "timeout_seconds": 20.0
}
```

## 3. 在 Agent 上同时配置

先发布 `weather-instructions` Prompt 和 `weather-turns` Agent-turn 模型策略，再创建 `kind: "agent"`、Key 为 `weather-agent` 的版本：

```json
{
  "version": "1",
  "definition": {
    "definition_version": "agent.v1",
    "runtime": "pi",
    "instruction_prompt": {"key": "weather-instructions", "version": "1"},
    "model_policy": {"key": "weather-turns", "version": "1"},
    "skills": [{"key": "weather-report-guide", "version": "1"}],
    "tools": [{
      "alias": "lookup_weather",
      "capability": {"key": "weather-lookup", "version": "1"}
    }],
    "limits": {"max_turns": 4, "max_tool_calls": 2, "timeout_seconds": 60.0}
  },
  "dependencies": [
    {"key": "weather-instructions", "version": "1"},
    {"key": "weather-turns", "version": "1"},
    {"key": "weather-report-guide", "version": "1"},
    {"key": "weather-lookup", "version": "1"}
  ]
}
```

Agent 的输入输出 Schema 仍由业务能力显式定义，以上省略。MCP endpoint、credential_ref 和远程工具名属于部署目录，不放在 Agent 定义或 Task 输入里；Agent 只固定平台 Tool 及其模型可见 alias。平台目前提供通用工具描述，具体用途、参数解释和使用条件应在审核过的 Agent/Skill 指引及固定输入 Schema 中写清楚，不自动信任远程说明。

服务身份需要该资源的 `task:submit` / `task:read`、`prompt:render`、`model:invoke`、`skill:use` 和 `tool:invoke` Grant；Workflow 中的子 Agent 另需 `agent:invoke`。配置 Skill 不自动授权其提到的任何工具。

```bash
enterprise-ai worker --config-dir /private/platform-config \
  --executors agent --mcp-catalog /private/mcp-catalog.json
```

如果同时运行 Workflow，显式选择 `--executors workflow,agent --workflow-agents`，并按需添加 `--hub-bundles`。Pi 进程仍只接收审核后的上下文和工具描述；MCP I/O 留在 Python Gateway 路径，继续使用 Task 的身份、期限、取消和 Agent 工具账本。

## 验证与后续边界

检查 Skill 是否进入实际模型上下文、Pi 是否调用对应工具、MCP 会话是否结束、授权撤回是否阻止后续调用，以及 Agent/Tool/Model 记录是否匹配；不能仅以配置校验通过就宣称可用。

Skill 附属文件与按需加载、更多 MCP 传输类型仍是后续扩展，不应通过开放 Node 文件系统、任意命令或让 Pi 持有连接凭据来绕过现有 Gateway。
