# s10 源码分析：运行时 System Prompt

> 配套 [README.md](./README.md) 与 [CALLGRAPH.md](./CALLGRAPH.md) 阅读。
> 行号对应 `s10_system_prompt/` 当前版本：`code.py` 57 行，`harness/` 14 个模块
> 共 2136 行。

## 一、这一章真正新增了什么

s10 的新能力不是"再写一段更长的固定 Prompt"，而是把 System Prompt 变成运行态的
投影：

```text
tools registry + workdir + skill catalog + memory index
                        ↓
              SystemPromptAssembler
                        ↓
                  messages[0]
```

对比 s09 更能看清差别。s09 的 `BASE_SYSTEM_PROMPT`（s09/code.py L196）是模块级
常量，工作区路径在 import 时插值，Skill 目录也在 import 时拼进 `SYSTEM_PROMPT`
（L232），只有 Memory 索引会在每个 turn 重新追加（L1784）。工具能力则以散文方式
手写在提示里，没有任何机制保证它与 `TOOLS` 一致。

s10 把这四类信息统一成命名 section，全部在每次模型调用前从真实数据源重算：

| section | 数据源 | 出现条件 |
| --- | --- | --- |
| identity | `PARENT_IDENTITY` / `SUBAGENT_IDENTITY` L9、L16 | 恒定 |
| guidance | `PARENT_GUIDANCE` / `SUBAGENT_GUIDANCE` L10、L20 | 恒定 |
| tools | `registered_tool_names()` L26 读 schema 注册表 | 恒定 |
| workspace | `settings.workdir` | 恒定 |
| skills | `SkillLoader.catalog()` skill_loading.py L83 | 注册表非空 |
| memory | `memory.read_memory_index()` memory.py L159 | `MEMORY.md` 存在 |

## 二、组装链

`AgentHarness._prompt_context()`（agent_loop.py L98）负责收集运行态，它是 Prompt
与 Harness 之间唯一的耦合点：

```python
{
    "enabled_tools": registered_tool_names(tools),
    "workspace": str(self.settings.workdir),
    "skill_catalog": self.skills.catalog() if self.skills.registry else "",
    "memory_catalog": memory.read_memory_index(),
}
```

`SystemPromptAssembler.assemble()`（system_prompt.py L55）把 context 展开成
`(name, text)` 序列：前四个 section 固定生成，Skill 与 Memory 只在对应字符串
非空时追加，并把生效的 section 名记录到 `last_sections`（L85）。

三点值得注意：

1. **工具清单以 schema 为单一事实来源。** `registered_tool_names()` 直接读
   `tool["function"]["name"]`，因此新增或删除工具不需要同步任何散文描述。
2. **Skill 与 Memory 只进目录。** 两个 section 都只放名称与描述，并显式告诉模型
   完整内容要另外获取：`load_skill` 取 SKILL.md 全文（skill_loading.py L91），
   Memory 正文由 s09 召回后临时附加到 user turn（memory.py L332）。
3. **`_prompt_context(tools)` 以工具集为参数。** 同一段代码传入 `PARENT_TOOLS` 或
   `SUB_TOOLS` 就得到父或子 Agent 的上下文，父子差异集中在注册表而不是散落在
   两份文案里。

## 三、刷新时机与顺序约束

`refresh_system_prompts()`（agent_loop.py L110）是唯一刷新入口，四个调用点：

| 位置 | 时机 | 作用 |
| --- | --- | --- |
| L80 | `__init__` 末段 | 装配完成后生成首版父 / 子 Prompt |
| L179 | `agent_loop` 进入时 | 写回 `messages[0]`，随后才做 Memory 召回 |
| L205 | 每轮模型调用前 | 反映本轮最新工具、Skill、Memory 状态 |
| L134 | `_subagent_system_prompt` | SubAgent 启动时取最新子 Prompt |

它的内部顺序是：重扫 Skill（L114）→ 组装父 Prompt（L117）→ 组装子 Prompt
（L120）→ 可选就地更新首条 system 消息（L127-L130）。

两处顺序约束是这一章最容易忽略的实现细节：

- **先刷新，再压缩。** L205 的刷新必须在 L206 的 `compactor.prepare()` 之前，
  否则 s08 会按旧 Prompt 体积估算预算。
- **只改写 `messages[0]`。** 存在 system 消息就原地替换，否则插入到最前；历史
  user 与 tool 消息一律不动，避免破坏 `tool_calls` / `role=tool` 的配对，
  也不会覆盖 s09 注入到 user turn 的 `<relevant-memories>` 块。

每轮重扫 Skill 还带来一个有用的性质：Agent 自己刚写出的 `skills/x/SKILL.md`，
在下一次模型调用时就会出现在 skills section 里。

## 四、缓存语义

`get()`（system_prompt.py L88）是一层薄缓存：

```text
context → context_key(sort_keys JSON) → 与 _last_key 相同 ? 复用 : assemble()
```

