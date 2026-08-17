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
├── tests/
│   ├── test_s01.py
│   ├── test_s02.py
│   ├── test_s03.py
│   └── test_s04.py
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

真实 API Key 只从环境变量或本地 `.env` 文件读取，`.env` 不会提交到仓库。

## Roadmap

后续将逐步补充更多工具、任务规划、子 Agent、Skill 加载、上下文压缩和 Memory 等 Agent Harness 能力。

## Safety

`s01` 只实现了基础命令拦截、超时和输出截断；`s02` 给文件工具增加了
`CC_WORKDIR` 路径边界；`s03` 增加教学版权限管线，但命令字符串匹配仍可能被
Shell 变体绕过；`s04` 的 Hook 是进程内同步回调，没有隔离第三方 Hook。代码仍
用于学习，请在受控目录和隔离环境中运行，不要直接用于生产环境。
