# s11：Error Recovery

这一章在 s01–s10 的累计 Harness 上增加有界错误恢复。目标不是“捕获所有异常后无限
重试”，而是先判断失败类型，再选择不会破坏消息历史的恢复动作。

- 官方教程：[s11 Error Recovery](https://learn.shareai.run/zh/s11/)
- 官方源码：[s11_error_recovery/code.py](https://github.com/shareAI-lab/learn-claude-code/blob/main/s11_error_recovery/code.py)
- 上一章：[s10 System Prompt](../s10_system_prompt/)
- 源码分析：[ANALYSIS.md](./ANALYSIS.md)
- 调用图：[CALLGRAPH.md](./CALLGRAPH.md)

## 本章新增能力

| 失败类型 | 识别信号 | 恢复动作 | 上限 |
| --- | --- | --- | --- |
| 输出被截断 | `finish_reason=length/max_tokens` | 8K 提升到 64K 后原请求重放；仍截断则追加续写提示 | 最多 3 次续写 |
| Prompt 过长 | API 错误文本包含 context/prompt-too-long 标记 | 强制 reactive compact 后重试 | 每个 user turn 1 次 |
| 速率限制 | HTTP 429 | 指数退避、0–25% jitter；优先服从 `Retry-After` | 最多 10 次请求 |
| 服务过载 | HTTP 529 | 同样退避；连续 3 次可切换备用模型 | 最多 10 次请求 |
| 其他错误 | 不属于以上类别 | 写成最终 assistant 错误消息并停止 | 不重试 |

恢复状态只在当前 user turn 内存在。下一个用户请求会重新从主模型、8K 输出预算和零
重试状态开始，不会让一次故障永久改变 Harness。

## 三条恢复路径

### 1. 输出截断

第一次截断时，模型返回的内容可能停在半句话甚至半个工具调用中，因此不能写入
`messages`。Harness 只把 `max_tokens` 从 8000 提升到 64000，然后用原消息历史
重新请求。

64K 下仍截断时才把当前文本片段写成 assistant 消息，并追加一条 user continuation
提示，让模型从断点继续。最多追加三次；第四次仍截断就返回最后一段可见输出，避免
无限续写。截断的工具调用不会执行，因为不完整参数不具备安全执行条件。

### 2. Prompt 过长

`ContextCompactor.prepare()` 是请求前的主动压缩；API 仍报告上下文过长时，
s11 再调用一次 `reactive_compact()`。这个动作每个 turn 只允许一次。第二次溢出
说明当前压缩策略已经无法满足供应商窗口，Harness 会返回有界错误，而不是反复摘要。

### 3. 429 / 529

`with_retry()` 只处理明确的瞬态错误：

```text
delay = min(0.5 * 2^attempt, 32) + random(0, base * 25%)
```

如果响应带 `Retry-After`，直接使用服务端秒数。连续三次 529 且设置了
`FALLBACK_MODEL_ID` 时，下一次请求改用备用模型；429 会打断连续 529 计数。
最后一次失败后直接返回 `MaxRetriesExceeded`，不会再做没有后续请求的 sleep。

## 代码边界

```text
s11_error_recovery/
├── code.py
└── harness/
    ├── agent_loop.py          父 Agent 状态机与恢复动作编排
    ├── error_recovery.py      错误分类、退避计算、RecoveryState
    ├── provider.py            model/max_tokens 请求参数适配
    ├── context_compact.py     s08 压缩算法与 reactive compact
    └── ...                    s01–s10 其余累计能力
```

`error_recovery.py` 不依赖 `AgentHarness`，只提供纯分类函数、有界状态和重试器。
`agent_loop.py` 决定何时改写 `messages`，因为只有循环知道当前 assistant 是否
完整、是否允许追加 continuation，以及 compact 后应从哪里重试。

当前课程切片把恢复接在父 Agent 的主模型请求上。Memory 召回/提取、Compact 摘要和
SubAgent 内部请求仍保留各自原有失败语义；这是本章明确的实现边界，不把所有 SDK
调用悄悄包装成全局重试。

## 配置

```bash
export DASHSCOPE_API_KEY=...
export MODEL_ID=qwen-plus
# 可选：连续三次 529 后使用
export FALLBACK_MODEL_ID=qwen-turbo

cd /data/projects/cc-harness-lab
python3 s11_error_recovery/code.py
```

CLI 启动时会打印主模型、备用模型、工作目录和当前 System Prompt sections。

## 测试

```bash
python3 -m pytest -q tests/test_s11.py
python3 -m pytest -q
```

s11 的测试同时保留 s10 的 14 项结构回归，并新增 12 项恢复测试，覆盖：

- 429 指数退避、`Retry-After` 和最大尝试次数；
- 三次连续 529 后切换 fallback；
- 非瞬态错误不重试；
- 首次截断不污染历史，8K 正确提升到 64K；
- 64K 截断后的续写消息和三次上限；
- prompt-too-long 只压缩一次；
- 恢复耗尽后 CLI 得到 assistant 错误文本而不是 Python traceback。

429、529、Prompt 溢出和输出截断使用确定性的故障注入测试，不依赖真实服务恰好发生
故障；真实百炼调用用于验证正常模型和工具链仍兼容。

## 与官方实现的关系

机制与教程一致：8K→64K、三次 continuation、一次 reactive compact、429/529
指数退避、三次 529 fallback 和十次总尝试。差异来自本仓库的累计架构和协议：

- 官方是聚焦单章机制的 Anthropic 教学切片；本目录保留 s01–s10 全部能力；
- 官方读取 Anthropic 的 `stop_reason=max_tokens`；本仓库读取 OpenAI-compatible
  的 `finish_reason=length/max_tokens`；
- 官方工具结果使用 content blocks；本仓库必须维护 `tool_calls` 与独立
  `role=tool` 消息的配对；
- 本仓库把纯恢复策略拆进 `error_recovery.py`，Agent Loop 只编排状态迁移；
- 不可恢复异常被转成可见 assistant 消息，交互式 CLI 不会整进程退出。