`context_key()`（L45）用 `sort_keys=True` 与 `default=str` 做稳定序列化，因此
字典插入顺序变化不会造成无意义重建；`cache_hits` 与 `assembly_count`（L41-L42）
把命中情况变成可断言的可观察量，测试正是据此验证缓存
（test_s10.py L136、L144）。

缓存的边界要说清楚：

- 它只省掉字符串拼接，不省掉 `SkillLoader.scan()` 的磁盘扫描——扫描发生在
  缓存判定之前；
- 它是 Harness 内部状态，与供应商侧的 Prompt Cache 无关；
- 父子 assembler 各自独立（agent_loop.py L72、L75），身份、工具集和缓存互不
  影响。

## 五、为什么模块按课程能力命名

早期拆分偏工程组件：`schemas.py` + `execution.py` + `tools.py`、
`compact_tool.py` + `compaction.py`、`client.py` + `protocol.py`。这些边界不算
错，但回看某一课时要在多个文件之间跳转。现在按 s01–s10 的能力命名，一课对应
一个模块，文件名不带 `s`：章节号用于学习导航，能力名用于代码引用。

跨章节的 `config.py`、`models.py`、`provider.py` 保持基础设施身份。
`models.py` 只有三个契约（`ToolRequest`、`TodoItem`、`SkillRecord`），是多数模块
共同依赖的最小交集，因此不归入任何一课。

合并不等于把代码搅在一起，模块内部仍保留原来的对象边界。

### tool_use.py 内部三层

```text
PARENT_TOOLS / SUB_TOOLS   L129 / L136   模型可见的能力契约
        ↓
ToolExecutor               L140          JSON 解析 → Hook → dispatch
        ↓
BuiltinTools               L186          Bash / Read / Write / Edit / Glob / Skill
```

改 schema 不会悄悄改变运行行为，改 handler 也不会自动向模型暴露新能力。父子
Agent 共用一个 `ToolExecutor`，只是传入不同的 handler 表和 `display_prefix`。

### context_compact.py 的控制面与算法面

- `CompactToolController`（L373）：校验 `compact({})` 必须为空参数、触发
  PreToolUse / PostToolUse、并用 `already_compacted` 保证每个 user turn 最多
  压缩一次；
- `ContextCompactor`（L17）：L3 大结果落盘（L124）、L1 中段裁剪（L154）、
  L2 旧工具结果占位（L192）、L4 摘要替换（L274）与 reactive 应急压缩（L288）。

`prepare()`（L315）按 L3 → L1 → L2 顺序做便宜的结构压缩，只有估算仍超过
`CONTEXT_CHAR_LIMIT` 才调用 L4；L4 连续失败三次（`MAX_COMPACT_FAILURES`）才向上
抛异常，否则静默降级继续跑。

### permission.py 与 hooks.py 为什么不合并

`PermissionPolicy.check()`（permission.py L73）回答"这次调用能不能执行"：命中
`DENY_LIST` 直接硬拒绝，写文件越界或危险 bash 走交互确认。`HookManager`
（hooks.py L19）回答"生命周期事件有哪些订阅者"。

`install_default_hooks()`（hooks.py L40）把权限检查注册为第一个 PreToolUse
回调（L70），再注册日志（L71）。注册顺序即行为顺序：`trigger()` 一旦有回调返回
非 None 就短路（L35），所以权限拒绝会在日志与 handler 之前终止调用。Hook 还承载
UserPromptSubmit、PostToolUse 观察与 Stop 续写，与权限是两个概念。

## 六、父循环怎么把十章串起来

`agent_loop.py` 对应第一课，却是最终实现的 composition root。`__init__`
（L44-L96）分五步装配：Skill / Todo / 基础工具 → Hook 与执行器 → compact 控制器
与压缩器 → `memory.configure()` → 父子 Prompt assembler，最后才构造 SubAgent 与
父 handler 表。

一个 turn 的骨架：

```text
agent_loop(messages, active_request)                      L163
  → latest_user_request 兜底                              L172
  → extraction_messages = deepcopy(messages[-12:])        L176
  → refresh_system_prompts(messages)                      L179
  → memory.load_memories / inject_recalled_memories       L180-L181
  while True:
      → 连续 3 轮未 todo_write 则注入 reminder             L195
      → refresh_system_prompts(messages)                  L205
      → compactor.prepare(messages, active_request)        L206
      → 本 turn 压缩过则从 tools 中移除 compact            L209
      → completion_request → chat.completions.create       L219
      → 溢出且未重试 ? reactive_compact + continue          L225-L233
      → assistant 消息双写主历史与提取快照                 L239-L240
      → 无 tool_calls ?                                    L243
            Stop Hook 要求继续 ? 追加 user 并 continue      L247-L252
            否则 extract_memories → consolidate → return   L255-L257
      → 逐个 tool_call：                                    L267
            compact → CompactToolController.request        L270
            其他   → ToolExecutor.execute                   L278
            每次都追加 role=tool 并同步快照                 L282-L288
      → 整批写完后才 compact_history                        L289-L292
```

