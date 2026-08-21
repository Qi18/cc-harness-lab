# s10 源码剖析：运行时 System Prompt 相对 s09 做了哪些修改

> 配套 [README.md](./README.md) 阅读。本文基于 s09 单文件（1936 行）与 s10 harness
> 包（56 行入口 + 10 个模块共 1951 行）的对比，回答一个问题：
> **为了让 System Prompt 成为运行时状态的产物，harness 动了哪些地方，为什么这么动。**
> 行号对应各模块当前版本。

## 一、修改总览

s09 的全部能力（权限、Hooks、Todo、SubAgent、Skills、四层压缩、Memory）逐条保留。
s10 做了两件互相支撑的事：**Prompt 动态化** + **单文件拆包**。

| 类别 | 修改 | 位置 |
|---|---|---|
| 架构 | `code.py` 从 1936 行缩到 56 行，只做 CLI | [code.py](./code.py) |
| | 新增 `harness/` 包：agent / prompt / tools / memory / compaction / hooks / skills / config / models | `harness/*.py` |
| Prompt 子系统 | `SystemPromptAssembler`、四个身份/边界常量、`registered_tool_names` | prompt.py L9-96 |
| 组装接入 | 父/子两个 assembler 实例、`_prompt_context`、`refresh_system_prompts` | agent.py L59-95 |
| 执行逻辑 | `agent_loop` 每轮刷新 system message | agent.py L213, L228 |
| 删除 | s09 的 `BASE_SYSTEM_PROMPT` / `build_system_prompt()` / 模块级 `SYSTEM_PROMPT` 常量 | — |

`tests/test_s10.py` 9 个用例（已实测通过）。

## 二、s09 的 Prompt 是怎么来的

s09 把 Prompt 在**导入时**就定死：

```python
BASE_SYSTEM_PROMPT = ("You are a coding agent working in {WORKDIR}. …")   # 长字符串
SKILL_REGISTRY = scan_skills()                    # 进程启动扫一次
SYSTEM_PROMPT = build_system_prompt(BASE_SYSTEM_PROMPT)   # 模块级常量
```

只有 Memory 索引是动态的——`build_memory_system_prompt()` 每轮把 `MEMORY.md`
拼到这个常量尾部。这带来四个具体问题（README 里列了，这里给出对应的代码原因）：

| 问题 | s09 的成因 |
|---|---|
| Prompt 与真实工具不一致 | 工具清单写在散文里，`TOOLS` 列表改了 Prompt 不会跟着改 |
| 没有 Skill 仍带 Skill 说明 | `build_system_prompt` 无条件拼接 `Available skills:`，空时是 `(no skills found)` |
| 改一个能力容易碰坏其他约束 | 所有行为边界挤在一个长字符串里，没有边界 |
| 无法判断一次变化来自哪 | Prompt 是不透明字符串，没有"哪些 section 生效"的可观测量 |

还有一条 s09 的隐性限制：`SKILL_REGISTRY` 只在导入时扫一次，**运行中新建的 Skill
不会被发现**。

## 三、SystemPromptAssembler：命名 section

`assemble()`（prompt.py [L53](./harness/prompt.py)）把 Prompt 拆成 `(名字, 正文)`
的有序列表，前四段无条件生成、后两段按运行态决定：

```python
sections = [
    ("identity",  self.identity),
    ("guidance",  self.guidance),
    ("tools",     f"Available tools: {tools}."),
    ("workspace", f"Working directory: {context['workspace']}"),
]
if skill_catalog: sections.append(("skills",  …))
if memory_catalog: sections.append(("memory", …))
```

两个关键点：

**① 工具清单来自注册表本身**。`registered_tool_names`（[L26](./harness/prompt.py)）
直接从 tool schema 里读 `function.name`，按注册顺序返回。Prompt 里说有什么工具，
就是模型实际拿到什么工具——**不可能声称一个未注册的工具**。这是从"散文描述"变成
"派生事实"。

**② 条件 section 只看真实状态**。`_prompt_context`（agent.py [L69](./harness/agent.py)）
里 `skill_catalog` 在 `self.skills.registry` 为空时给空串，`memory_catalog` 直接取
`memory.read_memory_index()`（无索引时返回空串）。于是没有 Skill 的工作区不会看到
`(no skills found)` 这种噪声，Prompt 短且全是有效信息。

`last_sections` 记录本次实际生成了哪几段（[L81](./harness/prompt.py)），把"这次
Prompt 由哪些运行时事实构成"变成**可断言的可观测量**——测试和排查都靠它。

## 四、缓存与 context key

`get()`（[L84](./harness/prompt.py)）是唯一对外入口：算 key、命中就返回缓存、
未命中才 `assemble()`，并分别累加 `cache_hits` 和 `assembly_count`。

key 的生成方式是刻意的（[L44](./harness/prompt.py)）：

```python
json.dumps(context, sort_keys=True, ensure_ascii=False,
           separators=(",", ":"), default=str)
```

