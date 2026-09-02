# s13 源码分析：异步执行与同步消息边界

> 配套 [README.md](./README.md) 与 [CALLGRAPH.md](./CALLGRAPH.md) 阅读。
> 行号对应 `s13_background_tasks/` 当前版本：`code.py` 59 行，`harness/` 17 个模块
> 共 3330 行；未标模块名的行号属于 `background_tasks.py`。
> 对照 [s13 官方教程](https://learn.shareai.run/zh/s13/)和
> [官方源码](https://github.com/shareAI-lab/learn-claude-code/blob/main/s13_background_tasks/code.py)。

## 一、这一章改变的是执行时序，不是循环形状

s12 及以前，一次普通工具调用只有一条同步时间线：

```text
模型 tool_call
  → AgentHarness._execute_tool_batch          agent_loop.py L235
  → ToolExecutor.execute                      tool_use.py L237
  → handler(**arguments)                      tool_use.py L226
  → role=tool observation                     agent_loop.py L264-L270
  → 下一轮模型调用
```

s13 把 Bash 的"协议完成"和"执行完成"拆成两条时间线：

```text
父线程：tool_call → prepare（含权限）→ start worker
                  → placeholder(role=tool) → 下一轮模型调用
worker：            handler → PostToolUse → 终态快照
父线程：下一个模型边界 → collect_notifications
                  → task_notification(role=user) → 模型消费
```

关键难点不在 `threading.Thread`，而在两个约束：

1. 原 `tool_call_id` 必须在当轮闭合，且只能配对一次；
2. 晚到的真实结果只能作为新的外部事件出现，不能是第二个 tool result。

Agent Loop 的形状没有变化：仍然是"请求 → 分类 → 工具批次 → 下一轮"。s13 只在这条
循环的三个位置插入通知收集，并把 Bash 的一段执行拆到别的线程里去。

与前两章对比可以看清三者正交：

| 维度 | RecoveryState（s11） | TaskRecord（s12） | BackgroundTask（s13） |
| --- | --- | --- | --- |
| 存储 | 内存 dataclass | `.tasks/{id}.json` | 进程内字典 |
| 生命周期 | 一个 user turn | 跨会话 | 当前进程 |
| 谁修改 | `with_retry` | 五个任务 handler | worker 线程 |
| 结果去向 | 重试或收口文本 | `Error:` / JSON observation | 独立 user 通知 |

## 二、两个 Bash schema：能力差异写进注册表

`tool_use.py` 定义两个同名工具的两份 schema：

- `BASH_TOOL`（L46-L51）只有 `command`，描述强调 synchronously；
- `BACKGROUND_BASH_TOOL`（L52-L63）增加可选 `run_in_background`，描述说明它用于
  "不该阻塞父 Agent Loop 的慢操作"。

装配时 `PARENT_TOOLS`（L152-L160）用 `BACKGROUND_BASH_TOOL` 替换首项，再展开
`*BASE_TOOLS[1:]`；`SUB_TOOLS`（L161）继续用完整 `BASE_TOOLS`，其中首项还是同步版。

```python
PARENT_TOOLS = [
    BACKGROUND_BASH_TOOL,
    *BASE_TOOLS[1:],
    ...
]
SUB_TOOLS = [*BASE_TOOLS, LOAD_SKILL_TOOL]
```

`BASE_TOOLS[1:]` 这种写法把"首项是 bash"变成结构约定，替换时不会漏掉后面四个文件
工具，也不会重复维护两份文件工具定义。

由此"SubAgent 不能后台执行"是注册表事实：子模型收到的 JSON schema 里根本没有这个
字段，而不是靠 Prompt 提醒。`SUBAGENT_GUIDANCE`（system_prompt.py L24-L27）也确实
没有出现任何后台字样，父侧 `PARENT_GUIDANCE` 才有三句说明（system_prompt.py
L14-L16）：慢命令设 `run_in_background=true`、期间继续做独立工作、把后续
`task_notification` 当结果而不要重跑命令。

父 Agent 的工具数量没有变化，仍是 14 个。s13 只替换了一份 schema，没有新增工具名，
所以 System Prompt 的 tools section 与 s10 建立的结构回归维度都不需要迁移。

## 三、ToolExecutor 为什么拆成 prepare / execute_prepared

s12 的 `ToolExecutor.execute()` 把四件事包在一个同步函数里：解析 JSON、触发
PreToolUse（其中含权限）、调用 handler、触发 PostToolUse。

如果把整个函数原样丢进 worker，会出现两个问题：

1. `PermissionPolicy.check`（permission.py L73）里的交互确认
   （`ask_user` → `input()`，permission.py L60-L71）会在后台线程读 stdin，多个后台
   任务能同时争抢同一个终端，而父循环可能已经在向模型发下一个请求；
2. 被拒绝的调用也会先创建一个 worker 和一个 `bg_id`，然后立刻以拒绝文本结束——留下
   一个从未真正执行过的任务记录。

当前实现按线程归属把管线切成三段：

| 阶段 | 位置 | 所在线程 | 职责 |
| --- | --- | --- | --- |
| `prepare` | tool_use.py L180-L217 | 父线程 | JSON 解析、打印、`ToolRequest`、PreToolUse、参数剥离 |
| `execute_prepared` | tool_use.py L219-L235 | 前台或 worker | handler、`TypeError` 收口、PostToolUse |
| `execute` | tool_use.py L237-L252 | 调用线程 | 顺序组合前两段，供 SubAgent 等旧调用点使用 |

`prepare()` 有三个提前返回，全部是字符串而不是 `PreparedToolCall`：JSON 不合法
（L192-L193）、参数不是 object（L194-L195）、PreToolUse 返回阻断原因（L201-L204）。
调用方用 `isinstance(prepared, str)` 区分这两类结果，因此"没有被批准"和"已批准待
执行"在类型上就是两种东西。

`PreparedToolCall`（L164-L170）是 frozen dataclass，三个字段各有明确读者：

```python
request: ToolRequest                  # 给 Hook 看的完整原始请求
handler: Callable[..., str] | None    # None 表示 unknown tool
handler_arguments: dict[str, Any]     # 剥掉控制字段后的真实调用参数
```

参数剥离在 L206-L212：按 `drop_arguments` 过滤 payload。`ToolRequest`（L200）在过滤
之前构造，所以权限 Hook 看到的是模型的完整请求（包含 `run_in_background`），而 Bash
handler 只收到 `command`，不会因为陌生关键字触发 `TypeError`。控制面参数属于
Harness，业务参数属于 handler，两者的边界就落在这七行里。

`handler is None` 的判断被推迟到 `execute_prepared()`（L222-L223）。这意味着伪造的
工具名也会正常走完 prepare，最后由执行阶段返回 `Error: unknown tool`——unknown 与
参数错误因此不会互相掩盖。

## 四、分发顺序：权限先于线程

`AgentHarness._dispatch_parent_tool()`（agent_loop.py L184-L214）是 s13 唯一的新
分发点，取代了 s12 的 `_execute_parent_tool`。它的顺序是刻意的：

```text
name = tool_call.function.name                          L187
drop_arguments = {"run_in_background"} if name == "bash" else {}   L188-L190
prepared = executor.prepare(..., drop_arguments=...)     L191-L196
isinstance(prepared, str) → 直接返回该文本                L197-L200
should_run_background(name, prepared.request.arguments)  L203
  真 → start(runner=lambda: execute_prepared(prepared))  L205-L212
        → placeholder(bg_id, command)                    L213
  假 → execute_prepared(prepared)                        L214
```

四点值得注意：

- `drop_arguments` 只对 `bash` 生效（L188-L190）。其他工具即使被塞进
  `run_in_background`，也会照常传给 handler 并得到 `Error: invalid arguments`，不会
  被静默吞掉。控制字段的白名单集中在 `BACKGROUND_CONTROL_ARGUMENTS`（L50）。
- 后台判定读的是 `prepared.request.arguments`（L202），即未剥离的原始参数，因此
  `run_in_background` 还在。
- `runner` 是绑定默认参数的 lambda（L209-L211），闭包捕获的是同一个
  `PreparedToolCall`，前台与后台走的是完全同一份解析与授权结果。
- 拒绝分支不创建 worker（L197-L200），所以 `sudo` 这类命令不会留下 `bg_` 记录。

## 五、后台判定只有两条规则

`should_run_background()`（L91-L106）是 classmethod，只有三个分支：

```python
if tool_name != "bash":
    return False
if "run_in_background" in arguments:
    return arguments["run_in_background"] is True
return cls.is_slow_operation(tool_name, arguments)
```

第二个分支用 `is True` 而不是真值判断：字符串 `"false"`、数字 `1` 都不算显式同意，
只有 Python 布尔 `True` 才后台执行。这样模型给出的畸形值会退回同步执行，而不是意外
获得一个无法追踪的后台任务。

显式 `False` 是有意义的控制：它会短路启发式，所以 `pytest -q` 声明为前台就前台执行，
不会被关键词重新覆盖。反过来 `echo hello` 声明为后台也真的后台执行——判定不试图比
模型更懂哪条命令慢。

`is_slow_operation()`（L78-L89）只做小写子串匹配，关键词表 `SLOW_COMMAND_HINTS`
（L24-L36）共 11 项。它的定位写在 L22-L23 的注释里：兼容模型忘填字段的兜底，不承担
调度器的耗时预测职责。代价是 `git test-branch`、`latest-build.log` 这类命令会被误判
为慢命令，被动进入后台。这在教学版里是可接受的：误判的后果是多一条通知，而不是错误
结果。

## 六、BackgroundTaskManager 的四条并发不变量

`BackgroundTask`（L39-L53）是 frozen dataclass，八个字段是某个时间点的快照：

```python
BackgroundTask(
    id: str,
    original_tool_call_id: str,
    tool_name: str,
    command: str,
    status: str,
    result: str = "",
    started_at: float = 0.0,
    finished_at: float | None = None,
)
```

管理器（L56-L282）持有三块可变状态，共享同一把 `threading.Lock`（L73）：

```text
_counter          L74   分配 bg_0001、bg_0002 …
_tasks[bg_id]     L75   当前任务快照
_threads[bg_id]   L76   可 join 的 daemon Thread
```

**不变量一：ID 唯一。** `_next_id()`（L108-L111）本身不加锁，但它只在 `start()` 的
临界区里被调用（L133-L134），与任务注册（L143）处于同一个 `with self._lock`。因此并发
分发不会拿到重复 ID，也不会出现"ID 已分配但任务未注册"的中间态。

**不变量二：状态只前进一次。** worker 完成时用 `dataclasses.replace`（L161-L166）
生成新快照而不是原地改字段，读方拿到的任何 `BackgroundTask` 都自洽。更新前先
`self._tasks.get(bg_id)`（L158）并判空：`collect_notifications()` 只移除终态任务，
正常情况下 current 必然存在，这个判空是防御未来出现别的移除路径（注释在 L159）。

**不变量三：一次性投递。** 终态任务在 `collect_notifications()` 的锁内被 `pop`
（L226），详见第九节。

**不变量四：worker 不碰 `messages`。** 整个模块没有任何消息列表的引用；worker 唯一
的写入目标就是 `_tasks`。消息状态机因此仍是单线程的，Context Compact 与 Memory 注入
不需要考虑并发。

`thread.start()` 被刻意放在锁外（L179，而 `with self._lock` 在 L133-L176 结束）。
worker 第一件事就是执行工具，结束时又要回到同一把锁写终态；在持锁期间启动会让
"worker 抢锁"和"start 仍持锁"发生在同一时刻。这里的 `Lock` 不可重入，把启动挪出临界
区是最省心的写法。`_threads[bg_id]` 在锁内先登记（L176），所以 `wait()` 不会看到
"任务已存在但线程未登记"的窗口。

## 七、worker 的失败收口

`worker()`（L145-L166）是定义在 `start()` 内部的闭包：

```python
def worker() -> None:
    status = COMPLETED
    try:
        result = str(runner())
    except Exception as exc:  # worker 不能把异常泄漏到线程边界之外
        status = FAILED
        result = ("Error: background task raised "
                  f"{type(exc).__name__}: {exc}")
    finished_at = self._time_fn()
    with self._lock:
        ...
```

三个设计点：

1. `except Exception` 覆盖一切非退出异常。线程里未捕获的异常只会打印 traceback 然后
   静默结束，父线程既拿不到结果也等不到通知，任务会永远停在 `running`。捕获后它变成
   一条 `status=failed` 的通知，模型至少知道该重试还是换方案。
2. 状态判定不解释文本。Bash handler 的非零退出码、超时说明都是正常字符串返回值，仍
   记为 `completed`；只有越过 runner 边界的 Python 异常才是 `failed`。这区分"工具成
   功执行并报告命令失败"与"后台执行设施自身出问题"。
3. `finished_at` 在取锁之前采样（L156）。等锁的时间不会被算进任务耗时。

`TERMINAL_STATUSES`（L18）是 `{completed, failed}`，收集逻辑只依赖这个集合，未来加入
`cancelled` 只需要改一处。

## 八、placeholder 与 notification 是两种消息

`placeholder()`（L183-L192）是 staticmethod，只说明任务已被接收：

```text
[Background task bg_0001 started] Command: {command}. Result will be
delivered as a task_notification when complete.
```

它由 `_dispatch_parent_tool()` 返回（agent_loop.py L213），随后被
`_execute_tool_batch()` 写成常规 tool result（agent_loop.py L264-L270），完成
OpenAI function calling 要求的 assistant/tool 配对。文本里同时给出 `bg_id` 和命令
原文，模型据此知道"稍后会有一条通知"，而不是把空结果当成命令失败。

真实结果走另一条路：`_format_notification()`（L204-L213）渲染四个字段。

```text
<task_notification>
  <task_id>…</task_id>
  <status>…</status>
  <command>…</command>
  <summary>…</summary>
</task_notification>
```

`original_tool_call_id` 存在任务记录里，但故意不进入通知正文。它只用于内部诊断；
公开它会诱导后续代码把通知重新按 ID 配对成 tool result，而那正是要避免的做法。

四个字段全部经过 `html.escape()`（L208-L211，理由写在 L205）。命令由模型给出、输出
由外部进程产生，两者都在不可信边界之外；不转义时一段构建日志里出现
`</summary></task_notification>` 就能伪造标签闭合，把后续文本挪到通知之外。

`_result_summary()`（L194-L202）限制摘要长度，默认 `DEFAULT_SUMMARY_CHARS = 1000`
（L20），超出部分替换为 `... (N chars omitted from notification)`。截断只影响通知
文本，`task.result` 仍保存完整输出。构造参数 `summary_chars` 在 `__init__` 里校验必须
为正（L66-L67），因为 0 或负数会让摘要恒为空，属于配置错误而非边界情况。

## 九、一次性投递怎样保证

`collect_notifications()`（L215-L239）把"选择"和"渲染"分在锁的两侧：

```python
with self._lock:
    ready_ids = sorted(
        bg_id for bg_id, task in self._tasks.items()
        if task.status in TERMINAL_STATUSES
    )
    ready = [self._tasks.pop(bg_id) for bg_id in ready_ids]
    for bg_id in ready_ids:
        self._threads.pop(bg_id, None)
```

锁内只做字典操作（L220-L228），渲染与打印在锁外（L231-L238）。因此持锁时间与输出长度
无关，一次 50 万字符的构建日志不会卡住正在收尾的 worker。

移除即"已投递"，不需要额外的 `notified=True` 标记：两个模型边界即使紧邻调用，也只有
第一个能拿到终态任务，第二个得到空列表。`_threads` 同步清理（L227-L228），所以已投递
任务的 Thread 对象不会一直被引用。

排序键是 `bg_id` 字符串。四位零填充 ID 在 `bg_9999` 以内等价于创建顺序；超过之后
字符串排序不再等于数值排序。这是进程内教学演示可以接受、但值得知道的边界。

代价是 `snapshot()`（L241-L246）看不到已投递任务：字典里只剩 `running` 的项。它服务
于状态展示与测试断言，不是历史账本。同理 `running_count`（L278-L282）统计的是当前
字典中状态为 `running` 的任务数。

## 十、Agent Loop 的三个收集点

`_inject_background_notifications()`（agent_loop.py L216-L233）做三件事：取通知
（L223）、把每条通知作为 `role=user` 追加到主历史与提取快照（L224-L227）、返回条数
（L233）。返回值使得调用方可以据此改变控制流。

它在 `agent_loop()` 里被调用三次：

| 收集点 | 位置 | 时机 | 解决的问题 |
| --- | --- | --- | --- |
| ① | L304 | 建立提取快照之后、刷新 Prompt 之前 | 上一个 user turn 结束后才完成的任务 |
| ② | L324 | 每次 `while` 循环开头，模型请求之前 | 工具批次执行期间完成的任务 |
| ③ | L438-L441 | 模型不再请求工具、给出最终答案之前 | 恰好在模型推理期间完成的任务 |

收集点①必须晚于 `active_request = active_request or latest_user_request(messages)`
（L297）。通知的 role 是 `user`，如果先注入再回溯"最近的用户请求"，
`<task_notification>` 就会被当成当前 turn 的目标，压缩摘要也会跟着跑偏。注释写在
L302-L303。

收集点②与 Prompt 刷新、`compactor.prepare` 的顺序同样固定：L324 注入 → L337 刷新
Prompt → L338 压缩预算 → L339 计算可见工具。通知先进历史，压缩时才会把它计入预算，
不会出现"注入后立即超预算"的情况。

收集点③是唯一会改变控制流的一处：

```python
if not assistant.tool_calls:
    if self._inject_background_notifications(
        messages, extraction_messages
    ):
        continue
    answer = assistant.content or ""
```

有通知就 `continue`，多花一次模型调用。这修的是典型的 check-after-work 竞态：模型在
推理时任务刚好完成，此时直接返回"命令还在后台运行"会把已经可见的结果丢掉。注释在
L436-L437。

三个点之外，worker 不会主动写历史。这条边界的直接后果是通知无法打断正在进行的模型
请求——延迟上限是一次模型调用的时间，换来的是消息列表始终单线程可写。

## 十一、通知为什么同时进入 extraction_messages

s09 Memory 在 turn 开始时保存独立的压缩前快照（L301），提取阶段用的是它而不是主
历史。如果通知只追加 `messages`，提取时就只能看到"启动了后台任务"，看不到"任务完成
及其结果"。

所以 `_inject_background_notifications()` 对每条通知分别写入两处，且提取快照用
`copy.deepcopy`（L227）。两份历史后续各自演化：主历史可能被 L338 的 `prepare` 或
L468 的 `compact_history` 改写，快照不受影响。

这不等于把整段工具输出写进长期记忆。提取阶段仍由 `memory.extract_memories`
（L453）按自身规则筛选，工具输出与临时状态会被拒绝。这里保证的只是输入历史完整。

## 十二、生命周期与退出边界

worker 是 daemon 线程，名字为 `cc-background-{bg_id}`（L168-L175）。命名不只是为了
调试：回归测试直接用 `threading.current_thread().name` 断言 PreToolUse 在父线程、
PostToolUse 在 `cc-background-bg_0001` 线程。

daemon 意味着 Python 退出时不等待它们。`code.py` 的 CLI 循环没有在退出前调用
`wait_for_all()`，所以未完成的后台命令会随进程一起消失，也没有磁盘记录可供恢复——
这与 s12 的持久任务板正好相反，是本章最明显的取舍。

管理器提供两个等待入口：

- `wait(bg_id, timeout)`（L248-L259）：锁内取线程（L252-L253），锁外 `join`。线程
  不存在时返回 `True`，因为"已被 collect 移除"与"从未存在"对调用方是同一件事。
- `wait_for_all(timeout)`（L261-L276）：先算出统一 deadline（L266），再对每个线程
  join 剩余时间（L270-L275），因此总等待时间受同一个 timeout 约束，而不是每个线程
  各等一遍。它不启动轮询线程，也不持锁 join。

线程终究不是进程管理器。Bash handler 内部是 `subprocess.run`，Python 层没有取消原语
可以可靠终止已经启动的子进程；进程崩溃也不保证清理整个进程树。生产级实现通常需要
进程组、输出落盘、watchdog、恢复日志与显式的 cancel / kill 工具。

## 十三、错误与边界

| 位置 | 检查 | 失败输出 |
| --- | --- | --- |
| `BackgroundTaskManager.__init__` | `summary_chars` 必须为正 | 构造时 `ValueError`（L66-L67） |
| `start` | 只有 `bash` 能后台执行 | `ValueError`（L126-L127） |
| `start` | command 必须是非空字符串 | `ValueError`（L128-L129） |
| `prepare` | arguments 必须是 JSON object | `Error:` observation（tool_use.py L192-L195） |
| `prepare` | PreToolUse / Permission | 拒绝文本作为 tool result（tool_use.py L201-L204） |
| `execute_prepared` | 工具名未注册 | `Error: unknown tool …`（tool_use.py L222-L223） |
| `execute_prepared` | handler 形参不匹配 | `Error: invalid arguments …`（tool_use.py L227-L231） |
| `worker` | runner 抛出任何异常 | `status=failed` + `Error: background task raised …`（L149-L154） |
| s11 `with_retry` | 模型请求 429 / 529 | 重试或 fallback（error_recovery.py L126） |

`start()` 的两个 `ValueError` 与其他条目性质不同：它们是 Harness 内部约定被违反，而
不是模型输入不合法——模型的畸形输入在 `should_run_background()` 就已经退回同步执行，
根本到不了 `start()`。因此这里用异常而不是 `Error:` 文本。

后台失败同样不会进入 Error Recovery。s11 处理的是"模型请求失败"，需要重发 API；后台
失败已经是一次成功响应之后的世界状态变化，模型应据此调整计划。

## 十四、回归测试怎么锁住这些性质

`tests/test_s13.py` 共 46 项，前 36 项继承 s10–s12（结构回归 14 项、Error Recovery
12 项、Task System 10 项），证明后台能力没有削弱既有路径。其中两项被本章直接加强：
`test_tool_section_comes_from_real_registries`（L149）断言父 14 子 6 且只有父侧
`bash` 带 `run_in_background`，`test_modules_follow_course_capability_names`（L248）
把期望模块数提高到 13。

`BackgroundTasksTest`（L840）的 10 项对应上面的分析：

| 测试 | 锁住的性质 |
| --- | --- |
| L876 | 显式布尔优先于慢命令启发式 |
| L897 | 通知转义、摘要截断、同一事件只投递一次 |
| L919 | worker 异常变成 `status=failed` 而不是崩溃 |
| L937 | 并发分发得到唯一 ID |
| L970 | PreToolUse 在父线程、PostToolUse 在 worker 线程 |
| L998 | 显式 `false` 走同步且控制参数被剥离 |
| L1011 | 权限拒绝不启动 worker |
| L1019 | 通知是独立 user 事件，不复用 `tool_call_id` |
| L1043 | 上一 turn 完成的任务对本 turn 首个请求可见 |
| L1072 | 推理期间完成时强制多一轮，不返回"仍在运行" |

线程归属那一项（L970）不是实现细节测试，而是安全边界测试：只要有人把 PreToolUse 或
权限检查挪进 worker，它立刻失败。同理 L1011 保证被拒绝的命令不会留下任务记录。

确定性靠两个注入点实现：`BackgroundTaskManager(time_fn=…)` 让时间可控，`wait()` /
`wait_for_all()` 让测试等待具体 worker 而不是 `sleep` 轮询。

## 十五、已知遗留与延伸方向

- **没有持久化。** 任务只活在进程内存里，重启即丢失。若要跟上 s12 的做法，需要给
  每个任务一个磁盘记录和重启后的孤儿清理策略。
- **没有面向模型的查询与取消。** 官方 Claude Code 有 `BashOutput` / `KillShell`
  这类工具，本章只有通知一条出路，模型无法主动轮询进度或中止任务。
- **没有并发上限与单任务超时。** 模型连续分发十个 `docker build` 也会全部启动。
- **通知可能被压缩吞掉。** 通知是普通 user 消息，落入 Context Compact 的摘要范围，
  而此时真实结果已从 `_tasks` 中移除，无法重取。
- **摘要按前 1000 字符截断。** 失败信息往往在输出尾部，正是被截掉的那一段。
- **只有 `bash` 支持后台。** `task`（SubAgent）通常比 Bash 更慢，却仍是同步的。
- **启发式是纯关键词匹配。** 误判只会改变执行方式而不改变结果，但会让通知变多。
