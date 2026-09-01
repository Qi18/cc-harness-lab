# s11 agent_loop 调用图

> 配套 [README.md](./README.md) 与 [ANALYSIS.md](./ANALYSIS.md) 阅读。
> 本图描述 [harness/agent_loop.py](./harness/agent_loop.py) 中 `agent_loop`
> （L234-L402）一次用户 turn 的完整调用关系，按"装配 → turn 开始 → 请求与瞬态恢复 →
> 响应分类 → 工具批次 → turn 结束"六段组织。未标模块名的行号都属于 `agent_loop.py`。

图例：🔴 恢复分支（s11 新增）；🟢 Prompt 组装（s10）；🟣 Memory 子系统（s09）；
🔵 Compact 子系统（s08）；🟠 模型 API 调用；⬜ 循环阶段、工具与 Hook。

## 总览

![s11 Error Recovery 调用图](./callgraph.svg)

SVG 沿用 s10 的制图约定：左列自上到下是父 Agent 的阶段，右列是被调用的子系统，
实线为控制流、虚线为模块引用。s11 新增的恢复分支用红底标出：阶段 ② 内部的
429 / 529 与 prompt 溢出两行、阶段 ③ 的三行截断处理，以及右列首个
`error_recovery.py` 盒。左侧竖向回路把“重放 / 续写”与“下一轮工具循环”归到同一条
边：两者都回到阶段 ② 顶部，因此每次重试都会重新刷新 Prompt 并重新 `prepare`。

## 六个阶段的要点

### ⓪ 装配：composition root 只在这里执行

| 调用 | 位置 | 作用 |
|---|---|---|
| `sleep_fn` / `random_fn` 参数 | L58-L59 | 🔴 退避时钟与抖动可注入，测试不等待 |
| `SkillLoader` / `TodoManager` / `BuiltinTools` | L70-L72 | 先建有状态能力 |
| `install_default_hooks` → `ToolExecutor` | L76-L77 | Hook 必须早于执行器 |
| `CompactToolController` / `ContextCompactor` | L80-L86 | 🔵 控制面与算法面分离 |
| `memory.configure(settings)` | L88 | 🟣 把模块级 Memory 绑定到本 Harness |
| 父 / 子 `SystemPromptAssembler` | L91-L96 | 🟢 身份不同，缓存也各自独立 |
| `refresh_system_prompts()` | L99 | 🟢 生成首版 Prompt，供 `code.py` 建首条消息 |
| `SubagentRunner(prompt_supplier=...)` | L104-L110 | 子 Agent 只拿到取 Prompt 的回调 |

`settings.fallback_model` 不在装配期读取，它由每次 `with_retry` 调用时传入（L293），因此
更换环境变量后无需重建 Harness。

### ① turn 开始：每 turn 恰好一次

| 调用 | 位置 | 作用 |
|---|---|---|
| `latest_user_request` | L243 / context_compact.py L343 | `active_request` 未传时从历史尾部兜底 |
| `copy.deepcopy(messages[-12:])` | L247 | 建立提取快照，与主历史隔离 |
| `refresh_system_prompts(messages)` | L250 | 🟢 先把最新 Prompt 写回 `messages[0]` |
| `memory.load_memories` | L251 / memory.py L303 | 🟣 side-query 选最多 5 条 |
| `memory.inject_recalled_memories` | L252 / memory.py L332 | 🟣 正文附加到最新 user turn |
| `compactor = compactor or self.compactor` | L256 | 🔵 测试可注入替代压缩器 |
| `RecoveryState(current_model=settings.model)` | L261 | 🔴 恢复额度归零，与 turn 同生命周期 |

三个计数器并列创建：`rounds_since_todo`、`manual_compactions`、`recovery`（L259-L261）。
它们都不跨 turn 累积。

### ② 请求与瞬态恢复：每次循环一次

