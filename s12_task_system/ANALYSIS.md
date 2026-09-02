# s12 源码分析：持久任务图

> 配套 [README.md](./README.md) 与 [CALLGRAPH.md](./CALLGRAPH.md) 阅读。
> 行号对应 `s12_task_system/` 当前版本：`code.py` 59 行，`harness/` 16 个模块
> 共 2880 行；未标模块名的行号属于 `task_system.py`。
> 对照 [s12 官方教程](https://learn.shareai.run/zh/s12/)和
> [官方源码](https://github.com/shareAI-lab/learn-claude-code/blob/main/s12_task_system/code.py)。

## 一、这一章改变的是项目状态，不是 Agent Loop 形状

s12 没有再加一层循环。模型仍然经过同一条路径：

```text
模型 tool_call
  → AgentHarness._execute_tool_batch          agent_loop.py L186
  → ToolExecutor.execute                      tool_use.py L155
  → TaskManager handler                       L395-L402
  → role=tool observation                     agent_loop.py L218-L224
  → 下一轮模型调用
```

新能力发生在循环周围。s11 之前唯一的计划状态是 `TodoManager.todos`——进程内存、无
owner、无依赖；s12 增加了一块磁盘任务板，它在消息被压缩或进程重启之后仍然存在。

与 s11 对比可以看清两章的正交性：Error Recovery 处理"模型请求失败"，状态放在
`RecoveryState`（error_recovery.py L31-L39）里，与 turn 同生死；Task System 处理
"项目工作进展"，状态放在 `.tasks/*.json` 里，比进程更长寿。两者互不引用。

| 维度 | RecoveryState（s11） | TaskRecord（s12） |
| --- | --- | --- |
| 存储 | 内存 dataclass | `.tasks/{id}.json` |
| 生命周期 | 一个 user turn | 跨会话 |
| 谁修改 | `agent_loop` / `with_retry` | 五个任务工具 handler |
| 失败表现 | 重试、降级、收口 | `Error: ...` observation |

## 二、TaskRecord 是磁盘协议

`TaskRecord`（L107-L116）是 frozen dataclass，六个字段固定：

```python
TaskRecord(
    id: str,
    subject: str,
    description: str,
    status: str,
    owner: str | None,
    blocked_by: tuple[str, ...],
)
```

`to_dict()`（L118）把内部 `blocked_by` 映射回教程公开字段 `blockedBy`，`from_dict()`
（L128）则是磁盘信任边界。即使文件不是 Harness 刚写的，也必须重新验证：

| 检查 | 位置 | 拒绝原因 |
| --- | --- | --- |
| ID 匹配 `SAFE_TASK_ID` | L137-L138 / L21 | 文件名不安全或不是 `task_` 前缀 |
| subject 非空字符串 | L139-L140 | 空标题无法在看板上区分任务 |
| description 是字符串 | L141-L142 | 类型漂移会让 `get_task` 输出不可预测 |
| status ∈ 三个合法值 | L143-L144 / L20 | 状态机只认识这三个状态 |
| owner 为 null 或非空 | L145-L146 | `owner: ""` 等于没有归属 |
| blockedBy 是安全 ID 数组 | L147-L151 | 依赖 ID 会被直接拼成文件路径 |
| blockedBy 无重复 | L152-L153 | 重复依赖会让 blocker 列表出现重复项 |

`_load()`（L200）在此之上还比较文件名 stem 与 JSON 内的 `id`（L211-L214）。否则复制
或手改文件可能让 `get_task("A")` 实际返回任务 B，后续状态更新会写错目标。测试直接
写入 `{broken` 验证损坏文件被报告且不被覆盖（test_s12.py L776）。

## 三、ID 与路径边界

默认 ID 由 `_default_id()`（L178-L181）生成：

```python
f"task_{time.time_ns()}_{secrets.token_hex(4)}"
```

纳秒时间让同目录内的 ID 大致按创建顺序排列，随机后缀降低同一时刻的碰撞概率。
`_new_id()`（L192-L198）最多尝试十次并在耗尽时抛 `TaskError`，因此错误的自定义
`id_factory`（例如永远返回同一个字符串）不会造成无限循环。

路径安全由两道独立边界组成，职责不同：

| 边界 | 位置 | 保护对象 |
| --- | --- | --- |
| `SAFE_TASK_ID` + `_path()` | L21 / L189-L190 | 单个文件名：不允许斜杠、点开头、超长 ID |
| `assert_inside_workdir` | agent_loop.py L75-L77 / config.py L54-L60 | 整个任务目录不得逃逸 `CC_WORKDIR` |

前者让 `get_task("../secret")` 直接返回 `Error:`（test_s12.py L759），后者让
`CC_TASKS_DIR` 指向工作区外时 `AgentHarness` 构造即抛 `ValueError`
（test_s12.py L783）。目录本身也以 `mode=0o700` 创建（L175）。

## 四、写盘为什么必须用临时文件

直接 `write_text()` 会先截断正式文件。如果进程在 JSON 只写了一半时退出，跨会话恢复
反而读到损坏状态——这恰好摧毁了持久化的意义。`_save()`（L217-L241）的顺序是：

```text
mkstemp(prefix=".{id}.", dir=tasks_dir)     L221-L225
  → json.dump 完整记录                       L229-L235
  → flush                                    L236
  → os.fsync                                 L237
  → os.replace(temp, final)                  L238
  → chmod(final, 0o600)                      L239
  → finally: unlink(missing_ok=True)         L241
```

临时文件与目标在同一目录，`os.replace` 才能保持同一文件系统上的原子替换。`finally`
删除未成功替换的临时文件，测试同时断言 JSON 内容、最终 mode 600 与没有残留 `*.tmp`
（test_s12.py L667）。

这个原子性只覆盖"一个 writer 不留下半个文件"。claim 是
`_load → 检查 → _save` 三步（L346-L363），两个进程可能同时通过检查；没有文件锁就
没有跨进程互斥。

## 五、依赖检查只有一处实现

`blocking_dependencies(task)`（L249-L261）是依赖语义的单一事实来源：

```text
for dependency_id in task.blocked_by:
    TaskNotFound        → blocker      L256-L258
    status != completed → blocker      L259-L260
    completed           → satisfied
```

`can_start()`（L263-L264）只判断 blocker 列表是否为空；`claim_task()` 在真正写状态前
再次调用同一函数（L353）；`list_tasks()` 的 ready / blocked 展示也用它（L322）。因此
看板显示"ready"与 claim 实际能否成功永远不会使用两套规则，测试对这两者同时断言
（test_s12.py L683）。

缺失依赖不抛出整个调用，而是留在 blocker 列表里。拼错 ID 的任务会明确显示
`blocked by task_missing`，不会因为读不到依赖就被错误放行（test_s12.py L698）。

`_normalize_dependencies()`（L266-L281）在创建时做输入侧清理：非数组直接报错
（L270-L271）、非法 ID 报错（L274-L278）、重复项按首次出现顺序去重（L279-L280）。
`create_task()` 随后拒绝任务把自己列为依赖（L296-L297）——这是本章唯一的环检测，只
覆盖长度为 1 的自环。

## 六、严格状态机

### claim_task（L344-L366）

```text
_load task                                  L346
  → status 必须是 pending                   L347-L350
  → owner 必须非空                          L351-L352
  → blocking_dependencies 必须为空          L353-L357
  → replace(status=in_progress, owner=…)    L358-L362
  → 原子写盘                                L363
  → "Claimed …"                             L364
```

检查顺序让错误有稳定含义：已经 in_progress 的任务返回
`is in_progress, cannot claim`，不会再去讨论它的依赖是否完成。测试逐个断言这四类
拒绝文本（test_s12.py L710）。

### complete_task（L368-L393）

```text
_load task                                  L370
  → status 必须是 in_progress               L371-L374
  → replace(status=completed) + 写盘        L375
  → 扫描直接下游                            L379-L385
  → "Completed …" (+ "Unblocked: …")        L386-L391
```

教程示例函数直接把状态设为 completed，但同一页定义的生命周期是
pending → in_progress → completed。本仓库选择严格执行状态机，因此 pending 不能跳过
claim（`is pending, cannot complete`）。

下游报告有三个同时成立的条件（L382-L384）：candidate 仍是 pending、它的 `blockedBy`
包含刚完成的 task_id、它其余的 blocker 现在为空。这个交集避免把早就 ready 的无关任务
写进 `Unblocked`，测试用一个"独立清理任务"做反向断言（test_s12.py L736）。

顺序上先持久化再扫描（L375 早于 L379），所以扫描时 `blocking_dependencies()` 读到的
已经是新的 completed 状态。

## 七、五个工具怎样进入现有架构

`task_system.py` 同时拥有任务数据、行为和 `TASK_TOOLS` schema（L49-L96）。它不导入
`tool_use.py`，schema 由模块内 `_task_tool()`（L24-L46）构造，形状与 s02 的
`function_tool()` 一致：`type=function`、`additionalProperties=False`、
显式 `required`。方向单一，因此不存在 `tool_use ↔ task_system` 循环依赖。

`tool_use.py` 反向导入 `TASK_TOOLS`（L16），插在父注册表的 `compact` 之前
（L137-L144）：

```text
BASE_TOOLS(5) + todo_write + task + load_skill + TASK_TOOLS(5) + compact = 14
SUB_TOOLS = BASE_TOOLS(5) + load_skill = 6
```

`SUB_TOOLS`（L145）没变。隔离由注册表与 handler 表共同保证：

- 子模型请求里没有任务 schema（test_s12.py L320 断言子请求恰好那 6 个名字）；
- `SubagentRunner.handlers` 里没有 TaskManager handler（test_s12.py L265）；
- 即使子模型凭记忆伪造 `create_task`，`ToolExecutor` 也只返回 unknown tool
  （tool_use.py L177-L179）。

任务工具经过完整的普通工具管线：JSON 解析（tool_use.py L163）、必须是 object
（L166-L167）、PreToolUse 可拦截（L173-L176）、`TypeError` 转成参数错误
（L183-L184）、PostToolUse 观察结果（L186）。测试直接用一个伪造的 `create_task`
tool_call 走 `_execute_tool_batch`，断言磁盘上真的多了一个任务文件
（test_s12.py L790）。

## 八、AgentHarness 只做装配

`__init__`（agent_loop.py L54-L124）仍是五步装配，s12 只在第一步插入三行：

| 步骤 | 位置 | 作用 |
| --- | --- | --- |
| 校验任务目录 | L75-L77 | `assert_inside_workdir(settings.tasks_dir, …)` |
| 创建 `TaskManager` | L78 | 目录不存在时以 mode 700 建立 |
| 合并 handler | L121 | `**self.tasks.handlers()` 进入 `_parent_handlers` |

没有新增 `execute_task_tool` 转发方法，也没有在 `_execute_tool_batch()`（L186-L225）
里给 Task 写特例：任务工具返回字符串、不改写整个 `messages`，因此走
`_execute_parent_tool()`（L182-L184）这条普通分发路径。`compact` 仍是唯一的控制工具
例外（L203-L209）。测试同时断言 `_parent_handlers` 含 `create_task`、`harness.tasks`
是 `TaskManager`，且旧的转发方法名仍然不存在（test_s12.py L224、L265）。

`Settings` 侧同样只是普通配置项：字段 `tasks_dir`（config.py L26）、来源
`CC_TASKS_DIR` 且默认 `<workdir>/.tasks`（config.py L46-L48）。

## 九、System Prompt 的最小变化

s10 的 tools section 直接读取真实注册表（system_prompt.py L28-L31、L62），因此父
Prompt 会自动列出五个任务工具，不需要手写第二份清单。测试断言 `create_task` 出现在
父 Prompt、不出现在子 Prompt（test_s12.py L143）。

真正手写的改动只有父 guidance（system_prompt.py L10-L17）新增三句约束：

- `todo_write` 用于当前执行清单；
- persistent task tools 用于必须跨会话存活的项目级工作；
- 先 list，只 claim ready 的任务，验证之后才 complete。

SubAgent guidance（L22-L25）不提任务系统，与它的工具边界一致。任务板内容本身不常驻
System Prompt：需要状态时调用 `list_tasks`，需要描述时调用 `get_task`，避免每轮把
所有任务正文重复注入上下文——这与 Skill/Memory 只放目录的策略一致。

## 十、错误与恢复边界

`TaskManager` 的五个公开 handler 都把 `TaskError` / `OSError` 捕获成
`Error: ...` 字符串（L311-L312、L334-L335、L341-L342、L365-L366、L392-L393）。这一点
决定了任务失败落在哪一层：它是一次**成功**模型响应里的工具 observation，模型应据此
修正 ID 或等待依赖，而不是让 s11 把它误判成模型 API 故障。

| 失败 | 处理层 | 表现 |
| --- | --- | --- |
| 状态跳转、依赖未满足、任务不存在 | TaskManager handler | `Error:` observation |
| 磁盘 JSON 损坏、写盘 IO 失败 | TaskManager handler | `Error:` observation |
| 参数不是 JSON object、字段不匹配 | `ToolExecutor` tool_use.py L163-L184 | `Error:` observation |
| tasks 目录逃逸工作区 | 装配期 config.py L54-L60 | 构造抛 `ValueError` |
| 429 / 529 / prompt 过长 / 输出截断 | s11 `with_retry` error_recovery.py L126 | 重试、压缩、续写 |
| Bash 危险命令、文件越界 | Permission / BuiltinTools | 拒绝或询问 |

## 十一、父循环怎么把十二章串起来

一个 turn 的骨架（`agent_loop()` agent_loop.py L242-L410），s12 相对 s11 整体下移
八行，形状未变：

```text
agent_loop(messages, active_request, compactor)            L242
  → latest_user_request 兜底                                L251
  → extraction_messages = deepcopy(messages[-12:])          L255
  → refresh_system_prompts(messages)                        L258
  → memory.load_memories / inject_recalled_memories         L259-L260
  → recovery = RecoveryState(settings.model)                L269
  while True:
      → 连续 3 轮未 todo_write 则注入 reminder               L274-L281
      → refresh_system_prompts(messages)                     L284
      → compactor.prepare(messages, active_request)          L285
      → 本 turn 压缩过则从 tools 中移除 compact              L286
      → with_retry(lambda model: create(**completion_request))L290-L304
            except:
              prompt too long 且未压缩 ? reactive + continue  L308-L320
              否则 _append_failure_result → return            L321-L323
      → finish_reason 截断 ?                                  L330
            未升级 ? max_tokens=64K + continue                L331-L339
            已升级 → 存片段 → 达上限 ? return : continuation   L341-L374
      → assistant 消息双写主历史与提取快照                    L377-L379
      → 无 tool_calls ?                                       L382
            Stop Hook 要求继续 ? 追加 user 并 continue         L386-L391
            否则 extract_memories → consolidate → return       L394-L396
      → _execute_tool_batch 逐个执行并配对 role=tool           L400-L405
      → 整批写完后才 compact_history                           L407-L410
```

三处顺序约束继续成立，任务工具也依赖它们：

1. **刷新在压缩之前**（L284 → L285），否则 s08 会按旧 Prompt 体积估算预算；父 Prompt
   现在多了五个工具名，这条约束因此更重要。
2. **完整性先于写入**：截断判定（L330）早于 `assistant_payload()`（L377），残缺的
   `tool_calls` 永远不会进入任务工具分发——半个 `create_task` 参数不会写出半个任务。
3. **整批 role=tool 写完才压缩历史**（L400-L405 → L407-L410）。任务工具的输出可能被
   L4 摘要吞掉，但磁盘任务文件不受压缩影响，这正是任务板与 Todo 的关键差别。

## 十二、行为不变量

`tests/test_s12.py` 共 36 项，分三组。

s10 结构回归（`SystemPromptTest`，14 项）：

| 断言 | 位置 |
| --- | --- |
| 无 Skill / Memory 时只有四个固定 section | L132 |
| 工具行来自真实注册表，父 14 子 6，`create_task` 只在父 Prompt | L143 |
| Skill 出现后 skills section 才出现 | L157 |
| memory section 只放目录，不含正文 | L171 |
| context 不变则复用缓存 / 变化则重建 | L185、L193 |
| 刷新只替换首条 system 消息 | L201 |
| Agent Loop 真正发送组装结果 | L208 |
| 关键能力都是真实对象，含 `harness.tasks` | L224 |
| 十二个能力模块存在、旧碎片文件名消失 | L232 |
| 父工具路由私有，`create_task` 不在子 handler 表 | L265 |
| 工具批次返回控制信号并逐个配对 `role=tool` | L288 |
| SubAgent 工具集恰好那 6 个 | L320 |
| 工作区与 Todo 旧规则仍生效 | L332 |

s11 恢复路径（`ErrorRecoveryTest`，12 项）：

| 断言 | 位置 |
| --- | --- |
| 分类函数与退避公式（0.5 / 32 / Retry-After） | L376 |
| 429 退避序列恰好 `[0.5, 1.0]` | L390 |
| `Retry-After` 优先且不叠 jitter | L417 |
| 三次 529 后第四次请求用 fallback | L438 |
| 重试有界；非瞬态只尝试一次 | L470 |
| 首次截断不污染历史，8K→64K | L498 |
| 64K 截断后追加片段与 continuation | L530 |
| continuation 恰好 3 条，共 5 次请求 | L548 |
| 空片段返回显式错误并写回 assistant | L572 |
| prompt too long 压缩恰好一次后恢复 | L590 |
| 第二次溢出变成 assistant 错误 | L606 |
| 非瞬态错误返回文本而不是 traceback | L622 |

s12 任务系统（`TaskSystemTest`，10 项）：

| 断言 | 位置 |
| --- | --- |
| 完整 JSON、mode 600、无 `*.tmp` 残留 | L667 |
| ready / blocked 看板与 `can_start`、claim 结果一致 | L683 |
| 缺失依赖保持 blocked 而不是崩溃 | L698 |
| claim / complete 四类非法跳转都被拒绝 | L710 |
| 只报告真正被本次完成解锁的下游 | L736 |
| 新 `TaskManager` 实例读到旧任务与 description | L751 |
| 空 subject、非数组 blockedBy、路径穿越、自依赖 | L759 |
| 损坏 JSON 可观察且原文不被覆盖 | L776 |
| `CC_TASKS_DIR` 逃逸工作区时构造失败 | L783 |
| 任务工具经过父 ToolExecutor 并真的落盘 | L790 |

`TaskSystemTest.setUp`（L638-L647）注入 `id_factory`，让 ID 变成 `task_001` 这类确定
值，因此看板文本可以被精确断言；恢复组仍用脚本化 completion 与注入的
`sleep_fn` / `random_fn`（L370-L371），整套测试一秒内跑完。

## 十三、权衡与已知遗留

### 1. 并发只到"单文件不半写"

`os.replace` 保证任何时刻读到的任务文件都是完整 JSON，但 claim 的
read-modify-write 没有跨进程锁（L346-L363）。单 Agent 使用没有问题；要支持多进程
竞争认领，需要文件锁、`O_EXCL` 标记文件或数据库事务。

### 2. 任务图能力有意保持最小

没有修改依赖、删除任务、release / reopen 的公开工具，也没有通用环检测——只拒绝自环
（L296-L297）。因为公开工具只能在创建时声明依赖，正常流程难以构造长环；手工编辑 JSON
仍可能造成永久阻塞，此时只能继续手工修复。

### 3. 规模与可见性

`_all()`（L243-L247）每次都读取全部 `task_*.json`，`list_tasks` 与
`complete_task` 的下游扫描都建立在它之上；任务数量很大时没有索引、缓存或分页。任务板
也不自动注入 System Prompt，模型必须主动 `list_tasks` 才知道当前状态。

### 4. 从前章继承的遗留

`memory.py` 仍使用模块级配置（memory.py L39），同一进程不适合并存两个记忆目录；
`skill_loading.py` 每轮扫盘；`SystemPromptAssembler.invalidate()`（system_prompt.py
L101）仍无调用方；`memory.build_memory_system_prompt`（memory.py L321）自 s10 起已被
memory section 取代；s11 的恢复仍只覆盖父循环主请求。

## 十四、验证

```bash
python3 -m pytest -q tests/test_s12.py
# 36 passed
```

`python3 s12_task_system/code.py` 启动时打印
`model=... fallback=... workdir=... sections=...`。任务侧的运行证据直接看文件系统：

```bash
ls -l "$CC_WORKDIR/.tasks"          # 每个任务一个 mode 600 的 JSON
cat "$CC_WORKDIR/.tasks"/task_*.json
```

`list_tasks` 的每行前缀即状态标签（`[ready]`、`[blocked by …]`、
`[in_progress owner=…]`、`[completed]`），可以在不读代码的情况下判断当前任务图停在
哪一步。
