# s13 源码剖析：Background Tasks

> s13 建立在 s12 的完整 Harness 上，只增加后台 Bash 生命周期与结果回注。

## 一、累计关系

```text
System Prompt（s10）
  → Error Recovery（s11）
  → Task System（s12）
  → Background Tasks（s13）
```

`prompt.py`、`recovery.py` 和 `tasks.py` 都继续参与真实调用链。本章新增
`background.py`，并在 `AgentHarness` 中接入启动、轮询通知和关闭清理。

## 二、显式后台执行

只有父 Agent 的 Bash 参数明确为 `run_in_background: true` 时才进入后台：

- 不根据命令文本猜测是否耗时；
- 非布尔值直接返回参数错误；
- 子 Agent 的 Bash schema 不暴露该字段；
- 执行层也禁止子 Agent 启动后台任务。

Schema 与执行层双重限制，避免模型通过手写参数绕过能力边界。

## 三、权限与消息协议

执行顺序是：

```text
解析参数 → PreToolUse/权限检查 → 后台分发 → PostToolUse
```

权限拒绝的命令不会启动线程。启动成功后，原 `tool_call_id` 已经与“后台任务已启动”的
占位结果配对，完成时不能再发送第二个同 ID 的 tool 消息。因此真实结果包装成新的
`role=user` 通知：

```xml
<task_notification>
  <task_id>bg_0001</task_id>
  <status>completed</status>
  <summary>...</summary>
</task_notification>
```

命令与输出会 XML 转义并截断。System Prompt 明确把通知定义为运行时数据，而不是新的用户
指令。

## 四、线程所有权模型

```text
worker 线程                  Agent Loop 主线程
──────────                  ───────────────
执行命令
写完成队列          ─────▶  在模型轮次边界 drain
不修改 messages             由主线程注入通知
```

worker 永不直接修改消息历史，因此模型请求序列化期间不会被并发插入消息。这是轮询模型：
后台完成不会主动唤醒已经结束的 Agent Loop，结果会在当前循环下一轮或用户下一次 turn 注入。

## 五、与 Memory 和 Compact 的边界

后台通知只写主消息历史，不写 Memory 提取快照，避免构建日志、测试输出被误提取成长期事实。
它仍可被 Compact 归档，但 `latest_user_request` 会跳过 `<task_notification>`，
不会把运行时通知误当成当前用户请求。

## 六、进程清理与权衡

同步 Bash 和后台 Bash 共用 `ShellExecutor`。子进程通过独立进程组启动，超时或关闭时
先终止、再强制清理。清理路径包括单次命令 finally、`AgentHarness.close()`、CLI 的
try/finally 与 atexit 兜底。

这提供的是生命周期清理，不是安全沙箱。其他已知边界：

- `PostToolUse` 看到启动占位文本，不是稍后的真实输出；
- 后台 ID 进程内递增，重启归零且不持久化；
- daemon worker 关闭时只有限等待；
- 后台任务与 s12 持久任务图是两套生命周期。

`tests/test_s13.py` 覆盖显式开关、父子隔离、权限顺序、占位结果、通知注入与转义、
Memory 快照隔离、进程清理，以及 s12 Task System 的累计可用性。

## 一句话总结

s13 用“worker 只执行并写队列、主线程只在轮次边界注入通知”的单向数据流实现非阻塞 Bash，
同时保住权限顺序、tool_call_id 协议、Memory 质量和消息列表的单线程所有权。
