# CC Harness 评测方案

> 状态：规划中。当前先固化评测设计；完成 s01–s20 和 Comprehensive Agent 后再实现
> `evals/`、采集 baseline，并接入持续评测。

## 评测目标

公开 Agent Benchmark 通常测量的是“模型 + Prompt + Harness + 工具 + 环境”的组合，
不能把模型升级带来的收益直接归因于 Harness。本项目将评测拆成两部分：

1. **Harness conformance**：协议、状态机、权限、隔离、Compact、Memory、Task 和后台
   任务等机制是否满足不可违反的约束。
2. **Model + Harness capability**：固定模型后，完整 Agent 能否稳定、安全、低成本地
   完成真实任务。

评测以环境最终状态为主，例如实际文件、测试结果、任务状态和进程状态；Agent 最后的
文字声明不能单独作为成功证据。轨迹主要用于定位失败、检查策略和衡量效率。

## 评测分层

### L0：确定性机制测试

使用 fake model 和固定工具输出，覆盖所有协议与状态机约束。该层不访问真实模型，
每次提交都执行，目标是 100% 通过。

重点约束：

- assistant tool call 与 `role=tool` 结果完整配对；
- 工具参数符合 schema，未知工具和异常结果不会破坏 Agent Loop；
- 权限和 Hook 在 handler 执行前生效，审批不能扩大 workspace 边界；
- SubAgent 不递归委派，不继承父 Agent 消息历史；
- Compact 不切断工具消息组；
- Memory 不保存 API Key、Token、密码等秘密；
- Task DAG 拒绝缺失依赖、自依赖和传递环；
- 后台结果不丢失、不重复通知，退出时不留下孤儿进程。

### L1：固定模型能力评测

在隔离的临时项目中运行真实模型，使用确定性 grader 检查最终状态。初期准备
20–50 个任务，每个任务至少运行 3 次；发布评测建议运行 5 次。

任务按难度和能力标签分桶：

- 5 分钟：单工具、单文件、单约束；
- 30 分钟：跨文件修改、测试与错误恢复；
- 2 小时：长上下文、多阶段任务、委派与后台任务。

### L2：可靠性与故障注入

对同一任务进行等义改写，并注入可恢复故障：

- API timeout、HTTP 429、短暂连接失败；
- 无效 JSON、部分工具输出、schema drift；
- 命令超时、后台进程异常退出；
- 文件在读取后发生变化；
- Compact 位于工具调用边界附近；
- Memory 文件缺失或损坏。

### L3：安全与对抗评测

覆盖路径穿越、符号链接逃逸、危险命令变形、权限绕过、工具输出中的间接 Prompt
Injection、秘密泄漏、Memory 污染、SubAgent 越权、后台进程逃逸和评测答案污染。

安全指标必须与正常任务效用同时报告，避免通过“拒绝所有工具”获得虚假的安全高分。

### L4：外部 Benchmark

- Terminal-Bench / Harbor：优先验证终端、文件和环境操作能力；
- SWE-bench：在 Git、代码搜索、测试恢复和完整编码工作流成熟后接入；
- AgentDojo：验证工具型 Agent 的 Prompt Injection 安全与正常效用；
- METR 风格时间分桶：衡量不同人类工作时长任务上的自主完成率。

## 核心结果指标

设 `y[i,j]` 表示任务 `i` 第 `j` 次运行是否成功，`N` 为任务数，`k` 为
每题重复次数。

| 指标 | 定义 |
| --- | --- |
| Strict Resolution Rate | 完全满足所有必要断言的任务数 / 总任务数 |
| Partial Completion | 已满足断言数 / 全部断言数 |
| FAIL_TO_PASS | 原失败测试变为成功的比例 |
| PASS_TO_PASS | 原成功测试继续成功的比例 |
| pass@1 | 第一次运行成功的任务比例 |
| pass@k | 每题 k 次中至少成功一次的任务比例 |
| pass^k | `sum(product(y[i,1:k])) / N`，衡量连续可靠成功 |
| Flaky Rate | k 次运行中既有成功又有失败的任务比例 |
| Intervention-free Rate | 无人工介入完成的任务比例 |

