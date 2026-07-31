# s01：Agent Loop

这一章只实现两个东西：

1. 调用大模型；
2. 当模型请求调用 Bash 时，执行命令并把结果喂回模型。

核心循环可以压缩为：

```text
while True:
    response = LLM(messages, tools)
    messages.append(response)

    if response 没有 tool_calls:
        return response.content

    for tool_call in response.tool_calls:
        result = execute(tool_call)
        messages.append(result)
```

当模型仍需要观察环境时，它会继续产生 `tool_calls`；当它认为任务完成时，
返回普通文本，循环结束。这就是后续权限、Hook、SubAgent、Compact 等机制
共同包裹的最小内核。

## 启动

在仓库根目录执行：

```bash
python s01_agent_loop/code.py
```

输入任务后，Agent 可以在当前工作目录执行 Bash。输入 `q`、`exit` 或空行退出。

## 百炼配置

程序读取以下环境变量：

| 变量 | 必填 | 默认值 |
| --- | --- | --- |
| `DASHSCOPE_API_KEY` | 是 | 无 |
| `DASHSCOPE_BASE_URL` | 否 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `MODEL_ID` | 否 | `qwen-plus` |
| `CC_WORKDIR` | 否 | 程序启动目录 |

不要把真实 Key 写入源码或提交 `.env`。

默认使用 `qwen-plus`。如果业务空间已开通 Kimi K3，可将 `MODEL_ID` 改为
`kimi/kimi-k3`；代码会自动传入 `reasoning_effort=max`，并在工具调用后的
下一轮请求中保留 assistant 消息里的 `reasoning_content`。
