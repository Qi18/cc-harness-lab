# s10 源码剖析：task system 与模块化相对 s09 做了哪些修改

> 配套 [README.md](./README.md) 阅读。本文基于 s09/code.py 与 s10 harness 包的
> 对比（单文件 1936 行 → 59 行入口 + 9 个模块共 2267 行），回答一个问题：
> **为了引入持久任务图并停止单文件膨胀，harness 动了哪些地方，为什么这么动。**
> 行号对应各模块当前版本。

## 一、修改总览

s09 的全部机制（权限、Hooks、Todo、SubAgent、Skills、四层压缩、Memory）逐条保留，
s10 做了两件事：**新增持久任务图** + **把单文件拆成可组合模块**。

| 类别 | 修改 | 位置 |
|---|---|---|
| 架构 | `code.py` 从 1936 行缩到 59 行，只做 CLI 与公共导出 | [code.py](./code.py) |
| | 新增 `harness/` 包：agent / tasks / tools / memory / compaction / hooks / skills / config / models | `harness/*.py` |
| 任务子系统 | `TASK_ID_PATTERN`、`TaskRecord`、`TaskStore`、`TaskManager` | tasks.py L14-326 |
| | 六个任务工具 schema `TASK_TOOLS` | tasks.py L333-408 |
| 配置 | `Settings.tasks_dir`（`CC_TASKS_DIR` 可覆盖）、`assert_inside_workdir` | config.py L26, L51-57 |
| 工具面 | `PARENT_TOOLS` 9 → 15 个；`SUB_TOOLS` 保持 6 个 | tools.py L125-133 |
| 提示词 | 系统提示新增"先建节点、再用返回 ID 加边、只认领未阻塞任务" | agent.py L26-36 |
| 执行逻辑 | `agent_loop` 从模块级函数变为 `AgentHarness` 方法，逻辑等价 | agent.py L198-300 |
| 组装 | `parent_handlers` / `sub_handlers` 显式分表 | agent.py L77-89 |

配套：`.gitignore` 已忽略 `.tasks/`，根 README 增补 s10 章节与 `CC_TASKS_DIR` 说明，
`tests/test_s10.py` 17 个用例（已验证全部通过），项目全量 138 项。

## 二、为什么在这一章模块化

s01–s09 的单文件是刻意的教学形态：打开一个文件就是当章全貌。但严格累计到 s09 已
接近 2000 行，再叠 Task、后台任务、Cron、Teams、MCP 会让机制边界和回归影响都难以
判断。s10 的处理是**冻结旧章节、只把本章及以后改成局部 package**：

```text
code.py (59)            CLI 入口 + 公共导出
harness/
├── agent.py    (300)   AgentHarness：组装 + 父/子 Agent Loop
├── tasks.py    (408)   本章新增：TaskStore / TaskManager / TASK_TOOLS
├── tools.py    (267)   工具 schema + BuiltinTools handler
├── memory.py   (533)   s09 记忆子系统
├── compaction.py (364) s08 四层压缩
├── hooks.py    (142)   HookManager + PermissionPolicy
├── skills.py    (96)   SkillLoader
├── config.py    (67)   Settings + create_client
└── models.py    (24)   ToolRequest / TodoItem / SkillRecord
```

拆分不是纯搬移，伴随一次**依赖注入改造**：s09 的 `WORKDIR`、`SKILLS_DIR`、
`MEMORY_DIR` 是模块级常量，导入时就由环境变量定死；s10 改为 `Settings.from_env()`
一次解析，再显式注入各组件。`TaskStore(settings.tasks_dir, settings.workdir)`
（agent.py L59）因此可以在测试里指向临时目录——[test_s10.py](../tests/test_s10.py)
的 `settings()` 就是这么构造的，不需要改环境变量或 monkeypatch 全局。

唯一保留全局桥接的是记忆模块：`memory.configure(settings)`（agent.py L69）把
配置写回模块级 `WORKDIR / MEMORY_DIR / MODEL`（memory.py L38-44）。这是为了让
s09 的记忆代码几乎零改动地平移过来，代价是 memory 仍是单例——同一进程内跑两个
`AgentHarness` 会互相覆盖记忆目录。

## 三、TaskStore：持久化与安全边界

一条任务是 `.tasks/task_<8 hex>.json`，字段固定六个（tasks.py L17-24）：

```json
{"id": "task_a1b2c3d4", "subject": "create API", "description": "...",
 "status": "pending", "owner": null, "blockedBy": ["task_11223344"]}
```

四道边界校验，每次读写都过：

