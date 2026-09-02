# s13：Background Tasks

这一章在 s01–s12 的累计能力之上，把"工具调用完成"和"命令真正结束"分成两件事：父
Agent 的慢 Bash 可以后台执行，原 `tool_call` 立刻收到一个 `bg_XXXX` 占位结果，真实
输出稍后作为独立的 `<task_notification>` 重新进入消息历史。新增的第十三个能力模块是
`background_tasks.py`——线程、状态与通知格式；何时消费通知仍由 Agent Loop 决定。

- 官方教程：[s13 Background Tasks](https://learn.shareai.run/zh/s13/)
- 官方源码：[s13_background_tasks/code.py](https://github.com/shareAI-lab/learn-claude-code/blob/main/s13_background_tasks/code.py)
- 上一章：[s12 Task System](../s12_task_system/)
- 源码分析：[ANALYSIS.md](./ANALYSIS.md)
- 调用图：[CALLGRAPH.md](./CALLGRAPH.md)

## 这一章解决什么

前十二章的每个工具调用都是同步的：`ToolExecutor.execute()` 返回之前，Agent Loop 不
会发起下一次模型请求。读文件、写文件、查任务板都在毫秒级，这个假设成立；`pip
install`、`docker build`、整套 pytest 可能跑几分钟，模型在此期间既不能读代码也不能
安排别的工作。

朴素的解法是"把命令扔进线程，不管结果"，但那会同时破坏两件事：OpenAI 工具协议要求
每个 `tool_call_id` 恰好有一个 `role=tool` 结果，而后台线程如果直接往 `messages`
里追加内容，就会和 Context Compact、Memory 注入在同一个列表上并发写。

s13 的做法是把这两个问题分别收口：

1. 原 `tool_call_id` 立即得到 placeholder，协议在当轮就闭合；
2. worker 只更新 `BackgroundTaskManager` 里的状态，不碰消息列表；
3. 父线程在模型调用边界统一收集终态任务，追加独立的 `role=user` 通知。

消息状态机因此仍然是单线程的，唯一的共享可变状态是任务字典，并由一把锁保护。

## 前台 Bash 与后台 Bash

| 维度 | 前台 Bash | 后台 Bash（s13） |
| --- | --- | --- |
| 触发 | 默认 | `run_in_background=true` 或慢命令启发式 |
| 阻塞 | 阻塞父循环直到命令结束 | 立即返回，父循环继续 |
| tool result | 真实输出 | `[Background task bg_0001 started]` |
| 真实结果 | 同一条 `role=tool` | 后续独立的 `role=user` 通知 |
| PreToolUse | 父线程 | 父线程（分发前完成） |
| PostToolUse | 父线程 | worker 线程（结果产生时） |
| 失败形态 | handler 返回 `Error:` 文本 | 通知里 `status=failed` |
| 可用范围 | 父 Agent 与 SubAgent | 仅父 Agent |

父 Agent 的工具数量没有变化，仍是 14 个：后台能力是 `bash` schema 上的一个可选布尔
字段，不是新工具。SubAgent 仍拿到 6 个同步工具。

## 工具协议

父 Agent 看到的 `bash` 多一个可选字段：

```json
{
  "command": "python3 -m pytest -q",
  "run_in_background": true
}
```

两套 schema 是两个独立常量：`BASH_TOOL` 只有 `command`，`BACKGROUND_BASH_TOOL` 多
一个 `run_in_background`。`PARENT_TOOLS` 用后者替换列表首项，`SUB_TOOLS` 继续使用
前者，因此"子 Agent 不能后台执行"是注册表事实，而不是 Prompt 里的口头约束。

是否后台由 `should_run_background()` 判定，规则只有两条：

1. 参数里出现 `run_in_background` 时，只认 `True`，`False` 一律前台；
2. 没有这个字段时，才用慢命令关键词启发式（`install`、`build`、`test`、`deploy`、
   `compile`、`pytest`、`make` 等）兜底。

启发式只作用于 `bash`。`echo hello` 显式声明为后台就后台执行，`pytest -q` 显式声明
为前台就前台执行——模型的明确选择永远优先于关键词匹配。

`run_in_background` 只控制 Harness 分发，不属于 Bash handler 的参数。分发前会把它
从传给 handler 的字典中剔除，但 `ToolRequest` 保留完整原始参数，权限 Hook 因此能看
到模型的真实请求。

## 两种结果

后台调用会产生两条内容，它们的角色完全不同：

```text
placeholder（同一轮，role=tool，配对原 tool_call_id）
[Background task bg_0001 started] Command: python3 -m pytest -q.
Result will be delivered as a task_notification when complete.

notification（稍后某一轮，role=user，没有 tool_call_id）
<task_notification>
  <task_id>bg_0001</task_id>
  <status>completed</status>
  <command>python3 -m pytest -q</command>
  <summary>46 passed in 0.74s</summary>
</task_notification>
```

通知不复用原 `tool_call_id`：一个 ID 只能有一个结果，第二条同 ID 的 `role=tool` 会
直接违反协议。用独立 user 事件表达"外部世界发生了变化"，与 Todo reminder、Stop Hook
续写是同一种手法。

通知里的 `command` 和 `summary` 都经过 `html.escape()`。命令由模型给出、输出由外部
进程产生，两者都不可信；不转义时 `</task_notification>` 之类的内容可以伪造标签闭合。
摘要默认限制 1000 字符，超出部分替换为 `... (N chars omitted from notification)`，
避免一次构建日志把上下文顶满。原始 `tool_call_id` 只留在任务记录里，不进入通知正文。

## 任务状态与并发不变量

`BackgroundTask` 是不可变快照，字段包括 `id`、`original_tool_call_id`、`tool_name`、
`command`、`status`、`result`、`started_at`、`finished_at`。状态只有三个：

```text
running ──> completed        # runner 正常返回
        └─> failed           # runner 抛出异常
```

`BackgroundTaskManager` 用一把 `threading.Lock` 保护 ID 计数器、任务字典和线程字典，
并保证四条不变量：

- **ID 唯一。** 分配 `bg_0001` 这类序号与注册任务在同一个临界区完成，五个线程同时
  分发也不会拿到重复 ID。
- **worker 不抛异常。** runner 的任何异常都被捕获成 `status=failed` 与
  `Error: background task raised ...` 文本，线程边界不泄漏异常。
- **一次性投递。** `collect_notifications()` 在锁内把终态任务从字典里 `pop` 出来，
  同一个完成事件最多变成一条通知。
- **worker 不改 messages。** 线程只写任务字典；消息列表始终由父线程独占。

`thread.start()` 有意放在锁外：worker 第一件事就是执行工具，而工具完成时又要回到同
一把锁更新状态，在持锁期间启动会把竞争窗口变成潜在的自死锁风险。

## 三个收集点

通知只在父线程的三个位置进入历史，全部位于模型调用边界：

| 位置 | 时机 | 解决的问题 |
| --- | --- | --- |
| turn 开始 | 快照建立之后、Prompt 刷新之前 | 上一个 turn 结束后才完成的任务 |
| 每轮循环开头 | 每次模型请求之前 | 工具批次执行期间完成的任务 |
| 最终回答之前 | 模型不再调用工具时 | 恰好在模型推理期间完成的任务 |

第三个收集点会在注入成功时 `continue`，也就是多花一次模型调用。代价是可见的：如果
结果已经到达，就不该把"还在后台运行"当成最终答案交给用户。

三处都调用同一个 `_inject_background_notifications()`，通知同时写入主历史和 Memory
提取快照（互相独立的深拷贝），因此后续压缩不会让提取阶段丢掉这次外部事件。

## 权限与 Hook 边界

后台分发被拆在权限之后、执行之前：

```text
prepare()  父线程：JSON 解析 → 打印 → PreToolUse / Permission → 组装 handler 参数
   ↓ 被拒绝或参数非法 → 直接作为 tool result 返回，不创建 worker
   ↓ 通过
should_run_background ? start(worker) + placeholder : execute_prepared()
```

`ToolExecutor` 因此从一个 `execute()` 变成 `prepare()` + `execute_prepared()` 两
段，`execute()` 只是二者的顺序组合，供 SubAgent 和旧调用点继续使用。

这样切分带来两条明确保证：`sudo` 这类命令在父线程就被拒绝，永远不会有 worker 被创
建；PostToolUse 则在 worker 线程里、真实结果产生时触发，观察到的是真输出而不是
placeholder。测试直接断言两个 Hook 各自运行在哪个线程名下。

## 模块结构

```text
s13_background_tasks/
├── code.py                    59   CLI 入口
└── harness/
    ├── config.py              60   环境变量、模型、fallback 与持久目录
    ├── models.py              24   跨模块数据契约
    ├── provider.py            52   百炼客户端、请求与响应适配
    ├── agent_loop.py         469   01 Agent Loop：装配能力并驱动父循环
    ├── tool_use.py           369   02 Tool Use：Schema、两段执行、handler
    ├── permission.py          85   03 Permission：deny / ask 决策
    ├── hooks.py               74   04 Hooks：生命周期事件注册与触发
    ├── todo_write.py          46   05 TodoWrite：会话内 Todo 状态与校验
    ├── subagent.py            72   06 SubAgent：隔离历史的同步子循环
    ├── skill_loading.py       99   07 Skill Loading：扫描、目录、按需加载
    ├── context_compact.py    389   08 Context Compact：L1–L4 与手动 compact
    ├── memory.py             540   09 Memory：召回、提取、整理与持久化
    ├── system_prompt.py      105   10 System Prompt：section 组装与缓存
    ├── error_recovery.py     193   11 Error Recovery：分类、退避、有界状态
    ├── task_system.py        442   12 Task System：记录、依赖、状态机、五个工具
    ├── background_tasks.py   283   13 Background Tasks：线程、状态、通知
    └── __init__.py            28   包门面
```

合计 3389 行，其中 `harness/` 3330 行。章节编号只写在文档里，不写进文件名。

## 课程与代码的对应关系

| 章节 | 能力 | 模块 | 主要对象 |
| --- | --- | --- | --- |
| 01 | Agent Loop | `agent_loop.py` | `AgentHarness` |
| 02 | Tool Use | `tool_use.py` | `PARENT_TOOLS`、`ToolExecutor`、`PreparedToolCall` |
| 03 | Permission | `permission.py` | `PermissionPolicy` |
| 04 | Hooks | `hooks.py` | `HookManager`、`install_default_hooks` |
| 05 | TodoWrite | `todo_write.py` | `TodoManager` |
| 06 | SubAgent | `subagent.py` | `SubagentRunner` |
| 07 | Skill Loading | `skill_loading.py` | `SkillLoader` |
| 08 | Context Compact | `context_compact.py` | `ContextCompactor`、`CompactToolController` |
| 09 | Memory | `memory.py` | 召回、注入、提取、整理函数 |
| 10 | System Prompt | `system_prompt.py` | `SystemPromptAssembler` |
| 11 | Error Recovery | `error_recovery.py` | `RecoveryState`、`with_retry`、分类函数 |
| 12 | Task System | `task_system.py` | `TaskRecord`、`TaskManager`、`TASK_TOOLS` |
| 13 | Background Tasks | `background_tasks.py` | `BackgroundTask`、`BackgroundTaskManager` |

`config.py`、`models.py`、`provider.py` 是跨章节基础设施，不强行归入某一课。

## 为什么这样切分

**线程、状态与通知格式放在一个模块。** `background_tasks.py` 同时拥有
`BackgroundTask`、`BackgroundTaskManager` 与通知渲染：一次性投递、终态判定和通知文本
是同一组不变量的三个视角，拆开会让"任务已完成"和"通知已发出"逐渐不一致。

**执行拆成两段，而不是给线程再包一层。** `prepare()` 返回 `PreparedToolCall`，
前台与后台共用同一份解析结果与同一次权限检查。如果换成"后台专用执行函数"，权限、
参数校验、日志就会出现两份实现，迟早只修一边。

**注入点全在 Agent Loop。** 模块只提供 `collect_notifications()`，什么时候把通知写
进历史属于消息状态机的职责。这也是 worker 能保持"不碰 `messages`"的前提。

**通知是 user 事件，不是第二个 tool result。** 协议上一个 `tool_call_id` 只允许一个
结果；语义上后台完成确实是"外部世界发生的事"，与用户新消息同类。

**时钟可注入。** `BackgroundTaskManager(time_fn=...)` 与 `wait()` /
`wait_for_all()` 让测试无需 `sleep` 轮询即可断言完成顺序和竞态处理。

## 依赖方向

课程编号代表学习顺序，不代表 Python 依赖层级。`background_tasks.py` 是第十三课，却
因为只依赖标准库而位于最底层。

```text
config · models · system_prompt · error_recovery · task_system · background_tasks
          ↓
provider · permission · skill_loading · todo_write
          ↓
hooks · memory
          ↓
tool_use（导入 TASK_TOOLS）· context_compact
          ↓
subagent
          ↓
agent_loop（装配 BackgroundTaskManager）→ code.py
```

箭头向下表示"依赖上一层"。下层模块都不导入 `AgentHarness`，因此依赖图无环。

## 行为保持不变

新增后台执行没有削减前章能力：

- 父 Agent 仍是 14 个工具，SubAgent 仍是 6 个同步工具，且不能后台执行；
- 前台 Bash 行为不变，`run_in_background=false` 与不传该字段的旧调用完全等价；
- 普通工具仍经过 Permission 与 PreToolUse / PostToolUse，路径边界继续生效；
- s12 的持久任务板、依赖阻塞与原子落盘不变；
- s11 的 429/529 退避、fallback、8K→64K 续写与一次 reactive compact 都保留；
- Todo 仍限制最多一个 `in_progress`，连续三轮未更新会补提醒；
- System Prompt 仍每次模型调用前按运行态重组，只改写首条 system 消息；
- Context Compact 仍是 L1–L4 加每 turn 一次的 `compact` 控制工具；
- Memory 仍使用独立提取快照，通知也会同步写入该快照。

## 运行与测试

```bash
export DASHSCOPE_API_KEY=...
export MODEL_ID=qwen-plus
export FALLBACK_MODEL_ID=qwen-turbo        # 可选，连续三次 529 后启用
export CC_WORKDIR=/tmp/cc-s13-demo

cd /data/projects/cc-harness-lab
python3 s13_background_tasks/code.py
python3 -m pytest -q tests/test_s13.py
```

终端会打印三类后台日志：分发时的 `[background] dispatched bg_0001: ...`、完成时的
`[background done] ...`、注入时的 `[background inject] N notification(s)`。

可以这样手动验证一次完整链路：

1. 让 Agent 用 `run_in_background=true` 跑一条 `sleep 20 && echo done`；
2. 观察它立刻拿到 `bg_0001` 占位结果并继续读文件；
3. 等 20 秒后再提一个问题，通知会在下一次模型请求前进入历史；
4. 让 Agent 跑 `sudo true` 并声明后台，确认权限在父线程直接拒绝、没有 worker 启动。

46 个 s13 测试分四组：14 项沿用 s10 的结构回归（section 组装、注册表来源、缓存、工具
隔离，并断言十三个能力模块存在、父子 `bash` schema 只有父侧带
`run_in_background`），12 项沿用 s11 的恢复路径回归，10 项沿用 s12 的任务系统回归，
10 项覆盖后台执行本身——显式选择优先于启发式、通知的转义与截断与一次性投递、worker
异常变成 `failed`、并发分发的唯一 ID、Pre/Post Hook 分别落在父线程与 worker 线程、
显式 `false` 走同步且剥掉控制参数、权限拒绝不启动 worker、通知是独立 user 事件、
上一 turn 完成的任务对首次请求可见，以及模型推理期间完成时强制多一轮。

## 已知遗留

- 后台任务只活在当前进程里：worker 是 daemon 线程，CLI 退出即丢失，没有像 s12 那样
  的磁盘事实来源，也没有重启恢复；
- 没有面向模型的后台查询、取消或清理工具（无 `BashOutput` / `KillShell` 等价物），
  模型只能等通知；
- 没有并发上限，也没有对单个后台命令的超时控制；
- 通知只在模型调用边界注入，无法打断正在进行的模型请求；
- 摘要上限 1000 字符，长输出的尾部（往往是失败摘要）可能被截掉；
- 通知是普通 user 消息，可能被 Context Compact 的摘要吞掉，而真实结果此时已不可再取；
- 只有 `bash` 支持后台，`task`、任务工具与文件工具仍是同步的；
- 慢命令启发式是纯关键词匹配，`git test-branch` 这类命令会被误判为慢命令。

## 与官方代码的关系

核心机制与教程一致：daemon 线程执行、占位结果先完成协议配对、`<task_notification>`
形式的完成通知，以及"后台任务仍有 ID 和状态"这一前提。

本仓库的主要差异：

- 官方 s13 为聚焦后台执行保留较小 Agent kernel；本目录继续累计 s01–s12 全部能力；
- 官方使用 Anthropic 工具块，本仓库使用百炼 OpenAI-compatible `tool_calls` /
  `role=tool`，因此 placeholder 是协议硬要求，而不只是交互体验；
- 通知以独立 `role=user` 消息注入，明确不复用原 `tool_call_id`；
- `PreToolUse` 与 Permission 在父线程完成后才创建 worker，PostToolUse 在 worker 线程
  观察真实结果；
- 通知内容做 XML 转义并限制摘要长度，原 `tool_call_id` 不进入通知正文；
- 显式 `run_in_background` 严格优先于慢命令启发式；
- 额外提供 `wait()` / `wait_for_all()` 与可注入时钟，让并发测试确定化；
- 官方教学版和本实现都没有后台任务的持久化、取消工具与并发上限。
