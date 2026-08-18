# s07：Skill Loading

这一章在 s06 的基础上增加按需技能加载。技能不是新的执行权限，而是一组写在
`SKILL.md` 中的专业说明；Agent 仍然只能通过已有工具行动。

## 两级加载

启动时，Harness 扫描 `skills/*/SKILL.md`，只解析 YAML frontmatter 中的 `name`
和 `description`，把紧凑目录加入父 Agent 和子 Agent 的 system prompt：

```text
Available skills:
- code-review: Review changed code for correctness, regressions, security, and tests.
```

完整正文不会在启动时进入上下文。只有模型调用：

```json
{"name": "code-review"}
```

`load_skill` 才从 `SKILL_REGISTRY` 返回完整 `SKILL.md`，并像其他工具结果一样追加到
当前消息历史。这就是“目录始终可见，正文按需付费”。

## 技能目录

```text
skills/
└── code-review/
    └── SKILL.md
```

清单格式：

```markdown
---
name: code-review
description: Review changed code for correctness and regressions.
---

# Code Review

这里是模型加载后才会看到的完整流程。
```

扫描只接受技能根目录的直接子目录及其中的 `SKILL.md`。符号链接解析后如果逃出技能
根目录会被忽略。`load_skill` 接受注册名称而不是文件路径，因此不能用 `../` 任意
读取文件。扫描在进程启动时执行一次；新增技能后需要重启 s07。

## 工具边界

父 Agent 有八个工具：

```text
bash, read_file, write_file, edit_file, glob, todo_write, task, load_skill
```

子 Agent 可以使用五个基础工具和 `load_skill`，但仍然没有 `todo_write` 与 `task`。
父子加载技能时都经过相同的 `PreToolUse` 和 `PostToolUse` Hook。

## 启动

```bash
cd /data/projects/cc-harness-lab
python3 s07_skill_loading/code.py
```

默认扫描当前工作目录下的 `skills/`。也可以显式指定：

```bash
CC_SKILLS_DIR=/path/to/skills python3 s07_skill_loading/code.py
```

建议依次尝试：

```text
What skills are available? Do not load any skill yet.
Load the code-review skill and summarize its checklist.
Review the current changes; load the relevant skill first.
```

观察 system prompt 中是否只有目录，调用 `load_skill` 后工具结果中是否才出现完整正文。
