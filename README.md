# cc-harness-lab

从零实现一个 Claude Code 风格的 Agent Harness，用尽量少的代码理解智能体运行时的核心机制。

这个仓库不是通用 Agent 框架，而是按阶段演进的学习与实验项目：每个目录聚焦一个可独立运行的能力。

## 与官方项目的关系

本仓库跟随 ShareAI Lab 的 Learn Claude Code 课程学习 Harness Engineering，但不是
官方仓库的 fork，也不是逐行翻译。这里保留课程的核心机制，用自己的消息协议、模型
客户端、安全边界和测试重新实现，并让每个阶段真正继承前一阶段的完整能力。

官方入口：

- [Learn Claude Code 官方源码](https://github.com/shareAI-lab/learn-claude-code)
- [Learn Claude Code 中文教程](https://learn.shareai.run/zh/)

两者共同遵循同一个核心循环：模型读取 `messages`，决定是否调用工具；Harness 只负责
执行工具、返回 observation、管理上下文和权限。差异主要发生在循环周围，而不是 Agent
Loop 的基本形状。

### 整体差异

| 维度 | 官方 Learn Claude Code | 本仓库 cc-harness-lab |
| --- | --- | --- |
| 当前课程范围 | 主线 s01–s17，另有 legacy track 和 Web 教学平台 | 当前完成 s01–s10，保留代码、中文说明和测试 |
| 章节组织 | 每章隔离一个机制，部分章节使用较小 kernel，s15 再组装完整 Harness | 每章直接复制并继承上一章，s09 已包含 s01–s08 全部能力 |
| 默认模型协议 | Anthropic SDK，`tool_use` / `tool_result` content blocks | 百炼 OpenAI-compatible，`tool_calls` / 独立 `role=tool` 消息 |
| 默认配置 | `ANTHROPIC_API_KEY`、`ANTHROPIC_BASE_URL`、`MODEL_ID` | `DASHSCOPE_API_KEY`、`DASHSCOPE_BASE_URL`、`MODEL_ID` |
| Tool Schema | Anthropic `name + input_schema` | OpenAI function calling 的 `type=function + function.parameters` |
| 工具执行 | 教学版直接分发 handler | handler 分发外叠加输出截断、路径边界、权限和 Hook |
| 文件边界 | 课程示例按章节展示 workspace 检查和审批 | 从 s02 起统一限制在 `CC_WORKDIR`，审批也不能扩大边界 |
| 子 Agent | 聚焦 fresh `messages[]` 和结果返回 | 额外限制 30 轮、禁止递归 `task`，复用权限与 Hook |
| Context Compact | 讲解四层压缩机制 | 适配 OpenAI 消息配对，增加 transcript、主动 compact 和单次 reactive retry |
| Memory | selection、extraction、consolidation | 与 s08 联动，使用压缩前快照，并增加常见 Secret 拒绝和可配置目录 |
| 验证方式 | 官方 runnable lessons 与上游测试 | 每章对应 `tests/test_sXX.py`，当前全量 138 项测试 |
| 文档形态 | 英文默认文档、中文/日文翻译、图片和 Web 课程 | 中文 README、源码分析和远端可运行实验，不包含 Web 平台 |

### 为什么本仓库代码更长

官方课程强调“每章只看一个机制”，因此某些章节会换回较小的 Agent kernel；当前官方
s01–s09 的 `code.py` 大约从 141 行增长到 757 行。本仓库在 s01–s09 采用严格累计的
单文件方式，从 s01 的 202 行增长到 s09 的 1930 行。增长部分不全是当前章节的新能力，
而是前面所有机制都继续存在，例如 s09 同时保留权限、Hooks、Todo、SubAgent、Skills
和 Compact。

这种组织适合观察真实 Harness 怎样逐步变复杂，也能验证新增机制没有破坏旧能力；但到
s10 时单文件会超过 2400 行。因此本仓库从 s10 起冻结旧章节，把最新阶段拆成局部
`harness/` package：能力仍然累计，但配置、工具、Hook、压缩、记忆、任务和 Agent Loop
各自有明确模块。官方的隔离式章节更容易单独阅读，完整能力则在后面的 integrated
harness 重新汇合。

### 协议适配不是字段改名

官方 Anthropic 格式把工具调用放在 assistant content blocks 中，并把多个工具结果作为
下一条 user 消息里的 `tool_result` blocks 返回。本仓库使用 OpenAI-compatible 格式：
assistant 产生 `tool_calls`，每个结果追加为独立的 `role=tool` 消息，并通过
`tool_call_id` 配对。

这影响的不只是 API 调用，还影响：

- 工具调用和结果的解析、追加与日志展示；
- s08 裁剪时如何保证 assistant tool call 与连续 tool messages 不被切断；
- compact 摘要如何保留当前请求和工具边界；
- Kimi 等兼容模型需要的 `extra_body` 扩展参数。

所以本仓库是在复现 Harness 机制，而不是复制官方消息结构。

### s01–s10 的实现偏差

| 阶段 | 共同主题 | 本仓库相对官方的主要实现选择 |
| --- | --- | --- |
| s01 | Agent Loop | 改用百炼客户端，并提前加入命令超时、输出上限和基础 deny list |
| s02 | Tool Use | 文件工具统一经过 `safe_path()`，阻止绝对路径、`..` 和符号链接逃逸 |
| s03 | Permission | 明确拆成硬拒绝、软询问、默认放行；人工批准不能突破工作目录 |
| s04 | Hooks | 保留四类同步 Hook，并把 s03 权限管线注册成 `PreToolUse` |
| s05 | TodoWrite | 增加结构校验、单一 `in_progress` 约束和连续三轮未更新提醒 |
| s06 | SubAgent | 同步执行、最多 30 轮、共享文件系统但隔离消息，禁止递归委派 |
| s07 | Skills | 扫描 `skills/*/SKILL.md`，只常驻目录，全文通过 `load_skill` 按需进入上下文 |
| s08 | Compact | 为 OpenAI tool messages 重写边界处理，增加主动工具、落盘恢复和应急压缩 |
| s09 | Memory | 索引常驻 system、正文附加到当前 user turn，使用独立提取快照和 Secret 过滤 |
| s10 | Task System | 从本章开始模块化；增加两阶段依赖图、原子文件替换和父 Agent 专属任务工具 |

每章更细的运行逻辑、权衡和与官方单章源码的差异，记录在对应目录的 README 或
ANALYSIS 文档中。例如 s09 的 Memory 对照见
[`s09_memory/README.md`](s09_memory/README.md#与官方代码的区别)。

### 当前尚未覆盖的官方主线

官方当前后续章节还包括：

- s11 Background Tasks：后台线程和完成通知；
- s12 Cron Scheduler：持久化定时触发；
- s13 Agent Teams：持久队友、原子任务领取和工作目录绑定；
- s14 MCP Plugin：外部工具发现和命名空间路由；
- s15 Integrated Harness：把课程机制重新组装进一个完整循环；
- s16 Workflow Runtime：固定编排、事件和可恢复 journal；
- s17 Goal Loop：独立评估器决定是否允许 Agent 停止。

因此，本仓库目前只能与官方 s01–s10 对齐，不能被描述为官方完整实现，也不是 Claude
Code 本体的等价替代。

## Current Stage

### `s01_agent_loop`

实现一个最小但完整的 Agent Loop：

```text
User Task
   -> LLM
   -> Tool Call
   -> Bash Execution
   -> Observation
   -> LLM
   -> Final Answer
```

当前能力：

- 使用阿里云百炼的 OpenAI 兼容接口
- 支持模型发起 Bash 工具调用
- 将工具结果追加回对话并继续推理
- 保留多轮会话上下文
- 支持自定义模型、Base URL 和工作目录
- 对部分高风险命令进行拦截
- 限制命令执行时间和输出长度

### `s02_tool_use`

在 s01 的循环之上增加可扩展的工具分发：

- 提供 `bash`、`read_file`、`write_file`、`edit_file` 和 `glob` 五个工具
- 通过 `TOOL_HANDLERS` 按名称查表执行，新增工具不需要修改 Agent Loop
- 文件工具只能访问 `CC_WORKDIR`，阻止相对路径、绝对路径和符号链接逃逸
- 支持模型在一轮响应中顺序调用多个工具

### `s03_permission`

在每次工具执行前增加三段式权限管线：

- 硬拒绝：危险命令直接阻止，不能通过审批覆盖
- 软询问：删除、修改系统路径等风险操作暂停等待用户确认
- 默认放行：安全读取和工作目录内文件操作无需打断用户
- 默认拒绝：审批输入不是 `y` 或 `yes` 时不执行工具
- 审批不会扩大 `CC_WORKDIR` 的文件系统边界

### `s04_hooks`

把跨领域行为注册到生命周期 Hook，而不是继续堆进核心循环：

- `UserPromptSubmit`：用户输入进入模型前
- `PreToolUse`：handler 执行前，可阻止工具调用
- `PostToolUse`：handler 执行后，可检查输出或追加副作用
- `Stop`：Agent 准备返回最终答案前，可要求继续一轮
- s03 权限管线作为 `PreToolUse` Hook 保留

### `s05_todo_write`

增加显式计划和进度管理能力：

- 第六个工具 `todo_write` 创建或整体替换当前任务列表
- 支持 `pending`、`in_progress`、`completed` 三种状态
- 任务内容不能为空，同一时刻最多一个任务处于 `in_progress`
- TODO 保存在当前进程内存中，终端实时展示状态
- 连续三轮没有调用 `todo_write` 时向模型注入更新提醒

### `s06_subagent`

增加同步子 Agent 和上下文隔离：

- 第七个工具 `task` 为子任务创建全新的消息历史
- 子 Agent 只有五个基础工具，不能递归调用 `task`
- 子工具调用继续经过权限与生命周期 Hook
- 子循环最多运行 30 轮，只向父 Agent 返回最终文本结论
- 子上下文会丢弃，但文件系统副作用保留在共享工作目录

### `s07_skill_loading`

增加两级、按需的 Skill 加载：

- 启动时扫描 `skills/*/SKILL.md`，只把名称和描述注入 system prompt
- 第八个工具 `load_skill` 按注册名称返回完整技能内容
- 完整正文通过工具结果进入当前上下文，不常驻初始提示词
- 父 Agent 和子 Agent 都能加载技能，子 Agent仍不能递归委派
- 注册表隔离文件路径，越过技能根目录的符号链接不会被扫描

### `s08_context_compact`

增加分层上下文压缩和恢复记录：

- L3 先把超预算工具结果写入 `.task_outputs/tool-results/`
- L1 裁剪旧的中间消息，并保护工具调用与结果的配对边界
- L2 只保留最近 3 条完整工具结果，其余替换为短占位符
- L4 超过字符阈值时保存 JSONL transcript，并调用模型生成事实摘要
- API 上下文溢出时最多执行一次 reactive compact
- 第九个工具 `compact` 允许父 Agent 主动请求压缩

### `s09_memory`

在 s08 的会话压缩之外增加跨会话持久记忆：
本章设计与 Memory 单章差异见 [`s09_memory/README.md`](s09_memory/README.md)，对照
[s09 官方教程](https://learn.shareai.run/zh/s09/)和
[官方源码](https://github.com/shareAI-lab/learn-claude-code/blob/main/s09_memory/code.py)。

- `.memory/*.md` 保存带 YAML frontmatter 的独立记忆
- `.memory/MEMORY.md` 只把紧凑目录注入 system prompt
- 每轮通过无工具 side-query 选择最多 5 条相关记忆，失败时关键词降级
- 最终回答后提取稳定偏好、反馈、项目事实和 reference
- 拒绝临时状态、重复记录及 API Key/Secret 等敏感内容
- 达到 10 条后合并去重到最多 8 条，失败时恢复原文件

### `s10_task_system`

在累计保留 s01–s09 能力的基础上增加模块化持久任务图。详细设计见
[`s10_task_system/README.md`](s10_task_system/README.md)，对照
[s10 官方教程](https://learn.shareai.run/zh/s10/)和
[官方源码](https://github.com/shareAI-lab/learn-claude-code/blob/main/s10_task_system/code.py)。

- `code.py` 缩减为 59 行入口，核心拆分为 9 个 `harness` 模块
- `.tasks/task_<8 hex>.json` 保存任务状态、owner 和 `blockedBy`
- 先创建全部节点，再用运行时 ID 添加依赖边
- 拒绝缺失依赖、自依赖、传递环和认领后的依赖修改
- `claim_task` 只允许认领已解锁任务，`complete_task` 报告新解锁下游
- 任务目录必须位于 `CC_WORKDIR`，更新通过临时文件和 `os.replace` 完成
- 六个任务工具仅提供给父 Agent，等待后续 Teams 章节再处理并发认领

## Project Structure

```text
.
├── s01_agent_loop/
│   ├── README.md
│   └── code.py
├── s02_tool_use/
│   ├── README.md
│   └── code.py
├── s03_permission/
│   ├── README.md
│   └── code.py
├── s04_hooks/
│   ├── README.md
│   └── code.py
├── s05_todo_write/
│   ├── README.md
│   └── code.py
├── s06_subagent/
│   ├── README.md
│   └── code.py
├── s07_skill_loading/
│   ├── README.md
│   └── code.py
├── s08_context_compact/
│   ├── README.md
│   └── code.py
├── s09_memory/
│   ├── README.md
│   └── code.py
├── s10_task_system/
│   ├── README.md
│   ├── code.py
│   └── harness/
│       ├── agent.py
│       ├── compaction.py
│       ├── memory.py
│       ├── tasks.py
│       └── ...
├── skills/
│   └── code-review/
│       └── SKILL.md
├── tests/
│   ├── test_s01.py
│   ├── test_s02.py
│   ├── test_s03.py
│   ├── test_s04.py
│   ├── test_s05.py
│   ├── test_s06.py
│   ├── test_s07.py
│   ├── test_s08.py
│   ├── test_s09.py
│   └── test_s10.py
├── .env.example
└── requirements.txt
```

## Quick Start

```bash
git clone https://github.com/Qi18/cc-harness-lab.git
cd cc-harness-lab

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# 编辑 .env，填入 DASHSCOPE_API_KEY

python s01_agent_loop/code.py

# 第二章：工具分发
python s02_tool_use/code.py

# 第三章：权限管线
python s03_permission/code.py

# 第四章：生命周期 Hooks
python s04_hooks/code.py

# 第五章：TodoWrite 计划
python s05_todo_write/code.py

# 第六章：同步子 Agent
python s06_subagent/code.py

# 第七章：按需加载 Skills
python3 s07_skill_loading/code.py

# 第八章：分层上下文压缩
python3 s08_context_compact/code.py

# 第九章：跨会话长期记忆
python3 s09_memory/code.py

# 第十章：模块化持久任务图
python3 s10_task_system/code.py
```

运行测试：

```bash
python -m pytest
```

## Configuration

| 环境变量 | 默认值 | 作用 |
| --- | --- | --- |
| `DASHSCOPE_API_KEY` | 无 | 阿里云百炼 API Key，必填 |
| `DASHSCOPE_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | OpenAI 兼容接口地址 |
| `MODEL_ID` | `qwen-plus` | 使用的模型 |
| `CC_WORKDIR` | 当前启动目录 | Bash 工具的工作目录 |
| `CC_SKILLS_DIR` | `<CC_WORKDIR>/skills` | s07 扫描的技能目录 |
| `CC_MEMORY_DIR` | `<CC_WORKDIR>/.memory` | s09 长期记忆目录，必须位于工作目录内 |
| `CC_TASKS_DIR` | `<CC_WORKDIR>/.tasks` | s10 持久任务目录，必须位于工作目录内 |

真实 API Key 只从环境变量或本地 `.env` 文件读取，`.env` 不会提交到仓库。

## Roadmap

后续按官方当前主线继续实现，同时保持百炼 OpenAI-compatible 适配和累计回归：

1. s11 Background Tasks
2. s12 Cron Scheduler
3. s13 Agent Teams
4. s14 MCP Plugin
5. s15 Integrated Harness
6. s16 Workflow Runtime
7. s17 Goal Loop

## Safety

`s01` 只实现了基础命令拦截、超时和输出截断；`s02` 给文件工具增加了
`CC_WORKDIR` 路径边界；`s03` 增加教学版权限管线，但命令字符串匹配仍可能被
Shell 变体绕过；`s04` 的 Hook 是进程内同步回调，没有隔离第三方 Hook；s05 的
TODO 只存在于内存中，进程退出即丢失；s06 子 Agent 同步占用父循环，且共享同一
工作目录，不提供进程或文件隔离；s07 会把加载的技能内容作为模型指令使用，因此
只应安装和加载可信的 `SKILL.md`；s08 transcript 和落盘工具结果可能包含源代码或
命令输出等敏感内容，应限制工作目录权限并按需清理；s09 会持久化模型提取出的长期
上下文，虽然代码会拒绝常见密钥格式，仍应定期审阅 `.memory/`，不要把凭据、客户
数据或其他敏感信息写入记忆；s10 的单文件更新是原子的，但 claim 还不是跨进程原子
操作，不应用作并发任务队列。代码仍用于学习，请在受控目录和隔离环境中运行，不要
直接用于生产环境。
