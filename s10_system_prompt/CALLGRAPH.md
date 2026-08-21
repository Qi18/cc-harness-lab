# s10 AgentHarness 调用图

> 配套 [README.md](./README.md) 与 [ANALYSIS.md](./ANALYSIS.md) 阅读。
> 本图描述 [harness/agent.py](./harness/agent.py) 中 `AgentHarness.agent_loop`
> （L205-305）一次用户 turn 的调用关系，并单列本章核心的 **Prompt 组装链**。
> 行号对应各模块当前版本。

图例：🟩 Prompt 组装（s10 新增）；🟣 Memory（s09）；🔵 Compact（s08）；
🟠 模型 API 调用；⬜ 工具 / hooks / 通用逻辑。

## 总览

![s10 AgentHarness 调用图](./callgraph.svg)

## Prompt 组装链

四个调用点共用同一条链路，区别只在传不传 `messages`：

| 调用点 | 位置 | 传 `messages` | 目的 |
|---|---|---|---|
| `__init__` 末尾 | agent.py L67 | 否 | 填好 `self.system_prompt` / `self.sub_system_prompt` |
| `agent_loop` 开头 | agent.py L213 | 是 | 写入 `messages[0]` |
| **每轮循环内** | agent.py L228 | 是 | 本 turn 内状态变化即时生效 |
| `spawn_subagent` | agent.py L144 | 否 | 子 Agent 自建 messages |

链路本身（`refresh_system_prompts`，agent.py L79）：

| 步骤 | 位置 | 说明 |
|---|---|---|
| `skills.registry = skills.scan()` | agent.py L83 | **每轮重扫磁盘**，Skill 可热发现 |
| `_prompt_context(tools)` | agent.py L69 | 打包四项运行时事实 |
| ↳ `registered_tool_names(tools)` | prompt.py L26 | 工具清单由注册表派生，不可能失真 |
| ↳ `skills.catalog() if registry else ""` | agent.py L73-75 | 无 Skill 则不生成该 section |
| ↳ `memory.read_memory_index()` | agent.py L76 | 无索引则不生成该 section |
| `SystemPromptAssembler.get` | prompt.py L84 | 父/子两个独立实例，各自缓存 |
| ↳ `context_key` | prompt.py L44 | 确定性 JSON（`sort_keys` + `default=str`），不用 `hash()` |
| ↳ 命中 → `cache_hits++` / 未命中 → `assemble()` | prompt.py L86-92 | 两个计数器可断言 |
| `assemble` | prompt.py L53 | identity · guidance · tools · workspace ｜ skills ｜ memory |
| ↳ `last_sections` | prompt.py L81 | 本次生效的 section 名，可观测量 |
| 写回 `messages[0]` | agent.py L92-95 | 首条不是 system 就 `insert(0, …)` |

## 三个阶段的要点

### ① turn 开始

| 调用 | 位置 | 作用 |
|---|---|---|
| `latest_user_request` | agent.py L211 | `active_request` 未传时兜底 |
| `copy.deepcopy(messages[-12:])` | agent.py L212 | 提取快照，与 Compact 解耦 |
| `refresh_system_prompts(messages)` | agent.py L213 | 组装并写入 `messages[0]` |
| `memory.load_memories` → `inject_recalled_memories` | agent.py L214-215 | 注入**最近 user 消息**，不碰 system |

两条注入路径互不干扰：Prompt 刷新只改 `messages[0]`，记忆召回只改最后一条 user。

### ② while True 循环体

| 调用 | 位置 | 作用 |
|---|---|---|
| todo reminder 检查 | agent.py L224-227 | 3 轮未 `todo_write` 注入提醒 |
| **`refresh_system_prompts(messages)`** | agent.py L228 | 每轮刷新；状态不变则命中缓存 |
| `compactor.prepare` | agent.py L229 | s08 四层管线；system 前缀永不压缩 |
| `chat.completions.create` | agent.py L245 | 已手动压缩过则从工具表摘除 `compact` |
| `is_prompt_too_long_error` → `reactive_compact` | agent.py L249-256 | 溢出兜底，最多 1 次 |
| assistant 消息双写 | agent.py L264-265 | 主历史 + 提取快照 |
| `request_manual_compact` | agent.py L286 | `compact` 内联特判（要改 `messages` 本身） |
| `execute_tool` → `execute_with_handlers` | agent.py L293 / L97 | PreToolUse → handler → PostToolUse |
| tool 结果双写 | agent.py L296-302 | 沿用原 `tool_call_id` |
| `compact_history` | agent.py L303-305 | 批次收尾统一执行 |

### ③ turn 结束

| 调用 | 位置 | 作用 |
|---|---|---|
| `hooks.trigger("Stop")` | agent.py L269 | 要求继续则回到 ② |
| `memory.extract_memories` | agent.py L275 | 输入是快照，不受 Compact 影响 |
| `memory.consolidate_memories` | agent.py L276 | 写入改变 `MEMORY.md` → **下一轮 memory section 随之更新** |

## 三条贯穿性线索

1. **Prompt 不能声称不存在的能力**：`tools` section 由 `PARENT_TOOLS` / `SUB_TOOLS`
   派生，而 `execute_with_handlers` 分发的也正是同一份 handler 表。父子各用一个
   assembler，子 Agent 的 Prompt 里不会出现 `todo_write` / `task` / `compact`。
2. **缓存在扫描下游**：`skills.scan()` 和 `read_memory_index()` 每轮都执行，之后才
   算 context key。命中缓存省下的是字符串拼接，不是磁盘 IO。
3. **三个子系统各改各的消息位置**：Prompt 改 `messages[0]`、记忆召回改最后一条
   user、Compact 重建整个列表但保留 system 前缀——互不覆盖是这一章能安全每轮刷新的
   前提。
