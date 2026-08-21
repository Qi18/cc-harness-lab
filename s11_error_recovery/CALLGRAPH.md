# s11 AgentHarness 调用图

> 配套 [README.md](./README.md) 与 [ANALYSIS.md](./ANALYSIS.md) 阅读。
> 本图描述 [harness/agent.py](./harness/agent.py) 中 `AgentHarness.agent_loop`
> （L240-372）一次用户 turn 的调用关系，重点是**三条独立预算的恢复路径**。
> 行号对应各模块当前版本。

图例：🔴 Recovery（s11 新增）；🟩 Prompt 组装（s10）；🟣 Memory（s09）；
🔵 Compact（s08）；🟠 模型 API 调用；⬜ 工具 / hooks / 通用逻辑。

## 总览

![s11 AgentHarness 调用图](./callgraph.svg)

## 三条路径的预算

每个 turn 新建一个 `RecoveryState`（agent.py L256），三类故障各用各的字段，
互不消耗：

| 故障 | 判据 | 恢复动作 | 上限 | 状态字段 |
|---|---|---|---|---|
| 输出截断 | `finish_reason ∈ {length, max_tokens}` | 首次升 8K→64K；之后注入续写提示 | 升级 1 + 续写 3 | `has_escalated` / `continuation_count` |
| 上下文超限 | `is_prompt_too_long_error` | `reactive_compact` 后重试 | 1 次（布尔） | `has_attempted_reactive_compact` |
| 429 / 529 | 状态码 ｜ 类名 ｜ 消息文本 | 退避重试；连续 3 次 529 切备用模型 | 10 次 | `consecutive_529` / `current_model` |

## 四个阶段的要点

### ⓪ turn 开始（继承 s09/s10，未改动）

| 调用 | 位置 | 作用 |
|---|---|---|
| `copy.deepcopy(messages[-12:])` | agent.py L247 | 提取快照 |
| `refresh_system_prompts(messages)` | agent.py L248 | s10 组装链，写 `messages[0]` |
| `memory.load_memories` → `inject_recalled_memories` | agent.py L249-250 | 注入最近 user 消息 |
| `RecoveryState(self.settings.model)` | agent.py L256 | **本章新增**，预算按 turn 重置 |
| `max_tokens = DEFAULT_MAX_TOKENS` | agent.py L257 | 起始 8K |

### ① 循环体：请求与瞬态重试

| 调用 | 位置 | 作用 |
|---|---|---|
| todo reminder 检查 | agent.py L260-263 | 3 轮未 `todo_write` 注入提醒 |
| `refresh_system_prompts` | agent.py L264 | 每轮刷新，命中缓存则复用 |
| `compactor.prepare` | agent.py L265 | s08 四层管线 |
| **`with_retry(create, state, fallback, …)`** | agent.py L281 / recovery.py L98 | 只重试 429/529 |
| ↳ 成功 | recovery.py L112 | `consecutive_529 = 0` |
| ↳ 非瞬态错误 | recovery.py L115-116 | **立刻 raise**，不盲目重试 |
| ↳ `retry_delay` | recovery.py L86 | `Retry-After` 优先；否则指数退避 + 0-25% 抖动 |
| ↳ 连续 3 次 529 | recovery.py L120-126 | 切 `fallback_model` 并清零计数 |
| ↳ 预算耗尽 | recovery.py L133 | `MaxRetriesExceeded`（`from last_error` 保留链） |
| `except`：`is_prompt_too_long_error` | agent.py L291 | 首次 → `reactive_compact` + `continue` |
| ↳ 已试过 / 其他异常 | agent.py L298-303 | `_record_error` |

### ② 循环体：截断检测与工具执行

| 调用 | 位置 | 作用 |
|---|---|---|
| **`is_output_truncated(finish_reason)`** | agent.py L312 | **在 `tool_calls` 判断之前** |
| ↳ 第 1 次 | agent.py L313-316 | 升 64K、`continue`；**payload 不入列** |
| ↳ 第 2 次起 | agent.py L317-327 | payload 双写 + 注入 `CONTINUATION_PROMPT`，≤3 次 |
| ↳ 续写用尽 | agent.py L328 | 优先返回 `assistant.content`，为空才给错误串 |
| assistant 双写 | agent.py L330-331 | 未截断的正常路径 |
| `request_manual_compact` | agent.py L352 | `compact` 内联特判 |
| `execute_tool` → `execute_with_handlers` | agent.py L359 / L116 | Pre/PostToolUse 权限 |
| tool 结果双写 | agent.py L362-368 | 沿用原 `tool_call_id` |
| `compact_history` | agent.py L369-371 | 批次收尾 |

### ③ turn 结束与四个出口

| 出口 | 位置 | 行为 |
|---|---|---|
| 正常结束 | agent.py L341-343 | `extract_memories` → `consolidate_memories` → `return answer` |
| Stop Hook 要求继续 | agent.py L335-340 | 续写消息双写后回到循环 |
| `_record_error` | agent.py L298 / L301 | 错误作 assistant 消息入列，**跳过记忆提取** |
| 续写用尽 | agent.py L328 | 返回已产出内容，同样跳过记忆提取 |

## 三条贯穿性线索

1. **第一次截断当作没发生过**：`payload` 不入列、`messages` 未被污染，只改
   `max_tokens` 后原样重试。这避免把半截 `tool_calls` 写进协议历史——那会导致
   assistant 有工具调用却无对应 `role=tool` 结果，消息序列非法。
2. **只有确定的瞬态错误消费重试预算**：认证、参数、程序 bug 立即上抛；工具 handler
   返回的 `Error: …` 是 observation，根本不经过 `with_retry`。
3. **失败不写进长期记忆**：三个非正常出口都直接 `return`，不调
   `extract_memories`。API 故障和截断残片不会污染 `.memory/`。

## 可测试性

`sleep_fn` / `random_fn` 从 `AgentHarness.__init__`（agent.py L52-59）一路传到
`with_retry`（recovery.py L104-105）。测试注入 `lambda _: None` 和固定随机源后，
"重试 10 次直到预算耗尽"这类用例**零等待**执行，退避时长也可精确断言——否则一个
完整预算测试要真实等待 0.5+1+2+4+8+16+32… 秒。

父 Agent（L281）和 `spawn_subagent`（L178）都走 `with_retry`，子 Agent 使用自己的
`RecoveryState`（L169），预算独立。
