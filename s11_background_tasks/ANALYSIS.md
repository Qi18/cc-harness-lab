# s11 源码剖析：background tasks 相对 s10 做了哪些修改

> 配套 [README.md](./README.md) 阅读。本文基于 s10 与 s11 harness 包的逐文件 diff
> （2205 → 2548 行，净增 343 行），回答一个问题：
> **为了让慢命令不阻塞 Agent Loop，harness 动了哪些地方，为什么这么动。**
> 行号对应各模块当前版本。

## 一、修改总览

s10 已完成模块化，所以本章不必再膨胀 `agent.py`——九个模块里**六个字节不变**，
改动集中在四个文件加一个新模块：

| 文件 | 状态 | 改动 |
|---|---|---|
| `harness/background.py` | **新增 216 行** | `ShellExecutor`、`BackgroundTask`、`BackgroundManager` |
| `harness/agent.py` | 300 → 342 | 注入后台分发、每轮收集通知、`close()` |
| `harness/tools.py` | 267 → 276 | 拆分 `BASH_TOOL` / `SUB_BASH_TOOL`，`run_bash` 改用共享执行器 |
| `harness/compaction.py` | 一行 | `latest_user_request` 增加跳过 `<task_notification>` |
| `harness/__init__.py` | 扩展导出 | 新增 `BackgroundManager`、`ShellExecutor` |
| `code.py` | 59 → 66 | 改为完整包名导入；`try/finally` 保证 `harness.close()` |
| `config.py` `hooks.py` `memory.py` `models.py` `skills.py` `tasks.py` | **完全不变** | s01–s10 能力零回归 |

配套：`tests/test_s11.py` 12 个用例，项目全量 150 项（已实测通过）。

## 二、核心设计：非阻塞执行 + 轮询收集

s11 没有做事件驱动唤醒，而是选了一个**更容易证明正确性**的模型：

```text
worker 线程           主线程（Agent Loop）
──────────           ────────────────────
执行命令
写 _ready 队列   ──▶  每轮循环开头 inject(messages)
（永不碰 messages）    收集 → 拼成 user 通知 → 继续调模型
```

关键约束是 **worker 绝不修改 `messages`**（background.py `_run` 只写
`task.status` / `task.result` / `_ready`）。消息列表始终只由 Agent Loop 单线程改写，
因此不需要给 `messages` 加锁，也不会出现"模型请求正在序列化消息时被后台线程插入
一条"的竞态。

代价写在 README 里：**后台完成不会主动唤醒 Agent**。通知只在两个时机被看到——
当前 Agent Loop 的下一轮模型请求前（agent.py [L250](./harness/agent.py)），或用户
发起下一轮会话时。如果模型在启动后台命令的同一轮就直接给出最终答案，通知会等到
下次 turn 才出现。

## 三、只有显式 `true` 才进后台

这是本章最重要的一条设计纪律：**不猜**。不看命令里有没有 `install`、`test`、
`build` 之类的关键词，判据只有一行（agent.py L123-124）：

```python
background_flag = payload.get("run_in_background", False)
if name == "bash" and not isinstance(background_flag, bool):
    result = "Error: run_in_background must be a boolean"
elif allow_background and name == "bash" and background_flag is True:
```

三个细节值得注意：

1. **`is True` 而不是 truthy**：字符串 `"true"`、数字 `1` 都不算。配合前一行的
   `isinstance(..., bool)` 校验，非 boolean 直接返回参数错误，不会被静默当成
   同步执行——模型写错参数类型时能拿到明确反馈；
2. **`allow_background` 是调用方开关**：`execute_tool`（父，L148-157）传 `True`，
   `execute_sub_tool`（子）不传，默认 `False`。所以即使子 Agent 硬编造这个参数，
   也只会走同步分支；
