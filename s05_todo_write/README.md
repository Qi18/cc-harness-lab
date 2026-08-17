# s05：TodoWrite

这一章给 Agent 增加显式规划能力。`todo_write` 不读取文件、不执行命令，只维护
一份模型和用户都能看见的任务列表。

## 工具输入

每次调用传入完整任务列表：

```json
{
  "todos": [
    {"content": "Inspect the code", "status": "completed"},
    {"content": "Implement the change", "status": "in_progress"},
    {"content": "Run tests", "status": "pending"}
  ]
}
```

状态只有三种：

- `pending`：尚未开始；
- `in_progress`：当前正在执行；
- `completed`：已经完成。

## Harness 校验

`run_todo_write()` 在替换旧计划前完成整份列表校验：

- `todos` 必须是数组；
- 每项只能包含 `content` 和 `status`；
- `content` 必须是非空字符串；
- `status` 必须属于三种允许值；
- 最多只能有一个 `in_progress`。

任何一项不合法，整个更新失败，上一份有效计划保持不变。

## 内存状态

```python
CURRENT_TODOS: list[TodoItem] = []
```

计划保存在当前 Python 进程内，不写磁盘。退出 s05 后任务列表消失；后续 s12 的
Task System 才会引入文件持久化、依赖关系和并发协调。

## Reminder

Agent Loop 记录连续多少轮没有调用 `todo_write`。达到三轮后，在下一次模型请求前
加入：

```xml
<reminder>Update your todos before continuing.</reminder>
```

调用 `todo_write` 会把计数器清零。该机制只是教学用的注意力提醒，不增加执行
能力，也不是 Claude Code 源码中的固定三轮策略。

## 启动

```bash
cd /data/projects/cc-harness-lab
python s05_todo_write/code.py
```

建议输入一个明确的多步骤任务，例如：

```text
Create a small Python package with a function, tests, and a README, then run the tests.
```

观察：

1. 模型是否先调用 `todo_write`；
2. 是否只有一个任务处于 `in_progress`；
3. 工作推进时是否把任务更新为 `completed`；
4. 最终回答前是否完成验证任务。
