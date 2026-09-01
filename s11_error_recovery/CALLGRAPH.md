# s11 Agent Loop 调用图

> 配套 [README.md](./README.md) 与 [ANALYSIS.md](./ANALYSIS.md) 阅读。
> 图中只展开 s11 新增恢复分支；工具批次、Memory 和 System Prompt 的正常链路继续
> 沿用 s10。

![s11 Error Recovery 调用图](./callgraph.svg)

## 主循环

```mermaid
flowchart TD
    A[进入 user turn] --> B[创建 RecoveryState<br/>model=primary, max_tokens=8K]
    B --> C[刷新 System Prompt]
    C --> D[ContextCompactor.prepare]
    D --> E[with_retry 调用模型]

    E -->|429 / 529| F[指数退避 + jitter]
    F -->|连续 3 次 529| G{配置 fallback?}
    G -->|是| H[切换 current_model]
    G -->|否| E
    H --> E
    F -->|未达到 fallback 阈值| E
    F -->|总尝试达到 10| Z[写 assistant Error 并返回]

    E -->|prompt too long| I{已经 reactive compact?}
    I -->|否| J[reactive_compact]
    J --> E
    I -->|是| Z

    E -->|普通不可恢复错误| Z
    E -->|响应成功| K{finish_reason 截断?}

    K -->|否| L[写入完整 assistant]
    L --> M{有 tool_calls?}
    M -->|是| N[执行完整工具批次<br/>写 role=tool 结果]
    N --> C
    M -->|否| O[Stop Hook]
    O -->|要求继续| C
    O -->|停止| P[Memory 提取与整理]
    P --> Q[返回最终文本]

    K -->|是，首次| R[max_tokens 8K → 64K<br/>不保存片段]
    R --> E
    K -->|是，已升 64K| S[保存纯文本片段]
    S --> T{已追加 3 次 continuation?}
    T -->|否| U[追加 continuation user 消息]
    U --> E
    T -->|是| V[返回最后片段]
```

## 恢复模块调用关系

```text
agent_loop.py
├─ RecoveryState(settings.model)
├─ with_retry(fn, state, fallback_model)
│  ├─ is_rate_limit_error
│  ├─ is_overloaded_error
│  ├─ retry_after_seconds
│  └─ retry_delay → sleep_fn
├─ is_prompt_too_long_error
│  └─ ContextCompactor.reactive_compact
├─ is_output_truncated
│  ├─ 8K → 64K 原请求重放
│  └─ partial assistant → continuation user
└─ format_unrecoverable_error
   └─ 最终 assistant Error
```

## 消息写入规则

| 分支 | assistant 写入 `messages` | user 写入 `messages` | 是否重试原请求 |
| --- | --- | --- | --- |
| 429 / 529 | 否 | 否 | 是 |
| 首次输出截断 | 否 | 否 | 是，提升到 64K |
| 64K 后输出截断 | 是，只保存文本片段 | 是，continuation | 否，续写 |
| 首次 prompt too long | 否 | 否，历史被压缩替换 | 是 |
| 第二次 prompt too long | 是，错误文本 | 否 | 否 |
| 普通不可恢复错误 | 是，错误文本 | 否 | 否 |
| 完整响应 | 是，完整 payload | 仅 Stop Hook 可能追加 | 按正常循环 |

最关键的边界是：未完成响应永远不会进入工具分发。只有
`finish_reason` 表示完整后，`assistant.tool_calls` 才可能由
`_execute_tool_batch()` 执行并逐个补齐 `role=tool` 消息。