| 调用 | 位置 | 作用 |
|---|---|---|
| todo reminder 检查 | L266-L273 | 连续 3 轮未 `todo_write` 注入提醒 |
| `refresh_system_prompts(messages)` | L276 | 🟢 反映本轮工具、Skill、Memory 状态 |
| `compactor.prepare` | L277 / context_compact.py L315 | 🔵 L3 → L1 → L2 → L4 |
| `_visible_parent_tools` | L278 / L158 | 本 turn 压缩过就不再暴露 `compact` |
| `with_retry(fn, recovery, fallback)` | L282 / error_recovery.py L126 | 🔴 仅拦截 429 / 529，最多 10 次 |
| ↳ `provider.completion_request` | L284 / provider.py L32 | 收口 `model` 与 `max_tokens` 覆盖 |
| ↳ `chat.completions.create` | L283 | 🟠 主模型调用 |
| ↳ `retry_delay` → `sleep_fn` | error_recovery.py L113 / L180 | 🔴 0.5s 起步、32s 封顶、0–25% jitter |
| ↳ 连续 3 次 529 → 切 `current_model` | error_recovery.py L153-L162 | 🔴 下一次请求才使用 fallback |
| `is_prompt_too_long_error` | L301 / error_recovery.py L75 | 🔴 与瞬态错误分开判定 |
| ↳ `compactor.reactive_compact` | L304 / context_compact.py L288 | 🔵 每 turn 仅一次，压缩后 `continue` |
| `_append_failure_result` | L313 / L220 | 🔴 其余异常收口为最终 assistant 文本 |

刷新一定在 `prepare` 之前（L276 → L277）：否则压缩会按旧 Prompt 体积估算预算。恢复分支
全部以 `continue` 回到循环顶部，因此每次重试都重新走一遍"刷新 → 压缩 → 组装请求"。

### ③ 响应分类：完整性检查先于任何历史写入

| 调用 | 位置 | 作用 |
|---|---|---|
| `response.choices[0]` / `choice.message` | L317-L318 | 取本轮响应 |
| `is_output_truncated(finish_reason)` | L322 / error_recovery.py L92 | 🔴 `length` 与 `max_tokens` 都算截断 |
| 首次截断：`max_tokens = 64000` | L323-L331 | 🔴 丢弃片段，用原历史重放 |
| 已升级：保存纯文本片段 | L333-L337 | 🔴 只写 content，不写残缺 `tool_calls` |
| 达到 `MAX_CONTINUATIONS` | L340-L353 | 🔴 返回最后片段；片段为空则返回显式错误 |
| 追加 `CONTINUATION_PROMPT` | L355-L366 / error_recovery.py L19 | 🔴 最多 3 条续写提示 |
| `assistant_payload` | L369 / provider.py L23 | 完整消息才双写主历史与快照 |

截断分支在 `assistant_payload()` 之前就 `continue`，所以不完整的响应**永远不会进入工具
分发**。

### ④ 工具批次：compact 仍是唯一例外

| 调用 | 位置 | 作用 |
|---|---|---|
| `_execute_tool_batch` | L392 / L178 | 只维护协议与分发，返回控制信号 |
| ↳ `CompactToolController.request` | L196 / context_compact.py L364 | 🔵 校验空参数、走 Hook、每 turn 限一次 |
| ↳ `_execute_parent_tool` → `ToolExecutor.execute` | L203 / L174 / tool_use.py L153 | 解析 → PreToolUse → handler → PostToolUse |
| ↳ `PermissionPolicy.check` | hooks.py L70 / permission.py L73 | 硬拒绝或交互确认，返回非 None 即短路 |
| `role=tool` 双写 | L210-L216 | 每个 `tool_call_id` 都要有配对结果 |
| `compactor.compact_history` | L399-L402 / context_compact.py L274 | 🔵 批次收尾才真正改写历史 |

### ⑤ turn 结束：仅在给出最终回答时

| 调用 | 位置 | 作用 |
|---|---|---|
| `hooks.trigger("Stop")` | L378 | 返回非 None 则追加 user 消息并回到 ② |
| `memory.extract_memories` | L386 / memory.py L425 | 🟣 输入是快照，不受本 turn 压缩影响 |
| `memory.consolidate_memories` | L387 / memory.py L476 | 🟣 ≥10 条合并到 ≤8 条 |
| `return answer` | L388 | 正常出口 |

