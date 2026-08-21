# s12 AgentHarness 调用图

> 配套 [README.md](./README.md) 与 [ANALYSIS.md](./ANALYSIS.md) 阅读。
> 本图描述 [harness/agent.py](./harness/agent.py) 中 `AgentHarness.agent_loop`
> （L249-380）一次用户 turn 的调用关系。本章的看点是**四层能力如何叠加**——
> Task 只是新注册的 handler，没有任何 `agent_loop` 内特判。
> 行号对应各模块当前版本。

图例：🟢 Task（s12 新增）；🔴 Recovery（s11）；🟩 Prompt（s10）；🟣 Memory（s09）；
🔵 Compact（s08）；🟠 模型 API 调用；⬜ 工具 / hooks / 通用逻辑。

## 总览

![s12 AgentHarness 调用图](./callgraph.svg)

## 一次请求的分层顺序

```text
s10  refresh_system_prompts   → tools section 自动包含六个任务工具
s08  compactor.prepare        → 四层压缩
s11  with_retry(create, …)    → 截断 / 超限 / 429 / 529 各自有限恢复
     ↓ 调用成功
s12  execute_tool → TaskManager.run_* → TaskStore → .tasks/*.json
```

恢复发生在**模型调用层**，任务工具执行在其后——一次 429 重试不会重复执行
`create_task`。

## 四个阶段的要点

### ⓪ 组装期

| 调用 | 位置 | 作用 |
|---|---|---|
| `Settings.from_env` | config.py | `CC_TASKS_DIR` → `tasks_dir` |
| `TaskStore(tasks_dir, workdir)` / `TaskManager(store)` | agent.py L63-64 | **本章新增**，路径边界随构造参数传入 |
| 两个 `SystemPromptAssembler` | agent.py L79-84 | 父 / 子各一（s10） |
| `sleep_fn` / `random_fn` | agent.py L52-59 | 可注入的时钟与随机源（s11） |
| `parent_handlers` / `sub_handlers` | agent.py L75-86 | 15 / 6 个工具，任务工具仅父 Agent |

### ① turn 开始

| 调用 | 位置 | 作用 |
|---|---|---|
| `copy.deepcopy(messages[-12:])` | agent.py L256 | 提取快照 |
| `refresh_system_prompts(messages)` | agent.py L257 | tools section 自动含六个任务工具 |
| `memory.load_memories` → `inject_recalled_memories` | agent.py L258-259 | 注入最近 user 消息 |
| `RecoveryState(self.settings.model)` | agent.py L265 | 恢复预算按 turn 重置 |

### ② while True 循环体

| 调用 | 位置 | 作用 |
|---|---|---|
| todo reminder 检查 | agent.py L268-271 | 3 轮未 `todo_write` 注入提醒 |
| `refresh_system_prompts` | agent.py L273 | 每轮刷新，命中缓存则复用 |
| `compactor.prepare` | agent.py L274 | s08 四层管线 |
| `with_retry(create, state, fallback, …)` | agent.py L290 | 只重试 429/529 |
| ↳ `is_prompt_too_long_error` → `reactive_compact` | agent.py L307 | 一次；再失败走 `_record_error` |
| ↳ `is_output_truncated` | agent.py L321 | 首次升 64K 且 payload 不入列 |
| assistant 双写 | agent.py L340 | 主历史 + 提取快照 |
| `execute_tool` → `execute_with_handlers` | agent.py L160 / L128 | PreToolUse → handler → PostToolUse |
| ↳ `run_create` → `TaskStore.create` | tasks.py L276 / L93 | `open("x")` 排他创建 + 冲突重摇 ID |
| ↳ `run_update` → `update_dependencies` | tasks.py L284 / L168 | 全批校验、去重、自依赖与传递环检测后才保存 |
| ↳ `run_claim` / `run_complete` | tasks.py L322-326 → L224 / L238 | 依赖齐备才认领；完成时用差集报告新解锁 |
| ↳ `TaskStore.save` | tasks.py L118 | 同目录临时文件 + `os.replace` 原子替换 |
| ↳ `_observe` | tasks.py L269 | 存储异常 → `Error: …` 观察，不击穿 Loop |
| tool 结果双写 | agent.py L371-377 | 沿用原 `tool_call_id` |
| `compact_history` | agent.py L379 | 批次收尾，每 turn 一次 |

### ③ turn 结束

| 出口 | 位置 | 行为 |
|---|---|---|
| 正常结束 | agent.py L350-352 | `extract_memories` → `consolidate_memories` → `return` |
| Stop Hook 要求继续 | agent.py L344-349 | 续写消息双写后回到循环 |
| `_record_error` | agent.py L243 / L307 / L310 | 错误入 messages，**跳过记忆提取** |

## 任务文件的四道边界

`.tasks/task_<8 hex>.json` 每次读写都过：

| 关口 | 位置 | 检查 |
|---|---|---|
| `_root` | tasks.py L36 | `tasks_dir` resolve 后必须在 `workdir` 内 |
| `_path` | tasks.py L46 | `fullmatch ^task_[0-9a-f]{8}$`，拼接后再验 `relative_to` |
| `load` | tasks.py L137 | `set(data)` 必须**严格等于**六字段集合 |
| `_validate_record` | tasks.py L66 | 状态白名单 · owner 类型 · `blockedBy` 无重复无自依赖 |

依赖文件缺失或损坏时，`incomplete_dependencies`（tasks.py L211）把它计为未完成——
任务**保持阻塞**而不是误判为就绪放行。

## 三条贯穿性线索

1. **新能力只需注册**：`tools.py` 追加 `*TASK_TOOLS`、handler 表追加六项，就自动获得
   了 s10 的 Prompt 同步、统一入口的权限管线和 Pre/PostToolUse Hook。`agent_loop`
   内零特判——只有 `compact` 需要内联，因为它要改写 `messages` 本身。
2. **权限隔离在组装期固化**：父/子 handler 表和工具列表在 `__init__` 就定死，循环体内
   不再判断"这个工具允许吗"。子 Agent 的 Prompt 由 `SUB_TOOLS` 派生，因此连提都不会
   提到任务工具。
3. **三条独立持久层**：`.tasks/`（任务图，跨会话）、`.memory/`（长期事实，跨会话）、
   `.transcripts/` + `.task_outputs/`（压缩归档，会话内可回溯）。互不依赖，任何一条
   失效都不阻断主回答。
