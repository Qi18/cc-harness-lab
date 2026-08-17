# s05 源码剖析：todo_write 的完整数据流

> 配套 [README.md](./README.md)（使用说明）阅读。本文从源码角度回答一个问题：
> **`todo_write` 的状态到底在哪里被修改，其余代码各自扮演什么角色。**
> 行号对应 `code.py` 当前版本。

## 一、结论先行

`todo_write` 在 `code.py` 里有 6 个接入点，但真正修改状态的只有一处：

```python
CURRENT_TODOS = normalized   # L377，全文件唯一的写入点
```

其余 5 处要么声明接口、要么引导模型行为、要么监督调用频率，全部不碰状态。
状态收敛于一点，修改只能走"校验 → 整体替换"这一条路。

## 二、六个接入点

| 位置 | 代码 | 角色 |
|---|---|---|
| L30 | `CURRENT_TODOS: list[TodoItem] = []` | **状态存放处**：模块级全局变量，进程内存，不落盘 |
| L58-60 | `class TodoItem(TypedDict)` | 数据结构：`content: str` + `status: str` |
| L67-69 | `SYSTEM_PROMPT` | **行为约定**：多步任务先写 todo；做之前标 `in_progress`，做完标 `completed` |
| L184-220 | `TOOLS` 中的 JSON Schema | **接口声明**：`enum` / `minLength` / `additionalProperties: false` 构成第一层校验 |
| L348-391 | `run_todo_write()` | **唯一的写路径**：第二层校验 + 原子替换 + 渲染 + 纠偏提示 |
| L400 | `TOOL_HANDLERS["todo_write"]` | 路由注册，`execute_tool` 按名分发 |

外加一处只读的监督逻辑：`agent_loop`（L585-626）的 reminder 计数器，见第五节。

## 三、run_todo_write 内部：校验 → 替换 → 渲染 → 纠偏

### 1. 两层校验，四条规则（L352-375）

JSON Schema 是第一层（模型侧约束），Python 校验是第二层（防模型或 API 不遵守
schema）。规则：

- `todos` 必须是 list；
- 每项的 key **恰好**是 `{"content", "status"}` —— 注意 L361 用
  `set(todo) != {"content", "status"}` 做精确匹配，白名单式拒绝模型自作主张
  加的 `id`、`priority` 等字段；
- `content` 非空字符串（strip 后）；
- `status` ∈ `{pending, in_progress, completed}`；
- 业务不变量：`in_progress` 最多一个，强制"一次只专注做一件事"。

错误消息都带 `todo {index}`，让模型知道第几项错了、能定向修正。

### 2. 原子替换（L377）

任何一项校验失败就提前 return 错误字符串，旧清单保持不动——不存在
"改了一半"的中间状态。整体替换（而非增量 diff）让语义保持简单：模型只需
维护完整清单，不用记 id。

### 3. 双通道输出（L378-391）

- **print 给人看**（L384）：勾选框风格的清单渲染（`✓` / `▸` / 空格）；
- **return 给模型看**（L387-391）：确认字符串，并在"有 pending 但没有
  in_progress"时附加一句
  `"; mark one pending task in_progress before acting"`。

后者是 harness 设计的经典技巧：不改 system prompt、不硬性拦截，在工具返回值
里注入轻量引导信号，让模型在循环中自我纠偏——成本最低的行为塑造手段。

## 四、完整调用链

```text
模型发起 tool_call "todo_write"
    → agent_loop 捕获（L628），重置 reminder 计数器（L626）
    → execute_tool 解析 JSON、跑 PreToolUse 钩子（L562-567）
    → TOOL_HANDLERS 查表 → run_todo_write(**payload)（L576）
    → 两层校验全部通过
    → CURRENT_TODOS = normalized（L377）★ 唯一修改点
    → print 渲染清单给用户（L384）
    → return 确认字符串（可能附带纠偏提示）
    → 作为 tool 消息回到对话，进入下一轮
```

## 五、Reminder：读状态的监督者

`agent_loop` 维护 `rounds_since_todo` 计数器（L587）：

```python
used_todo_write = any(tc.function.name == "todo_write" for tc in tool_calls)
rounds_since_todo = 0 if used_todo_write else rounds_since_todo + 1
```

连续 `TODO_REMINDER_ROUNDS`（3）轮没调用 `todo_write`，就在下一次模型请求前
注入一条 user 消息：

```xml
<reminder>Update your todos before continuing.</reminder>
```

它只读计数、不碰 `CURRENT_TODOS`，是纯粹的注意力提醒。

## 六、设计要点小结

1. **状态收敛**：单一全局变量 + 单一写入函数，排查任何清单异常只需要看
   `run_todo_write` 一个地方。
2. **校验即防御**：模型生成的参数一律视为不可信输入，schema（模型侧）与
   Python（harness 侧）双层校验。
3. **失败无副作用**：校验不过整体拒绝，旧计划完整保留。
4. **双通道输出**：print 服务用户体验，return 服务模型认知，内容各自裁剪。
5. **软性纠偏**：返回值提示 + 周期性 reminder，引导而非强制，把"保持计划
   新鲜"变成模型在循环里可以学到的行为。