代码任务只有在 `FAIL_TO_PASS=1` 且 `PASS_TO_PASS=1` 时才记为 strict resolved。
如果 grader 支持部分分，应同时保留 partial reward，但不能用它替代严格成功率。

## Harness 专属指标

### 协议与工具

- Tool Schema Valid Rate
- Tool Call/Result Pair Integrity
- Tool Error Recovery Rate
- Duplicate Side-effect Rate
- Unknown Tool Recovery Rate
- Max-turn Correct Termination Rate

### 权限与安全

- Unauthorized Action Rate
- Permission Bypass Rate
- Permission Overblock Rate
- Path/Symlink Escape Rate
- Secret Leakage Rate
- Secret Persistence Rate
- Prompt Injection Attack Success Rate
- Benign Utility 与 Utility Under Attack

### Context Compact

- Compression Ratio = compact 后 token / compact 前 token
- Critical Fact Recall = 保留关键事实 / 标注关键事实
- Tool Pair Integrity after Compact
- Post-Compact Success Delta = 启用 Compact 成功率 - 禁用 Compact 成功率
- Reactive Compact Recovery Rate

### Memory

- Retrieval Precision = 相关记忆 / 注入记忆
- Retrieval Recall = 召回相关记忆 / 应召回记忆
- Memory Contamination Rate
- Secret Persistence Rate
- Cross-session Recall Success Rate

### SubAgent、Task 与 Background

- Delegation Success Rate 与 Result Handoff Accuracy
- Parent Context Saving 与 Delegation Cost
- Recursive Delegation Violation Rate
- DAG State Accuracy、Wrong Unlock Rate、Duplicate Claim Rate
- Background Non-blocking Return Latency
- Notification Loss/Duplicate Rate
- Timeout Cleanup Rate 与 Orphan Process Rate

## 效率与运行指标

- 每个成功任务的 input/output/total tokens；
- 每个成功任务的模型费用；
- P50/P95 wall-clock latency；
- P50/P95 模型轮数和工具调用数；
- 工具错误率、重试次数和无效重复读取次数；
- 上下文峰值、Compact 次数和压缩前后 token；
- SubAgent 数量、并发峰值和后台任务数量；
- Evaluation infrastructure failure rate。

费用和延迟不能脱离成功率单独优化，统一报告 `cost_per_solved_task` 和
`latency_per_solved_task`。

## Harness 贡献归因

固定模型、模型版本、system prompt、温度、预算、任务和环境，对 Harness 版本做成对
A/B：

```text
H0 = minimal agent loop
H1 = H0 + Compact
H2 = H1 + Memory
H3 = H2 + SubAgent
H4 = H3 + Task DAG
H5 = H4 + Background

Harness Uplift
  = success_rate(model_fixed, H_new)
  - success_rate(model_fixed, H_old)
```

再用至少两个不同模型重复相同实验。跨模型 uplift 的平均值用于判断 Harness 是否具有
普适收益，模型间差异用于识别协议或 Prompt 的特定适配。

每个 trial 必须从干净 workspace 开始，不能共享 `.memory`、`.tasks`、transcript、
Git 历史修改或后台进程。报告任务级结果和 95% 置信区间；版本比较使用相同任务的成对
结果，不能只比较两个独立平均数。

## 建议的评测数据结构

```text
evals/
├── cases/
│   ├── tool-use/
│   ├── permission/
│   ├── compact/
│   ├── memory/
│   ├── task-system/
│   ├── background/
│   └── coding/
├── fixtures/
├── graders/
│   ├── command_grader.py
│   ├── file_grader.py
│   ├── trace_grader.py
