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

## Project Structure

```text
.
├── s01_agent_loop/
│   ├── README.md
│   └── code.py
├── tests/
│   └── test_s01.py
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

`s01` 只实现了基础命令拦截、超时和输出截断，仍然是学习代码。请在受控目录和隔离环境中运行，不要直接用于生产环境。
