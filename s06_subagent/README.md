# s06：Subagent

这一章在 s05 的 TodoWrite 之上增加同步子 Agent。父 Agent 调用 `task` 后，Harness
创建一份全新的消息历史，让子 Agent 独立完成一个明确子任务。子循环结束时只把最终
结论返回给父 Agent；读文件、写文件和运行命令产生的工作区副作用会保留。

## 上下文隔离

子 Agent 的初始消息只有两条：独立的 `SUB_SYSTEM` 和父 Agent 给出的
`description`。父对话里积累的推理、工具结果和 TODO 不会复制进来，子循环的中间
消息也不会回传给父 Agent。

## 工具边界

父 Agent 有七个工具：

```text
bash, read_file, write_file, edit_file, glob, todo_write, task
```

子 Agent 只有前五个基础工具，不包含 `todo_write` 和 `task`，因此不能递归创建子
Agent。子工具仍通过相同的 `PreToolUse` 和 `PostToolUse` Hook，权限检查不会因上下文
隔离而跳过。终端用 `[sub]` 标识子 Agent 的工具调用。

## 同步执行和轮数限制

`task` 是同步工具：父循环会等待子循环结束。子循环最多请求模型 30 轮，防止模型
持续调用工具而无法收敛。完成时终端显示 `[Subagent spawned]` 和
`[Subagent done]`，工具结果只留在子上下文，父 Agent 只收到最后的文本结论。

## 启动

```bash
cd /data/projects/cc-harness-lab
python s06_subagent/code.py
```

建议输入：

```text
Use a subtask to find what testing framework this project uses, then verify the conclusion.
```

也可以让子 Agent 创建文件，再让父 Agent 读取文件验证，从而观察“消息隔离、文件
共享”的边界。

## 从 s05 继承的 TodoWrite

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

计划保存在当前 Python 进程内，不写磁盘。退出 s06 后任务列表消失；后续 s12 的
Task System 才会引入文件持久化、依赖关系和并发协调。

## Reminder

Agent Loop 记录连续多少轮没有调用 `todo_write`。达到三轮后，在下一次模型请求前
加入：

```xml
<reminder>Update your todos before continuing.</reminder>
```

调用 `todo_write` 会把计数器清零。该机制只是教学用的注意力提醒，不增加执行
能力，也不是 Claude Code 源码中的固定三轮策略。

## TodoWrite 观察方式

```bash
cd /data/projects/cc-harness-lab
python s06_subagent/code.py
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
