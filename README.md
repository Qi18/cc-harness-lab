# cc-harness-lab

用 Python 手写一个 Claude Code 风格的 Agent Harness，理解模型调用背后的工具执行、上下文管理、任务协调与运行时控制。

模型负责理解目标、选择工具和生成回答；Harness 负责把这些决定变成可执行、可观察、带约束的流程。这个项目围绕同一个 Agent Loop，逐章加入权限、Hooks、Memory、任务图、队友、Worktree 和动态工具接入。

这是学习与实验项目，不是 Claude Code 的等价替代，也不是生产级安全沙箱。

[快速开始](#快速开始) · [章节导航](#章节导航) · [整体架构](s20_comprehensive_agent/README.md) · [评测方案](EVALUATION.md)

## 核心设计

```text
用户输入 / 定时任务
        ↓
收取后台通知与团队消息
        ↓
组装 System Prompt + 召回 Memory + 压缩上下文
        ↓
模型请求 ← 错误分类、重试与恢复
        │
        ├─ 工具调用
        │    → 参数解析 → 权限与 Hooks → handler
        │    → 追加 tool result → 返回模型
        │
        └─ 文本回答
             → 再检查异步消息 → Stop Hook
             → 提取 Memory → 返回用户

循环外围
  Subagent    独立对话中的同步委派
  Background  已批准工具的后台执行
  Cron        到期入队，与前台 turn 串行
  Teams       独立队友循环、邮箱与协议
  Worktree    绑定任务的独立目录与分支
  MCP mock    动态发现、schema 适配与调用路由
```

实现关注的是机制之间的边界：

- **模型协议与执行分离**：schema 决定模型能看到什么，handler 决定本地能执行什么。
- **能力按角色分配**：Lead、Subagent 和 Teammate 使用不同的工具集合。
- **消息配对完整**：每个 tool call 对应一个 tool result；后台完成通知单独注入，不重复使用原调用 ID。
- **状态各有归属**：Todo 管当前清单，Task 管项目任务，Mailbox 管通信，Worktree 管目录。
- **并发不直接改主历史**：队友与后台执行者先产出消息，Lead 在固定接入点统一收取。
- **章节累计、模块独立**：能力持续累积，循环、权限、压缩、记忆和调度各自保留清晰的源码入口。

详细的 turn 调用链、状态归属与交叉场景见 [s20 整体架构](s20_comprehensive_agent/README.md)。

## 快速开始

运行环境：Python 3.10+、支持 Bash 和 flock 的 Unix 环境；Worktree 功能还需要 Git。

```bash
git clone https://github.com/Qi18/cc-harness-lab.git
cd cc-harness-lab

python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt

# 首次配置；已有 .env 时不要覆盖
cp -n .env.example .env
# 编辑 .env，填写 DASHSCOPE_API_KEY，并检查模型与工作目录配置

# 运行累计 Harness
python3 s19_mcp_plugin/code.py
```

想从最小循环开始阅读和运行：

```bash
python3 s01_agent_loop/code.py
```

s20 是复用 s19 的集成验收与架构文档，没有额外的 code.py。

建议先使用受控的测试目录作为 CC_WORKDIR。使用 Worktree 时，该目录必须是 Git 仓库根目录并至少已有一个提交；新 worktree 从已提交 HEAD 创建，不包含主目录的未提交改动或被忽略的环境文件。

### 可以尝试的任务

基础工具：

> 阅读工作目录中的代码，说明主要模块的职责。只读，不修改文件。

隔离协作：

> 创建两个小任务，先分别绑定 worktree，再启动队友。要求各自在自己的目录写入并回读一个文件，验证后显式完成任务。保留 worktree 供我检查，不要提交或合并。

动态工具：

> 连接 docs mock 服务，搜索 worktree，读取匹配文档，再解释任务完成与分支合并的区别。

MCP 示例使用进程内 mock：docs 查询固定数据，deploy 只更改模拟状态，不连接外部服务或执行真实部署。

## 配置

配置来自环境变量或本地 .env；以下默认值对应累计实现的 Settings。

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| DASHSCOPE_API_KEY | 无 | 模型访问凭据，交互调用必填 |
| DASHSCOPE_BASE_URL | https://dashscope.aliyuncs.com/compatible-mode/v1 | OpenAI-compatible 接口地址 |
| MODEL_ID | qwen-plus | 主模型 |
| FALLBACK_MODEL_ID | 无 | 错误恢复时可使用的备用模型 |
| CC_WORKDIR | 启动目录 | 工作区与工具执行目录 |
| CC_SKILLS_DIR | 工作区内的 skills/ | Skill 扫描目录 |
| CC_MEMORY_DIR | 工作区内的 .memory/ | 长期记忆目录 |
| CC_TASKS_DIR | 工作区内的 .tasks/ | 持久任务板目录 |

累计实现要求 Memory 和 Task 目录位于工作区内。配置其他模型或兼容接口时，仍需核对工具调用与供应商扩展参数，不能仅凭接口名称相同就假定完全兼容。

.env 已被 Git 忽略，但这不等于所有运行时文件都会被忽略或脱敏。不要在 prompt、测试产物、Memory、日志或提交中保存密钥和业务敏感数据。

## 章节导航

s01–s09 用单文件观察机制如何逐步加入循环；s10–s19 按课程能力拆分 harness/ 模块并保留累计实现；s20 复用 s19，用跨模块场景检查组合行为。

| 章节 | 主题 | 重点 |
| --- | --- | --- |
| [s01](s01_agent_loop/README.md) | Agent Loop | 模型 → 工具 → 结果 → 模型 |
| [s02](s02_tool_use/README.md) | Tool Use | schema、handler 与工具分发 |
| [s03](s03_permission/README.md) | Permission | 拒绝、询问与放行 |
| [s04](s04_hooks/README.md) | Hooks | 输入、工具前后与停止事件 |
| [s05](s05_todo_write/README.md) | TodoWrite | 当前执行清单与状态约束 |
| [s06](s06_subagent/README.md) | Subagent | 独立上下文、同步委派与能力限制 |
| [s07](s07_skill_loading/README.md) | Skills | 目录发现与正文按需加载 |
| [s08](s08_context_compact/README.md) | Context Compact | 分层压缩、结果落盘与协议配对 |
| [s09](s09_memory/README.md) | Memory | 召回、提取与跨会话记忆 |
| [s10](s10_system_prompt/README.md) | System Prompt | 根据真实运行态组装提示词 |
| [s11](s11_error_recovery/README.md) | Error Recovery | 有界重试、fallback 与上下文恢复 |
| [s12](s12_task_system/README.md) | Task System | 持久任务、归属与依赖图 |
| [s13](s13_background_tasks/README.md) | Background Tasks | 后台执行与一次性结果通知 |
| [s14](s14_cron_scheduler/README.md) | Cron Scheduler | 到期队列与 turn 串行执行 |
| [s15](s15_agent_teams/README.md) | Agent Teams | 常驻队友、独立循环与邮箱 |
| [s16](s16_team_protocols/README.md) | Team Protocols | 计划审批与关闭握手 |
| [s17](s17_autonomous_agents/README.md) | Autonomous Agents | 空闲扫描与自动认领 |
| [s18](s18_worktree_isolation/README.md) | Worktree Isolation | 任务绑定独立目录、分支与删除保护 |
| [s19](s19_mcp_plugin/README.md) | MCP Tools | mock 发现、动态工具池与调用适配 |
| [s20](s20_comprehensive_agent/README.md) | Comprehensive Agent | 整体调用链与跨模块集成验收 |

每章 README 解释具体机制、设计取舍和验证边界；总览不重复展开全部源码细节。

## 项目组织

```text
cc-harness-lab/
├── s01_agent_loop/ … s09_memory/       单文件教学章节
├── s10_system_prompt/ … s18_worktree_isolation/
│   ├── code.py                        章节 CLI
│   ├── harness/                       按能力拆分的累计实现
│   └── README.md                      机制说明
├── s19_mcp_plugin/
│   ├── code.py                        累计 Harness 交互入口
│   ├── harness/                       循环与各能力模块
│   └── README.md
├── s20_comprehensive_agent/
│   └── README.md                      整体架构，不复制 Harness
├── skills/                            示例 Skill
├── tests/                             章节测试与集成场景
├── EVALUATION.md                       系统化评测设计
├── .env.example                       配置模板
└── requirements.txt
```

查看主体实现可从 [agent_loop.py](s19_mcp_plugin/harness/agent_loop.py) 开始，再按调用关系进入各能力模块。模块文件采用能力名，不再添加章节前缀；config、models 和 provider 保存公共配置、数据结构与模型协议适配。

## 与官方教程的关系

项目跟随 ShareAI Lab 的 Learn Claude Code 课程学习 Harness Engineering，是按课程机制独立重写的练习，不是官方仓库的 fork，也不是逐行翻译。

- [Learn Claude Code 官方源码](https://github.com/shareAI-lab/learn-claude-code)
- [Learn Claude Code 中文教程](https://learn.shareai.run/zh/)

本仓库的实现选择集中在这些方面：

| 维度 | 本项目的做法 |
| --- | --- |
| 模型协议 | 使用百炼 OpenAI-compatible 接口，通过 tool_calls 与独立 role=tool 消息交互 |
| 章节组织 | 累计保留前章能力；模块化后仍能按章节对比机制变化 |
| 工具执行 | 统一经过 ToolExecutor、权限与 Hooks，父子角色分别注册能力 |
| 压缩适配 | 保护 OpenAI 工具调用与结果的配对，压缩前归档，超大结果落盘 |
| 状态与并发 | 区分任务归属、消息通知、队友生命周期与文件工作区 |
| 动态工具 | 沿用教学 mock 范围；检测名称冲突，工具变化驱动 Prompt 缓存失效 |
| 验证方式 | 章节回归之外，增加跨模块场景，检查消息、产物、状态及线程行为 |
| 综合章节 | s20 直接验收 s19，不再维护另一份相同实现 |

协议适配不只是重命名字段：它同时影响工具结果追加、压缩边界、恢复流程和供应商扩展参数。项目的重点是理解并实现这些运行时契约，而不是复刻产品界面。

## 测试与评测

自动化测试使用标准库 unittest，不依赖额外安装 pytest，也不需要真实模型 API Key：

```bash
# 全仓回归
python3 -m unittest discover -s tests

# MCP 教学桥
python3 -m unittest tests.test_s19

# 跨模块集成场景
python3 -m unittest tests.test_s20
```

测试用可控模型响应验证路由与状态迁移；需要文件、Git 或线程的场景在临时工作区真实执行。部分章节 README 另有真实模型实验说明，两类验证不能互相替代。

[评测方案](EVALUATION.md)讨论任务完成率、重复运行一致性、安全与权限、恢复、成本和延迟等指标。单元测试与集成测试用于检查实现契约，不应被解释成真实任务成功率或生产可靠性结论。

## 使用边界

- **不是安全沙箱。** 文件工具有路径检查，Bash 有教学版权限策略，但同一用户进程中的 shell 不能仅靠字符串规则和 cwd 实现强隔离。
- **MCP 是 mock。** 没有真实 stdio/HTTP 传输、协议握手、认证或生产服务授权；工具描述中的标注不是用户许可。
- **协作不等于自动验收。** 队友总结和线程退出不会自动完成任务；失败可能留下 in_progress，系统没有任务租约或自动重分配。
- **隔离不等于集成。** Worktree 不自动合并；前置任务完成也不会把其分支产物自动带入后续任务。
- **持久文件不等于会话恢复。** 后台任务和部分团队状态仍在内存中，进程退出可能丢失工作；CLI 不提供统一取消全部活动的机制。
- **提示词不是强制策略。** Skills、Memory、团队消息和工具结果都需要信任边界，文字提醒不能替代执行层约束。
- **运行记录可能敏感。** Transcript、工具结果、邮箱和 Memory 可能包含代码与上下文，应自行控制访问和保留范围。

请在可恢复、受控的工作区中学习和实验；不要直接赋予生产凭据或用于不可逆的业务操作。
