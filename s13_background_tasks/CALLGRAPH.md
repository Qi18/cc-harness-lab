# s13 AgentHarness 调用图

> s13 在 s12 累计链路上增加后台 Bash；worker 永不直接修改消息历史。

```mermaid
flowchart TD
    U["用户消息"] --> I["主线程：注入已完成后台通知"]
    I --> P["s10 System Prompt"]
    P --> C["Context Compact"]
    C --> R["s11 Error Recovery"]
    R --> A{"模型响应"}
    A -->|最终文本| E["Stop Hook → Memory"]
    A -->|tool_calls| H["PreToolUse 权限检查"]
    H -->|同步工具 / s12 Task| X["handler → PostToolUse"]
    H -->|"bash + run_in_background=true"| B["BackgroundManager.start<br/>立即返回唯一 tool result"]
    B --> W["worker：ShellExecutor.run"]
    W --> Q["完成队列"]
    Q --> I
    X --> C
    B --> C
```

关键边界：

- 权限检查发生在线程启动之前；
- 子 Agent 的 Bash schema 没有 `run_in_background`；
- 原 `tool_call_id` 只对应启动占位结果；
- 完成结果以转义后的 `role=user` 运行时通知注入；
- 通知进入当前上下文和 Compact，但不进入 Memory 提取快照；
- 后台完成采用轮询收集，不会主动唤醒已结束的 Agent Loop。
