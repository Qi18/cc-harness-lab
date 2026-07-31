# cc-harness-lab

从零实现一个 Claude Code 风格的 Agent Harness。

当前进度：

- `s01_agent_loop`：一个 Agent Loop + 一个 Bash 工具

代码使用阿里云百炼的 OpenAI 兼容接口。真实 API Key 只通过
`DASHSCOPE_API_KEY` 环境变量或本地 `.env` 文件读取，不会写进仓库。

## 运行 s01

```bash
cd /root/cc-harness-lab
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，填入 DASHSCOPE_API_KEY
python s01_agent_loop/code.py
```

默认配置：

- Base URL：`https://dashscope.aliyuncs.com/compatible-mode/v1`
- 模型：`qwen-plus`
- 工作目录：启动程序时所在的目录

以上配置都可以通过同名环境变量覆盖。
