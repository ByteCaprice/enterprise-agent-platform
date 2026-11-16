# 开发与仓库规范

## 当前工作重心

平台是生产级设计的 prototype。优先关注领域划分、状态权威、稳定接口、扩展点和失败语义；不把补齐部署流水线或扩大测试数量当作当前主目标。

- `interfaces` 装配 `orchestration` 用例；`framework` 纯算法和 `infra` 外部适配各自依赖 `domain`，二者不互相导入。`domain` 不依赖其他平台模块。
- `capability_hub/` 是同仓库业务开发层，维护业务 Agent、代码 Workflow、确定性步骤和只读工具。平台核心不反向导入它；仅 `interfaces` 显式装配选定包。开发、源码摘要、发布和执行契约见[业务能力 Hub](capability-hub.md)。
- 当前代码流程工作是尚未发布的 `workflow.code.v1` 活动契约细化：用 `CodeActivity` / `activities` 声明资源和预算，由 Python 控制阶段与分支；不改固定图 DSL 的 `nodes`，不引入新协议版本或执行引擎。公共调用使用 `ctx.call`，并行/映射仅走框架拥有、负责取消和回收的结构化子任务，不对公共调用使用裸 gather。
- frame 与调用通过父级和 scope 进入同一节点账本；重复调用有独立哈希身份，后代共享根 Task / Attempt / 租约、期限及总节点预算。required、每次实际调用的 outcome、终态 Agent 和结果凭据都须完成校验；unavailable 不等于业务“clear”，也不能隐藏授权、租约、取消、账务、配置或未知检查点故障。
- `remitter_search` 仅演示调用方候选名称搜索；历史业务代码仅作需求例子，不做迁移、移植或业务功能扩张。业务规则和证据契约属于业务包，不加入平台核心。全部 Agent 仍用 Pi，Gateway 及 `SourceManifest` 的 Workflow/工具独立实现角色不变；此次细化不包含运行环境验证声明。
- Task/Attempt 是执行和最终结果权威；运行时、节点、调用证据和交付不能建立竞争状态机。
- Worker 只依赖 `domain/task_worker.py` 的显式执行端口，不导入具体存储、HTTP 或授权服务装配；实际依赖由进程入口安装。
- Executor、Broker、Model/Tool Gateway 和策略解析器使用 `domain/execution_ports.py` 的限定依赖，不保留 PlatformStore，也不穿透其他服务取得仓储。模型策略解析用组合而非 Gateway 继承；Prompt 渲染、生命周期与有界校验从入口注入。账本端口仍对应原来的原子 fenced 命令，不能因接口变窄拆开账务提交。
- Worker 清单从实际安装的 descriptors 和业务实现引用构造；能力要求由固定制品及相关依赖的封闭语义推导。领取前精确匹配 profile，以及代码能力的实现 Key/版本/来源摘要，不用角色布尔值、同名注册或隐式版本兼容代替；裸函数注入也须显式声明能力。缺少支持不消耗 Attempt。profile 是 ABI，来源摘要只覆盖显式文件清单，二者都不是完整构建证明。
- Task/Gateway/评测接入使用项目/owner 限定的 admission Unit of Work；不在服务间传递 SQLAlchemy Session，不在项目写锁内运行验证子进程。幂等复用在准备前和最终事务都检查，准备失败不能覆盖已并发受理的相同请求。
- 评测评分、数据集完整性与比较规则保持独立于 ORM；锁外评分之后必须核对待保存快照，不能让批次任务半提交或用旧任务证据覆盖新评分。
- Task 完成策略位于 `domain/task_completion.py`，ORM 证据读取与 fenced 提交位于专用完成仓储。隔离 Schema 匹配不得放回完成事务的写锁窗口；提交前仍须核对校验快照、固定制品、实时状态和最终数据库 fence。
- 事件追加、审计、预算和交付职责分开命名；跨职责的必要效果由显式事务组合入口组织，不在审计或日志辅助函数中隐藏。不要为了分层将原子终态操作拆成多个独立提交。
- 远端请求发送、响应到达、内容接纳、Usage 与重试许可分开建模。模型调用自身保存 `OutboundEvidence`；通用日志只用于观察，不成为重试状态机。内容拒绝时保留已知 Usage，未知结果不自动重放，旧重试布尔值不能作为许可。
- 进程资源由 `interfaces/runtime.py` 管理；非 HTTP 命令不通过创建 FastAPI 应用获取存储，创建失败和正常退出均须释放资源。
- 能力版本、执行计划与安装目录有明确的不可变边界；实际资源授权仍在调用位置检查。
- 能力语义和依赖角色由 `domain/capability_definitions.py` 统一解释；新增语义先定义版本及兼容范围，再接入发布、计划、执行与恢复，不在各服务重复实现条件分支。Schema 检查通过注入的有界端口执行，不在发布写锁内启动隔离验证器。
- 发布前的校验快照不是永久授权；存储事务必须重新核对根制品与直接依赖的完整内容身份。修改规则执行或映射语义时评估 interpreter/checkpoint profile，不能仅保持内容 hash 不变就复用旧检查点；profile 也不能冒充源码构建摘要。
- 未实现的等待、审批、外部 I/O 恢复等能力明确拒绝，不用占位成功或隐式重试冒充支持。
- 组织身份、数据留存、财务和业务副作用政策须明确提供，不能用演示值代替。

