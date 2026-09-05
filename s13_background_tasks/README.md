# s13 Background Tasks：命令在后台执行，结果稍后回来

s13 让父 Agent 可以在 Bash 命令执行期间继续工作。它把一次工具调用拆成两个阶段：先返回“后台任务已启动”，等命令结束后，再把结果作为通知送回模型上下文。

本文按本仓库的实际代码介绍这一机制。逐函数分析见 [ANALYSIS.md](./ANALYSIS.md)，完整接入路径见 [调用图](./CALLGRAPH.md)。

## 1. 为什么需要后台任务？

假设 Agent 要修改代码并运行测试。同步执行时，测试命令不返回，父循环就无法继续请求模型。即使还有可以独立完成的代码检查，也必须等待。

s13 允许模型这样调用 Bash：

```json
{"command":"python3 -m unittest discover -s tests","run_in_background":true}
```

Harness 启动 worker，立即返回一个类似 `bg_0001` 的任务标识。模型随后可以读取其他文件；测试输出则在完成后经由通知返回。

这里后台执行的是一次工具调用，worker 自己不会请求模型，也没有独立的对话历史。这与 s15 的队友 Agent 不同。

## 2. 一次后台执行经历什么？

1. 模型请求 Bash，Harness 先解析参数、触发 PreToolUse 并执行权限检查。
2. 检查通过后，Harness 登记任务为 `running`，启动 worker。
3. 原工具调用立即收到“已启动”的占位结果，父循环继续。
4. worker 执行工具，在真实结果产生后触发 PostToolUse，并更新任务状态。
5. 父循环到达收集点时取走终态任务，将通知放入历史。
6. 下一次模型请求看到通知，才能根据结果判断后续操作。

```text
模型请求 Bash
  → prepare：参数、Hook、权限
  → start(worker) ───────────────→ 执行 Bash → 写回终态
  → role=tool：已启动 bg_0001                     │
  → 父循环继续工作                               │
  → 收集完成通知 ←───────────────────────────────┘
  → role=user：task_notification → 模型处理结果
```

“启动成功”不等于“测试通过”。如果后续操作依赖测试结论，仍需要等到结果并检查内容。

## 3. 为什么需要两种结果？

原来的工具请求需要一个与 `tool_call_id` 配对的返回值。后台分发后，这个位置先由占位文本完成：

```text
role=tool，tool_call_id=原调用 ID
[Background task bg_0001 started] Command: ...
Result will be delivered as a task_notification when complete.
```

真正的结果稍后作为独立事件注入：

```text
role=user
<task_notification>
  <task_id>bg_0001</task_id>
  <status>completed</status>
  <command>python3 -m unittest discover -s tests</command>
  <summary>...测试输出...</summary>
</task_notification>
```

这份实现不会再给同一个工具调用追加第二条工具结果。`bg_0001` 用来关联后台工作与完成通知，原 `tool_call_id` 只保留在内部记录中。

通知默认保留输出的前 1000 个字符，并转义命令和摘要中的 XML 特殊字符。它是结果摘要，不是完整日志接口；XML 转义也不等于语义上的提示注入防护。

## 4. 什么时候走后台？

后台选择只作用于父 Agent 的 Bash 分发路径，SubAgent 继续同步执行工具。

| 参数 | 当前行为 |
| --- | --- |
| `run_in_background=true` | 后台执行 |
| `run_in_background=false` | 前台执行，即使命令包含慢操作关键词 |
| 未传该字段 | 根据 `install`、`build`、`test`、`pytest`、`make` 等子串判断 |
| 传入字符串、数字等非布尔值 | 不会因该值启动后台执行 |

因此，不传字段不总是等于 `false`。关键词判断只是启发式，并没有测量执行时间，例如命令中的文件名包含 `test` 也可能触发后台路径。

`run_in_background` 是 Harness 的控制参数，在调用 Bash handler 前会剥离；权限 Hook 仍能看到包含它的原始请求。

## 5. 三个模块怎样分工？