1. **目录边界**（`_root`，[L36](./harness/tasks.py)）：`tasks_dir` resolve 后必须在
   `workdir` 内，否则抛 `Task store escapes the working directory`。即使
   `CC_TASKS_DIR` 指到工作区外也拦得住；
2. **ID 形状**（`_path`，[L46](./harness/tasks.py)）：必须 `fullmatch`
   `^task_[0-9a-f]{8}$`，拼路径后再验一次 `relative_to(root)`——`../task_deadbeef`
   和 `task_DEADBEEF` 都被拒；
3. **字段集合严格相等**（`load`，[L137-139](./harness/tasks.py)）：
   `set(data) != expected` 即报错。不是"包含必需字段"而是**完全等于**，多一个
   字段也拒——防止手工编辑或旧版本文件被半解释；
4. **记录内容合法**（`_validate_record`，[L66](./harness/tasks.py)）：状态在白名单、
   owner 为 null 或非空串、`blockedBy` 是列表且无重复、每个依赖 ID 合法且不等于自身。

写入分两条路径，各有讲究：

- **create**（[L93](./harness/tasks.py)）用 `open("x")` **排他创建**，`FileExistsError`
  就重摇 ID，最多 100 次。不是"先 exists 再 write"——那样有 TOCTOU 窗口；
- **save**（[L118](./harness/tasks.py)）先写同目录隐藏临时文件
  `.{name}.{uuid}.tmp`，再 `os.replace()` 原子替换，`finally` 里
  `unlink(missing_ok=True)` 清理残留。替换成功后临时文件已不存在，`missing_ok`
  让这个清理对成功与失败两条路都安全。

同目录临时文件是必要的：`os.replace` 跨文件系统不保证原子，放在目标同目录才确保
同一 mount。

## 四、两阶段建图：为什么不能一轮建完

Task ID 由 Harness 运行时生成。模型在一轮里发出的并列 `tool_calls` 是**在任何工具
结果返回前一次性决定的**——第二个 `create_task` 不可能引用第一个调用还没返回的 ID。
所以建图必须两轮（README 里也写明了）：

```text
第 1 轮：create_task × N        → 拿到真实 ID
第 2 轮：update_task(id, addBlockedBy=[...]) → 加边
```

`update_task` 的 schema 描述里直接写了这条约定（tasks.py L354-357），把协议约束
前移到模型看得见的地方。

`update_dependencies`（[L168](./harness/tasks.py)）是**先全批校验、后一次保存**：

1. 目标必须 `status == "pending"` 且 `owner is None`——已认领/已完成的任务不许改依赖；
2. `dict.fromkeys` 去重（保序）；
3. 逐条校验 ID 格式、非自依赖、依赖文件存在；
4. `_depends_on(dependency, task_id)`（[L155](./harness/tasks.py)）用显式栈 DFS +
   `visited` 集合做**传递环检测**——不只查直接环，`first→second→third` 后再让
   `first` 依赖 `third` 也会被拒；
5. 全部通过才 `extend` + `save`。

因此不存在"前一条边写成功、后一条边失败"的半更新——[test_s10.py L118](../tests/test_s10.py)
正是验证这点：混入一个不存在的依赖，整批回退，`blockedBy` 仍为 `[]`。

## 五、状态机与 owner

只有两次合法迁移，没有回退边：

```text
pending --claim_task--> in_progress --complete_task--> completed
```

- **claim**（[L224](./harness/tasks.py)）：状态不是 pending 就返回说明文本而非抛异常；
  依赖未齐返回 `Blocked by: [...]`；通过则写 owner + 转 in_progress；
- **complete**（[L238](./harness/tasks.py)）：状态必须 in_progress，且
  `task.owner != owner` 直接拒绝——[test_s10.py L145](../tests/test_s10.py) 验证了
  alice 认领、bob 无法完成；
- **新解锁的下游**用差集算出来：完成前先记下"已就绪集合" `ready_before`（L245-251），
  写盘后再算一次，只报告**新进入就绪状态**的任务（L254-261）。这样避免把本来就已
  就绪的任务反复报成"刚解锁"。

依赖缺失时的方向是**保守的**：`incomplete_dependencies`（[L211](./harness/tasks.py)）
捕获 `ValueError` 并把该依赖计入未完成——依赖文件被删或损坏时任务**保持阻塞**，
而不是误判为就绪放行。

`TaskManager.owner` 默认 `"agent"`（L207），本章只有单执行者。原子写只保护单文件
替换，**不等于跨进程原子认领**：两个进程同时 claim 同一任务仍会竞争。真正的
teammate identity 和并发协调留给后续 Agent Teams 章节。

## 六、六个工具与权限隔离

