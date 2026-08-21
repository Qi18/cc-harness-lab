# s12 源码剖析：Task System

> s12 建立在真正的 s10 System Prompt 与 s11 Error Recovery 之上。本章只新增持久任务图，不重新实现前两章。

## 一、累计关系

```text
动态 System Prompt（s10）
  → API 恢复策略（s11）
  → Agent Loop
  → Task System（s12）
```

因此 `harness/prompt.py` 与 `harness/recovery.py` 继续参与调用链；本章主要新增
`harness/tasks.py`，并把六个任务工具接入父 Agent。

## 二、任务与 Todo 的区别

`todo_write` 是当前会话里的轻量检查清单；Task System 是写入 `.tasks/` 的持久任务图。
一条任务可跨会话恢复，并表达依赖、负责人和状态迁移。

```json
{"id":"task_a1b2c3d4","subject":"create API","description":"...",
 "status":"pending","owner":null,"blockedBy":["task_11223344"]}
```

## 三、两阶段建图

任务 ID 由 Harness 创建后才知道，不能在同一批并行工具调用里既创建节点又引用新 ID：

```text
第 1 轮：create_task × N
第 2 轮：update_task(id, addBlockedBy=[真实 ID])
```

`TaskStore.update_dependencies` 会先验证整批依赖，再一次保存。它检查 ID、任务存在性、
自依赖和传递环；任一项失败时不会留下半更新的图。

## 四、持久化与安全边界

- `tasks_dir` 必须位于工作区内；
- ID 必须匹配 `task_[0-9a-f]{8}`；
- JSON 字段集合和字段类型必须严格合法；
- 创建使用排他模式，更新使用同目录临时文件加 `os.replace`；
- 损坏或缺失的依赖按“仍被阻塞”处理，不会误放行。

这保证的是单机文件状态完整性，不是分布式事务。多个进程同时认领同一任务仍需要锁或外部
协调服务。

## 五、状态机与工具隔离

```text
pending --claim_task--> in_progress --complete_task--> completed
```

只有依赖全部完成的任务才能认领；只有 owner 能完成已认领任务。父 Agent 新增
`create_task`、`update_task`、`list_tasks`、`get_task`、`claim_task`、
`complete_task` 六个工具，子 Agent 不拥有它们。

任务工具仍经过统一 PreToolUse/PostToolUse 管线，存储异常转换为模型可读的
`Error: ...`，不会直接击穿 Agent Loop。

## 六、与 s10、s11 的衔接

每轮请求前，s10 根据真实工具注册表、工作目录、Skills 与 Memory 元数据组装 System
Prompt。随后 s11 包装模型调用，处理输出截断、上下文过长、429/529 与模型降级。请求成功
后，Agent Loop 才执行 s12 的任务工具。

`tests/test_s12.py` 覆盖持久化、依赖建图、环检测、状态迁移、owner 隔离、工具注册与
跨章节导入。它验证单进程语义，不把 `.tasks/` 当作生产级分布式队列。

## 一句话总结

s12 把会话内 Todo 升级成了可恢复的持久任务图，同时完整继承 s10 的动态提示词和 s11 的
错误恢复；新增能力集中在 TaskStore、TaskManager 与父 Agent 的六个任务工具。
