# 文档

此目录是经审阅可进入授权 remote 的公开指南，描述当前框架、使用契约和产品级路线图；不存放内部评审、需求全文、环境事实或交接记录。当前定位为生产级设计的 prototype，不是生产验收声明。

| 读者 | 推荐顺序 |
|---|---|
| 首次了解平台 | [项目 README](../README.md) → [功能与架构](overview.md) |
| 业务接入开发者 | [API 使用](api.md)，从纯规则任务开始 |
| 独立前端开发者 | [前端接入契约](frontend-integration.md) → [API 使用](api.md)；先离线导出 OpenAPI |
| 部署与运维人员 | [部署与运维](deployment.md)，先准备独立数据库与私有配置 |
| 平台贡献者 | [贡献说明](../CONTRIBUTING.md) → [开发与仓库规范](development.md) |
| 规划后续建设 | [平台建设路线图](roadmap.md)，区分已具备能力、待办与待确认事项 |
| 整理或准备提交文件 | [文档与文件分发策略](repository-policy.md)，区分 remote、本地与发行物 |

接口字段与响应以运行服务的 `/docs` 和类型契约为准。文档中的地址、身份、任务输入和路径均为示例，不包含可用的部署配置或凭据。

公开 Markdown 使用明确白名单，其他 Markdown 与结构化配置默认不放行。新增公开文档须同时审阅内容并更新 `.gitignore` 和 `scripts/repository_policy.py`。内部资料优先保存在被忽略的 `.local/`；公开指南不链接这些私有文件。完整规则见[分发策略](repository-policy.md)，日常开发入口见[开发规范](development.md)。
