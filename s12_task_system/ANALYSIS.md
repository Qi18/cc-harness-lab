# s12 源码剖析：Task System 相对 s11 做了哪些修改

> 配套 [README.md](./README.md) 阅读。本文基于 s11 与 s12 harness 包的逐文件 diff
> （2161 → 2581 行，净增 420 行），回答一个问题：
> **为了让"待办"活过进程，harness 动了哪些地方，为什么这么动。**
> 行号对应各模块当前版本。

## 一、修改总览

s12 建立在真正的 s10 System Prompt 与 s11 Error Recovery 之上，**不重新实现前两章**。
十一个模块里九个字节不变，改动只有一个新模块加两处极小的接入：

| 文件 | 状态 | 改动 |
|---|---|---|
| `harness/tasks.py` | **新增 408 行** | `TaskRecord`、`TaskStore`、`TaskManager`、`TASK_TOOLS` |
| `harness/agent.py` | +11 行 | 构造两个组件 + 注册六个 handler |
| `harness/tools.py` | +3 行 | `PARENT_TOOLS` 追加 `*TASK_TOOLS` |
| `harness/__init__.py` | 扩展导出 | `TaskRecord` / `TaskStore` |
| `prompt.py` `recovery.py` `compaction.py` `memory.py` `hooks.py` `config.py` `models.py` `skills.py` | **完全不变** | s01–s11 能力零回归 |

`agent.py` 的两处接入总共只有 8 行有效代码：

```python
self.task_store = TaskStore(settings.tasks_dir, settings.workdir)   # L63
self.tasks = TaskManager(self.task_store)                            # L64
...
"create_task": self.tasks.run_create,      # L81-86，六个 handler
```

**没有任何 `agent_loop` 内的特判**——任务工具是普通 handler，这一点后面还会提到。
`tests/test_s12.py` 17 个用例（已实测通过）。

## 二、Task 与 Todo 的分工

`todo_write`（s05）是进程内的执行清单，退出即消失，也无法表达依赖和负责人：

| 能力 | TodoWrite | Task System |
|---|---|---|
| 存储 | 进程内列表 | `.tasks/task_<8 hex>.json` |
| 粒度 | 当前 Agent 的步骤 | 可独立认领的任务节点 |
| 依赖 | 无 | `blockedBy` 有向图 |
| 负责人 | 无 | `owner` |
| 恢复 | 会话结束即丢失 | 新进程重新读取 |
| 更新 | 整表替换 | 单记录创建、查询、认领、完成 |

两者并存而非替代：Todo 管"我这一轮要做什么"，Task 管"这个项目还剩哪些可认领的工作"。

## 三、TaskStore：四道边界

一条任务是六个固定字段的 JSON（tasks.py [L17-24](./harness/tasks.py)）：

```json
{"id": "task_a1b2c3d4", "subject": "create API", "description": "…",
 "status": "pending", "owner": null, "blockedBy": ["task_11223344"]}
```

每次读写都过四道校验：

1. **目录边界**（`_root`，[L36](./harness/tasks.py)）：`tasks_dir` resolve 后必须在
   `workdir` 内。即使 `CC_TASKS_DIR` 指到工作区外也会抛
   `Task store escapes the working directory`；
2. **ID 形状**（`_path`，[L46](./harness/tasks.py)）：`fullmatch` `^task_[0-9a-f]{8}$`，
   拼路径后再验一次 `relative_to(root)`——`../task_deadbeef` 和 `task_DEADBEEF` 都被拒；
3. **字段集合严格相等**（`load`，[L137-139](./harness/tasks.py)）：`set(data) != expected`
   即报错。不是"包含必需字段"而是**完全等于**，多一个字段也拒；
4. **记录内容合法**（`_validate_record`，[L66](./harness/tasks.py)）：状态在白名单、
   owner 为 null 或非空串、`blockedBy` 无重复、每个依赖 ID 合法且不等于自身。

写入分两条路径，各有讲究：

- **create**（[L93](./harness/tasks.py)）用 `open("x")` **排他创建**，`FileExistsError`
  就重摇 ID，最多 100 次。不是"先 exists 再 write"——那样有 TOCTOU 窗口；
- **save**（[L118](./harness/tasks.py)）先写**同目录**隐藏临时文件
  `.{name}.{uuid}.tmp`，再 `os.replace()` 原子替换，`finally` 里
  `unlink(missing_ok=True)` 清理。同目录是必要的：`os.replace` 跨文件系统不保证原子。

## 四、两阶段建图：为什么不能一轮建完

Task ID 由 Harness 运行时生成。模型在一轮里发出的并列 `tool_calls` 是**在任何工具
结果返回前一次性决定的**——第二个 `create_task` 不可能引用第一个还没返回的 ID：

```text
第 1 轮：create_task × N              → 拿到真实 ID
第 2 轮：update_task(id, addBlockedBy=[…])  → 加边
```

这条约定直接写进了 `update_task` 的 schema 描述（tasks.py L354-357：
"Create every node before adding dependency edges"），让协议约束出现在模型看得见的
地方，而不只是文档里。

`update_dependencies`（[L168](./harness/tasks.py)）**先全批校验、后一次保存**：

1. 目标必须 `pending` 且 `owner is None`——已认领/已完成的任务不许改依赖；
2. `dict.fromkeys` 去重（保序）；
3. 逐条校验 ID 格式、非自依赖、依赖文件存在；
4. `_depends_on`（[L155](./harness/tasks.py)）用显式栈 DFS + `visited` 集合做**传递环
   检测**——`first→second→third` 之后再让 `first` 依赖 `third` 同样被拒；
