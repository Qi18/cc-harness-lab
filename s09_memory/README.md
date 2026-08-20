# s09：Durable Memory

这一章在 s08 的上下文压缩之上增加跨 Compact、跨进程会话的长期记忆。Compact
解决“当前会话继续做什么”，Memory 解决“未来会话仍应知道什么”。s08 已有的权限、
Hooks、TodoWrite、SubAgent、Skills 和九个父 Agent 工具全部保留。

## 生命周期

```text
用户请求
  → MEMORY.md 索引进入 system prompt
  → side-query 从目录中选择最多 5 条相关记忆
  → 完整记忆临时附加到当前 user turn
  → s08 分层 Compact + Agent Loop
  → 最终回答后提取新的持久事实
  → 文件达到阈值时合并去重
```

索引只有名称、文件名和描述，体积小且每轮常驻；完整正文只在相关时加载，单轮总预算
为 20,000 字符。召回内容带 `<relevant-memories>` 边界，并明确标记为背景知识，不能
覆盖当前用户请求。

## 文件仓库

默认存储在工作目录的 `.memory/`：

```text
.memory/
├── MEMORY.md
├── user-preference-tabs.md
└── project-deploy-policy.md
```

每条记录是带 YAML frontmatter 的 Markdown：

```markdown
---
name: user-preference-tabs
description: User prefers tabs for indentation
type: user
---

Use tabs instead of spaces when editing code.
```

支持四种类型：

- `user`：稳定的用户偏好
- `feedback`：反复适用的做事方式
- `project`：稳定的项目背景与约束
- `reference`：未来仍有用的入口和定位信息

Memory 文件路径只能是 `.memory/` 的直接子文件；`CC_MEMORY_DIR` 即使自定义，也必须
位于 `CC_WORKDIR` 内，避免长期存储绕过工作区边界。

## 召回

每个用户 turn 开始时，Harness 把最近请求和记忆目录发送给一次无工具 side-query，
模型只返回目录下标。解析失败或 API 暂时不可用时，自动降级为 name + description
关键词匹配。选中的文件经过路径校验后读取，最多 5 条、总计不超过 20,000 字符。

## 提取

只有主 Agent 真正准备返回最终答案时才提取；Stop Hook 要求继续时不会提前写入。
提取输入使用本 turn 的独立快照，因此即使 s08 在执行中 Compact，也不会只剩有损
摘要。Harness 还会移除自己注入的 recall 块，避免把旧记忆重新提取一遍。

候选记录必须同时满足：

- 类型合法且字段完整
- 模型明确标记 `scope=persistent`
- 不包含“本次任务”“当前会话”等临时语义
- 不与已有名称、描述或正文重复
- 不匹配 API Key、access token、secret 等敏感信息模式

模型判断负责语义筛选，Harness 校验负责确定性安全边界。提取失败只打印提示，不影响
主回答。

## 整理

记录达到 10 条后触发 consolidation：模型合并重复项、应用较新的修正并删除过时内容，
最终最多保留 8 条，避免整理后下一轮立即再次达到阈值。替换前会保存内存快照；任一
文件写入失败都会恢复原记录和索引。

## 与 Compact 的关系

```text
Session Memory / Compact  → 同一会话的任务连续性
Durable Memory            → 跨会话的偏好和稳定知识
```

主动或自动 Compact 不会删除 `.memory/`。相关 Memory 会在下一次用户请求时重新选择，
所以长期事实不依赖摘要是否完整。

## 与官方代码的区别

对照资料：

