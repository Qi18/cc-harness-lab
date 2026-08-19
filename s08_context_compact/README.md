# s08：Context Compact

这一章在 s07 的完整 Harness 上增加上下文压缩。Agent 的工具能力没有变化，但长期
运行产生的旧消息、文件内容和命令输出会逐渐填满模型上下文，因此每次模型调用前都
按“便宜操作优先”的顺序整理消息。

## 执行管线

```text
messages
  → L3 tool_result_budget（大结果落盘）
  → L1 snip_compact（裁剪旧中间消息）
  → L2 micro_compact（旧工具结果占位）
  → L4 compact_history（超阈值时调用 LLM 摘要）
  → model call
```

实际执行顺序是 `budget → snip → micro → auto compact`。必须先保存大结果，再把旧结果
替换为占位符，否则原始内容会先丢失，无法落盘恢复。

## L1：中间消息裁剪

消息超过 50 条时保留开头、结尾和一条 `[snipped ...]` 标记。切口会识别 OpenAI
兼容格式中的 `assistant.tool_calls` 与后续连续 `role="tool"` 消息，避免留下没有
对应调用的孤立工具结果。被裁历史先保存到 `.transcripts/*.jsonl`。

## L2：旧工具结果占位

完整保留最近 3 条工具结果。更旧且超过 120 字符的结果替换为：

```text
[Earlier tool result compacted. Re-run if needed.]
```

如果结果已由 L3 保存，占位符会保留落盘路径。

## L3：工具结果预算

模型一轮可以并行调用多个工具，我们的 OpenAI-compatible 消息会形成一组连续的
`role="tool"` 消息。该批次总内容超过 200,000 字符时，从最大的结果开始写入：

```text
.task_outputs/tool-results/<tool-call-id>-<unique>.txt
```

上下文中只保留 `<persisted-output>`、完整路径和最多 2,000 字符预览。

## L4：LLM 摘要

前三层完成后，消息 JSON 估算仍超过 50,000 字符时：

1. 把完整历史保存为 JSONL transcript；
2. 额外调用一次模型生成事实摘要，不开放任何工具；
3. 保留 system prompt，用一条 `[Compacted]` 消息替换其余历史。

摘要明确区分 `Current user request` 与 `Conversation summary (reference only)`，避免把
旧对话里的文字重新解释成当前命令。连续三次自动摘要失败后熔断并抛出错误。

## 应急压缩

如果模型 API 仍返回 `prompt_too_long`、`context_length_exceeded` 或同类错误，Harness
执行一次 `reactive_compact`：保存 transcript、总结旧历史，并保留最近五条消息。
尾部边界同样不会拆开工具调用组。应急重试最多一次，失败后把异常交给调用方。

## 主动 compact 工具

父 Agent 新增第九个工具：

```json
{"name": "compact", "arguments": {}}
```

模型可以主动请求完整摘要。`compact` 是父上下文控制工具，不提供给子 Agent；调用
仍经过 `PreToolUse` 和 `PostToolUse` Hook。每个用户 turn 最多主动压缩一次；完成后
本 turn 的工具目录会移除 `compact`，避免压缩后的当前请求再次触发同一工具形成循环。

## 启动

```bash
cd /data/projects/cc-harness-lab
python3 s08_context_compact/code.py
```

建议尝试连续读取多个文件、要求读取较大输出，或直接让模型调用 `compact`，观察
`[auto compact]`、`[reactive compact]`、transcript 和 persisted-output 标记。
