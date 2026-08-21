# s11 源码剖析：错误分类与恢复状态机相对 s10 做了哪些修改

> 配套 [README.md](./README.md) 阅读。本文基于 s10 与 s11 harness 包的逐文件 diff
> （1951 → 2161 行，净增 210 行），回答一个问题：
> **为了让模型调用失败可分类、可恢复且有上限，harness 动了哪些地方，为什么这么动。**
> 行号对应各模块当前版本。

## 一、修改总览

s10 的十个模块里**七个字节不变**，改动集中在一个新模块加三处接入：

| 文件 | 状态 | 改动 |
|---|---|---|
| `harness/recovery.py` | **新增 139 行** | 错误分类器、`RecoveryState`、`with_retry`、退避算法 |
| `harness/agent.py` | 305 → 372 | 注入 `sleep_fn`/`random_fn`、`_record_error`、三条恢复路径 |
| `harness/config.py` | +2 行 | `Settings.fallback_model`（`FALLBACK_MODEL_ID`） |
| `harness/__init__.py` | 扩展导出 | 新增 recovery 相关符号 |
| `code.py` | 56 → 59 | 入口微调 |
| `compaction.py` `hooks.py` `memory.py` `models.py` `prompt.py` `skills.py` `tools.py` | **完全不变** | s01–s10 能力零回归 |

`tests/test_s11.py` 11 个用例（已实测通过）。

## 二、核心设计：三条独立预算的恢复路径

s10 的 `agent_loop` 对失败只有一种态度——`raise`（除了 reactive compact 那一条）。
s11 把失败拆成三类，每类有**自己的预算**，互不消耗：

| 故障 | 判据 | 恢复动作 | 上限 |
|---|---|---|---|
| 输出截断 | `finish_reason ∈ {length, max_tokens}` | 首次升 `max_tokens`，之后注入续写提示 | 升级 1 次 + 续写 3 次 |
| 上下文超限 | `is_prompt_too_long_error(exc)` | 调 s08 `reactive_compact` 后重试 | 1 次 |
| 429 / 529 | status code / 异常类名 / 消息文本 | 指数退避 + 抖动，连续 529 切备用模型 | 10 次 |

预算分离体现在 `RecoveryState`（recovery.py [L28](./harness/recovery.py)）的五个字段上：

```python
current_model: str                        # 可能被 fallback 替换
has_escalated: bool = False               # 输出预算是否已升级
continuation_count: int = 0               # 已续写次数
consecutive_529: int = 0                  # 连续过载次数
has_attempted_reactive_compact: bool = False
```

每个 turn 新建一个 `state`（agent.py L256），所以预算按 turn 重置——上一轮的重试
次数不会让这一轮提前放弃。

## 三、输出截断：为什么第一次不追加

这是本章最讲究的一处。截断检查放在 `tool_calls` 判断**之前**（agent.py
[L312](./harness/agent.py)）：

```python
if is_output_truncated(getattr(choice, "finish_reason", None)):
    if not state.has_escalated:
        max_tokens = ESCALATED_MAX_TOKENS      # 8K → 64K
        state.has_escalated = True
        continue                                # ← 注意：payload 没有入列
    messages.append(payload)                    # 64K 仍截断才保存
    ...
```

**第一次截断不把 `payload` 写进 `messages`**，用完全相同的消息重试。原因是截断的
assistant 消息可能包含**半截 `tool_calls`**——JSON 参数只输出了一半。一旦写进历史：

- 这条 assistant 有 `tool_calls` 但没有对应的 `role=tool` 结果 → 消息序列非法；
- 或者参数 JSON 解析失败 → 工具执行必然报错；
- 更糟的是它会永久留在上下文里，后续每轮都带着这段垃圾。

先升预算重试，等于**当这次响应没发生过**。只有 64K 仍然截断（说明任务本身太大，
不是预算问题），才保存部分输出并注入续写提示：

```text
Output token limit hit. Resume directly — no apology, no recap.
Pick up mid-thought and break remaining work into smaller pieces.
```

提示词明确要求"不道歉、不复述、从中断处继续、把剩余工作拆小"——避免模型用几百
token 重新总结已经说过的内容，那会让截断问题自我循环。

截断检查位于 `tool_calls` 分支之前还有一个副作用：**截断响应里的工具调用永远不会
被执行**。半截参数不会打到文件系统上。

## 四、`with_retry`：只消费确定的瞬态错误

`with_retry`（recovery.py [L98](./harness/recovery.py)）的核心是一行取反判断：

```python
if not (is_rate_limit_error(error) or is_overloaded_error(error)):
    raise
```

**只有 429 和 529 会被重试，其余立刻上抛**。认证失败、参数错误、程序 bug 重试
十次也不会变好，只会浪费 30 秒并掩盖真正的问题。

三个分类器都用"三选一"的宽松匹配（recovery.py L46-60）：

```python
error_status_code(error) == 429 or "ratelimit" in name or "429" in text
```

`error_status_code`（[L37](./harness/recovery.py)）先看 `error.status_code`，再看
`error.response.status_code`——不同 SDK 版本把状态码放在不同位置。类名和消息文本
是兜底，应对包装过的异常。

### 退避算法

```python
min(0.5 * 2**attempt, 32.0) + random(0, base * 0.25)
```

- **指数增长**，32 秒封顶；
- **0–25% 抖动**：多个客户端同时被限流时，纯指数退避会让它们在同一时刻一起重试，
  抖动把重试时刻打散；