- [s09 Memory 官方教程](https://learn.shareai.run/zh/s09/)
- [s09 Memory 官方源码](https://github.com/shareAI-lab/learn-claude-code/blob/main/s09_memory/code.py)

功能方向与官方一致，但本目录不是官方 s09 的逐行移植。官方代码是突出 Memory
机制的最小教学切片；这里是在 s01–s08 完整 Harness 上继续累计演进，并适配阿里云
百炼的 OpenAI-compatible 协议。

| 维度 | 官方 s09 | 本仓库 s09 |
| --- | --- | --- |
| 代码规模 | 约 757 行 | 约 1930 行，包含前八章能力 |
| 模型协议 | Anthropic SDK、content blocks | 百炼 OpenAI-compatible、`role=tool` |
| 工具 | 5 个基础工具 | 父 Agent 9 个、子 Agent 6 个 |
| 前序能力 | 精简的工具、权限和 Hook 示例 | 权限、Hooks、Todo、SubAgent、Skills、Compact 全部保留 |
| Memory 索引 | 注入 system prompt | 注入 system prompt |
| 完整相关记忆 | 一并放入 system prompt | 临时附加到当前 user turn |
| 提取输入 | 当前 messages | 独立 turn 快照，不受 Compact 丢失细节影响 |
| 整理目标 | prompt 要求最多保留 30 条 | Harness 强制最多保留 8 条 |
| 敏感信息 | 主要依靠提取 prompt | prompt + 常见 API Key/Token/Secret 确定性过滤 |
| 测试 | 官方教学实现 | 14 个 s09 单测，并参加全仓库回归 |

### 保留完整 Harness

官方 s09 当前只声明 `bash`、`read_file`、`write_file`、`edit_file` 和 `glob`。
本实现继续提供 `todo_write`、`task`、`load_skill`、`compact`，并保留 s08 的四层
压缩、reactive compact、transcript 和大工具结果落盘。因此新增代码的主体不全是
Memory；较大的文件规模主要来自前八章累计能力。

### Memory 的注入位置

官方 `build_system()` 把索引和完整相关记忆都组装进 system prompt。本实现只把
稳定、紧凑的 `MEMORY.md` 索引放进 system；完整正文使用
`<relevant-memories>` 标记附加到最新 user turn，并声明它只是背景数据，当前用户请求
始终具有更高优先级。

这有利于保持 system 主体稳定，也让召回内容与系统规则分区；代价是会临时改写当前
user message。因此 Harness 额外保存 `active_request`，并在提取前通过
`without_recalled_memory()` 删除召回块，避免旧 Memory 被再次提取。

### 与 Compact 的组合

官方精简 s09 没有包含 s08 的 `ContextCompactor`。本实现进入 Agent Loop 时建立
`extraction_messages` 独立快照，后续 assistant 和 tool result 同步追加。即使主消息
历史发生主动、自动或 reactive Compact，结束时的 Memory 提取仍能看到本 turn 的
原始细节，而不是只看到有损摘要。

### 安全和可配置性增强

除官方已有的路径校验、临时信息过滤和重复检查外，本实现还增加：

- `CC_MEMORY_DIR` 可配置，但解析后的目录必须位于 `CC_WORKDIR` 内；
- 只允许读取 `.memory/` 的直接子文件，拒绝路径穿越；
- 确定性拒绝常见 `sk-...`、`api_key=...`、`access_token=...` 和 `secret=...`；
- `.memory/` 默认被 Git 忽略，降低个人上下文被意外提交的概率；
- client 通过函数参数传入，而不是依赖全局 Anthropic client，便于测试和替换模型。

敏感信息正则只是第二道防线，不是完整 Secret Scanner。不规则凭据、客户数据和其他
隐私内容仍可能被模型提取，因此应定期审阅 `.memory/`。

### Consolidation 行为

两者都在记录数达到 10 条时允许整理，并在写入失败时恢复快照。官方 prompt 要求最多
保留 30 条，而且只在当前 turn 成功提取新记录后调用整理。本实现每个正常结束的 turn
都会检查阈值，并把模型返回结果强制截断为最多 8 条，使成功整理后立即低于触发阈值。

这种做法更容易稳定控制文件数量；但如果整理持续失败，达到阈值后每轮都会重试，
可能增加一次模型调用。后续可增加失败退避、时间间隔和文件锁，向真实 Claude Code
的 Dream 门控靠近。

### 当前已知差异

- 官方临时记忆标记覆盖英文、中文和日文；本实现目前只覆盖英文和中文。
- 官方重建索引时，手写 Memory 缺少 `description` 会用正文第一行兜底；本实现通过
  `write_memory_file()` 创建时强制 description 非空，但手工写入的不完整文件会显示
  空描述。

## 启动

```bash
cd /data/projects/cc-harness-lab
python3 s09_memory/code.py
```

可分多轮验证：

```text
I prefer tabs for indentation. Remember this preference.
Create a Python file using my preferred indentation.
What indentation preference did I tell you?
```

观察 `[Memory: stored ...]`、`[Memory: recalled ...]`，以及 `.memory/MEMORY.md`
是否更新。`.memory/` 默认被 Git 忽略，因为它可能包含个人或项目私有上下文。
