# s06 源码剖析：subagent 相对 s05 做了哪些修改

> 配套 [README.md](./README.md) 阅读。本文基于 s05/code.py 与 s06/code.py 的
> 逐行 diff（671 → 794 行，净增 123 行），回答一个问题：
> **为了引入同步子代理，harness 动了哪些地方，为什么这么动。**
> 行号对应 s06/code.py 当前版本。

## 一、修改总览

s05 的全部机制（safe_path 沙箱、权限管道、hooks、todo_write、reminder）原样
保留，s06 在其上叠加了 9 处修改，可归为四类：

| 类别 | 修改 | 位置 |
|---|---|---|
| 新常量/全局 | `MAX_SUBAGENT_ROUNDS = 30` | L27 |
| | `ACTIVE_CLIENT: OpenAI \| None = None` | L32 |
| 提示词 | `SYSTEM_PROMPT` 增加两句委派指引 | L69-70 |
| | 新增 `SUB_SYSTEM` 子代理系统提示 | L76-81 |
| 工具面 | 新增 `task` 工具 schema 并注册 | L235-257 |
| | 派生 `SUB_TOOLS`（裁剪版工具列表） | L258-262 |
| | handler 表一拆二：`SUB_TOOL_HANDLERS` / `TOOL_HANDLERS` | L433-439, L495-499 |
| 执行逻辑 | 新增 `spawn_subagent()` 子代理循环 | L442-492 |
| | `execute_tool` 重构为 `execute_tool_with_handlers` | L650-703 |

## 二、核心新增：spawn_subagent（L442-492）

`task` 工具的 handler，本质是**一个缩小版的 agent_loop**：

```python
messages = [
    {"role": "system", "content": SUB_SYSTEM},
    {"role": "user", "content": description},
]
```

关键设计：

1. **上下文隔离**——子代理从全新的 messages 列表起步，看不到父代理的任何
   历史；父代理也只拿到子代理的最后一条文本结论（L474-476）。中间几十轮
   工具调用产生的观察值全部留在子代理的局部变量里，随函数返回被丢弃。
   这就是 subagent 的价值：**用一句结论换掉一大段上下文消耗**。
2. **轮次上限**——`for _ in range(MAX_SUBAGENT_ROUNDS)`（30 轮）。注意父循环
   是 `while True` 没有上限，子循环却有：父代理有人看着（交互式），子代理
   是无人值守的黑盒，必须有失控保险。超限返回错误字符串，让父代理接手。
3. **无规划、无提醒、无 Stop 钩子**——子循环没有 todo reminder 计数器，也不
   触发 `Stop` 钩子。规划是父代理的职责，子代理只管执行。

## 三、能力裁剪：两级工具面（L258-262, L433-499)

```python
SUB_TOOLS = [t for t in TOOLS if t["function"]["name"] not in {"todo_write", "task"}]
```

| | 父代理 | 子代理 |
|---|---|---|
| bash / read / write / edit / glob | ✓ | ✓ |
| todo_write | ✓ | ✗（计划归父代理管，避免两份清单打架） |
| task | ✓ | ✗（禁止递归委派，防止无限套娃） |

裁剪做了**双保险**：`SUB_TOOLS` 让模型在 schema 层看不到这两个工具；
`SUB_TOOL_HANDLERS` 让即使模型幻觉调用也查不到 handler（返回
`unknown tool`）。`SUB_SYSTEM` 里的 "Do not delegate" 则是第三层提示词约束。

`TOOL_HANDLERS = {**SUB_TOOL_HANDLERS, "todo_write": ..., "task": ...}`
（L495-499）——父表由子表扩展而来，共享的 5 个 handler 只定义一次。

## 四、执行器重构：execute_tool_with_handlers（L650-703）

s05 的 `execute_tool` 硬编码查 `TOOL_HANDLERS`；s06 把它泛化为参数化版本：

```python
def execute_tool_with_handlers(name, arguments, handlers, display_prefix=""):
```

原有的解析→PreToolUse→分发→PostToolUse 流水线一行未改，只是：

- `handlers` 变成参数 → 父/子各传自己的表；
- `display_prefix` → 子代理的工具调用打印成 `[sub] bash {...}`，终端上能
  区分是谁在干活。

然后 `execute_tool` / `execute_sub_tool`（L691-703）成为两个薄封装。
**重要推论：父子共用同一条 hook 链**——`permission_hook`（deny list、
路径检查、人工确认）对子代理的每次工具调用同样生效，子代理不是安全后门。

## 五、ACTIVE_CLIENT：一个务实的妥协（L32, L708-709）

```python
global ACTIVE_CLIENT
ACTIVE_CLIENT = client   # agent_loop 入口处赋值
```

为什么需要它：`TOOL_HANDLERS` 里所有 handler 的签名都是
`handler(**payload)`——参数只能来自模型生成的 JSON。`spawn_subagent`
需要 OpenAI client 才能发请求，但 client 不可能（也不应该）由模型传入，
于是用模块级全局变量把 client "旁路"进去。

这是教学代码的务实选择；工程化的做法通常是把 handler 收进一个持有 client
的类，或用闭包/partial 注入依赖。

## 六、提示词的配套修改

**父代理**（L69-70）新增两句：

- "Use task to delegate a complex, self-contained subtask when isolated
  context helps." —— 何时委派：复杂、自包含、隔离上下文有收益；
- "Check the subagent's conclusion and verify any filesystem changes
  yourself." —— **不轻信**：子代理的结论要亲自验证，这是父子协作的
  信任边界。

**子代理**（`SUB_SYSTEM`，L76-81）三个关键词：focused（只做被指派的事）、
"Do not delegate"（不准再委派）、"return only a concise conclusion"
（返回值就是父代理的观察值，必须精炼）。

## 七、完整调用链

```text
父模型发起 tool_call task({"description": "..."})
    → execute_tool → PreToolUse 钩子（权限检查照常）
    → TOOL_HANDLERS["task"] = spawn_subagent(description)
        → 新建隔离 messages（SUB_SYSTEM + description）
        → 循环 ≤30 轮：请求模型（SUB_TOOLS）→ execute_sub_tool
          （同一条 hook 链，打印带 [sub] 前缀）
        → 模型停止调工具 → 返回最终结论字符串
    → 结论作为 task 的工具结果回到父对话
    → 父代理验证结论，继续自己的 todo 推进
```

## 八、设计要点小结

1. **隔离即价值**：子代理用独立 message 历史吞掉中间过程，父代理上下文
   只增加一条结论——这是 subagent 机制存在的根本理由。
2. **能力递减**：child = parent − 规划 − 委派，三层实施（schema 不可见、
   handler 查不到、提示词声明）。
3. **安全面不变**：父子共用同一条 hook/权限链，委派不产生特权逃逸。
4. **失控保险不对称**：交互式的父循环无上限，无人值守的子循环硬性 30 轮。
5. **同步阻塞**：`spawn_subagent` 是普通函数调用，父代理等子代理跑完才
   继续——并发要等后续章节。
