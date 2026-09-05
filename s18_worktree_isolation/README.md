# s18 Worktree Isolation：任务分工之外，还要隔离工作目录

s17 解决“谁来做”：队友空闲时扫描任务板，自动认领可执行任务。s18 解决“在哪里做”：给任务绑定独立的 Git worktree，让不同队友修改同名文件时不互相覆盖。

**任务管状态与归属，worktree 管目录与分支；绑定两者，不把两套生命周期合并。**

本文以本仓库当前实现为准。前置概念见 [s17 Autonomous Agents](../s17_autonomous_agents/README.md)，课程入口见 [s18 Worktree Isolation](https://learn.shareai.run/zh/s18/)。

## 1. 为什么任务锁还不够？

Alice 领取“重构认证”，Bob 领取“修改登录页”。两人领取的是不同任务，任务锁完全正常，但如果都写主目录下的 config.py，后写入的内容仍可能覆盖前者。

任务锁保护任务记录的读改写，不能替代源码工作区隔离。

本章为每个绑定任务创建独立目录与分支：

```text
项目根目录                         main：shared.txt = baseline
├── .tasks/                        共享任务板
├── .mailboxes/                    共享团队通信
└── .worktrees/
    ├── index.json                 Harness 管理的 worktree 索引
    ├── events.jsonl               创建、保留、删除事件
    ├── alpha/                     wt/alpha：shared.txt = alpha-isolated
    └── beta/                      wt/beta： shared.txt = beta-isolated
```

两份 shared.txt 路径不同，写入彼此独立。但它们仍共享 Git 仓库的对象与引用，不是两台虚拟机，也不是两个安全沙箱。

## 2. 一个任务如何进入独立目录？

推荐顺序是：先创建任务，再创建并绑定 worktree，最后启动队友。

```text
Lead
  create_task → pending / owner=None / worktree=None
  create_worktree(name, task_id)
    → 从已提交 HEAD 创建 wt/<name> 和 .worktrees/<name>
    → 保存 index，写入任务的 worktree 字段
    → 任务仍为 pending，等待认领
  spawn_teammate
    → 队友先执行启动 prompt，结束本轮后进入 idle

队友 idle
  claim_next → owner=队友名字，status=in_progress
  WorktreeContext.select(task) → 校验并切换局部工具目录
  注入 [Auto-claimed task]
  模型 → bash / write_file / read_file → 验证 → complete_task
    → status=completed，但目录与分支仍然保留

收尾
  keep_worktree → 留待 review
  remove_worktree → 通过保护检查后删除，解绑但不修改任务状态
```

创建 worktree 以当前仓库已提交的 HEAD 为起点，不会复制主目录的未提交改动，也不会自动拷贝被忽略的 .env、虚拟环境或依赖目录。运行环境需要另行准备；模型客户端使用进程已有的配置，不需要把密钥复制给队友。

工作目录必须是 Git 仓库根目录，而且仓库至少已有一个提交。

## 3. 绑定为什么不顺便认领？

自动认领只选择尚未归属、状态为 pending 且依赖已完成的候选。如果绑定目录时就把任务改为 in_progress，队友反而不会自动发现它。

TaskRecord 新增可选的 worktree 字段；旧记录缺少该字段时按未绑定处理。

当前绑定规则：

- 任务必须为 pending，且还没有绑定 worktree。
- 同一个 worktree 不能同时绑定另一项任务。
- 绑定不修改 status 和 owner。
- 删除 worktree 后清除绑定，但保留任务原来的状态和归属。

例如 completed 任务删除目录后仍然是 completed，不会退回 pending；pending 任务删除目录后也不会被假定为已完成。

bind_task_to_worktree 是内部方法，不是新增的模型工具。模型通常通过 create_worktree(name, task_id) 一次完成创建与绑定。

## 4. cwd 到底在哪里切换？

核心不是“在 prompt 中提醒模型换个目录”，而是为每名队友维护独立的 WorktreeContext。

| 组件 | 在本章中的作用 |
| --- | --- |
| WorktreeContext.path | 保存该队友当前的工具工作目录 |
| 局部 Settings | 替换 workdir，保留共享配置 |
| BuiltinTools | 为 bash、read_file、write_file 提供局部目录实现 |
| ToolExecutor 与默认 Hooks | 使用局部配置执行工具及默认权限检查 |
| system prompt | 每次模型请求前展示真实工具目录，帮助模型理解环境 |

代码不调用 os.chdir，因为队友运行在同一进程的不同线程里，切换进程目录会影响其他队友。

两条认领路径都会接到同一个 select：

```text
自动：AutonomousWorker.idle_poll
        → claim_next → on_claim=context.select → 注入任务

手动：队友 claim_task handler
        → TaskManager.claim_task → context.select → 返回新目录

随后：context.executor.execute
        → 动态读取 context.file_handlers
        → 当前目录下的 bash / read_file / write_file
```

动态 handler 会在每次执行时读取当前 context，因此同一批工具调用中，先 claim_task、后 write_file，也会使用认领后的目录。下一次模型请求会刷新 system 中的目录提示。

后续认领未绑定 worktree 的任务时，context 会恢复主目录和原工具执行器，不会误用上一个任务的目录。仅调用 complete_task 不会立即恢复目录，避免同一轮后续验证操作跑到主目录。

绑定目录丢失或校验失败时，select 抛出错误，不主动回退主目录执行。自动认领回调失败会使队友进入 failed；已经认领的任务可能留在 in_progress，需要人工处理。手动认领失败则由工具执行层返回错误，原上下文可能仍保留，不能把它当作事务性认领回滚。

## 5. Lead 和队友分别能调用什么？

Lead 新增三个工具，本章累计为 26 个；队友仍为 8 个，Subagent 仍为 6 个。本章接入的是常驻队友，不是为所有 Subagent 自动增加隔离。

| 新工具 | 行为 |
| --- | --- |
| create_worktree(name, task_id 可选) | 创建目录与 wt/name 分支，可同时绑定 pending 任务 |
| keep_worktree(name) | 校验归属、记录 keep 事件，保留目录与分支 |
| remove_worktree(name, discard_changes=False) | 校验后删除目录与分支、清除绑定，不自动完成任务 |

队友不新增 worktree 管理工具，仍使用原来的 bash、read_file、write_file、send_message、submit_plan、list_tasks、claim_task、complete_task。变化在执行目录，而不是工具数量。

## 6. 删除之前检查什么？

remove_worktree 不是直接递归删除目录。它先确认目标属于 Harness 管理范围，再调用 Git。

1. 名称必须是合法的单段名称；拒绝路径穿越、非法分支形式和符号链接目录。
2. index 中必须存在记录，且与 Git worktree 登记、当前分支 wt/name 和仓库归属一致。
3. 该管理器不能还有队友持有这个目录；绑定任务也不能处于 in_progress。
4. 默认检查所有文件改动，包括未跟踪和被忽略的文件。
5. 默认检查相对创建时 base 提交新增的提交，不能仅凭“git status 干净”就删除。
6. 检查通过后删除 worktree 和分支，再解绑、更新 index、记录 remove 事件。

discard_changes=true 只允许显式丢弃文件改动和新增提交，不绕过目录归属、活跃使用或 in_progress 检查。使用它之前需要确认产物确实不要了。

任务完成后队友可能仍在原目录里等待。活跃引用直到切换目录或线程退出时才释放，所以“任务 completed”不一定意味着“现在可删除目录”。keep 则不会结束队友、合并代码或清除绑定。

## 7. 模块边界与源码入口

| 模块 | 职责 |
| --- | --- |
| [worktree_isolation.py](./harness/worktree_isolation.py) | WorktreeManager 管 Git 生命周期、索引、事件和活跃引用；WorktreeContext 管单名队友的工具目录 |
| [task_system.py](./harness/task_system.py) | 保存 worktree 字段，保护绑定与任务状态读改写 |
| [autonomous_agents.py](./harness/autonomous_agents.py) | 自动认领后调用 on_claim，不直接管理 Git 或模型请求 |
| [agent_teams.py](./harness/agent_teams.py) | 创建队友 context，连接手动认领、工具分发、目录提示和退出释放 |
| [agent_loop.py](./harness/agent_loop.py) | 装配共享的 TaskManager、WorktreeManager，注入团队并注册 Lead handlers |
| [tool_use.py](./harness/tool_use.py) | 汇总工具 schema，沿用文件工具和统一执行器 |
| [system_prompt.py](./harness/system_prompt.py) | 告知 Lead 先绑定再启动，以及保留、删除和人工 review 的规则 |

任务事务使用线程锁和 .board.lock 上的 flock；创建、删除也进入任务事务，减少绑定与认领之间的竞争。WorktreeManager 的活跃引用在内存中，不是跨进程租约；不能据此保证多个独立 Harness 同时操作同一 worktree 的完整安全。

index 使用临时文件加原子替换保存，events.jsonl 追加 create、keep、remove 记录。新的管理器可读取已有 index，但这不代表运行中的队友、活跃引用和完整会话能够重启恢复。

## 8. 怎样运行和体验？

在仓库根目录、使用已安装项目依赖并配置模型凭据的 Python 环境运行：

```bash
python3 -m unittest tests.test_s18
python3 s18_worktree_isolation/code.py
```

建议先用一个已有基线提交的测试仓库作为 CC_WORKDIR，避免把练习产物混入正在开发的业务目录。模型访问凭据沿用已有环境变量或本地 .env，不要写进 prompt、README 或 Git。

可以给 Lead 这样的任务：

> 创建 alpha、beta 两个任务，分别要求在自己的工作目录把 shared.txt 写成 alpha-isolated 和 beta-isolated。先为两个任务分别创建并绑定 worktree，再启动 Alice、Bob。队友启动时只回复 READY，随后等待 Harness 自动认领。认领后检查 pwd 和分支，用 write_file 写入、read_file 回读、git diff 验证，再 complete_task。不要提交或合并代码，最后保留两个 worktree 供我检查。

这个 prompt 用于手动体验，不保证模型每次都完全遵守顺序。下面的真实验证使用脚本确定性地完成任务创建、目录绑定和队友启动，再让真实队友模型执行任务。

## 9. 已完成的验证与证据

### 自动化测试

实现完成时，s18 的 109 项测试、全项目的 665 项测试均通过；Python 编译检查也通过。[test_s18.py](../tests/test_s18.py) 在继承累计能力测试之外，覆盖真实临时 Git 仓库中的创建、绑定、失败回滚、目录切换、并发同名文件写入、删除保护、符号链接拒绝、分支变更拒绝和索引重读。

这些自动化测试中的模型响应是模拟的；Git worktree 操作是真实的。L20 的 Git 2.34.1 不支持 worktree list 的 -z 参数，当前实现使用 --porcelain 文本输出完成登记核对。

### 真实模型验证

2026-09-05 在 L20-Server 的独立测试仓库中使用百炼 qwen-plus，共 8 次真实模型调用，累计 10,437 tokens。

| 检查 | 实际观察 |
| --- | --- |
| 绑定与认领 | 绑定后仍 pending；Alice 自动认领 alpha，Bob 自动认领 beta |
| 实际工具目录 | 两名模型均调用 pwd 和 git branch --show-current，分别位于对应目录和 wt/alpha、wt/beta |
| 文件隔离 | 两边分别写入 alpha-isolated、beta-isolated，均含换行；主目录保持 baseline |
| 完成前验证 | 模型调用 read_file 和 git diff 后，下一次请求才调用 complete_task |
| 收尾 | 两项任务 completed；两名队友随后 idle 超时退出，状态 done |
| 删除与保留 | 脚本调用默认删除均被改动检查拒绝；keep 成功，两个目录与分支留存 |
| 主仓库 | 已跟踪文件与主分支提交没有变化 |

本次测试把 idle 超时设为 8 秒、轮询间隔设为 0.2 秒、每个工作周期最多 8 轮，不是默认参数。

测试脚本负责创建任务、绑定目录、启动队友和验收；真实模型负责决定工具调用、操作文件、验证结果及声明完成。删除与 keep 由脚本调用管理器验证，不能据此宣称 Lead 模型已自主完成整个管理流程。

首次“主目录 Git 完全干净”的断言失败：Harness 正常生成了未跟踪的 .mailboxes/。后续核对确认只有该运行时目录，没有业务文件修改或额外主分支提交。原始 report.json 保留失败记录，validation.json 记录复核结果；没有把首次运行改写为无条件全绿。

服务器证据目录：/data/cc-s18-live-pny3ev5i/。

- report.json：原始检查、任务状态、队友总结和 worktree 产物。
- validation.json：区分运行时目录后的复核结果。
- model_calls.jsonl：真实请求、响应、工具选择和用量。
- .worktrees/alpha/shared.txt、.worktrees/beta/shared.txt：保留的实际产物。
- .worktrees/events.jsonl：创建与保留事件；被拒绝的删除没有成功 remove 事件。

证据保留在当次服务器测试目录，不随仓库分发。这是一次小型功能验收，不是成功率、长时间稳定性或生产安全性评测。

## 10. 目前没有解决什么？

- **不会自动合并。** 两个 worktree 都能改同名文件，不代表之后合并没有冲突，需要人工 review 和合并策略。
- 任务依赖只控制何时可认领，不会把前置任务分支的产物自动带入后续 worktree。依赖解锁不等于代码已集成。
- 仅对绑定任务切换目录。启动 prompt 运行时尚未自动认领，未绑定任务也仍使用主目录。
- 文件工具的路径检查和 Bash 的默认权限策略不构成系统级沙箱。Bash 仍是同一用户进程，不能依赖 cwd 抵御恶意越界访问。
- 局部 context 重新安装默认 Hooks，不自动复制共享执行器上额外注册的自定义 Hooks。
- Git、任务 JSON、index 和事件日志不是一个整体原子事务。中途磁盘或 Git 操作失败可能留下部分状态，需要核对登记、分支和记录后修复。
- 没有 worktree 自动合并、自动垃圾回收、跨进程使用租约或完整崩溃恢复；已有任务租约与失败重新分配问题也没有在本章解决。

推荐阅读顺序：WorktreeManager.create_worktree → TaskManager.bind_task_to_worktree → WorktreeContext.select → 两条认领路径 → remove_worktree。

读完后可以自问：为什么绑定不能直接改为 in_progress？为什么 complete_task 后仍可能拒绝删除？为什么后续任务依赖已完成，却仍看不到另一个 worktree 的新代码？
