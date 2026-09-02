# s12：Task System

这一章在 s01–s11 的累计能力之上，把"计划"从进程内存搬到磁盘：TodoWrite 继续负责当前
会话的执行清单，Task System 负责跨会话、可认领、有依赖关系的项目级工作。新增的第十二
个能力模块是 `task_system.py`——纯粹的记录、依赖与状态机，何时装配任务板仍由 Agent
Loop 决定。父 Agent 因此从 9 个工具增长到 14 个，SubAgent 仍保持 6 个隔离工具。

- 官方教程：[s12 Task System](https://learn.shareai.run/zh/s12/)
- 官方源码：[s12_task_system/code.py](https://github.com/shareAI-lab/learn-claude-code/blob/main/s12_task_system/code.py)
- 上一章：[s11 Error Recovery](../s11_error_recovery/)
- 源码分析：[ANALYSIS.md](./ANALYSIS.md)
- 调用图：[CALLGRAPH.md](./CALLGRAPH.md)

## 这一章解决什么

前十一章的计划状态只有一处：`TodoManager.todos`。它活在进程内存里，没有 owner、没有
依赖，`compact` 之后能靠摘要留下痕迹，进程退出后就彻底消失。这对"我接下来做哪三步"够
用，对"整个项目还剩哪些工作、谁在做、重启后从哪继续"完全不够。

真实项目也不是一张无顺序的 checklist：schema 没完成时 API 不能开工，API 没完成时测试
不能认领。s12 的做法是把每个任务写成 `.tasks/{id}.json`，用 `blockedBy` 形成任务图，
并把 claim 作为真正开工前的状态门。上下文被压缩、进程退出或会话重启后，磁盘任务板仍
是唯一事实来源。

## TodoWrite 与 Task System

| 维度 | TodoWrite（s05） | Task System（s12） |
| --- | --- | --- |
| 粒度 | 当前任务的执行步骤 | 项目级工作单元 |
| 生命周期 | 当前进程 / 当前会话 | 跨会话持久化 |
| 存储 | 内存列表 | `.tasks/{id}.json` |
| 依赖 | 无 | `blockedBy` |
| 认领 | 无 | `owner` + `claim_task` |
| 状态 | pending / in_progress / completed | pending / in_progress / completed |
| 不变量 | 最多一个 in_progress | 依赖未完成不得 claim |
| 使用方式 | Agent 维护眼前计划 | 多 Agent 平台的任务板基础 |

两个系统同时存在，不互相同步：没有"Todo 自动升级成 Task"的隐藏逻辑。一个持久任务被
claim 之后，Agent 仍会用 TodoWrite 拆出当前实现步骤。

## TaskRecord 是磁盘协议

每个任务文件包含六个字段：

```json
{
  "id": "task_1725160000000000000_a1b2c3d4",
  "subject": "Create API endpoints",
  "description": "Implement CRUD routes and validation.",
  "status": "pending",
  "owner": null,
  "blockedBy": ["task_1725159999000000000_11223344"]
}
```

Python 内部使用不可变的 `TaskRecord`，把 `blockedBy` 映射成
`blocked_by: tuple[str, ...]`，写盘时恢复教程字段名。加载已有文件时会重新校验 ID、
标题、描述、状态、owner 和依赖数组，文件名中的 ID 还必须与 JSON 内的 `id` 一致——
磁盘不是可信输入，复制或手改文件不应让 `get_task("A")` 返回任务 B。

默认 ID 是 `task_<time.time_ns()>_<secrets.token_hex(4)>`：时间提供可读顺序，
随机后缀避免同一时刻碰撞。它适合单机教学任务板，但不是全局递增 ID，也没有
Claude Code 的 high-watermark 语义。

## 五个任务工具

| 工具 | 输入 | 行为 |
| --- | --- | --- |
| `create_task` | subject、可选 description / blockedBy | 创建 pending 任务并返回完整 JSON |
| `list_tasks` | 无 | 一行一个任务，标注 ready / blocked / in_progress / completed |
| `get_task` | task_id | 返回完整任务 JSON |
| `claim_task` | task_id、可选 owner | 依赖全部完成后 pending → in_progress |
| `complete_task` | task_id | 仅允许 in_progress → completed，并报告刚解锁的下游 |

这些工具只注册给父 Agent。当前 SubAgent 是一次性的隔离执行器，不是拥有持久身份的
teammate；让它直接 claim 项目任务会模糊 owner 与父级委派边界。s15 引入持久队友时再
扩展任务板的共享认领语义。

所有任务工具仍走同一个 `ToolExecutor`，因此 PreToolUse / PostToolUse Hook、参数校验
与调用日志都不会被绕过。

## 状态机

```text
pending ── claim_task ──> in_progress ── complete_task ──> completed
```

实现明确拒绝以下跳转：

- 依赖未完成的 pending 任务不能 claim；
- in_progress 或 completed 任务不能再次 claim；
- pending 任务不能直接 complete；
- completed 任务不能重复 complete；
- owner 必须是非空字符串。

本章没有 release 路径。Agent 中途退出后，in_progress 记录会保留，不会自动退回
pending；这是与教程一致的显式限制。

## 依赖与解锁

`blocking_dependencies()` 是依赖语义的单一事实来源，逐个读取 `blockedBy`：

1. 依赖文件不存在：仍算 blocker，避免拼错的 ID 被当成"已经完成"；
2. 依赖状态不是 completed：算 blocker；
3. 全部依赖 completed：`can_start()` 才返回 True。

看板显示 ready 与 claim 是否成功用的是同一个函数，不存在两套规则。
`complete_task()` 只报告真正包含当前任务 ID、且现在已经没有其他 blocker 的 pending
下游，无关的 ready 任务不会被误报成"刚刚解锁"。

教学版只演示 `blockedBy` 检查，本实现同样没有通用环检测。公开工具只能在创建时声明
依赖、不能修改已有图，正常生成 ID 的工作流不容易构造环；手工编辑 JSON 仍可能产生
循环或长期阻塞。

## 持久化与文件边界

任务目录默认是 `<CC_WORKDIR>/.tasks`，可用 `CC_TASKS_DIR` 覆盖，但 `AgentHarness`
装配时会先调用 `settings.assert_inside_workdir()`。目录位于工作区之外或经符号链接
逃逸时，Harness 直接拒绝启动。

每次保存都执行同一条链路：

```text
完整 JSON → 同目录 mode 600 临时文件 → flush + fsync → os.replace → 最终 mode 600
```

这样进程在写到一半时不会把正式任务文件变成半个 JSON。它保证的是单文件替换的原子性，
不等于跨进程 claim 的原子性：两个进程仍可能同时读到 pending 再各自写入，本章没有
文件锁。

## 模块结构

```text
s12_task_system/
├── code.py                    59   CLI 入口
└── harness/
    ├── config.py              60   环境变量、模型、fallback 与持久目录
    ├── models.py              24   跨模块数据契约
    ├── provider.py            52   百炼客户端、请求与响应适配
    ├── agent_loop.py         410   01 Agent Loop：装配能力并驱动父循环
    ├── tool_use.py           305   02 Tool Use：Schema、handler、执行管线
    ├── permission.py          85   03 Permission：deny / ask 决策
    ├── hooks.py               74   04 Hooks：生命周期事件注册与触发
    ├── todo_write.py          46   05 TodoWrite：会话内 Todo 状态与校验
    ├── subagent.py            72   06 SubAgent：隔离历史的同步子循环
    ├── skill_loading.py       99   07 Skill Loading：扫描、目录、按需加载
    ├── context_compact.py    389   08 Context Compact：L1–L4 与手动 compact
    ├── memory.py             540   09 Memory：召回、提取、整理与持久化
    ├── system_prompt.py      103   10 System Prompt：section 组装与缓存
    ├── error_recovery.py     193   11 Error Recovery：分类、退避、有界状态
    ├── task_system.py        442   12 Task System：记录、依赖、状态机、五个工具
    └── __init__.py            25   包门面
```

合计 2978 行，其中 `harness/` 2919 行。章节编号只写在文档里，不写进文件名。

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
| 12 | Task System | `task_system.py` | `TaskRecord`、`TaskManager`、`TASK_TOOLS` |

`config.py`、`models.py`、`provider.py` 是跨章节基础设施，不强行归入某一课。

## 为什么这样切分

**数据、依赖与状态机放在一个模块。** `task_system.py` 同时拥有 `TaskRecord`、
`TaskManager` 和 `TASK_TOOLS`：状态跳转的合法性、依赖判定和对外 schema 是同一组
约束的三个视角，分开会让"允许调用什么"与"真正允许发生什么"逐渐漂移。

**schema 在模块内自建，不导入 `tool_use.py`。** 任务工具遵循 s02 的 function calling
形状，但由模块内的 `_task_tool()` 构造，`tool_use.py` 反向导入 `TASK_TOOLS`。
方向单一，避免 `tool_use ↔ task_system` 循环依赖。

**Agent Loop 只做装配，不做转发。** 没有新增 `execute_task_tool` 方法，也没有在工具
批次里给 Task 开特例。任务工具返回字符串、不改写整个 `messages`，因此和文件工具走
同一条普通分发路径；`compact` 仍是唯一的控制工具例外。

**边界检查放在装配期。** `assert_inside_workdir(settings.tasks_dir)` 在创建
`TaskManager` 之前执行，配置错误在启动时就暴露，而不是等模型第一次调用 `create_task`
才发现任务写到了工作区之外。

**ID 工厂可注入。** `TaskManager(tasks_dir, id_factory=...)` 让测试使用
`task_001`、`task_002` 这类确定 ID，断言看板文本和依赖关系时不必匹配时间戳。

## 依赖方向

课程编号代表学习顺序，不代表 Python 依赖层级。`task_system.py` 是第十二课，却因为只
依赖标准库而位于最底层。

```text
config · models · system_prompt · error_recovery · task_system   无内部依赖
          ↓
provider · permission · skill_loading · todo_write
          ↓
hooks · memory
          ↓
tool_use（导入 TASK_TOOLS）· context_compact
          ↓
subagent
          ↓
agent_loop（装配 TaskManager）→ code.py
```

箭头向下表示"依赖上一层"。下层模块都不导入 `AgentHarness`，因此依赖图无环。

## 行为保持不变

新增持久任务板没有削减前章能力：

- 父 Agent 现在有 14 个工具；SubAgent 仍只有 6 个基础工具，不能委派或操作任务板；
- s11 的 429/529 退避、fallback 切换、8K→64K 续写与一次 reactive compact 都保留；
- 普通工具仍经过 Permission 与 PreToolUse / PostToolUse；
- Todo 仍限制最多一个 `in_progress`，连续三轮未更新会补提醒；
- System Prompt 仍每次模型调用前按运行态重组，且只改写首条 system 消息；
- Context Compact 仍保留 L1–L4、手动 compact 与每 turn 一次的 compact 工具；
- Memory 仍使用独立提取快照，不受本 turn 压缩影响；
- `compact` 仍是控制工具，任务工具是返回字符串的普通 handler；
- 工作区路径边界、危险命令检查与 Memory 敏感值过滤继续生效。

## 运行与测试

```bash
export DASHSCOPE_API_KEY=...
export MODEL_ID=qwen-plus
export FALLBACK_MODEL_ID=qwen-turbo        # 可选，连续三次 529 后启用
export CC_WORKDIR=/tmp/cc-s12-demo
export CC_TASKS_DIR=/tmp/cc-s12-demo/.tasks   # 可选；默认就是 <CC_WORKDIR>/.tasks

cd /data/projects/cc-harness-lab
python3 s12_task_system/code.py
python3 -m pytest -q tests/test_s12.py
```

CLI 启动时打印主模型、fallback 模型、工作目录和当前生效的 Prompt sections；任务目录
不在启动横幅里，可用 `list_tasks` 或直接查看 `.tasks/` 确认。

可依次让 Agent：

1. 创建 schema 任务；
2. 创建 `blockedBy=schema` 的 API 任务；
3. 创建 `blockedBy=API` 的 tests 任务；
4. list 后尝试认领 API（应被拒绝）；
5. claim 并 complete schema；
6. 再次 list（API 应显示 ready，tests 仍 blocked）。

36 个 s12 测试分三组：14 项沿用 s10 的结构回归（section 组装、注册表来源、缓存、工具
隔离、Todo 与工作区边界，并断言十二个能力模块存在、旧碎片文件名消失），12 项沿用 s11
的恢复路径回归，10 项覆盖任务系统本身——任务文件与私有权限、依赖阻塞、缺失依赖、严格
状态机、下游解锁、跨实例恢复、非法输入与路径穿越、损坏文件、工作区边界，以及任务工具
经过父 ToolExecutor 与 Hook 管线。

## 已知遗留

- 没有依赖环检测，也没有修改依赖、删除任务或 release / reopen 路径；
- 单文件替换是原子的，但 claim 的 read-modify-write 没有跨进程文件锁；
- ID 不是递增 high-watermark，极低概率碰撞只做最多十次重试；
- SubAgent 不直接操作持久任务板；
- 任务板不自动注入 System Prompt，Agent 需要调用 `list_tasks` 查看当前状态；
- 任务目录扫描每次都读全部 `task_*.json`，任务很多时没有索引或分页；
- s11 只恢复父循环主请求这条边界继续保持。

## 与官方代码的关系

核心机制与教程一致：五个工具、每任务一个 JSON、`blockedBy`、owner、claim、complete、
缺失依赖视为 blocked，以及 pending → in_progress → completed。

本仓库的主要差异：

- 官方 s12 为聚焦 Task System 保留较小 Agent kernel，并明确省略完整 s11；本目录继续
  累计 s01–s11 全部能力；
- 官方使用 Anthropic 工具块，本仓库使用百炼 OpenAI-compatible `tool_calls` /
  `role=tool`；
- 本仓库强制 tasks 目录位于 `CC_WORKDIR`，并用 mode 600 临时文件、fsync 与原子替换；
- 本仓库严格拒绝 pending 直接 complete，并只报告真正由本次完成动作解锁的下游；
- 官方教学版和本实现都没有通用 DAG 环检测、release 路径与跨进程 claim 锁。
