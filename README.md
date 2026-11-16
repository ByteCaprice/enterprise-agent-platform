# Enterprise AI Platform

一个采用生产级职责与执行契约设计的企业 AI 平台原型，面向业务系统提供统一任务接入、Workflow / Agent、模型与工具治理、执行记录和质量能力。采用独立的领域契约、进程入口和数据库 schema。当前以框架收敛为主，不把功能数量或检查通过当作生产就绪证明。

## 主要功能

- 能力草稿、不可变版本、依赖校验、发布与默认版本切换。
- Project、ServiceIdentity、Key、资源 Grant 与显式 OIDC JWT 绑定。
- Task/Attempt、幂等接入、数据库租约、取消、结果与执行时间线。
- Workflow 规则/模型/工具/Agent 节点；受治理的 Pi Agent 执行。
- 同仓库 `capability_hub/` 可选示例包；固定源码引用的 Python Workflow、自定义处理步骤及共享 Pi Agent。
- 全局、项目、能力及 Agent 并发准入；预算预留与对账、签名 Callback、评测、业务反馈和受控记忆。
- 独立 HTTP API、运行健康查询与 Worker；可选的参考控制台。

## 开发与启动

要求 Python 3.11+、PostgreSQL；Agent 执行另需 Node 22.19+。

```bash
python -m pip install -e '.[dev]'
npm ci --ignore-scripts --prefix framework/agent_runtime/pi/node
```

运行前先按[部署说明](docs/deployment.md)准备独立数据库 schema、管理员身份及私有配置。程序不会自动建库或执行迁移。

```bash
enterprise-ai check-release
enterprise-ai check-config --config-dir /path/to/platform-config
enterprise-ai serve --config-dir /path/to/platform-config
# 单独运行 Workflow Worker；HTTP 进程不会自动执行任务。
enterprise-ai worker --config-dir /path/to/platform-config
```

默认 HTTP 地址为 `http://127.0.0.1:50020`，提供后端 API，OpenAPI 位于 `/docs`。正式前端独立开发；仅在需要参考客户端时使用 `enterprise-ai serve --config-dir /path/to/platform-config --reference-console`，显式挂载 `/console/`。

首次验证建议使用 [API 指南中的纯规则 Workflow](docs/api.md#第一个任务纯规则-workflow)，不需要模型或外部工具。接入真实模型和工具前，再配置对应资源与授权。

## 文档

| 文档 | 内容 |
|---|---|
| [文档导航](docs/README.md) | 按使用角色选择阅读顺序 |
| [功能与架构](docs/overview.md) | 核心概念、模块职责与执行边界 |
| [API 使用](docs/api.md) | 第一个任务、认证、查询与运营接口 |
| [能力 Hub](docs/capability-hub.md) | 代码目录、Python 混合编排、发布和显式 Worker 安装 |
| [Skills、MCP 与 Pi](docs/agent-skills-mcp.md) | 静态指引、受治理工具连接和运行配置 |
| [天气查询示例](docs/weather-query-demo.md) | 可选的多城市外部数据与模型组合示例 |
| [前端接入契约](docs/frontend-integration.md) | 独立前端的分工、OpenAPI、页面权限与异步交互 |
| [部署与运维](docs/deployment.md) | 初始化、启动、升级、监控与故障处置 |
| [开发与仓库规范](docs/development.md) | 代码结构、验证方法与提交规范 |
| [平台建设路线图](docs/roadmap.md) | 后续框架工作、待确认事项与验收目标 |
| [文档与文件分发策略](docs/repository-policy.md) | 哪些可进入 remote，哪些仅保留本地 |
| [贡献说明](CONTRIBUTING.md) | 修改代码和提交前的基本要求 |

## 发布与隐私

本仓库只维护通用源码、测试、公开文档和空白部署模板，不提供组织环境配置、真实凭据或客户样本。私有配置、个人工具数据和运行产物必须遵守 `.gitignore`，不能加入版本控制。

发布前运行 `python -m scripts.check_repository_files`，并审阅待提交内容、历史、提交身份及 GitHub remote。不要强制添加忽略文件，也不要直接上传整个工作目录。当前未指定开源许可证；公开发布前须确认代码权属、发布授权并选择许可证。第三方依赖的许可及署名要求仍须遵守。详见[分发策略](docs/repository-policy.md#公开发布)。

## 使用边界

当前版本适合受控试用，不代表生产验收。模型/工具访问、组织身份映射、价格、留存及网络策略必须显式配置。

外部调用不会因结果未知而自动重放；取消不保证远端回滚。Callback 至少一次，接收方须去重。记忆删除标记不等于物理擦除。平台数据库由 `infra/platform_migrations/` 管理，不通过业务 seed 初始化。

公开指南与内部材料分开管理，具体见[文档与文件分发策略](docs/repository-policy.md)。公开文档只描述稳定契约；内部设计、需求基线、环境记录、实际配置和客户材料保留本地。Git、Docker 构建上下文和 Python 发行物各有独立保护规则；通过检查不自动授权上传。