| 模块 | 职责 |
| --- | --- |
| [tool_use.py](./harness/tool_use.py) | `prepare` 做执行前检查；`execute_prepared` 执行并触发 PostToolUse |
| [background_tasks.py](./harness/background_tasks.py) | 管理 worker、任务 ID、状态快照和通知格式 |
| [agent_loop.py](./harness/agent_loop.py) | 决定前台或后台执行，以及何时把通知写入历史 |

worker 只更新任务表，不修改父循环的 `messages`。任务表由锁保护；模型上下文的更新和 Compact 仍由父循环管理。

状态转换很小：

```text
running
  ├─ runner 正常返回 → completed
  └─ runner 抛出异常 → failed
```

**completed 不代表命令业务成功。** 当前 Bash handler 不根据非零退出码抛异常，超时也会返回错误文本。因此，退出码非零或输出“Error:”时，后台状态仍可能是 `completed`。判断测试是否通过需要检查输出；若需要可靠的自动成功判定，还需结构化返回退出码。

## 6. 通知怎样进入主循环？

`_inject_background_notifications` 在三个位置收集结果，并同步写入对话历史和 Memory 提取快照。

| 位置 | 用途 |
| --- | --- |
| turn 开始 | 接收上一轮结束后完成的任务 |
| 每轮循环入口 | 接收执行其他工具期间完成的任务 |
| 模型准备给最终回答时 | 补收模型推理期间完成的任务 |

最后一次收集如果拿到了通知，循环会 `continue`，让模型先看结果再给最终回答。

但如果此时没有完成通知，父循环可以结束 turn，即使后台还有任务。worker 完成不会自己发起新一轮模型请求，也不能打断正在进行的模型调用。用户下次输入时，尚未收集的通知仍可进入历史。

`collect_notifications` 在锁内取走并删除终态记录，避免正常运行时重复收集。这不是持久消息队列：收集后、注入前发生崩溃仍可能丢失通知。

## 7. 运行边界

- 后台任务表和 worker 只存在于当前进程。daemon 线程不会阻止 CLI 退出，也没有任务重启恢复。
- 后台路径复用 Bash handler，仍受其 **120 秒超时**限制；后台化不会让命令无限运行，也不保证所有后代进程都被完整清理。
- 没有面向模型的查询、取消工具，也没有后台并发数量上限。`wait`、`wait_for_all` 是内部辅助接口。
- 通知摘要可能截掉错误尾部。Bash handler 本身也会截断长输出；任务记录被收集后，管理器不再保留该记录。需要长期保存日志时，应在执行命令时明确写入工作区文件。
- 权限检查在分发前完成，被拒绝的调用不会创建 worker。此处的“父线程”指当前调用分发线程；到 s14 中，它也可能是处理定时 turn 的线程。

## 8. 它与前后章节是什么关系？

| 能力 | 管理对象 | 关键问题 |
| --- | --- | --- |
| s12 Task System | 持久任务记录与依赖 | 有哪些任务、谁负责、是否被阻塞？ |
| s13 Background Tasks | 当前进程内的一次 Bash 执行 | 命令执行时，父循环怎样继续工作并接收结果？ |
| s14 Cron Scheduler | 时间规则和待投递 prompt | 什么时候自动让 Agent 开始工作？ |
| s15 Agent Teams | 有独立上下文的队友 | 多个 Agent 怎样并行推理并互相通信？ |

s13 的 `bg_0001` 不会自动对应到 s12 的任务板记录；这些能力可以组合使用，但状态并没有自动同步。

## 9. 阅读与验证

先读 `AgentHarness._dispatch_parent_tool`，看检查与分发如何拆开；再读 `BackgroundTaskManager.start` 和 `collect_notifications`；最后搜索主循环中的三个注入点。

从仓库根目录运行已有测试：

```bash
python3 -m unittest tests.test_s13
```

[test_s13.py](../tests/test_s13.py) 覆盖显式选择、权限拒绝、Hook 线程位置、worker 异常、通知截断与转义，以及结束前补收结果等行为。若已配置模型访问，可运行 `python3 s13_background_tasks/code.py` 做交互验证。

读完后可以自问：占位结果解决了什么？worker 为什么只写任务表？为什么看到 completed 还不能直接宣布测试通过？
