# cc-harness-lab

从零实现一个 Claude Code 风格的 Agent Harness，用尽量少的代码理解智能体运行时的核心机制。

这个仓库不是通用 Agent 框架，而是按阶段演进的学习与实验项目：每个目录聚焦一个可独立运行的能力。

## 当前进度

当前已完成 s01–s13，并通过 243 项自动化测试：

| 章节 | 主题 | 关键实现 |
| --- | --- | --- |
| [s10](s10_system_prompt/) | System Prompt | 根据真实工具、工作区、Skills 和 Memory 元数据动态组装并缓存 |
| [s11](s11_error_recovery/) | Error Recovery | 输出截断续写、Prompt 溢出压缩，以及 429/529 有界退避与 fallback |
| [s12](s12_task_system/) | Task System | 持久任务图、依赖阻塞、严格状态机和原子 JSON 落盘 |
| [s13](s13_background_tasks/) | Background Tasks | 后台 Bash、线程安全状态、一次性完成通知和消息竞态处理 |

各章节采用累计实现：后章保留前章能力，而不是孤立示例。

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
| 当前课程范围 | 主线 s01–s20，另有 legacy track 和 Web 教学平台 | 当前完成 s01–s13，保留代码、中文说明和测试 |
| 章节组织 | 每章隔离一个机制，部分章节使用较小 kernel，s15 再组装完整 Harness | 每章直接复制并继承上一章，s13 已包含 s01–s12 全部能力 |
| 默认模型协议 | Anthropic SDK，`tool_use` / `tool_result` content blocks | 百炼 OpenAI-compatible，`tool_calls` / 独立 `role=tool` 消息 |
| 默认配置 | `ANTHROPIC_API_KEY`、`ANTHROPIC_BASE_URL`、`MODEL_ID` | `DASHSCOPE_API_KEY`、`DASHSCOPE_BASE_URL`、`MODEL_ID` |
| Tool Schema | Anthropic `name + input_schema` | OpenAI function calling 的 `type=function + function.parameters` |
| 工具执行 | 教学版直接分发 handler | handler 分发外叠加输出截断、路径边界、权限和 Hook |
| 文件边界 | 课程示例按章节展示 workspace 检查和审批 | 从 s02 起统一限制在 `CC_WORKDIR`，审批也不能扩大边界 |
| 子 Agent | 聚焦 fresh `messages[]` 和结果返回 | 额外限制 30 轮、禁止递归 `task`，复用权限与 Hook |
| Context Compact | 讲解四层压缩机制 | 适配 OpenAI 消息配对，增加 transcript、主动 compact 和单次 reactive retry |
| Memory | selection、extraction、consolidation | 与 s08 联动，使用压缩前快照，并增加常见 Secret 拒绝和可配置目录 |
| Background | daemon thread + 完成通知 | 保持同一语义，并保证 Permission 在父线程、通知不复用 `tool_call_id` |
| 验证方式 | 官方 runnable lessons 与上游测试 | 每章对应 `tests/test_sXX.py`，当前全量 243 项测试 |
| 文档形态 | 英文默认文档、中文/日文翻译、图片和 Web 课程 | 中文 README、源码分析和远端可运行实验，不包含 Web 平台 |

### 为什么本仓库代码更长

官方课程强调“每章只看一个机制”，因此某些章节会换回较小的 Agent kernel；当前官方
s01–s09 的 `code.py` 大约从 141 行增长到 757 行。本仓库在 s01–s09 采用严格累计的
单文件方式，从 s01 的 202 行增长到 s09 的 1930 行。增长部分不全是当前章节的新能力，
而是前面所有机制都继续存在，例如 s09 同时保留权限、Hooks、Todo、SubAgent、Skills
和 Compact。

这种组织适合观察真实 Harness 怎样逐步变复杂，也能验证新增机制没有破坏旧能力；但到
s10 时单文件会超过 2400 行。因此本仓库从 s10 起冻结旧章节，把最新阶段拆成局部
`harness/` package：能力仍然累计，但配置、权限、工具、Hook、压缩、记忆、Prompt 和 Agent Loop
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

### s01–s13 的实现偏差

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
| s10 | System Prompt | 按真实工具、workspace、Skill 与 Memory 状态组装命名 section，并缓存不变 context |
| s11 | Error Recovery | 适配 `finish_reason`，8K→64K 后有界续写；429/529 退避并支持 fallback |
| s12 | Task System | 在累计 Harness 中加入父 Agent 专属任务板，严格校验依赖和状态，并原子持久化到 `.tasks/` |
| s13 | Background Tasks | 父 Bash 可后台执行，Pre Hook 保持主线程，真实结果作为独立且有界的一次性通知注入 |

