# s12 AgentHarness 调用图

> 本图只表达稳定的调用关系，不绑定易漂移的源码行号。

```mermaid
flowchart TD
    U["用户消息"] --> M["Memory 召回并注入"]
    M --> P["s10 SystemPromptAssembler<br/>工具 / workspace / Skills / Memory metadata"]
    P --> C["s08 ContextCompactor.prepare"]
    C --> R["s11 Error Recovery<br/>截断 / continuation / compact / 429 / 529 / fallback"]
    R --> A{"模型响应"}
    A -->|最终文本| E["Stop Hook → Memory 提取与整理"]
    A -->|tool_calls| H["PreToolUse → handler → PostToolUse"]
    H --> T["s12 TaskManager"]
    T --> S["TaskStore: .tasks/*.json"]
    H --> O["其他父 Agent 工具"]
    S --> C
    O --> C
```

关键边界：

- s10 从真实注册表组装提示词，不声明不存在的工具；
- s11 只包围模型 API 调用，所有恢复分支都有次数上限；
- s12 六个任务工具只注册给父 Agent；
- TaskStore 负责持久图和状态机，异常转换成模型可读观察；
- Task、Memory、Compact transcript 是互相独立的持久层。