- **`Retry-After` 优先**（[L92-93](./harness/recovery.py)）：服务端明确说了等多久
  就照做，本地算法只是没有该头时的估计。`retry_after_seconds` 同时试
  `retry-after` 和 `Retry-After` 两种大小写，解析失败返回 `None` 而不是抛错。

### 连续 529 切备用模型

```python
if state.consecutive_529 >= MAX_CONSECUTIVE_529 and fallback_model
        and state.current_model != fallback_model:
    state.current_model = fallback_model
    state.consecutive_529 = 0
```

只有 529（过载）累计，429（限流）不累计——限流是配额问题，换模型通常没用；过载是
这个模型的容量问题，换一个才有意义。切换后计数器归零，给新模型同样的预算。

注意 `state.consecutive_529 = 0` 也出现在成功路径（[L112](./harness/recovery.py)）：
一次成功就清零，避免跨越很长时间的零星 529 累积触发误切换。

预算耗尽抛 `MaxRetriesExceeded`（[L24](./harness/recovery.py)），并用
`from last_error` 保留原始异常链，排查时能看到最后一次真实失败。

## 五、agent_loop 不再抛异常

s10 的 `agent_loop` 遇到非 prompt-too-long 的异常会 `raise`，调用方要自己 try。
s11 引入 `_record_error`（agent.py [L233](./harness/agent.py)）：

```python
answer = f"Error: {text[:300]}"
messages.append({"role": "assistant", "content": answer})
return answer
```

三个失败出口都走它：reactive compact 后仍超限、非瞬态异常、重试预算耗尽
（`MaxRetriesExceeded` 会被同一个 `except` 捕获）。

两个值得注意的后果：

1. **错误进入对话历史**（作为 assistant 消息），下一轮用户提问时模型能看到上一轮
   失败过——这是有意的上下文保留；
2. **失败路径不触发记忆提取**：`_record_error` 直接 `return`，不经过
   `extract_memories` / `consolidate_memories`。API 故障不会被写成长期记忆。

截断路径的最终出口略有不同（[L328](./harness/agent.py)）：续写三次仍未完成时返回
`assistant.content or "Error: output recovery limit reached"`——**优先返回模型已经
产出的部分内容**，只有内容为空才给错误字符串。这是合理的：三次续写攒下的文本对用户
仍有价值。

## 六、可注入的时钟与随机源

`with_retry` 的 `sleep_fn` 和 `random_fn` 是关键字参数（recovery.py L104-105），
`AgentHarness.__init__` 也把它们提到构造参数（agent.py [L52-59](./harness/agent.py)）：

```python
def __init__(self, client, settings, *,
             sleep_fn=time.sleep, random_fn=random.uniform):
```

测试可以传入 `lambda _: None` 和固定随机源，于是"重试 10 次"的用例**零等待**执行，
并且退避时长可精确断言。如果直接调 `time.sleep`，一个完整的重试预算测试要跑
0.5+1+2+4+8+16+32… 秒。

`with_retry` 同时被父 Agent（agent.py L281）和 `spawn_subagent`（L178）使用，两处
都传入同一对注入函数——子 Agent 的瞬态失败同样受保护。子 Agent 用自己的
`RecoveryState`（L169），预算独立。

## 七、一处需要注意的重复定义

s11 里 `is_prompt_too_long_error` 有**两个不同实现**：

| 位置 | 匹配的标记 |
|---|---|
| compaction.py L337 | `prompt_too_long`、`too many tokens`、`maximum context length`、`context_length_exceeded`、`request too large`、`http 413` |
| recovery.py L63 | `prompt`+`long` 同时出现、`prompt_is_too_long`、`context_length_exceeded`、`max_context_window` |

`agent.py` 导入的是 **recovery 版本**（L34），而 s10 用的是 compaction 版本。两者
并非等价：

- recovery 版新增了 `max_context_window`；
- 但**丢失了** `too many tokens`、`maximum context length`、`request too large`、
  `http 413` 四个标记（`prompt_too_long` 因含 "prompt" 和 "long" 仍能命中）。

也就是说，返回 `HTTP 413` 或 "too many tokens" 的服务端，在 s11 里不会走 reactive
compact，而会被 `_record_error` 记成普通错误。compaction.py 里的那份现在无人调用。
建议后续合并为单一实现并取两者标记的并集。

## 八、防御与权衡

- **恢复逻辑本身不能变成死循环**：三条路径各自有硬上限，且
  `has_attempted_reactive_compact` 是布尔而非计数器——压缩过一次就不再尝试；
- **工具业务错误不是 API 故障**：handler 返回的 `Error: ...` 字符串照常作为
  observation 进入对话，不会被 `with_retry` 误判重试；
- **`max_tokens` 升级不回退**：`has_escalated` 一旦为真，本 turn 后续所有请求都用
  64K。turn 结束后随 `state` 一起丢弃；
- **权衡：错误变成字符串**。`agent_loop` 不再抛异常，调用方无法用 `try/except`
  区分"模型说了 Error"和"调用失败了"，只能看返回文本前缀；
- **权衡：`MaxRetriesExceeded` 被自己的 except 吞掉**。它继承 `RuntimeError`，会被
  agent.py L290 的 `except Exception` 捕获并转成 `_record_error` 文本，异常类型信息
  只保留在字符串里。

## 九、一句话总结

s11 把"模型调用可能失败"从隐式假设变成显式状态机：截断、超限、瞬态三类故障各自
分类、各自有独立且有限的预算，第一次截断丢弃响应而不是把半截 `tool_calls` 写进
历史，只有 429/529 消费重试次数，时钟与随机源可注入因此整套逻辑能零等待测试——
**恢复能力本身也被约束住了，不会成为新的不确定性来源**。