两个细节体现协议正确性：`compact` 不能当成普通 handler，因为它要改写整个
`messages`，所以在工具循环里内联拦截并延迟到批次末尾执行；`extraction_messages`
与主历史双写，保证 turn 结束时的 Memory 提取看到的是原始细节，而不是被压缩过的
摘要。

## 七、SubAgent 的隔离面与共享面

`SubagentRunner`（subagent.py L18）构造时只拿到 client、settings、共享
`ToolExecutor`、基础 handler 表和一个 `prompt_supplier`。它看不到父 Agent 的
`messages`、`TodoManager` 或 `ContextCompactor`。

每个任务新建 `messages=[system, user]`（L40），并固定使用 `SUB_TOOLS`（L46）。
该集合不含 `todo_write`、`task`、`compact`，所以递归委派不是靠 Prompt 劝阻，
而是在注册层直接排除。上限是 30 轮（L15），超出返回错误字符串而不是抛异常。

共享的是工作区、Provider、`ToolExecutor` 与 `HookManager`，因此 Permission 对
SubAgent 同样生效；`prompt_supplier` 每次调用都会先刷新，避免子 Agent 拿到过期的
Skill / Memory 目录。

## 八、Compact 与 Memory 的分工

```text
Context Compact（s08）        当前会话在预算内的任务连续性
  ├─ transcript / 大结果落盘
  ├─ L1–L4 结构压缩与摘要
  └─ active_request 贯穿摘要

Durable Memory（s09）         跨 turn、跨会话的稳定信息
  ├─ 索引 metadata 进入 System Prompt（s10 变成 memory section）
  ├─ 相关正文临时附加到最近 user 消息
  └─ turn 结束后从独立快照提取、达阈值整理
```

s10 对这层关系的唯一改动是入口：索引不再由 `build_memory_system_prompt()` 拼接，
而是作为 memory section 由 assembler 生成。

## 九、行为不变量

`tests/test_s10.py` 12 项测试同时覆盖新能力与旧约束：

| 断言 | 位置 |
| --- | --- |
| 无 Skill / Memory 时只有四个固定 section | L85 |
| 工具行来自真实注册表，父 9 子 6 | L96 |
| Skill 出现后 skills section 才出现 | L108 |
| memory section 只放目录，不含正文 | L122 |
| context 不变则不重复组装 | L136 |
| context 变化则重新组装 | L144 |
| 刷新只替换首条 system，不增删消息 | L152 |
| Agent Loop 真正发送组装结果 | L159 |
| SubAgent 工具集隔离且 Prompt 无 `todo_write` | L212 |
| 工作区边界与单一 `in_progress` 仍生效 | L224 |
| 十个能力模块存在、旧碎片文件名消失 | L182 |

## 十、权衡与已知遗留

### 1. 课程模块更长

`context_compact.py` 401 行、`memory.py` 540 行、`tool_use.py` 290 行都明显长于
原来的碎片文件。这里优先优化"按一课连续阅读"，而不是让每个文件尽量短。

### 2. Memory 仍是模块级状态

`memory.configure(settings)`（memory.py L39）改写模块级 `WORKDIR`、`MEMORY_DIR`
与 `MODEL`，因此同一进程内不适合并存两个使用不同记忆目录的 Harness。这是从单文件
阶段继承下来的全局状态，尚未消除。

### 3. Skill 每轮扫盘

`refresh_system_prompts()` 每轮调用 `scan()`，热发现简单可靠，但 Skill 较多时
产生固定 IO。可以引入 mtime 或目录版本缓存，代价是要处理时间戳不可靠的情况。

### 4. Agent Loop 的公开边界

Harness 的对外边界已经收紧：

- 不再保留 `execute_with_handlers`、`execute_sub_tool`、`spawn_subagent`、
  `request_manual_compact` 等兼容性转发入口；
- 父工具路由表是私有的 `_parent_handlers`，普通工具通过
  `_execute_parent_tool()` 交给 `ToolExecutor`；
- `_execute_tool_batch()` 只维护 assistant/tool 协议并返回 Todo、Compact
  控制信号；真正的工具实现仍在 `tool_use.py`，压缩算法仍在
  `context_compact.py`，SubAgent 循环仍在 `subagent.py`。

仍保留的无调用方实现有：

- `memory.build_memory_system_prompt`（memory.py L321）被 system_prompt.py 的
  memory section 取代；
- `SystemPromptAssembler.invalidate`（system_prompt.py L99）没有调用方——目前
  context key 变化即可自然失效，它是为将来强制重建预留的。

## 十一、验证

```bash
python3 -m pytest -q tests/test_s10.py
# 14 passed
```

`python3 s10_system_prompt/code.py` 启动时会打印 `sections=...`，可直接观察本次
生效的 section 组合；写入一个新 Skill 或一条 Memory 后再看，能确认 Prompt 确实
跟随运行态变化。
