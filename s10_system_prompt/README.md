# s10：System Prompt

s10 在 s09 的完整能力上，把硬编码 System Prompt 改成按运行时状态组装的命名
section，并在状态不变时复用缓存结果。

- 官方教程：[s10 System Prompt](https://learn.shareai.run/zh/s10/)
- 官方源码：[s10_system_prompt/code.py](https://github.com/shareAI-lab/learn-claude-code/blob/main/s10_system_prompt/code.py)
- 上一章：[s09 Memory](../s09_memory/)

## 本章解决的问题

工具、Skill、Memory 和 workspace 都会变化。把这些内容写死在一个长字符串里，会造成：

- Prompt 内容与真实工具注册不一致；
- 没有 Memory 或 Skill 时仍携带无效说明；
- 修改一个能力时容易破坏其他 Prompt 约束；
- 无法判断一次 Prompt 变化来自哪个运行时事实。

本章新增 `harness/prompt.py`，把 Prompt 拆成：

| Section | 加载方式 | 来源 |
| --- | --- | --- |
| identity | 始终加载 | 父 Agent / SubAgent 身份 |
| guidance | 始终加载 | 累计行为边界 |
| tools | 始终加载 | 实际 Tool Schema 注册表 |
| workspace | 始终加载 | `Settings.workdir` |
| skills | 有 Skill 时加载 | `skills/*/SKILL.md` catalog |
| memory | 有索引时加载 | `.memory/MEMORY.md` metadata |

section 是否出现只看真实运行态，不通过用户消息里的关键词猜测。

## 缓存

`SystemPromptAssembler` 使用稳定 JSON 序列化生成 context key。工具、workspace、
Skill catalog 和 Memory catalog 全部不变时返回缓存；任意一项变化时重新组装。

这个缓存只避免 Python 重复拼接字符串，不等同于模型供应商的 API Prompt Cache。

## 与现有 Memory 的关系

Memory catalog 作为 metadata section 常驻 System Prompt；相关完整记忆仍由 s09 的
selection side-query 选择后注入最近用户消息。二者不会重复注入完整记录。

## 累计能力

父 Agent 保留 s01–s09 的九个工具：

`bash`、`read_file`、`write_file`、`edit_file`、`glob`、
`todo_write`、`task`、`load_skill`、`compact`。

SubAgent 只获得五个基础工具和 `load_skill`，不会递归委派或主动 Compact。

## 运行

```bash
python3 s10_system_prompt/code.py
```

观察启动时的 section 列表；创建 Skill 或 Memory index 后，下一模型轮次会自动刷新
Prompt。运行测试：

```bash
python3 -m pytest -q tests/test_s10.py
```

## 与官方实现的差异

官方章节使用三个工具的小型 kernel 专注讲 Prompt assembly。本项目继续累计 s01–s09，
因此 section 内容更完整；消息协议仍采用百炼 OpenAI-compatible 的
`tool_calls` / `role=tool`，而不是 Anthropic content blocks。