5. 全部通过才 `extend` + `save`。

所以不存在"前一条边写成功、后一条边失败"的半更新。[test_s12.py](../tests/test_s12.py)
里混入一个不存在的依赖后断言 `blockedBy` 仍为 `[]`，锁的就是这个语义。

## 五、状态机与 owner

只有两次合法迁移，没有回退边：

```text
pending --claim_task--> in_progress --complete_task--> completed
```

- **claim**（[L224](./harness/tasks.py)）：状态不对或依赖未齐时**返回说明文本而非抛
  异常**——这是给模型看的观察，不是程序错误；
- **complete**（[L238](./harness/tasks.py)）：状态必须 `in_progress`，且
  `task.owner != owner` 直接拒绝；
- **新解锁的下游用差集算**：完成前先记下已就绪集合 `ready_before`（L245-251），写盘后
  再算一次，只报告**新进入就绪状态**的任务（L254-261）。避免把本来就已就绪的任务反复
  报成"刚解锁"。

依赖缺失时方向是**保守的**：`incomplete_dependencies`（[L211](./harness/tasks.py)）
捕获 `ValueError` 并把该依赖计入未完成——依赖文件被删或损坏时任务**保持阻塞**，
而不是误判为就绪放行。

## 六、六个工具与权限隔离

`TASK_TOOLS`（tasks.py L333-408）有两处 schema 细节：`_id_property()`（L329）把
`pattern: ^task_[0-9a-f]{8}$` 写进 JSON Schema，让模型生成参数时就受约束；三个单参数
工具（`get_task`/`claim_task`/`complete_task`）用列表推导批量生成。

模型侧只看到字符串：`run_*` 适配器统一经 `_observe`（[L269](./harness/tasks.py)）把
`OSError / RuntimeError / ValueError` 转成 `Error: …` 文本。**异常不穿透到 Agent
Loop**——存储故障是模型要读的信息，不是进程要崩的理由。

权限上任务工具**仅限父 Agent**：`PARENT_TOOLS` 追加 `*TASK_TOOLS`，`SUB_TOOLS` 不含。
理由和 s08 不给子 Agent `compact` 同源——任务图是父级共享状态，本章还没有原子 claim
和独立身份，子 Agent 动它只会产生竞争。

这个隔离还有一层 s10 带来的加成：**子 Agent 的 System Prompt 里根本不会提到这六个
工具**。s10 的 `tools` section 由 `SUB_TOOLS` 派生，所以"描述"和"能力"在子 Agent 侧
自动对齐，不需要额外维护一句"你不能用任务工具"。

## 七、与 s10、s11 的衔接

本章最值得注意的其实是**它没做什么**。一次请求的实际顺序是：

```text
s10  refresh_system_prompts   → tools section 自动包含新增的六个任务工具
s08  compactor.prepare        → 四层压缩
s11  with_retry(create, …)    → 截断 / 超限 / 429 / 529 各自有限恢复
     ↓ 成功
s12  execute_tool → TaskManager.run_* → TaskStore
```

三个衔接点：

1. **Prompt 自动同步**：`_prompt_context` 从 `PARENT_TOOLS` 派生工具名，`tools.py` 里
   加了 `*TASK_TOOLS`，Prompt 就自动多出六个名字——**不需要改任何提示词文案**。这是
   s10 "Prompt 是运行时状态的投影"设计的直接兑现；
2. **恢复不影响任务**：s11 的重试发生在模型调用层，任务工具执行发生在其后。一次
   429 重试不会重复执行 `create_task`；
3. **任务工具没有特权路径**：走的是和 `bash`、`read_file` 同一个
   `execute_with_handlers`（agent.py L128），因此自动受 `PreToolUse` 权限管线和
   `PostToolUse` Hook 约束。只有 `compact` 仍需内联特判，因为它要改写 `messages` 本身。

## 八、防御与权衡

- **任务与记忆的容错方向相反**：`memory.list_memory_files` 遇到损坏文件**静默跳过**
  （记忆缺一条只是少点背景），`TaskStore.list`（[L149](./harness/tasks.py)）遇到损坏
  文件**抛异常**（任务图缺一个节点会让依赖判断出错，宁可整体报错）。外层 `run_list`
  用 `_observe` 兜成 `Error:` 文本，不会崩进程；
- **`complete` 里两次 `store.list()`**：为算差集要全量读两遍任务目录，是 O(N) 文件
  IO ×2。本章规模下可接受，但不是可扩展设计；
- **单执行者假设**：`TaskManager.owner` 默认 `"agent"`（L207），`claim` 的检查与写盘
  之间没有锁。原子写只保护**单文件替换**，不等于跨进程原子认领——两个进程同时 claim
  同一任务仍会竞争。README 明确写了"不要把 s12 当生产级分布式任务队列"；
- **`.tasks/` 属于工作区状态**：与 `.memory/`、`.transcripts/` 同类，已在
  `.gitignore` 中。

## 九、一句话总结

s12 用 408 行新增和 11 行接入，把"待办"从进程内列表升级为带依赖、带负责人、可跨会话
恢复的**持久有向图**——两阶段建图 + 全批校验 + 原子替换 + 保守阻塞。而它能只用 11 行
接入，正是因为 s10 让 Prompt 自动跟随工具注册表、s11 把失败恢复收敛在模型调用层：
**新能力只需要注册，不需要到处打补丁**。
