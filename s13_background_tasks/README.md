# s13：Background Tasks

s13 在模块化 s12 上加入显式后台 Bash：慢命令在线程中运行，Agent Loop 先收到占位
结果继续工作，命令完成后再通过独立通知回到上下文。

- 官方教程：[s13 Background Tasks](https://learn.shareai.run/zh/s13/)
- 官方源码：[s13_background_tasks/code.py](https://github.com/shareAI-lab/learn-claude-code/blob/main/s13_background_tasks/code.py)
- 上一章：[s12 Task System](../s12_task_system/)

## 问题

s12 的所有工具仍同步执行。`pytest`、依赖安装和构建可能持续数分钟；如果后续工作不依赖
它们的结果，Agent 本可以同时阅读配置、整理文档或处理另一个已解锁任务。

s13 让父 Agent 可以显式调用：

```json
{
  "command": "python3 -m pytest",
  "run_in_background": true
}
```

Harness 立即返回：

```text
[Background task bg_0001 started] The result will be collected on a later model round.
```

后台命令完成后，在后续模型轮次前注入：

```xml
<task_notification>
  <task_id>bg_0001</task_id>
  <status>completed</status>
  <command>python3 -m pytest</command>
  <summary>tests completed successfully</summary>
</task_notification>
```

## 模块位置

s12 已完成模块化，所以本章不用继续膨胀 `agent.py`：

```text
s13_background_tasks/
├── __init__.py
├── code.py
└── harness/
    ├── background.py    # s13 新增：进程、worker、完成队列和通知
    ├── agent.py         # 接入后台分发与每轮通知收集
    ├── tools.py         # 扩展父 Agent 的 Bash schema
    ├── recovery.py      # s11 的错误恢复
    └── ...              # s01-s12 累计模块
```

章节通过完整包名 `s13_background_tasks.harness` 导入，避免同一测试进程加载 s12 和 s13
时两个顶层 `harness` 包发生 `sys.modules` 缓存冲突。

## 只有显式 true 才进入后台

不根据 `install`、`build`、`test` 等关键词猜测。后台条件严格为：

```python
name == "bash" and run_in_background is True
```

- 参数缺失或 `false`：同步执行；
- 参数不是 boolean：返回参数错误；
- 非 Bash 工具：没有该参数；
- SubAgent 的 Bash schema：不暴露该参数，保持同步执行。

后台任务只适合独立慢命令。如果下一步必须读取该命令生成的文件或退出码，就应同步执行。

## 执行顺序

后台 Bash 仍经过统一工具入口：

```text
assistant tool_call
    ↓
PreToolUse Permission / Hooks
    ↓ allowed
BackgroundManager.start(command)
    ↓
placeholder role=tool
    ↓
下一轮继续调用模型
```

权限检查发生在启动线程之前，所以 `sudo`、`shutdown` 等硬拒绝命令不会因为设置了后台
参数而绕过权限管线。占位结果仍使用原始 `tool_call_id`，满足 OpenAI-compatible 的工具
消息配对要求。

## BackgroundManager

`BackgroundManager` 管理三类状态：

- `tasks`：运行中或等待收集的 `BackgroundTask`；
- `_threads`：daemon worker，用于退出时有限等待；
- `_ready`：已经完成、尚未通知模型的任务 ID。

ID 是进程内递增的 `bg_0001`、`bg_0002`。worker 只负责执行和把结果放进 ready 队列，
不会直接修改 `messages`。主线程在每次模型请求前调用 `background.inject(messages)`，从而
保持消息列表只由 Agent Loop 修改。

完成通知是一次性的：`collect()` 返回后会从 `tasks` 删除记录并清空对应 ready 项，后续
轮次不会重复注入。

## 一个 tool call 只对应一个 tool result

原始后台 Bash 已经收到占位 `role=tool`：

```text
assistant(tool_call_id=call_1)
tool(tool_call_id=call_1, "bg_0001 started")
```

完成结果不能再次伪装成 `tool_call_id=call_1`，否则同一个调用会对应两个结果，破坏消息
边界。因此完成结果是新的 `role=user` 通知，不包含 `tool_call_id`。通知中的 command 和
summary 会先经过 XML escaping，防止命令输出伪造闭合标签。

后台通知不会写进 Memory 的 extraction snapshot，避免测试输出或构建日志被误提取成长期
记忆；它仍进入主消息和 Context Compact 管线，因此当前会话可以使用并在必要时压缩。

## ShellExecutor 与进程清理

同步和后台 Bash 现在共享 `ShellExecutor`：

- `subprocess.Popen(..., start_new_session=True)` 创建独立进程组；
- 统一收集 stdout/stderr，限制为 50,000 字符；
- 默认 120 秒超时；
- 非零退出状态格式化成显式错误；
- 完成、超时或关闭时依次向进程组发送 `SIGTERM`、`SIGKILL`；
- `AgentHarness.close()` 和 `atexit` 负责进程退出清理。

这解决的是 Harness 生命周期清理，不是安全沙箱。后台 Bash 仍拥有当前进程的文件和网络
权限；脱离原进程组的子进程也可能逃过清理。生产实现仍需容器、用户隔离、cgroup 和更
严格的命令策略。

## 不会主动唤醒 Agent

worker 完成时只写 ready 队列，不会从线程里调用模型。通知只在以下时机被收集：

1. 当前 Agent Loop 的下一轮模型请求前；
2. Agent 已返回后，用户发起下一轮会话时。

因此它是“非阻塞执行 + 轮询收集”，不是事件驱动唤醒。这一边界让线程安全和消息顺序更
容易理解，后续可以再演进成 background event loop。

## 与官方 s13 的区别

| 维度 | 官方课程 | 本仓库 |
| --- | --- | --- |
| 基础代码 | s04 小型 kernel | 累计保留 s01-s12 全部能力 |
| 模块 | 单文件 `BackgroundManager` | 独立 `harness/background.py` |
| 模型协议 | Anthropic content blocks | OpenAI `tool_calls` + 独立 `role=tool` |
| 进程状态 | 模块级全局集合 | `ShellExecutor` 实例状态，显式注入 |
| 通知 | user text block | 独立 user 字符串，不复用 tool_call_id |
| 转义 | 直接插入 XML | command/summary 执行 XML escaping |
| SubAgent | 本章未组合 | 保留同步 SubAgent，但不开放后台参数 |
| Memory | 本章未组合 | 后台通知不进入长期记忆提取快照 |
| 验证 | 官方示例 | s13 12 项，并纳入项目全量回归 |

核心语义保持一致：显式后台 Bash、立即返回 `bg_id`、daemon thread、后续轮次收集
`task_notification`，以及通知不复用原始工具调用 ID。

## 运行

```bash
cd /data/projects/cc-harness-lab
python3 s13_background_tasks/code.py
```

可以输入：

```text
Run `sleep 1; printf background-ok` in the background. While it runs,
list the Markdown files. Do not finish until you see the task notification.
```

运行测试：

```bash
python3 -m pytest -q tests/test_s13.py
python3 -m pytest -q
```

## 下一章

s13 需要模型或用户触发下一轮才能收集结果，也不能按固定时间自动启动任务。s14 将加入
Cron Scheduler，让 Harness 可以保存和触发定时工作。
