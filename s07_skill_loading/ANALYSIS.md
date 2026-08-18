# s07 源码剖析：skill loading 相对 s06 做了哪些修改

> 配套 [README.md](./README.md) 阅读。本文基于 s06/code.py 与 s07/code.py 的
> 逐行 diff（794 → 944 行，净增 150 行），回答一个问题：
> **为了引入按需技能加载，harness 动了哪些地方，为什么这么动。**
> 行号对应 s07/code.py 当前版本。

## 一、修改总览

s06 的全部机制（沙箱、权限、hooks、todo、subagent）原样保留，s07 叠加了
一条全新的"技能"子系统，可归为四类：

| 类别 | 修改 | 位置 |
|---|---|---|
| 新依赖/常量 | `import yaml`（解析 frontmatter） | L16 |
| | `SKILLS_DIR`（`CC_SKILLS_DIR` 可覆盖，默认 `WORKDIR/skills`） | L31-33 |
| 技能子系统 | `SkillRecord` TypedDict | L69-74 |
| | `parse_skill_frontmatter()` 解析 YAML 头 | L77-108 |
| | `scan_skills()` 启动时扫描注册表 | L121-166 |
| | `list_skills()` 渲染精简目录 | L169-179 |
| | `SKILL_REGISTRY = scan_skills()` 模块级单次扫描 | L182 |
| 提示词 | 原 prompt 改名 `BASE_*`，`build_system_prompt()` 拼接技能目录 | L185-198 |
| 工具面 | 新增 `load_skill` 工具 schema + `run_load_skill()` handler | L373-392, L568-578 |

配套地，仓库根目录新增 `skills/` 目录，附一个 `code-review` 示例技能。

## 二、核心思想：渐进式披露（Progressive Disclosure）

技能文件可能很长（步骤、示例、注意事项），全部塞进 system prompt 会白白
烧掉上下文。s07 把信息分成两级：

**第一级：启动目录（每个技能只占一行）**

`build_system_prompt()`（L185-196）把精简目录追加到 prompt：

```text
Available skills:
- code-review: Review changed code for correctness, regressions, ...
Use load_skill to read full instructions only when a skill applies.
```

注释里写得很直白："never full skill bodies"。模型启动时只知道**有什么
技能、各自干什么**——刚好够它做"要不要用"的判断。

**第二级：按需全文**

模型判断某技能适用 → 调 `load_skill(name)` → 完整 SKILL.md 作为工具结果
进入上下文。不用的技能永远不占 token。

这正是 Claude Code 的 Skill 机制的教学复刻：技能不是代码，是**按需注入的
提示词**；load_skill 的返回值就是一段"现学现用"的操作手册。

## 三、SKILL.md 的结构与解析

技能 = `skills/<目录名>/SKILL.md`，YAML frontmatter + Markdown 正文：

```markdown
---
name: code-review
description: Review changed code for correctness, ...
---

# Code Review
1. Read the task and inspect the relevant diff ...
```

`parse_skill_frontmatter()`（L77-108）刻意宽容：没有 frontmatter、YAML
语法错、类型不对，一律降级为空 dict 而不是抛异常——一个坏技能文件不该
拖垮整个 harness。字段缺失时逐级回退：

- `name` 缺失 → 用目录名；
- `description` 缺失 → `_first_content_line()` 取正文第一个非空行。

## 四、scan_skills：确定性与防御（L121-166）

启动时扫描一次，构建 `SKILL_REGISTRY`，几个细节值得注意：

1. **确定性**——`sorted(skills_dir.iterdir(), key=...)` 按目录名排序，
   每次启动目录顺序一致，prompt 可复现；
2. **只认一层**——只扫 `skills_dir` 的**直接子目录**里的 `SKILL.md`，
   不递归，结构可预测；
3. **符号链接防御**——`resolved_manifest.relative_to(skills_root)`：
   manifest 若是指向技能目录外的软链接，resolve 后会暴露真实位置并被
   跳过，与 s02 `safe_path` 同一套思路；
4. **单文件失败不扩散**——读不了、解析不了就 `continue`，跳过该技能。

**一个已知的权衡**：`content` 在扫描时就全文读进内存（L160），运行中
修改 SKILL.md 不会生效（注册表不重扫）。教学版换取简单性；生产实现
通常在 load 时再读盘。

## 五、run_load_skill：按名查表，不收路径（L568-578）

```python
skill = SKILL_REGISTRY.get(name.strip())
```

接口设计上最关键的一点：**参数是注册表里的名字，不是文件路径**。模型
无法通过 `load_skill("../../etc/passwd")` 之类的输入读任意文件——名字
查不到就报错，且错误信息附上可用技能列表（又一次"错误是模型的纠错
信号"）：

```text
Error: skill not found: xxx. Available: code-review
```

## 六、父子代理均可用技能

`load_skill` 注册进的是 `SUB_TOOL_HANDLERS`（L585），而
`SUB_TOOLS = TOOLS − {todo_write, task}` 也保留了它；`SUB_SYSTEM` 同样由
`build_system_prompt(BASE_SUB_SYSTEM)` 生成、带技能目录。

也就是说与 s06 的"能力递减"不同，技能对子代理**全量开放**——技能只是
知识注入，不引入新的行动能力，不需要像 `task`（防套娃）那样裁剪。工具
分级的标准由此更清晰：**裁剪的是能力，不裁剪知识**。

## 七、完整调用链

```text
启动：scan_skills() → SKILL_REGISTRY（一次性，含全文缓存）
      build_system_prompt() → 目录进入父/子 system prompt

会话中：
模型看到目录，判断某技能适用
    → tool_call load_skill({"name": "code-review"})
    → PreToolUse 钩子照常 → run_load_skill 查注册表
    → 完整 SKILL.md 文本作为 tool 消息进入上下文
    → 模型按技能正文的步骤执行任务
```

## 八、设计要点小结

1. **两级披露**：目录常驻（一行/技能），全文按需——上下文经济学的又一
   次应用，与 s06 subagent（吞掉中间过程）互补；
2. **技能即提示词**：load_skill 不执行任何代码，只返回文本，安全面
   没有扩大；
3. **按名不按路**：注册表白名单式查找，杜绝路径穿越；
4. **宽容解析 + 确定性扫描**：坏文件跳过、排序遍历，启动行为可预测；
5. **知识全量开放，能力分级裁剪**：子代理可以学技能，但仍不能规划和
   委派。
