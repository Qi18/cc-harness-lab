# s10：模块化 Task System

s10 在 s09 的完整能力上加入持久任务图，并把持续膨胀的单文件 Harness 拆成可组合模块。

- 官方教程：[s10 Task System](https://learn.shareai.run/zh/s10/)
- 官方源码：[s10_task_system/code.py](https://github.com/shareAI-lab/learn-claude-code/blob/main/s10_task_system/code.py)
- 上一章：[s09 Memory](../s09_memory/)

## 为什么从这一章开始模块化

s01–s09 的 `code.py` 是教学快照：打开一个文件就能看到当章完整实现。但严格累计后，
s09 已接近 2000 行；继续把 Task、后台任务、Cron、Teams 和 MCP 放进同一个文件，会让
机制边界和回归影响越来越难判断。

s10 保留旧章节不动，只把本章及后续阶段改成局部 package：

```text
s10_task_system/
├── code.py                 # 59 行 CLI 入口和公共导出
└── harness/
    ├── agent.py            # 父/子 Agent Loop 与组件组装
    ├── compaction.py       # L1-L4 压缩与 reactive compact
    ├── config.py           # 百炼、模型和工作目录配置
    ├── hooks.py            # Permission 与生命周期 Hook
    ├── memory.py           # 记忆筛选、提取和整理
    ├── models.py           # 小型共享数据契约
    ├── skills.py           # Skill 目录和按需加载
    ├── tasks.py            # 本章新增：TaskStore 与任务状态机
    └── tools.py            # 工具 schema、基础 handler 与 Todo
```

这样做保留了两种学习视角：s01–s09 用单文件观察机制怎样逐步累积；s10 起用模块边界
观察一个可维护 Harness 怎样继续扩展。后续章节只需要增加或修改对应模块。

## Task System 解决什么问题

`todo_write` 是当前执行过程中的检查清单，退出进程后消失，也不能表达依赖和负责人。
Task System 面向跨会话、可协调的工作：

| 能力 | TodoWrite | Task System |
| --- | --- | --- |
| 存储 | 进程内列表 | `.tasks/task_<8 hex>.json` |
| 粒度 | 当前 Agent 的步骤 | 可独立认领的任务节点 |
| 依赖 | 无 | `blockedBy` 有向图 |
| 负责人 | 无 | `owner` |
| 恢复 | 会话结束即丢失 | 新进程可重新读取 |
| 更新 | 整表替换 | 单记录创建、查询、认领和完成 |

任务记录格式：

```json
{
  "id": "task_a1b2c3d4",
  "subject": "create API",
  "description": "Implement the public endpoint",
  "status": "pending",
  "owner": null,
  "blockedBy": ["task_11223344"]
}
```

状态机只有两次合法迁移：

```text
pending --claim_task--> in_progress --complete_task--> completed
```

## 两阶段构建依赖图

Task ID 由 Harness 在运行时生成。模型在同一轮发出的并列工具调用，是在任何工具结果
返回前一次性决定的，因此不能让第二个 `create_task` 引用第一个调用尚未返回的 ID。

正确流程分两轮：

1. 调用 `create_task` 创建所有节点，取得真实 ID。
2. 调用 `update_task(task_id, addBlockedBy=[...])` 添加依赖边。

例如：

```text
schema (pending)
   ↓
API (pending, blockedBy=[schema])
   ↓
tests (pending, blockedBy=[API])
```

`TaskStore.update_dependencies()` 会先验证整批依赖再保存，因此不存在“前一条边写入成功、
后一条边失败”的半更新。它还拒绝：

- 不存在或格式错误的 Task ID；
- 自依赖；
- 直接或传递依赖环；
- 已认领或已完成任务的依赖变更；
- 重复边（重复输入会去重）。

## 六个任务工具

父 Agent 新增：

- `create_task(subject, description?)`：创建 pending 节点并返回随机 ID；
- `update_task(task_id, addBlockedBy)`：两阶段添加依赖；
- `list_tasks()`：显示状态、owner 和依赖摘要；
- `get_task(task_id)`：读取完整 JSON 记录；
- `claim_task(task_id)`：依赖全部完成后认领；
- `complete_task(task_id)`：完成自己认领的任务，并报告刚解锁的下游。

这六个工具暂时不暴露给 s06 的同步 SubAgent。s10 只有单执行者顺序状态机，还没有原子
claim、独立 teammate identity 和并发协调；这些能力属于后续 Agent Teams 章节。父 Agent
仍可用 `task` 委派文件调查等子任务，但任务图由父 Agent 统一维护。

## 持久化与安全边界

`TaskStore` 接收明确的 `tasks_dir` 和 `workdir`，不再依赖 Agent Loop 的可变全局变量：

- 任务目录必须位于 `CC_WORKDIR` 内；
- ID 必须严格匹配 `task_[0-9a-f]{8}`；
- 文件内容必须拥有完整且合法的字段集合；
- 更新先写同目录临时文件，再通过 `os.replace()` 原子替换；
- 缺失或损坏的依赖文件按“仍被阻塞”处理，不会误放行任务。

本章的原子写只保护单文件替换，不等于多 Agent 的原子认领。两个进程同时 claim 同一任务
仍可能发生读写竞争，因此不要把 s10 当作生产级分布式任务队列。

## 与官方 s10 的区别

| 维度 | 官方课程 | 本仓库 |
| --- | --- | --- |
| 教学结构 | 单个 `code.py` 聚焦 Task System | 从 s10 起拆成 9 个 Harness 模块 |
| 前置能力 | 使用较小的 s04 kernel 叠加任务工具 | 保留 s01–s09 的全部累计能力 |
| 模型协议 | Anthropic tool blocks | 百炼 OpenAI-compatible `tool_calls` |
| 配置 | 模块级目录与客户端 | `Settings` 显式注入各组件 |
| 文件保存 | JSON 写入 | 排他创建，更新时临时文件 + `os.replace` |
| 数据校验 | ID、状态和依赖核心校验 | 额外校验字段集合、owner、重复依赖和目录边界 |
| 子 Agent | 本章未组合旧 SubAgent | 保留 SubAgent，但任务工具仅限父 Agent |
| 回归 | 官方课程测试 | s10 17 项，项目全量 138 项 |

核心概念保持一致：文件持久化、`blockedBy`、两阶段建图、pending/in_progress/completed
状态机，以及完成上游后解锁下游。本仓库的差异主要来自累计式架构、安全边界和 OpenAI
协议适配。

## 运行

```bash
cd /data/projects/cc-harness-lab
python3 s10_task_system/code.py
```

可以输入：

```text
Create durable tasks for schema, API, and tests. API depends on schema,
tests depend on API. Claim and complete the first unblocked task.
```

然后观察 `.tasks/` 目录：

```bash
find .tasks -maxdepth 1 -name 'task_*.json' -print
```

运行测试：

```bash
python3 -m pytest -q tests/test_s10.py
python3 -m pytest -q
```

## 下一章

s10 可以表示“什么任务已经就绪”，但执行慢命令时 Agent Loop 仍会同步等待。s11 将加入
Background Tasks，让慢操作在线程中运行，主循环继续处理其他工作，并在完成后注入通知。
