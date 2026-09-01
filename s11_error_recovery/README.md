# s11：Error Recovery

这一章在 s01–s10 的累计能力之上，把"请求失败"从异常穿透改成有界恢复状态机。它继续
沿用 s10 的局部 `harness/` package 与课程能力命名，新增第十一个能力模块
`error_recovery.py`：纯策略与状态放在模块里，何时改写消息历史仍由 Agent Loop 决定。

- 官方教程：[s11 Error Recovery](https://learn.shareai.run/zh/s11/)
- 官方源码：[s11_error_recovery/code.py](https://github.com/shareAI-lab/learn-claude-code/blob/main/s11_error_recovery/code.py)
- 上一章：[s10 System Prompt](../s10_system_prompt/)
- 源码分析：[ANALYSIS.md](./ANALYSIS.md)
- 调用图：[CALLGRAPH.md](./CALLGRAPH.md)

## 这一章解决什么

前十章对失败只有两种处理：s08 在 prompt 溢出时做一次 reactive 压缩，其余异常直接穿
透到 CLI。真实运行中还有三类常见失败——429 限流、529 过载、输出被 `max_tokens` 截断
——它们既不该崩掉进程，也不该无限重试。s11 的做法是：先分类失败，再选择一个**有上限
且不破坏消息协议**的恢复动作。

## 恢复矩阵

| 失败类型 | 识别信号 | 恢复动作 | 上限 |
| --- | --- | --- | --- |
| 速率限制 | HTTP 429 / 文本含 `rate limit` | 指数退避 + 0–25% jitter，优先服从 `Retry-After` | 10 次请求 |
| 服务过载 | HTTP 529 / 文本含 `overloaded` | 同样退避；连续 3 次切换 `FALLBACK_MODEL_ID` | 10 次请求 |
| 输出截断 | `finish_reason` ∈ {`length`, `max_tokens`} | 首次 8K→64K 重放原请求；仍截断则保存片段并追加续写提示 | 3 条 continuation |
| Prompt 过长 | 错误文本含 context / prompt-too-long 标记 | 强制 `reactive_compact()` 后原地重试 | 每 turn 1 次 |
| 其他错误 | 不属于以上任何一类 | 写成最终 assistant 错误消息并返回 | 不重试 |

上限之外没有兜底重试：`with_retry()` 抛 `MaxRetriesExceeded`，第二次溢出与非瞬态异常
都走同一个收口出口 `_append_failure_result()`。

## RecoveryState 的 turn 级语义

| 字段 | 含义 | 初值 |
| --- | --- | --- |
| `current_model` | 本次请求链使用主模型还是 fallback | `settings.model` |
| `max_tokens` | 当前输出预算 | 8000 |
| `has_escalated` | 是否已执行过 8K→64K | False |
| `continuation_count` | 64K 后已追加的续写次数 | 0 |
| `consecutive_529` | 连续过载次数，429 与成功都清零 | 0 |
| `has_attempted_reactive_compact` | 是否已用掉那唯一一次应急压缩 | False |

状态在 `agent_loop()` 进入时创建、返回时销毁。fallback 是一次请求链的降级，不是全局
模型配置变更；64K 是一次长回答的预算，不该让后续简单问题永久占用更大上限。

## 三条恢复路径

### 1. 输出截断

第一次截断的响应可能停在半句话、甚至半个 JSON 工具参数中间，所以整段丢弃，只把
`max_tokens` 从 8000 提升到 64000，然后用**原消息历史**重放。写回不完整的 `tool_calls`
会制造一个永远配不上 `role=tool` 的协议组，这是必须避免的。

64K 下仍截断才改策略：保存当前纯文本片段为 assistant 消息，再追加一条 continuation
user 提示，让模型从断点续写。最多三条 continuation；第四次仍截断就返回那次的最后片段。
截断响应里的工具调用一律不执行——参数可能残缺，安全优先于猜测。

### 2. Prompt 过长

`ContextCompactor.prepare()` 是请求前的主动压缩，`reactive_compact()` 是供应商仍然拒
绝时的应急压缩。s11 不复制任何压缩算法，只决定 reactive 入口在一个 turn 内可以用一次。
第二次溢出说明当前策略已无法满足窗口，返回有界错误，而不是反复摘要。

### 3. 429 / 529

```text
base  = min(0.5 * 2^attempt, 32.0)
delay = base + random(0, base * 25%)      # 有 Retry-After 时直接用服务端秒数
```

attempt 从 0 开始，因此无服务端提示时依次约 0.5、1、2、4、8、16、32 秒后保持 32 秒基线。
连续三次 529 且配置了 fallback 时切换模型，下一次请求生效；429 会打断 529 计数，避免把
不连续的过载拼成一次错误降级。最后一次尝试失败后不再 sleep，直接报 exhausted。

## 恢复动作与消息历史

| 分支 | assistant 写入 | user 写入 | 是否重试原请求 |
| --- | --- | --- | --- |
| 429 / 529 | 否 | 否 | 是，同一份历史 |
| 首次输出截断 | 否 | 否 | 是，预算提到 64K |
| 64K 后截断 | 是，仅纯文本片段 | 是，continuation | 否，改为续写 |
| 首次 prompt too long | 否 | 否（历史被压缩替换） | 是 |
| 第二次 prompt too long | 是，错误文本 | 否 | 否 |
| 非瞬态错误 | 是，错误文本 | 否 | 否 |
| 完整响应 | 是，完整 payload | 仅 Stop Hook 可能追加 | 走正常循环 |

最关键的边界只有一条：**未完成的响应永远不进入工具分发**。只有 `finish_reason` 表示完
整后，`assistant.tool_calls` 才会交给 `_execute_tool_batch()` 并逐个补齐 `role=tool`。

## 模块结构

```text
s11_error_recovery/
├── code.py                    59   CLI 入口
└── harness/
    ├── config.py              60   环境变量、模型、fallback 与工作区路径
    ├── models.py              24   跨模块数据契约
    ├── provider.py            52   百炼客户端、请求与响应适配
    ├── agent_loop.py         402   01 Agent Loop：装配能力并驱动可恢复父循环
    ├── tool_use.py           303   02 Tool Use：Schema、handler、执行管线
    ├── permission.py          85   03 Permission：deny / ask 决策
    ├── hooks.py               74   04 Hooks：生命周期事件注册与触发
    ├── todo_write.py          46   05 TodoWrite：Todo 状态与校验
    ├── subagent.py            72   06 SubAgent：隔离历史的同步子循环
    ├── skill_loading.py       99   07 Skill Loading：扫描、目录、按需加载
    ├── context_compact.py    389   08 Context Compact：L1–L4 与手动 compact
    ├── memory.py             540   09 Memory：召回、提取、整理与持久化
    ├── system_prompt.py      101   10 System Prompt：section 组装与缓存
    ├── error_recovery.py     193   11 Error Recovery：分类、退避、有界状态
    └── __init__.py            22   包门面
```

合计 2521 行，其中 `harness/` 2462 行。章节编号只写在文档里，不写进文件名。

## 课程与代码的对应关系

| 章节 | 能力 | 模块 | 主要对象 |
| --- | --- | --- | --- |
| 01 | Agent Loop | `agent_loop.py` | `AgentHarness` |
| 02 | Tool Use | `tool_use.py` | `PARENT_TOOLS`、`ToolExecutor`、`BuiltinTools` |
| 03 | Permission | `permission.py` | `PermissionPolicy` |
| 04 | Hooks | `hooks.py` | `HookManager`、`install_default_hooks` |
| 05 | TodoWrite | `todo_write.py` | `TodoManager` |
| 06 | SubAgent | `subagent.py` | `SubagentRunner` |
| 07 | Skill Loading | `skill_loading.py` | `SkillLoader` |
| 08 | Context Compact | `context_compact.py` | `ContextCompactor`、`CompactToolController` |
| 09 | Memory | `memory.py` | 召回、注入、提取、整理函数 |
| 10 | System Prompt | `system_prompt.py` | `SystemPromptAssembler` |
| 11 | Error Recovery | `error_recovery.py` | `RecoveryState`、`with_retry`、分类函数 |

`config.py`、`models.py`、`provider.py` 是跨章节基础设施，不强行归入某一课。

## 为什么这样切分

**策略与状态机分成两层。** `error_recovery.py` 只提供纯分类函数、退避计算和一个
`RecoveryState` 数据类，完全不接触 `messages`；`agent_loop.py` 决定何时重试、何时压缩、
何时追加消息、何时收口。只有循环同时知道当前响应是否完整、reactive 压缩是否用过、以及
这次改写会不会拆断 assistant/tool 协议组。

**不做全局重试装饰器。** 本章只包裹父 Agent 的主模型请求。Memory 召回与提取、Compact
摘要调用、SubAgent 内部循环都保留各自原有的失败语义——统一重试它们会引入不同的幂等性
和消息写回问题。这是显式的实现边界，不是遗漏。

**`is_prompt_too_long_error` 从 s08 移到 s11。** 它在 s10 属于 `context_compact.py`，
但本质是错误分类而非压缩算法。移动后所有分类函数集中在一处，`context_compact.py` 只保
留"怎么压缩"，`error_recovery.py` 负责"这是什么错"。分类标记同时扩充了
`prompt_is_too_long` 与 `max_context_window`。

**sleep 与随机数可注入。** `AgentHarness.__init__` 接受 `sleep_fn` / `random_fn`，默认
是 `time.sleep` 与 `random.uniform`。测试因此可以断言精确的退避序列，且完全不等待。

## 依赖方向

课程编号代表学习顺序，不代表 Python 依赖层级。`error_recovery.py` 是第十一课，却因为不
导入任何内部模块而位于最底层。

```text
config · models · system_prompt · error_recovery      无内部依赖
          ↓
provider · permission · skill_loading · todo_write
          ↓
hooks · memory
          ↓
tool_use · context_compact
          ↓
subagent
          ↓
agent_loop → code.py
```

箭头向下表示"依赖上一层"。下层模块都不导入 `AgentHarness`，因此依赖图无环。

## 行为保持不变

新增恢复状态机没有削减前章能力：

- 父 Agent 仍有 9 个工具，SubAgent 仍只有 6 个基础工具且不能再次 `task`；
- 普通工具仍经过 Permission 与 PreToolUse / PostToolUse；
- Todo 仍限制最多一个 `in_progress`，连续三轮未更新会补提醒；
- System Prompt 仍每次模型调用前按运行态重组，且只改写首条 system 消息；
- Context Compact 仍保留 L1–L4、手动 compact 与每 turn 一次的 compact 工具；
- Memory 仍使用独立提取快照，不受本 turn 压缩影响；
- 模型为 `kimi/kimi-k3` 时仍自动补 `reasoning_effort=max`；
- 工作区路径边界与危险命令检查仍生效。

## 运行与测试

```bash
export DASHSCOPE_API_KEY=...
export MODEL_ID=qwen-plus
export FALLBACK_MODEL_ID=qwen-turbo   # 可选，连续三次 529 后启用

cd /data/projects/cc-harness-lab
python3 s11_error_recovery/code.py
python3 -m pytest -q tests/test_s11.py
```

CLI 启动时打印主模型、fallback 模型、工作目录和当前生效的 Prompt sections。

26 个 s11 测试分两组：14 项沿用 s10 的结构回归（section 组装、工具注册表、缓存、工具
隔离、Todo 与工作区边界，并断言十一个能力模块存在、旧碎片文件名消失），12 项覆盖恢复
路径（退避序列、`Retry-After` 优先、三次 529 切 fallback、重试上限、非瞬态不重试、首次
截断不污染历史、continuation 上限、空片段返回显式错误、reactive compact 恰好一次、恢复
耗尽返回 assistant 文本）。故障用脚本化 completion 注入，不依赖真实服务恰好出错。

## 已知遗留

- 恢复只覆盖父循环主请求，side call（Memory、Compact 摘要、SubAgent）仍是各自语义；
- 若某模型拒绝 `max_tokens=64000`，第二次请求会被当作不可恢复的参数错误返回；
- prompt-too-long 依赖错误文本匹配，新供应商文案需要补分类测试；
- `Retry-After` 只解析秒数，不解析 HTTP-date；
- `memory.py` 仍是模块级配置，`skill_loading.py` 仍每轮扫盘（继承自前章）。

## 与官方代码的关系

机制与教程一致：8K→64K、三次 continuation、一次 reactive compact、429/529 指数退避、三
次 529 fallback、十次总尝试。差异来自本仓库的累计架构与协议：

- 官方是聚焦单章机制的 Anthropic 教学切片，本目录保留 s01–s10 全部能力；
- 官方读 `stop_reason=max_tokens`，本仓库读 OpenAI-compatible 的
  `finish_reason=length/max_tokens`，并同时兼容 `max_tokens`；
- 官方工具结果用 content blocks，本仓库必须维护 `tool_calls` 与独立 `role=tool` 的配对；
- 本仓库把纯恢复策略拆进 `error_recovery.py`，Agent Loop 只编排状态迁移；
- 不可恢复异常被转成可见 assistant 消息，交互式 CLI 不会整进程退出。
