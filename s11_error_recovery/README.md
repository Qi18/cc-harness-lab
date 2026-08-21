# s11：Error Recovery

s11 在 s10 动态 System Prompt 和 s01–s09 累计能力上，给模型调用增加分类、有限且
可测试的恢复策略。

- 官方教程：[s11 Error Recovery](https://learn.shareai.run/zh/s11/)
- 官方源码：[s11_error_recovery/code.py](https://github.com/shareAI-lab/learn-claude-code/blob/main/s11_error_recovery/code.py)
- 上一章：[s10 System Prompt](../s10_system_prompt/)

## 三条恢复路径

| 故障 | 判断 | 恢复 |
| --- | --- | --- |
| 输出截断 | OpenAI-compatible `finish_reason=length/max_tokens` | 首次 8K→64K，不保存截断响应；之后注入续写提示，最多 3 次 |
| 上下文超限 | `context_length_exceeded` 等错误 | 调用 s08 reactive compact 后重试一次 |
| 429 / 529 | status、异常类型或消息 | 指数退避 + 0–25% 抖动，优先 `Retry-After` |

连续三次 529 且配置了 `FALLBACK_MODEL_ID` 时，后续重试切换到备用模型。重试、
续写和 reactive compact 都有硬上限，防止恢复逻辑变成无限循环。

## 为什么第一次截断不追加

第一次 `length` 表示输出预算不足。Harness 直接把 `max_tokens` 从 8000 升到
64000，用完全相同的 messages 重试。如果先追加截断 assistant 消息，会把半截工具调用
或半截答案写进协议历史。

64K 仍截断时才保存输出并注入：

```text
Output token limit hit. Resume directly — no apology, no recap.
Pick up mid-thought and break remaining work into smaller pieces.
```

## 429/529 与其他错误

`with_retry` 只消费确定的瞬态错误。权限、认证、参数和普通程序错误不会盲目重试，
而是返回结构化错误给调用者。退避为：

```text
min(0.5 * 2^attempt, 32s) + random(0, base * 25%)
```

如果服务端提供数值型 `Retry-After`，它优先于本地退避。

## 累计边界

- s10 的运行时 Prompt assembly 与 cache 完整保留；
- s08 的 ContextCompactor 执行真正的 reactive compact；
- s09 Memory 仍只从有效对话提取，第一次丢弃的截断输出不会进入 Memory；
- 父 Agent 和 SubAgent 的瞬态 API 调用都经过 `with_retry`；
- Tool handler 返回的业务错误仍作为 observation，不会被误判成模型 API 故障。

## 运行

```bash
python3 s11_error_recovery/code.py
python3 -m pytest -q tests/test_s11.py
```

可选环境变量：

| 变量 | 说明 |
| --- | --- |
| `FALLBACK_MODEL_ID` | 连续 529 后使用的备用模型 |
| `MODEL_ID` | 主模型，默认 `qwen-plus` |

## 与官方实现的差异

官方使用 Anthropic `stop_reason=max_tokens`；本项目适配百炼 OpenAI-compatible
`finish_reason=length`，同时兼容部分服务返回 `max_tokens`。本项目复用真实 s08
Compact，而不是只保留最后五条消息的教学简化版。
