# Weather query：真实外部数据与代码 Workflow

`capability_hub/weather_query/` 是可运行的天气查询示例，不是平台内置业务规则。它用于验证混合流程：确定性处理、有界并行、重复子流程、真实只读 HTTP 工具和共享 Pi Agent。

```text
输入校验
  → 按城市并行调用 weather-observation 子流程
      → Tool Gateway → 城市定位与真实天气预报 API
  → Pi Agent → Model Gateway → OpenRouter 等已批准模型
  → 确定性结果组装与校验
```

所有子流程共享根 Task / Attempt / 租约；城市调用有独立身份和证据。模型只解释工具提供的数据，结构化天气数值由代码从工具结果保留，不由模型重新生成。

## 数据来源与边界

- 城市定位：[Open-Meteo Geocoding](https://open-meteo.com/en/docs/geocoding-api)，可使用国家码缩小范围；示例采用供应商返回的首个匹配并保留解析结果，不保证同名城市无歧义。
- 天气：[MET Norway Locationforecast](https://docs.api.met.no/doc/ForecastJSON)。结果是带目标时间的天气模型预报，**不是气象站实测，也不保证实时**；保留供应商更新时间、预报时间、抓取时间及单位。
- 使用固定 HTTPS 端点，不接受调用方指定 URL，不自动重试外部调用。部署方须遵守供应商的使用条款、署名和限流要求；持续使用 MET Norway 前，应通过 `WEATHER_QUERY_USER_AGENT` 配置包含部署方真实联系信息的标识。
- 找不到城市属于明确业务结果，不等于上游不可用。上游失败只能按活动声明返回 unavailable；不能伪造天气或把失败当作“晴天”。全部城市无可用数据时，不调用模型。

## 发布与安装

先在目标项目创建、发布 `interaction: "agent_turn"` 的模型策略，配置其 Provider、凭据引用和允许的模型。示例不携带某个环境的模型或密钥。

```bash
enterprise-ai hub-export --bundle weather-query \
  --policy-key approved-weather-policy --policy-version 1

enterprise-ai hub-publish --bundle weather-query \
  --policy-key approved-weather-policy --policy-version 1 \
  --project demo --config-dir /path/to/platform-config \
  --api-key-env PLATFORM_API_KEY
```

管理员还需创建只读工具 binding：

```json
{
  "key": "weather-query.current",
  "name": "Public weather forecast",
  "handler_key": "weather-query.current",
  "handler_version": "1",
  "operation_type": "read_only",
  "max_timeout_seconds": 30
}
```

服务身份至少需要相应资源的 `task:submit`、`task:read`、`workflow:invoke`、`agent:invoke`、`tool:invoke`、`model:invoke` 和 `prompt:render` 授权；发布或安装不自动授予这些权限。

```bash
enterprise-ai worker --config-dir /path/to/platform-config \
  --executors workflow,agent --workflow-agents --hub-bundles weather-query
```

## 提交示例

通过 `POST /v2/projects/{project}/tasks` 提交，身份凭据放在请求头，且提供独立的 `Idempotency-Key`：

```json
{
  "capability_key": "weather-query",
  "version": "1",
  "input": {
    "locations": [
      {"id": "shanghai", "name": "Shanghai", "country_code": "CN"},
      {"id": "beijing", "name": "Beijing", "country_code": "CN"}
    ]
  }
}
```

通过 Task 的 `/result`、`/nodes`、`/agent-executions`、`/model-calls`、`/tool-calls` 和 Attempt 的 `/execution-tree` 检查结果及证据，而不是只看模型输出文本。

SIT 和 OpenRouter 的本地验收凭据、运行辅助脚本及原始验收记录保留在忽略目录，不进入示例源码或发布包。离线替身测试不能替代真实链路验收；真实调用是否通过以对应验收记录为准。
