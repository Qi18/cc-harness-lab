# s11 源码剖析：错误分类与恢复状态机

```text
LLM request
  -> with_retry
       -> success
       -> 429: Retry-After 或 exponential backoff
       -> 529: backoff；连续三次切 fallback
       -> other: re-raise
  -> choice.finish_reason
       -> length #1: 8K -> 64K，messages 不变
       -> length #2+: 保存 partial + continuation
  -> prompt-too-long exception
       -> reactive_compact once
       -> still too long: bounded failure
  -> tools / final answer
```

`RecoveryState` 对一次父 Agent turn 保存：

- 当前模型；
- 是否已经升级过输出预算；
- 已执行的续写次数；
- 连续 529 次数；
- 是否执行过 reactive compact。

每条恢复路径都有独立预算，避免一种故障消耗另一种故障的次数。所有等待函数和随机
函数都可注入，因此测试不需要真实 sleep。
