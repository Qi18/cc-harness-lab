# s20 Comprehensive Agent：一个循环，多个协作机制

s20 不再复制一份 Harness。本章直接复用 [s19 的累计实现](../s19_mcp_plugin/)，用跨模块集成测试检查各机制能否共同工作，再整理整个系统的运行边界。

新增代码位于 [tests/test_s20.py](../tests/test_s20.py)。本目录没有新的 code.py 或 harness/；交互运行入口仍是 s19_mcp_plugin/code.py。

课程主题见 [s20 Comprehensive Agent Turn](https://learn.shareai.run/en/s20/)。这里的“综合”表示教学机制已集成，不表示已实现生产级可靠性或完整 Claude Code 能力。

## 1. 为什么不再复制一遍 s19？

本项目从前几章起就采用累计式实现：每章增加能力时，同时保留此前模块。到 s19，AgentHarness 已把工具、权限、Hooks、Memory、Compact、后台任务、Cron、Teams、Worktree 和 MCP mock 接到同一个父循环中。

所以 s20 更需要回答这些问题：

- 连接 MCP 后再压缩历史，新工具还在吗？
- 队友和后台任务在模型推理期间结束，Lead 会不会漏掉结果就返回？
- 工具执行被拒绝时，会不会仍启动后台线程？
- 模型请求失败后，定时任务是丢失、重复，还是可重投？
- Stop Hook 要求继续时，Memory 会不会把中间答案当最终结果？
- worktree 丢失后，会不会误在主目录执行或把任务标记完成？

这些属于机制之间的接口契约，不是再新增一组独立工具。

## 2. 一次完整 turn 的主调用链

```text
前台输入 ── run_turn ───────────┐
                              ├─ 同一把 _agent_lock ─→ agent_loop
Cron 队列 ─ Queue Processor ───┘

agent_loop
  1. 收取定时任务，确定 active_request
     保存 Memory 提取快照
     注入已完成后台通知和 Lead 邮件
  2. 刷新 System Prompt，召回并注入 Memory
  3. 每轮：
     收取新增通知/邮件 → 必要时追加 Todo 提醒
     刷新 Prompt → Compact.prepare → 组装动态工具 schema
     模型请求（Retry / fallback / 输出截断 / reactive compact）
       │
       ├─ 有工具调用
       │    写入 assistant
       │    完成整个工具批次，每个调用追加一个 role=tool
       │      普通工具 → ToolExecutor → 权限与 Hooks → handler
       │      Bash 后台执行 → 先返回占位结果
       │      MCP → 当前连接快照 → mock handler
       │      compact → 记录控制信号
       │    若请求 compact，整批结果齐全后归档并压缩
       │    回到下一轮
       │
       └─ 无工具调用
            再检查刚到达的后台/团队消息
            有消息 → 继续下一轮
            Stop Hook 要求继续 → 追加提醒并继续
            否则 Memory 提取、整理 → 返回最终答案
```

Cron 并不创建另一套 Agent Loop。Queue Processor 只有拿到与前台相同的锁后，才消费队列并调用 agent_loop。协议状态、工具池和共享消息仍属于同一份 Harness。

active_request 在本轮入口确定，用于压缩时保留当前目标。定时消息、团队邮件、后台通知和历史 Memory 不应取代显式传入的人类请求。

## 3. 循环外的并行执行分支

```text
Lead：编排 + 模型循环 + 汇总
├── SubagentRunner
│   └── 同步委派，独立消息；Lead 等待结果
├── BackgroundTaskManager
│   └── 已批准的工具在线程中执行 → 完成通知 → Lead 收信点
├── CronScheduler
│   └── 到期投递 Queue → Queue Processor 获取 Lead 锁
└── AgentTeamManager
    └── 每名队友有自己的模型循环和邮箱
        ├── 协议消息：计划、关闭请求与响应
        ├── idle：发现并认领 TaskManager 中的可执行任务
        └── WorktreeContext：绑定任务的 bash/read/write 使用局部目录
```

队友与 Lead 的模型请求可以并发；前台 turn 与 Cron turn 必须串行。后台线程和队友不会直接追加 Lead 的 messages，而是先写入自己的通知状态或邮箱，由 Lead 在确定的收信点注入。

## 4. 哪份状态由谁维护？

| 状态 | 维护者 | 重要边界 |
| --- | --- | --- |
| Lead messages | 父循环与持锁的定时 turn | 主模型可见历史；不是任何线程都能直接写 |
| extraction_messages | 父循环 | 独立快照，避免仅从压缩后的简短历史提取 Memory |
| transcript / 大输出文件 | ContextCompactor | 真正裁剪前归档、超大工具输出落盘；不等于持续写入的完整会话日志 |
| 工具 schema / handler | 静态内置表 + MCPManager 快照 | Prompt、模型请求和本地分发必须随连接更新 |
| Task 状态和 owner | TaskManager | 完成必须显式调用，线程退出不能代替验收 |
| 队友消息与协议 | MessageBus / TeamProtocolManager | 收到总结不等于任务完成，关闭请求不等于已经退出 |
| worktree 目录、索引、活跃引用 | WorktreeManager / WorktreeContext | completed 不意味着可以立刻删除，保留不等于合并 |
| 后台执行状态 | BackgroundTaskManager | 占位 tool result 与后续通知不是同一类消息 |
| 到期任务队列 | CronScheduler | 首次模型响应成功后确认投递；失败时恢复待投递任务 |

消息配对是本章重点：一条 assistant 中的每个 tool_call 都必须有一次对应的 tool result，不能把后台完成通知当成第二条同 ID 的 tool result，也不能在一个工具批次写到一半时插入摘要替换。

## 5. 本章九个集成测试验证了什么？

| 场景 | 验收点 |
| --- | --- |
| Worktree + Team + MCP + Background + Compact | 队友写入真实独立 worktree；Lead 同时连接、查询 MCP，启动真实 Bash 后台任务并执行 L4 摘要路径；压缩后新工具仍可见、compact 被隐藏、通知只投递一次 |
| MCP + 429 Retry | 重试请求保留相同 schema 和消息，不重复执行 connect_mcp |
| Cron + 请求失败 + MCP | 首次请求失败后任务回队列、移除当次 Scheduled 注入；第二次成功确认，MCP 连接仍在 |
| Permission + 后台分发 + 工具批次 | PreToolUse 拒绝 Bash 后不产生后台任务，但同批其他 MCP 调用可正常完成 |
| Stop Hook + Memory | 中间答案后继续检查，只有最终停止时提取一次 Memory |
| MCP 大输出 + L3 | 真实工具结果落盘，下一次模型请求只携带 persisted-output 标记和预览，保持调用配对 |
| Cron Runtime + MCP + 锁 | Lead 锁被占用时不请求模型；释放后看到已连接工具；空闲关停后处理线程实际退出 |
| Memory + Cron + Compact | 历史信息带背景标记，压缩消息仍明确保留人类 active_request |
| Task + 缺失 Worktree + Team 错误 | 队友失败、任务保持 in_progress、主文件不变；Lead 收到错误且 MCP 能力继续可用 |

组合场景不仅检查最终文本，还核验每次模型请求与压缩归档中的工具调用/结果是否配对，并回读实际文件、任务状态、通知、活跃引用和线程状态。

模型响应与 L4 摘要文本由可控桩生成，Memory 的模型选择、提取、整理也被替换；真实执行的是 Harness 路由、Hooks、Git、文件读写、压缩归档、线程、调度队列和邮箱逻辑。不能据此宣称真实模型已在综合任务上通过验收。

## 6. 如何复现？

在仓库根目录运行，无需模型 API Key：

```bash
python3 -m unittest tests.test_s20
python3 -m unittest discover -s tests
```

测试用 tempfile 创建独立工作区，真实 Git 操作只发生在临时仓库中，不修改本项目的分支和源码。并发测试用 Event 协调执行顺序，结束时等待其启动的队友和后台任务，并关闭 Cron Runtime。

交互体验仍使用累计实现：

```bash
python3 s19_mcp_plugin/code.py
```

交互运行需要项目已有的模型凭据。MCP 仍为 docs/deploy 进程内 mock，deploy 不执行真实部署。

## 7. 当前验收结果

2026-09-05 在 L20-Server 完成：

- s20 独立集成测试：9 项通过。
- 同一组集成测试重复运行 10 轮：90 次用例执行全部通过。
- 全仓自动化测试：799 项通过。

90 次是重复执行次数，不是额外增加 90 项独立测试；799 已包含 s20 的 9 项。重复运行用于发现易出现的时序问题，不是并发压力或长时间稳定性评测。

当次日志在服务器 /tmp/s20-unit.log、/tmp/s20-repeat.log、/tmp/s20-full-suite.log；这些是临时验证日志，不随 Git 分发，也不保证长期保留。长期可复现依据是仓库中的测试代码。

本轮没有发现需要修改 s19 主体实现的集成问题，因此没有为 s20 重写循环或新增平行运行时。上一章真实模型验证记录不能替代本章的综合真实模型验收。

## 8. 仍需明确的限制

- MCP 未实现 stdio/HTTP、JSON-RPC 握手、认证、取消与完整内容块转换。
- 默认权限规则、任务 owner 检查与 worktree cwd 不构成操作系统安全沙箱。
- 任务没有租约、心跳续租与自动重分配；队友失败可能留下 in_progress。
- 不自动合并 worktree；依赖完成不意味着分支产物自动集成。
- 队友和后台任务使用 daemon 线程。CLI 当前只调用 stop_cron_runtime，不是统一的全系统优雅关停；进程退出可能丢失未完成工作。
- stop_cron_runtime 对队列线程使用有界 join，测试通过的是空闲收尾场景，不能解释为可强制中断正在阻塞的模型调用。
- 持久任务和部分日志不等于整个进程的崩溃恢复。Memory 当前还有模块级配置，不宜把多个工作区的 Harness 当作完全隔离的同进程租户。
- 集成用例不覆盖所有模块组合，没有证明真实模型成功率、成本预算、长任务稳定性或生产部署安全性。

后续改进应由需求或评测驱动，例如真正 MCP、统一关停/取消、状态恢复和系统化评测，而不是继续复制更多章节目录。

## 9. 源码阅读路线

先看 [agent_loop.py](../s19_mcp_plugin/harness/agent_loop.py) 的 run_turn、agent_loop 和 _execute_tool_batch，再追踪以下接入点：

1. _visible_parent_tools / _dispatch_parent_tool → 动态 MCP 工具。
2. _inject_background_notifications / _inject_team_messages → 异步结果转主历史。
3. ContextCompactor.prepare / compact_history → 结构裁剪、落盘与摘要。
4. AgentTeamManager._run_teammate → 工作周期、收信与 idle 自动认领。
5. WorktreeContext.select → 队友局部工具目录。
6. _queue_processor_loop → 定时 turn 与前台 turn 的串行边界。
7. 无工具响应分支 → 再收信、Stop Hook、Memory 提取与最终返回。

读完可以自问：哪些事件属于 tool result，哪些是 user 通知？谁能直接修改主历史？哪些状态持久化了，哪些只在内存？为什么“所有模块都有单测”仍然不能替代跨模块验收？
