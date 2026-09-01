# s11 源码分析：有界 Error Recovery

> 配套 [README.md](./README.md) 与 [CALLGRAPH.md](./CALLGRAPH.md) 阅读。
> 对照 [s11 官方教程](https://learn.shareai.run/zh/s11/)和
> [官方源码](https://github.com/shareAI-lab/learn-claude-code/blob/main/s11_error_recovery/code.py)。

## 一、错误恢复为什么属于 Agent Loop

SDK 只能告诉调用方“请求失败”或“输出停止”，但不知道这段响应能不能写进
`messages`。只有 Agent Loop 同时掌握三件事：

1. 当前响应是否完整；
2. 消息历史是否已经做过 reactive compact；
3. 这次恢复是否会破坏 assistant/tool 协议组。

因此 s11 没有写一个“重试所有 API 调用”的全局装饰器，而是拆成两层：

```text
error_recovery.py                 agent_loop.py
分类、计数、退避、fallback   →   决定重试、压缩、追加消息或停止
```

纯策略层不接触消息；状态机层不重复实现错误识别和时间计算。这也让单元测试可以把
sleep 和随机数注入为确定函数，不需要真的等待。

## 二、RecoveryState 的 turn 级语义

`RecoveryState` 保存六个字段：

| 字段 | 含义 |
| --- | --- |
| `current_model` | 当前请求使用主模型还是 fallback |
| `max_tokens` | 当前输出预算，初始 8000 |
| `has_escalated` | 是否已经执行过 8K→64K |
| `continuation_count` | 64K 后已经追加的续写次数 |
| `consecutive_529` | 连续过载次数，429/成功会清零 |
| `has_attempted_reactive_compact` | 是否已经做过一次应急压缩 |

状态在 `agent_loop()` 进入时创建，turn 返回时销毁。这个边界很重要：fallback 是
一次请求链的降级，不是全局模型配置变更；64K 也是一次长回答的预算，不应该让后续
简单问题永久使用更大上限。

## 三、输出截断状态机

```text
8K 请求
  ├─ 完整 → 正常处理 assistant/tool_calls
  └─ 截断 → 丢弃片段，提升 64K，原历史重试
                ├─ 完整 → 正常处理
                └─ 截断 → 保存文本片段 + continuation
                              ├─ 完整 → 正常结束
                              └─ 截断 → 最多重复 3 次，之后返回最后片段
```

第一次为什么必须丢弃片段？如果先写进历史再重放原请求，模型会同时看到半截回答和
原用户任务，容易重复内容。更严重的是，截断点可能位于 JSON 工具参数中：把不完整
`tool_calls` 写回历史会制造一个没有合法 `role=tool` 结果的协议组。

64K 后不再简单重放，因为同一输出预算已经证明不足。此时保存纯文本片段并明确要求从
断点续写，才能把一份超长结果拆成多段。实现有意不执行截断响应中的工具调用；安全性
优先于猜测未完成参数。

计数语义是“三条 continuation user 消息”。首次 64K 截断追加第 1 条，第三条之后
如果下一次仍截断，返回那次的最后片段，不再追加第 4 条。

## 四、瞬态错误重试

`with_retry()` 只接受一个 `fn(model)`。每次调用都把 `state.current_model`
传给 Provider，因此 fallback 不需要修改全局 `Settings`。

默认退避：

```python
base = min(0.5 * (2 ** attempt), 32.0)
delay = base + random.uniform(0.0, base * 0.25)
```

attempt 从 0 开始，所以无服务端提示时依次约为 0.5、1、2、4、8、16、32 秒，之后
保持 32 秒基线。jitter 减少多个客户端同时重试造成的同步冲击。`Retry-After`
存在且能解析为秒数时优先，不再叠加本地 jitter。

连续三次 529 在第三次异常处理阶段切换 `current_model`，所以下一次请求才使用
fallback。429 会把 `consecutive_529` 清零，避免把不连续的过载拼成错误降级。
成功同样清零。总尝试数默认 10，最后一次失败后不 sleep，直接抛
`MaxRetriesExceeded`。

非 429/529 异常立即向上抛出。把鉴权失败、参数错误或安全拒绝当瞬态错误重试，既浪费
配额，也可能掩盖真实配置问题。

## 五、Prompt 过长与 s08 Compact 的关系

s08 已有两种入口：

- `prepare()`：每次请求前按预算执行 L3/L1/L2，必要时 L4 摘要；
- `reactive_compact()`：供应商仍拒绝时，用更激进方式保留 system、摘要和当前请求。

s11 不复制压缩算法，只决定 reactive 入口可以使用一次。第一次 prompt-too-long：
压缩当前 `messages` 并 `continue`；第二次：调用
`_append_failure_result()` 收口。

这里不能复用手动 `compact` 的 `manual_compactions` 计数。手动 compact 是模型
主动调用的控制工具，决定下一轮是否继续暴露 schema；reactive compact 是 API
失败恢复，两者触发源和状态语义不同。

## 六、不可恢复错误为什么写回 assistant

交互式 CLI 如果让异常穿透，用户会得到 traceback，当前进程和会话历史一起中断。
`_append_failure_result()` 把异常类型和最多 1000 个字符的详情写成：

```text
Error: RuntimeError: invalid request
```

这样本 turn 有明确终点，调用方仍能保存或展示结果。错误文本有界，避免服务端把巨大
请求或响应原样塞回上下文。这个出口不会触发 Memory 提取，因为失败信息通常不是值得
跨会话保存的用户事实。

## 七、Provider 适配

`completion_request()` 新增两个可选覆盖参数：

- `model`：由恢复状态选择主模型或 fallback；
- `max_tokens`：由 8K/64K 状态机选择输出预算。

模型专属参数仍集中在 Provider；例如切换到 `kimi/kimi-k3` 时会根据实际
`selected_model` 添加 `reasoning_effort=max`。Agent Loop 不需要理解供应商
extra body。

Anthropic 教程中的输出信号是 `stop_reason=max_tokens`；百炼的 OpenAI-compatible
响应通常是 `finish_reason=length`，部分兼容服务返回 `max_tokens`，因此分类函数
同时接受两者。

## 八、当前实现边界

本章只包裹父 Agent 的主循环请求，未全局包裹以下 side calls：

- Memory selection / extraction / consolidation；
- Compact 的摘要模型调用；
- SubAgent 自己的内部循环。

这些模块已有各自的降级或错误返回边界，而统一重试它们会引入不同的幂等性和消息写回
问题。后续若要扩展，应该复用 `error_recovery.py` 的纯策略，但为每类调用单独定义
恢复语义。

还存在三个供应商相关限制：

1. 某个模型若不接受 64000 的 `max_tokens`，第二次请求会作为不可恢复参数错误返回；
2. Prompt-too-long 目前兼容 SDK 状态和常见错误文本，新的供应商文案需要补分类测试；
3. `Retry-After` 当前只解析秒数，不解析 HTTP-date 形式。

这些是显式边界，不影响恢复次数的有界性。

## 九、测试策略

恢复测试不等待真实 429/529，而是用脚本化 completion 对象注入故障，并把
`sleep_fn` 和 `random_fn` 替换为确定函数。这样可以精确断言：

- 请求次数、退避序列和模型切换时机；
- 每轮实际发送的 `max_tokens`；
- 首次截断片段没有进入第二次请求；
- continuation 恰好最多三条；
- reactive compact 恰好最多一次；
- 非瞬态和恢复耗尽都返回可观察错误。

测试仍保留 s10 的全部结构断言，确保新增恢复状态机没有破坏工具隔离、Prompt 动态
组装、Compact 控制信号和工作区安全边界。