## 环境和必要检查

独立前端不需要导入 Python 业务模块或复制参考控制台。对接依据见[前端接入契约](frontend-integration.md)，使用 `enterprise-ai export-openapi` 从同版本源码导出 schema；不需要连接数据库、安装 Pi 或提供真实凭据。

使用团队约定的 Python 3.11+ 环境；Pi 执行需要 Node 22.19+ 和适配器目录中的 npm 锁文件。依赖变化同步 `pyproject.toml` 与公开来源锁文件，不将私有索引配置写进项目元数据。

```bash
python -m ruff check <本次修改的文件>
python -m pytest --noconftest -q tests/test_task_contracts.py tests/test_layering.py
# 或运行全部离线测试；数据库测试被排除，不会连接数据库：
python -m pytest -m 'not database' -q
```

只运行与修改范围对应的小型检查，不自动调用付费模型、真实工具或业务系统。数据库验证另行授权，只操作专用隔离 schema；不自动迁移持久应用环境。不要为通过检查而放松授权、租约、幂等或证据约束。

数据库测试使用 `--sit-config=/path/to/private-test-config.txt` 显式选择 SIT 测试环境，仅创建和清理会话拥有的随机 schema。未提供该选项时数据库测试跳过，不自动启动 Docker 或连接任何默认数据库。测试只装配平台 metadata，不依赖本地业务配置、插件目录或私有 demo。

## 文档与文件分类

完整分类以[文档与文件分发策略](repository-policy.md)为准。

| 目的 | 放置位置 | 分发方式 |
| --- | --- | --- |
| 稳定框架说明、API 与使用指南 | 文档导航中的公开文件 | 内容审阅后可进入授权 remote |
| 内部设计、需求对照、会议/工作记录 | `.local/` 等受控本地位置 | Git 忽略，不复制进公开指南 |
| 实际环境、仓库镜像认证、凭据与策略 | 仓库外配置或受控密钥系统 | 不提交，不打包 |
| 客户样本、截图、原始记录、备份与导出 | 受控本地/数据存储 | 无明确授权不分发 |
| 审核过的空白模板、公共锁文件和运行资产 | 明确放行的路径 | 同时遵守 Git、构建上下文和发行物边界 |

根目录采用白名单；文档、结构化配置、脚本也有独立的审核名单。不要把新文件换一个目录或改成 `.example` 名字，就假定适合公开。

新增公开文件时，核对 `scripts/repository_policy.py`、`.gitignore` 及适用的 Docker/发行物规则。内部资料继续保留本地，不需要为“整理”删除；平台代码和测试必须能仅从审核后的仓库内容导入，不能依赖被忽略的本地模块。

## 提交前检查

```bash
python -m scripts.check_repository_files
git status --short -- .
git diff --cached --stat -- .
```

检查器只读，核对公开文档链接、保护路径、候选内容、索引 blob、依赖来源和可识别凭据；不输出疑似秘密原文，不代替人工分类或完整密钥扫描。

已经跟踪的文件不会因新增忽略规则而退出索引；工作区变干净也不表示暂存内容已经清理。若发现私有文件，先确认影响范围再撤下索引，保留本地副本；已泄露凭据需撤销/轮换，历史处理另行授权。

本项目可能嵌套在父 Git 仓库内，先确认 `git rev-parse --show-toplevel`，不要使用会包含相邻项目的批量操作。禁止 `git add -f` 绕过保护；检查通过不自动授权 commit、push、发布、迁移或部署。