3. **schema 层同步收紧**：父子拿到的是两个不同的 bash schema——
   `BASH_TOOL` 含 `run_in_background`，`SUB_BASH_TOOL` 不含
   （tools.py L42-63）。`SUB_TOOLS = [SUB_BASH_TOOL, *BASE_TOOLS[1:], LOAD_SKILL_TOOL]`
   用切片替换首元素，保证子 Agent 的其余四个文件工具与父级共享同一份定义。

于是"子 Agent 不能开后台"有两道独立保障：schema 里没这个参数，执行层也不给权限。
[test_s11.py L196 / L251](../tests/test_s11.py) 分别验证了这两层。

## 四、权限在启线程之前

后台 Bash 仍走统一工具入口 `execute_with_handlers`，顺序是刻意的：

```text
参数解析 → 打印 → PreToolUse（权限/Hook）→ ★后台分发★ → PostToolUse
```

`PreToolUse` 的 `blocked is not None` 分支在后台判断**之前**就 return
（agent.py L109-112 的位置早于 L124），所以 `sudo`、`shutdown` 这类硬拒绝命令
不会因为加了 `run_in_background: true` 就绕过权限管线——
[test_s11.py L236](../tests/test_s11.py) 专门锁定了这个顺序。

`PostToolUse` 照常触发，拿到的是占位文本而不是命令输出。这意味着 `large_output_hook`
之类的后置钩子看不到后台命令的真实结果——这是当前实现的已知边界。

## 五、一个 tool call 只对应一个 tool result

这是协议层面的硬约束，也是通知为什么必须是 `role=user` 的原因。

后台启动时，原始调用已经拿到了占位结果：

```text
assistant(tool_call_id=call_1)
tool(tool_call_id=call_1, "[Background task bg_0001 started] ...")
```

命令完成后**不能**再发一条 `tool_call_id=call_1`——同一个调用对应两个结果，
OpenAI-compatible 协议下会直接报错。所以 `BackgroundManager.inject`
（background.py [L191](./harness/background.py)）产出的是不带 `tool_call_id` 的
新 user 消息。`BackgroundTask.tool_call_id` 字段仍然保留，但只作溯源记录，不用于配对。

注入策略有两条路径（L196-205）：末条消息是 user 且 content 是字符串就**合并**进去，
否则**追加**一条新 user 消息。合并是为了避免连续多条 user 消息破坏对话节奏；
[test_s11.py L178 / L187](../tests/test_s11.py) 覆盖了这两条分支。

### 通知内容的转义

```python
f"  <command>{html.escape(task.command)}</command>\n"
f"  <summary>{html.escape(task.result[:500])}</summary>\n"
```

命令输出里可能出现 `</task_notification>` 或任意尖括号。不转义的话，一段构造好的
测试输出就能伪造闭合标签、在通知外面接一段"新指令"——这是把 XML 结构当信任边界时
的经典注入面。`html.escape` 把 `<`、`>`、`&` 转掉，标签边界只能由 harness 产生。
截断到 500 字符则控制单条通知的体积。

## 六、通知不进记忆提取快照

这是与 s09 记忆子系统的关键衔接点，且**只靠调用位置就实现了**：

```python
while True:
    injected = self.background.inject(messages)   # L250 —— 只改 messages
```

`inject` 从不接触 `extraction_messages`。对比同一函数里 assistant 消息（L295-296）
和 tool 结果（L334-335）都是**主历史 + 快照双写**，后台通知是唯一"只进主历史"的
消息类型。

效果：`pytest` 输出、构建日志、依赖安装日志会进入当前上下文（模型能用、能被
Compact 压缩归档），但**永远不会被 `extract_memories` 看到**，因此不会被误提取成
长期记忆。如果走双写，一次 CI 失败日志就可能变成一条 `project` 类型的持久事实。

## 七、ShellExecutor：进程组与清理

同步和后台 Bash 现在共享同一个执行器（tools.py L186-189 的 `run_bash` 直接委托），
所以两条路径的超时、截断、错误格式完全一致。