- **`sort_keys=True`**：dict 顺序不影响 key；
- **`separators`**：去掉空格，key 更短；
- **`default=str`**：`Path` 之类非 JSON 类型不会抛错；
- **不用 `hash()`**：Python 的 `str.__hash__` 受 `PYTHONHASHSEED` 影响，**进程间不
  稳定**。这里的 key 只在进程内比较，用 hash 也能工作，但确定性 JSON 让缓存行为可
  复现、可断言，测试里能直接比对 key 字符串。

README 里明确划了边界：**这个缓存只避免 Python 重复拼字符串，不等于供应商的 API
prompt cache**。省下的是几十微秒的字符串拼接，不是 token 费用。

### 缓存省不掉磁盘扫描

值得注意的是 `refresh_system_prompts`（agent.py [L79](./harness/agent.py)）第一行：

```python
self.skills.registry = self.skills.scan()
```

**每轮都重新扫盘**，然后才算 key。所以：

- 好处：运行中新建的 Skill 会被下一轮自动发现——修掉了 s09 "扫一次定终身"的限制；
- 代价：缓存位于扫描**下游**，`scan()` 的目录遍历和 `SKILL.md` 读取每轮都会发生，
  缓存只挡住了后面的字符串拼接。Skill 目录很大时这是每轮的固定 IO。

同理 `memory.read_memory_index()` 每轮读一次 `MEMORY.md`。两者内容不变时 key 不变、
命中缓存。

## 五、父子双 assembler

```python
self.parent_prompt = SystemPromptAssembler(PARENT_IDENTITY, PARENT_GUIDANCE)
self.sub_prompt    = SystemPromptAssembler(SUBAGENT_IDENTITY, SUBAGENT_GUIDANCE)
```

两个**独立实例**，各自持有自己的缓存，并用不同的工具列表生成 context
（agent.py L84-89：`PARENT_TOOLS` vs `SUB_TOOLS`）。

这不只是文案差异。父 Agent 的 guidance 提到 `task`、`todo_write`、compact 摘要；
子 Agent 的 identity 明写 "do not delegate"。如果共用一个 assembler，子 Agent 的
Prompt 里就会出现它**根本没有的工具**的使用说明——模型会尝试调用不存在的工具，
拿到 `Error: unknown tool`。独立实例 + 独立工具列表让"Prompt 描述"和"实际能力"
在父子两侧都严格对齐。

顺带一提，两个 assembler 也让缓存互不干扰：父 Prompt 变化不会让子 Prompt 失效。

## 六、refresh 的四个调用点

| 位置 | 时机 | 传 `messages` 吗 |
|---|---|---|
| agent.py L67 | `__init__` 末尾 | 否——只填 `self.system_prompt` |
| agent.py L213 | `agent_loop` 开头 | 是 |
| agent.py L228 | **每轮循环内**，模型调用前 | 是 |
| agent.py L144 | `spawn_subagent` 开头 | 否——子 Agent 自建 messages |

传了 `messages` 时（[L90-95](./harness/agent.py)）会**就地改写** `messages[0]`；
首条不是 system 就 `insert(0, …)`。两个设计细节：

1. **只动 system message**，绝不改写历史 user/tool 记录——避免与 s09 的召回注入
   （改最后一条 user）和 s08 的压缩（重建整个列表）互相踩踏；
2. **每轮都刷**（L228）而不是每 turn 一次，因为一个 turn 内 Prompt 依赖的状态可能
   变化：模型用 `write_file` 新建了一个 `SKILL.md`，下一轮就该看到它。

## 七、与 Memory 的分工

s10 没有改变 s09 的双路召回，只是把其中一路搬进了 section 体系：

```text
Memory 索引（metadata）→ memory section，常驻 System Prompt
相关记忆完整正文       → s09 的 side-query 选择后注入最近 user 消息
```

两者不会重复注入完整记录：section 里只有 `MEMORY.md` 的名称+描述，并附一句
"Relevant full records are recalled separately"（prompt.py L78）。

## 八、防御与权衡

- **Prompt 不能撒谎**：工具 section 由注册表派生，是本章最硬的一条不变量；
- **可观测**：`last_sections` / `assembly_count` / `cache_hits` 三个字段让"Prompt 为
  什么变了"可断言，不用打印整段字符串对比；
- **`invalidate()` 目前无调用方**（prompt.py L94）：预留给"外部强制重建"的场景，
  当前所有失效都由 context key 变化自动触发；
- **权衡：每轮扫盘**。换来 Skill 热发现，代价是每轮固定 IO。Skill 数量大时可以考虑
  加 mtime 判断，但那会引入"改了内容没改 mtime"的新问题；
- **权衡：section 顺序写死在 `assemble()` 里**。想调整顺序或插入新 section 必须改
  这个函数，没有注册机制。对当前 6 个 section 是合适的简化。

## 九、一句话总结

s10 把 System Prompt 从"导入时定死的长字符串"变成"运行时状态的纯函数"：
工具清单由注册表派生因而不可能失真、Skill 与 Memory section 按真实存在与否出现、
父子用独立 assembler 保证描述与能力对齐、确定性 JSON context key 让缓存可复现——
**Prompt 不再是需要人工同步的文档，而是 harness 当前状态的投影**。