`TASK_TOOLS`（tasks.py L333-408）的 schema 有两处细节：`_id_property()`（L329）
把 `pattern: ^task_[0-9a-f]{8}$` 直接写进 JSON Schema，让模型在生成参数时就受约束；
三个单参数工具（`get_task`/`claim_task`/`complete_task`）用列表推导批量生成，避免
复制粘贴三段几乎相同的 schema。

模型侧只看到字符串结果：`run_*` 适配器统一经 `_observe`（[L269](./harness/tasks.py)）
把 `OSError / RuntimeError / ValueError` 转成 `Error: ...` 文本。**异常不穿透到
Agent Loop**——工具失败是模型要读的信息，不是进程要崩的理由。

权限上任务工具**仅限父 Agent**：

```python
PARENT_TOOLS = [*BASE_TOOLS, TODO_TOOL, SUBAGENT_TOOL, LOAD_SKILL_TOOL,
                COMPACT_TOOL, *TASK_TOOLS]      # 15 个
SUB_TOOLS = [*BASE_TOOLS, LOAD_SKILL_TOOL]      # 6 个
```

理由和 s08 不给子 Agent `compact` 同源：任务图是父级共享状态，s10 还没有原子 claim
和独立身份，子 Agent 动它只会产生竞争。父 Agent 仍可用 `task` 委派文件调查，但
图由父 Agent 统一维护。[test_s10.py L183](../tests/test_s10.py) 断言了这个隔离和
"前九章工具一个没少"。

## 七、agent_loop 的改动：等价平移

`agent_loop`（agent.py [L198](./harness/agent.py)）从模块级函数变成 `AgentHarness`
方法，控制流与 s09 **逐段等价**，s09 的两个关键不变量都保留：

| s09 行为 | s10 位置 | 说明 |
|---|---|---|
| `extraction_messages = copy.deepcopy(messages[-12:])` | L205 | 提取快照，与 Compact 解耦 |
| 重写 system 追加记忆索引 | L206-209 | `memory.build_memory_system_prompt` |
| 召回并注入到最新 user turn | L210-213 | `load_memories` + `inject_recalled_memories` |
| 每轮 `compactor.prepare` | L224 | s08 四层管线入口 |
| assistant / tool 消息双写快照 | L260, L297 | `copy.deepcopy` 后追加 |
| 仅最终答案时提取 + 整理 | L270-271 | Stop Hook 放行后才执行 |
| `compact` 动态摘除、批次收尾统一压缩 | L228-235, L298-300 | 每 turn 最多一次 |

唯一的行为差异是**日志变安静了**：s09 在 todo reminder 和 reactive compact 处各有
一行 `print`，s10 去掉了（对比 s09 L1796-1799 / L1826 与 s10 L220-223 / L247）。
功能不变，但排查 reactive compact 是否触发时少了一个肉眼信号——如果要复现 s09 的
可观测性，这两处需要自己补回。

任务工具**没有**在 `agent_loop` 里特判：它们是普通 handler，走
`execute_tool` → `execute_with_handlers`（L91-127），因此自动经过
`PreToolUse` / `PostToolUse` Hook 和权限管线。只有 `compact` 仍需内联特判，因为它
要改 `messages` 本身。

## 八、防御与权衡

- **任务 vs 记忆的容错方向相反**：`memory.list_memory_files` 遇到损坏文件**静默
  跳过**（记忆缺一条只是少点背景），`TaskStore.list` 遇到损坏文件**抛异常**
  （任务图缺一个节点会导致依赖判断出错，宁可整体报错）。外层 `run_list` 用
  `_observe` 兜成 `Error:` 文本，不会崩进程；
- **`complete` 里两次 `store.list()`**：为算差集要全量读两遍任务目录，任务量大时
  是 O(N) 文件 IO ×2。本章规模下可接受，但不是可扩展设计；
- **单执行者假设**：`owner` 默认 `"agent"`，`claim` 的检查与写盘之间没有锁，跨进程
  并发下会竞争。README 明确写了"不要把 s10 当生产级分布式任务队列"；
- **`memory` 仍是模块单例**：`configure()` 写全局，同进程多 harness 会互覆盖——是
  为了平移 s09 代码而刻意留下的技术债；
- **`.tasks/` 属于工作区状态**：与 `.memory/`、`.transcripts/` 同类，不应进版本库。

## 九、一句话总结

s10 做了两件互相支撑的事：用 `TaskStore` 把"待办"从进程内列表升级为带依赖、带
负责人、可跨会话恢复的**持久有向图**（两阶段建图 + 全批校验 + 原子替换 + 保守阻塞），
同时把 1936 行单文件拆成 9 个显式注入配置的模块——**前者让任务活过进程，后者让
harness 还能继续长下去**。
