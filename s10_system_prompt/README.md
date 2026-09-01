# s10：System Prompt

这一章在 s01–s09 的累计能力之上，把 System Prompt 从硬编码常量改成每轮按运行态
组装的命名 section。同时它是本仓库第一个不再使用单文件 `code.py` 的阶段：Harness
拆进局部 `harness/` package，并按 s01–s10 的课程能力命名，文件名本身不带 `s`。

- 官方教程：[s10 System Prompt](https://learn.shareai.run/zh/s10/)
- 官方源码：[s10_system_prompt/code.py](https://github.com/shareAI-lab/learn-claude-code/blob/main/s10_system_prompt/code.py)
- 上一章：[s09 Memory](../s09_memory/)
- 源码分析：[ANALYSIS.md](./ANALYSIS.md)
- 调用图：[CALLGRAPH.md](./CALLGRAPH.md)

## 这一章解决什么

前九章的 System Prompt 是写死的字符串。新增工具、生成新的 Skill 或写入新的
Memory 之后，Prompt 里的描述不会跟着变，模型被告知的能力与真实注册表逐渐漂移。
s10 的做法是：Prompt 只由可观察运行态生成，每个片段都能指向一个真实数据源。

## Prompt section

| section | 数据来源 | 出现条件 |
| --- | --- | --- |
| identity | `PARENT_IDENTITY` / `SUBAGENT_IDENTITY` | 恒定 |
| guidance | `PARENT_GUIDANCE` / `SUBAGENT_GUIDANCE` | 恒定 |
| tools | `registered_tool_names(PARENT_TOOLS / SUB_TOOLS)` | 恒定，随工具集变化 |
| workspace | `settings.workdir` | 恒定 |
| skills | `SkillLoader.catalog()` | 扫描到 Skill 时 |
| memory | `memory.read_memory_index()` | `MEMORY.md` 存在时 |

工具清单从真实 schema 注册表派生，因此父 Agent 的 9 个工具与 SubAgent 的 6 个
工具不可能与 Prompt 描述不一致。Skill 与 Memory 在这里只放目录元数据：完整
`SKILL.md` 仍需 `load_skill`，完整记忆正文仍由 s09 召回后临时附加到 user turn。

`last_sections` 记录本次实际生效的 section 名，`code.py` 启动时会打印它，可直接
观察 Skill 或 Memory 目录是否已经进入 Prompt。

## 组装时机

`refresh_system_prompts()` 是唯一入口，全部调用点都在 `agent_loop.py`：

| 调用点 | 时机 | 作用 |
| --- | --- | --- |
| `__init__` L81 | Harness 装配完成 | 生成首版父 / 子 Prompt |
| `agent_loop` L217 | user turn 开始 | 写回首条 system，再做 Memory 召回 |
| `agent_loop` L243 | 每次模型调用前 | 先刷新 Prompt，再按预算压缩消息 |
| `_subagent_system_prompt` L134 | SubAgent 启动时 | 避免子循环用到过期目录 |

每次刷新都会重扫 skills 目录，所以 Agent 自己刚写出的 `SKILL.md` 在下一次模型
调用前就能生效。传入 `messages` 时只替换首条 system 消息，不改写历史 user 与
tool 内容，避免破坏 s08 依赖的 `tool_calls` / `role=tool` 配对。

顺序也有要求：先写入最新 Prompt，再调用 `compactor.prepare()`，压缩才会按真实的
上下文体积裁剪。

## 缓存

`SystemPromptAssembler.get()` 把运行态 context 序列化成稳定 JSON key
（`sort_keys=True`）：key 不变就复用上次字符串并累加 `cache_hits`，key 变化才
重新组装并累加 `assembly_count`。字典顺序变化不会造成无意义的重建。

这是 Harness 内部的字符串复用，不是供应商 API 的 Prompt Cache。父 Agent 与
SubAgent 各持有一个 assembler，身份、指令、工具集与缓存互不干扰。

## 模块结构

```text
s10_system_prompt/
├── code.py                    57   CLI 入口
└── harness/
    ├── config.py              58   环境变量、模型和工作区路径
    ├── models.py              24   跨模块数据契约
    ├── provider.py            46   百炼客户端、请求与响应适配
    ├── agent_loop.py         300   01 Agent Loop：装配能力并驱动父循环
    ├── tool_use.py           303   02 Tool Use：Schema、handler、执行管线
    ├── permission.py          85   03 Permission：deny / ask 决策
    ├── hooks.py               74   04 Hooks：生命周期事件注册与触发
    ├── todo_write.py          46   05 TodoWrite：Todo 状态与校验
    ├── subagent.py            72   06 SubAgent：隔离历史的同步子循环
    ├── skill_loading.py       99   07 Skill Loading：扫描、目录、按需加载
    ├── context_compact.py    404   08 Context Compact：L1–L4 与手动 compact
    ├── memory.py             540   09 Memory：召回、提取、整理与持久化
    ├── system_prompt.py      101   10 System Prompt：section 组装与缓存
    └── __init__.py            19   包门面
```

合计 2228 行，其中 `harness/` 2171 行。章节编号只写在文档里，不写进文件名，这样
既保留学习顺序，又避免将来调整章节或复用模块时重命名代码。

## 课程与代码的对应关系

| 章节 | 能力 | 模块 | 主要对象 |
| --- | --- | --- | --- |
| 01 | Agent Loop | `agent_loop.py` | `AgentHarness` |
| 02 | Tool Use | `tool_use.py` | `PARENT_TOOLS`、`ToolExecutor`、`BuiltinTools` |
| 03 | Permission | `permission.py` | `PermissionPolicy` |
| 04 | Hooks | `hooks.py` | `HookManager`、`install_default_hooks` |
| 05 | TodoWrite | `todo_write.py` | `TodoManager` |
| 06 | SubAgent | `subagent.py` | `SubagentRunner` |
| 07 | Skill Loading | `skill_loading.py` | `SkillLoader` |
| 08 | Context Compact | `context_compact.py` | `ContextCompactor`、`CompactToolController` |
| 09 | Memory | `memory.py` | 召回、注入、提取、整理函数 |
| 10 | System Prompt | `system_prompt.py` | `SystemPromptAssembler` |

`config.py`、`models.py`、`provider.py` 是跨章节基础设施，不强行归入某一课。

## 为什么这样合并

**Tool Use 放在一个模块。** Schema 描述“模型看到什么”，执行器描述“怎么分发”，
handler 描述“具体怎么做”，三者都属于 s02。合并后回看课程不用跨三个文件，内部仍是
`JSON Schema → ToolExecutor → BuiltinTools handler` 三层边界。

**Compact 控制与算法放在一个模块。** `CompactToolController` 负责工具参数、Hook 与
每 turn 一次的控制规则，`ContextCompactor` 负责消息裁剪、工具结果落盘、摘要和溢出
恢复。二者都属于 s08 且总是一起被父循环使用。

**Permission 与 Hooks 保持分开。** Permission 是决策规则，Hooks 是生命周期扩展。
默认 Hook 会注册 `PermissionPolicy.check`，但 Hook 还承载日志、输出观察和 Stop
回调，因此仍是两个课程模块。

## 依赖方向

课程编号代表学习顺序，不代表 Python 依赖层级。`agent_loop.py` 对应第一章，却是
累计 Harness 的 composition root，所以会导入其后所有能力。

```text
config · models · system_prompt          无内部依赖
          ↓
provider · permission · skill_loading · todo_write
          ↓
hooks · memory
          ↓
tool_use · context_compact
          ↓
subagent
          ↓
agent_loop → code.py
```

箭头向下表示"依赖上一层"。下层模块都不导入 `AgentHarness`，因此依赖图无环。
SubAgent 只拿到 client、settings、共享 executor、基础 handler 和一个 prompt
supplier，看不到父 Agent 的消息历史、Todo 或 compactor。

## 行为保持不变

模块化只改变代码归属，没有删掉前章能力：

- 父 Agent 仍有 9 个工具，SubAgent 仍只有 6 个基础工具；
- SubAgent 仍使用 fresh message history，且不能再次调用 `task`；
- 普通工具仍经过 Permission 与 PreToolUse / PostToolUse；
- Todo 仍限制最多一个 `in_progress`，连续三轮未更新会补提醒；
- Context Compact 仍保留 L1–L4、手动 compact 和一次 reactive 重试；
- Memory 仍使用独立提取快照，不受本 turn 压缩影响；
- 模型为 `kimi/kimi-k3` 时仍自动补 `reasoning_effort=max`；
- 工作区路径边界与危险命令检查仍生效。

## 运行与测试

```bash
cd /data/projects/cc-harness-lab
python3 s10_system_prompt/code.py
python3 -m pytest -q tests/test_s10.py
```

12 个 s10 测试覆盖：section 命名与内容、工具行来自真实注册表、Skill 与 Memory
section 的条件出现、缓存命中与失效、刷新只改写首条 system 消息、Agent Loop 真正
发送组装结果、SubAgent 工具隔离，以及十个课程能力模块存在且旧的技术碎片文件名
（`agent.py`、`schemas.py`、`prompt.py` 等）已经消失。

## 已知遗留

- `memory.py` 仍使用模块级配置，同一进程内不适合创建多个不同记忆目录的 Harness；
- `skill_loading.py` 每轮重扫目录，Skill 较多时会产生固定 IO；
- `agent_loop.py` 保留少量兼容转发方法；
- `SystemPromptAssembler.invalidate()` 目前没有调用方。

## 与官方代码的关系

官方 s10 是聚焦 Prompt assembly 的小型单文件教学切片。本目录继续累计 s01–s09 的
完整实现，用来观察真实 Harness 如何增长，因此代码更长，也需要按能力分模块。新增的
Prompt 机制与课程目标一致；模型协议使用百炼 OpenAI-compatible 的 `tool_calls` 与
`role=tool`，而不是 Anthropic content blocks。