s11 新增了三个**非正常出口**，它们都不触发 Memory 提取：`_append_failure_result` 返回
（L313）、continuation 上限返回（L353）、以及第二次 prompt 溢出走同一个 L313。

## 恢复模块调用关系

```text
agent_loop.py
├─ RecoveryState(settings.model)              L261      本 turn 恢复额度
├─ with_retry(fn, state, fallback_model)      L282      error_recovery.py L126
│  ├─ is_rate_limit_error                               L53
│  ├─ is_overloaded_error                               L64
│  ├─ retry_after_seconds                               L98   服务端秒数优先
│  ├─ retry_delay → sleep_fn                            L113 / L180
│  └─ consecutive_529 ≥ 3 → current_model = fallback    L155-L157
├─ is_prompt_too_long_error                   L301      L75
│  └─ ContextCompactor.reactive_compact       L304      context_compact.py L288
├─ is_output_truncated                        L322      L92
│  ├─ 8K → 64K 原请求重放                     L323-L331
│  └─ partial assistant → continuation user   L333-L366
└─ format_unrecoverable_error                 L227      L187   截断到 1000 字符
   └─ 最终 assistant Error 消息               L228-L230
```

## 消息写入规则

| 分支 | assistant 写入 `messages` | user 写入 `messages` | 是否重试原请求 |
| --- | --- | --- | --- |
| 429 / 529 | 否 | 否 | 是，同一份历史 |
| 首次输出截断 | 否 | 否 | 是，预算提到 64K |
| 64K 后输出截断 | 是，只保存文本片段 | 是，continuation | 否，改为续写 |
| 首次 prompt too long | 否 | 否，历史被压缩替换 | 是 |
| 第二次 prompt too long | 是，错误文本 | 否 | 否 |
| 普通不可恢复错误 | 是，错误文本 | 否 | 否 |
| 完整响应 | 是，完整 payload | 仅 Stop Hook 可能追加 | 按正常循环 |

凡是写入 `messages` 的分支都同步写入 `extraction_messages`（L230、L337、L348、L360），
双写不变量在恢复路径上同样成立。

## 依赖层

| 层 | 模块 | 内部依赖 |
|---|---|---|
| L0 | `config.py`、`models.py`、`system_prompt.py`、`error_recovery.py` | 无 |
| L1 | `provider.py`、`permission.py`、`skill_loading.py`、`todo_write.py` | L0 |
| L2 | `hooks.py`（→ `permission`）、`memory.py`（→ `skill_loading`） | L0-L1 |
| L3 | `tool_use.py`、`context_compact.py`（均 → `hooks`） | L0-L2 |
| L4 | `subagent.py`（→ `tool_use`） | L0-L3 |
| L5 | `agent_loop.py` | L0-L4，composition root |

课程编号是学习顺序，不是依赖层级：`error_recovery.py` 是第十一课，却因为零内部导入而位于
最底层；`agent_loop.py` 对应 s01，却位于最上层。下层模块都不导入 `AgentHarness`，因此依赖
图无环。

## 三条贯穿性线索

1. **每条恢复路径都有计数上限**：`MAX_TRANSIENT_ATTEMPTS`=10、`MAX_CONSECUTIVE_529`=3、
   `MAX_CONTINUATIONS`=3、reactive compact 每 turn 1 次
   （error_recovery.py L14-L18、agent_loop.py L302）。上限用尽后一律汇聚到
   `_append_failure_result`，不存在"重试到死"的分支。
2. **完整性先于写入**：`finish_reason` 判定（L322）早于 `assistant_payload`（L369），
   `role=tool` 批次（L210-L216）早于 `compact_history`（L401）。两条规则合起来保证
   `tool_calls` 与 `role=tool` 永远成对，压缩与重试都以协议组为最小单元。
3. **恢复状态不外溢**：`RecoveryState` 与 turn 同生命周期，fallback 只改
   `state.current_model` 而不改 `Settings`，64K 只改 `state.max_tokens` 而不改默认值。
   下一个用户请求一定从主模型、8K 预算和零重试状态重新开始。