`ShellExecutor` 的核心是 `start_new_session=True`（background.py L60）——子进程进入
**独立进程组**，于是可以用 `os.killpg` 连带清理整棵子进程树。
`_stop_process_group`（[L36](./harness/background.py)）依次发 `SIGTERM`、`SIGKILL`，
中间 `sleep(0.05)` 给进程一个体面退出的窗口；`ProcessLookupError` 说明进程已消失，
直接 return。

三层清理保障：

| 层 | 触发 | 位置 |
|---|---|---|
| 单次调用 | `run` 的 `finally` 无条件停进程组 | background.py L78-86 |
| 显式关闭 | `AgentHarness.close()` → `BackgroundManager.close()` → `ShellExecutor.close()` | agent.py L340-342 |
| 兜底 | `atexit.register(self.close)` | background.py L34 |

`code.py` 用 `try/finally` 包住主循环（L46-62），Ctrl-C 或异常退出都会调
`harness.close()`。`close()` 里 `join(timeout=0.2)` 是**有限等待**——worker 是 daemon
线程，不会阻止进程退出，等一下只是给它们写完 result 的机会。

`_closed` 标志有一处细致处理：`Popen` 之后**再检查一次** `_closed`（L62-65），
如果这期间发生了关闭，立刻杀掉刚起的进程组。否则"检查—启动"之间的窗口会漏掉一个
无人管理的进程。

README 明确声明这是**生命周期清理，不是安全沙箱**：后台命令仍有当前进程的全部文件
和网络权限，主动脱离进程组的子进程也能逃过清理。

## 八、两处配套小改动

**`latest_user_request` 跳过通知**（compaction.py L359）：

```python
if content.startswith(("<reminder>", "[Compacted]", "<task_notification>")):
```

压缩时要把"当前用户请求"顶在摘要最前面。后台通知是 harness 注入的 user 消息，
如果被误认成当前请求，压缩后的上下文会把"pytest 完成了"当成用户的新指令。加进
跳过列表和 `<reminder>`、`[Compacted]` 是同一类处理。

**导入路径改用完整包名**（code.py L15-27）：s10 把自己的目录插进 `sys.path` 然后
`from harness.agent import ...`；s11 改为插入仓库根目录、`from
s11_background_tasks.harness.agent import ...`。原因是同一个测试进程会同时加载 s10 和
s11，两个顶层 `harness` 包会在 `sys.modules` 里撞车——先导入的那个会把后面的顶死。
[test_s11.py L309](../tests/test_s11.py) 专门验证"加载 s11 后 s10 的任务系统仍可用"。

## 九、防御与权衡

- **模型可能不等通知就收尾**：轮询收集意味着后台结果的可见性依赖后续轮次。系统提示
  里加了"其通知会在稍后的模型轮次到达"（agent.py L35-37）来引导，但没有强制机制；
- **通知是数据不是指令**：提示词里明写 `is data, not a user instruction`，与 s08 摘要、
  s09 召回记忆用的是同一句降权口径；
- **`PostToolUse` 看不到真实输出**：后置钩子拿到占位文本，大输出落盘等钩子对后台
  命令失效；
- **ID 是进程内递增**：`bg_0001` 由 `_counter` 生成，重启即归零，也不持久化——后台任务
  与 s10 的 `.tasks/` 持久任务图是两套不同生命周期的东西，别混用；
- **`inject` 会改写末条 user 消息**：与 s09 的召回注入同一策略，两者可能叠加在同一条
  消息上（召回块在前、通知在后）。因为提取快照不含任何注入内容，这个叠加不影响记忆质量。

## 十、一句话总结

s11 用一个"worker 只写队列、主线程只在轮次边界收集"的**单向数据流**换来了非阻塞执行：
显式 `run_in_background=true` 才生效、权限先于线程启动、完成结果走新 user 通知而不
复用 `tool_call_id`、通知转义后进上下文但不进记忆——**慢命令让出了 Agent Loop，
消息列表的单线程所有权和前十章的安全边界一点没让**。