每章更细的运行逻辑、权衡和与官方单章源码的差异，记录在对应目录的 README 或
ANALYSIS 文档中。例如 s09 的 Memory 对照见
[`s09_memory/README.md`](s09_memory/README.md#与官方代码的区别)。

### 当前尚未覆盖的官方主线

官方当前后续章节还包括：

- s14 Cron Scheduler：持久化定时触发；
- s15 Agent Teams：持久队友与 mailbox 协调；
- s16 Team Protocols：shutdown 与 plan approval 协议；
- s17 Autonomous Agents：空闲轮询、自动认领和自组织；
- s18 Worktree Isolation：任务绑定独立 Git worktree；
- s19 MCP Tools：外部工具发现和统一路由；
- s20 Comprehensive Agent：把全部机制组装进一个完整 Harness。

因此，本仓库目前只能与官方 s01–s13 对齐，不能被描述为官方完整实现，也不是 Claude
Code 本体的等价替代。

## Evaluation Plan

项目已经记录完整的 Harness 评测方案，覆盖严格任务完成率、`pass@k` / `pass^k`、
安全与权限、故障恢复、成本和延迟，以及 Compact、Memory、SubAgent、Task DAG 和
Background 的专项指标。

当前只固化方案；完成 s01–s20 和 Comprehensive Agent 后实施。详见 [EVALUATION.md](EVALUATION.md)。

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

### `s10_system_prompt`

在累计保留 s01–s09 能力的基础上，把 System Prompt 改成运行时组装。详细设计见
[`s10_system_prompt/README.md`](s10_system_prompt/README.md)，对照
[s10 官方教程](https://learn.shareai.run/zh/s10/)和
[官方源码](https://github.com/shareAI-lab/learn-claude-code/blob/main/s10_system_prompt/code.py)。

- identity、guidance、tools、workspace 是固定命名 section
- Skill catalog 和 Memory catalog 只在真实状态存在时加载
- 父 Agent 与 SubAgent 根据各自真实工具列表独立组装
- Harness 按 s01–s10 课程能力组织为 agent_loop、tool_use、permission、hooks、
  todo_write、subagent、skill_loading、context_compact、memory、system_prompt
- 文件名不带章节前缀；config、models、provider 保持跨章节基础设施
- context 使用稳定 JSON key；状态不变时复用缓存字符串
- Memory metadata 留在 system，完整记录仍由 s09 按需注入 user turn

### `s11_error_recovery`

在 s10 模块化 Harness 上加入有界恢复状态机。详细设计见
[`s11_error_recovery/README.md`](s11_error_recovery/README.md)，对照
[s11 官方教程](https://learn.shareai.run/zh/s11/)和
[官方源码](https://github.com/shareAI-lab/learn-claude-code/blob/main/s11_error_recovery/code.py)。

- 首次输出截断不保存半截 assistant，而是把输出预算从 8K 提升到 64K 后重放
- 64K 下仍截断时保存文本片段，最多追加三次 continuation
- Prompt 过长只允许一次 reactive compact，第二次失败即停止
- 429/529 最多尝试十次，采用指数退避、jitter 和 `Retry-After`
- 连续三次 529 且配置 `FALLBACK_MODEL_ID` 时切换备用模型
- 错误分类和退避位于 `error_recovery.py`，消息状态迁移仍由 `agent_loop.py` 负责

### `s12_task_system`

在 s11 累计 Harness 上加入可跨会话恢复的项目任务板。详细设计见
[`s12_task_system/README.md`](s12_task_system/README.md)，对照
[s12 官方教程](https://learn.shareai.run/zh/s12/)和
[官方源码](https://github.com/shareAI-lab/learn-claude-code/blob/main/s12_task_system/code.py)。

- `create_task`、`list_tasks`、`get_task`、`claim_task`、`complete_task` 五个父 Agent 工具
- 每个任务以 `.tasks/{id}.json` 持久化，字段为 `id`、`subject`、`description`、`status`、`owner`、`blockedBy`
- `pending -> in_progress -> completed` 是严格单向状态机，不提供跳步或回退
- 依赖不存在或尚未完成时不能 claim；完成任务会报告被直接解锁的下游任务
- 同目录临时文件经 `flush + fsync + os.replace` 原子替换，文件权限固定为 `0600`
- Task 工具继续经过 ToolExecutor、权限和 Hook；SubAgent 不暴露这些工具
- TodoWrite 仍负责当前执行清单，Task System 负责跨轮次、跨会话的项目工作图

### `s13_background_tasks`

在 s12 累计 Harness 上加入不会阻塞父循环的后台 Bash。详细设计见
[`s13_background_tasks/README.md`](s13_background_tasks/README.md)，对照
[s13 官方教程](https://learn.shareai.run/zh/s13/)和
[官方源码](https://github.com/shareAI-lab/learn-claude-code/blob/main/s13_background_tasks/code.py)。

- 父 Bash schema 增加可选 `run_in_background`，显式布尔值优先于慢命令启发式
- `PreToolUse` 与 Permission 在父线程完成，批准后才创建 daemon worker
- 原 `tool_call_id` 立即接收 placeholder，真实结果不重复使用该 ID
- worker 只更新线程安全任务状态，不直接修改消息列表
- 完成或失败结果在模型边界以独立 `role=user` 的 `<task_notification>` 一次性注入
- 命令和输出经过 XML 转义，通知摘要默认限制为 1000 字符
- 最终答案前再次收集通知，处理任务恰好在模型推理期间完成的竞态
- SubAgent 保持同步 Bash，避免子循环结束后产生无人消费的通知

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
├── s10_system_prompt/
│   ├── README.md
│   ├── ANALYSIS.md
│   ├── CALLGRAPH.md
│   ├── callgraph.svg
│   ├── code.py
│   └── harness/
│       ├── agent_loop.py
│       ├── tool_use.py
│       ├── permission.py
│       ├── hooks.py
│       ├── todo_write.py
│       ├── subagent.py
│       ├── skill_loading.py
│       ├── context_compact.py
│       ├── memory.py
│       ├── system_prompt.py
│       ├── provider.py
│       ├── config.py
│       └── models.py
├── s11_error_recovery/
│   ├── README.md
│   ├── ANALYSIS.md
│   ├── CALLGRAPH.md
│   ├── callgraph.svg
│   ├── code.py
│   └── harness/
│       ├── agent_loop.py
│       ├── error_recovery.py
│       └── ...                 # s01–s10 累计能力模块
├── s12_task_system/
│   ├── README.md
│   ├── ANALYSIS.md
│   ├── CALLGRAPH.md
│   ├── callgraph.svg
│   ├── code.py
│   └── harness/
│       ├── agent_loop.py
│       ├── task_system.py
│       └── ...                 # s01–s11 累计能力模块
├── s13_background_tasks/
│   ├── README.md
│   ├── ANALYSIS.md
│   ├── CALLGRAPH.md
│   ├── callgraph.svg
│   ├── code.py
│   └── harness/
│       ├── background_tasks.py
│       └── ...                 # s01–s12 累计能力模块
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
│   ├── test_s10.py
│   ├── test_s11.py
│   ├── test_s12.py
│   └── test_s13.py
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

# 第十章：运行时 System Prompt
python3 s10_system_prompt/code.py

# 第十一章：有界错误恢复
python3 s11_error_recovery/code.py

# 第十二章：持久任务系统
python3 s12_task_system/code.py

# 第十三章：后台任务与完成通知
python3 s13_background_tasks/code.py
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
| `MODEL_ID` | `qwen-plus` | 使用的主模型 |
| `FALLBACK_MODEL_ID` | 无 | s11 起连续 529 后可切换的备用模型 |
| `CC_WORKDIR` | 当前启动目录 | Bash 工具的工作目录 |
| `CC_SKILLS_DIR` | `<CC_WORKDIR>/skills` | s07 扫描的技能目录 |
| `CC_MEMORY_DIR` | `<CC_WORKDIR>/.memory` | s09 长期记忆目录，必须位于工作目录内 |
| `CC_TASKS_DIR` | `<CC_WORKDIR>/.tasks` | s12 持久任务目录，必须位于工作目录内 |

真实 API Key 只从环境变量或本地 `.env` 文件读取，`.env` 不会提交到仓库。

## Roadmap

后续按官方当前主线继续实现，同时保持百炼 OpenAI-compatible 适配和累计回归：

1. s14 Cron Scheduler
2. s15 Agent Teams
3. s16 Team Protocols
4. s17 Autonomous Agents
5. s18 Worktree Isolation
6. s19 MCP Tools
7. s20 Comprehensive Agent

## Safety

`s01` 只实现了基础命令拦截、超时和输出截断；`s02` 给文件工具增加了
`CC_WORKDIR` 路径边界；`s03` 增加教学版权限管线，但命令字符串匹配仍可能被
Shell 变体绕过；`s04` 的 Hook 是进程内同步回调，没有隔离第三方 Hook；s05 的
TODO 只存在于内存中，进程退出即丢失；s06 子 Agent 同步占用父循环，且共享同一
工作目录，不提供进程或文件隔离；s07 会把加载的技能内容作为模型指令使用，因此
只应安装和加载可信的 `SKILL.md`；s08 transcript 和落盘工具结果可能包含源代码或
命令输出等敏感内容，应限制工作目录权限并按需清理；s09 会持久化模型提取出的长期
上下文，虽然代码会拒绝常见密钥格式，仍应定期审阅 `.memory/`，不要把凭据、客户
数据或其他敏感信息写入记忆；s10 组装 Prompt 只保证内容与运行态一致，不代表模型会
严格遵守所有文字约束；s11 的重试有硬上限，但错误分类仍依赖供应商状态码和消息，
也不等价于生产级容灾；s12 的单文件写入是原子的，但没有跨进程 claim 锁、循环依赖
检测或任务释放协议，并发运行多个 Harness 时仍可能发生竞态；s13 的后台状态只在当前
Python 进程内，daemon thread 没有取消、恢复、实时日志、并发配额或生产级子进程树
清理，完整结果也只保存在内存。代码仍用于学习，请在受控目录和隔离环境中运行，不要
直接用于生产环境。
