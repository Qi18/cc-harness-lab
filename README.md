# cc-harness-lab

从零实现一个 Claude Code 风格的 Agent Harness，用尽量少的代码理解智能体运行时的核心机制。

这个仓库不是通用 Agent 框架，而是按阶段演进的学习与实验项目：每个目录聚焦一个可独立运行的能力。

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
│   └── test_s08.py
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

真实 API Key 只从环境变量或本地 `.env` 文件读取，`.env` 不会提交到仓库。

## Roadmap

后续将逐步补充更多工具、任务规划、子 Agent、Skill 加载、上下文压缩和 Memory 等 Agent Harness 能力。

## Safety

`s01` 只实现了基础命令拦截、超时和输出截断；`s02` 给文件工具增加了
`CC_WORKDIR` 路径边界；`s03` 增加教学版权限管线，但命令字符串匹配仍可能被
Shell 变体绕过；`s04` 的 Hook 是进程内同步回调，没有隔离第三方 Hook；s05 的
TODO 只存在于内存中，进程退出即丢失；s06 子 Agent 同步占用父循环，且共享同一
工作目录，不提供进程或文件隔离；s07 会把加载的技能内容作为模型指令使用，因此
只应安装和加载可信的 `SKILL.md`；s08 transcript 和落盘工具结果可能包含源代码或
命令输出等敏感内容，应限制工作目录权限并按需清理。代码仍用于学习，请在受控目录
和隔离环境
中运行，不要直接用于生产环境。
