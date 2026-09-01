# s11 源码分析：有界 Error Recovery

> 配套 [README.md](./README.md) 与 [CALLGRAPH.md](./CALLGRAPH.md) 阅读。
> 行号对应 `s11_error_recovery/` 当前版本：`code.py` 59 行，`harness/` 15 个模块
> 共 2462 行。

## 一、这一章真正新增了什么

s11 的新能力不是"给所有 SDK 调用套一层 try/except"，而是把失败变成一组**有上限、且不
破坏消息协议**的状态迁移：

```text
Exception / finish_reason
          ↓
error_recovery 分类（纯函数，不碰 messages）
          ↓
RecoveryState（本 turn 内计数）
          ↓
agent_loop 选择：重试 / 压缩 / 追加 continuation / 收口
```

对比 s10 更能看清差别。s10 的循环里只有一个恢复分支：`is_prompt_too_long_error` 命中
后调用一次 `reactive_compact` 重试（s10/agent_loop.py L252-L263），其余异常直接穿透
到 `code.py`，交互式会话随进程一起结束。输出截断在 s10 完全没有处理——`max_tokens` 甚
至没有出现在请求里。

s11 把四类失败统一成矩阵，每一格都有明确上限：

| 失败类型 | 分类函数 | 恢复动作 | 上限常量 |
| --- | --- | --- | --- |
| 429 | `is_rate_limit_error` error_recovery.py L53 | 退避重试 | `MAX_TRANSIENT_ATTEMPTS`=10 L15 |
| 529 | `is_overloaded_error` L64 | 退避 + fallback | `MAX_CONSECUTIVE_529`=3 L18 |
| 输出截断 | `is_output_truncated` L92 | 8K→64K，再 continuation | `MAX_CONTINUATIONS`=3 L14 |
| Prompt 过长 | `is_prompt_too_long_error` L75 | reactive compact | `has_attempted_reactive_compact` L39 |
| 其他 | —— | 收口为 assistant 错误 | 不重试 |

## 二、为什么分成纯策略层与状态机层

SDK 只能告诉调用方"请求失败"或"输出停止"，它不知道这段响应能不能写进 `messages`。只有
Agent Loop 同时掌握三件事：当前响应是否完整、历史是否已经做过 reactive compact、这次
改写会不会拆断 assistant/tool 协议组。

因此代码按"知不知道 messages"划界：

```text
error_recovery.py（193 行，零内部导入）      agent_loop.py
分类 · 计数 · 退避 · fallback 切换      →   决定重试、压缩、追加消息或停止
```

`error_recovery.py` 不导入任何 harness 模块，因此位于依赖图最底层，与 `config.py`、
`models.py`、`system_prompt.py` 同级。副作用只有两处 `print`（L158、L176），没有 IO、
没有全局状态。这也让测试可以把 `sleep_fn` 与 `random_fn` 换成确定函数
（test_s11.py L362-L363），精确断言退避序列而完全不等待。

## 三、RecoveryState 的 turn 级语义

`RecoveryState`（error_recovery.py L30-L39）六个字段全部是"本 turn 已经用掉哪些恢复
额度"：

| 字段 | 含义 | 谁修改 |
| --- | --- | --- |
| `current_model` | 本次请求链使用的模型 | `with_retry` L157 |
| `max_tokens` | 当前输出预算，初值 8000 | `agent_loop` L325 |
| `has_escalated` | 是否已 8K→64K | `agent_loop` L326 |
| `continuation_count` | 已追加的续写次数 | `agent_loop` L361 |
| `consecutive_529` | 连续过载次数 | `with_retry` L144、L154、L165 |
| `has_attempted_reactive_compact` | 应急压缩是否用过 | `agent_loop` L307 |

实例在 `agent_loop()` 的计数器区创建（agent_loop.py L261），与 `rounds_since_todo`、
`manual_compactions` 并列，turn 返回即销毁。这个边界是有意的：fallback 是一次请求链的
降级，不是把 `Settings.model` 改掉；64K 是一次长回答的预算，不应让后续简单问题永久占用
更大上限。

## 四、瞬态错误重试

`with_retry()`（error_recovery.py L126）签名只接受 `fn(model)`。每次调用都把
`state.current_model` 传进去（L143），所以模型切换不需要改写 `Settings`，Provider 侧只
看到一个普通的 `model` 覆盖参数。

循环体的判定顺序值得逐条看：

1. **成功即清零**（L144）：`consecutive_529 = 0`，一次成功之后不再残留过载计数。
2. **非瞬态立即上抛**（L149-L150）：把鉴权失败、参数错误或安全拒绝当瞬态错误重试，既
   浪费配额，也会掩盖真实配置问题。
3. **529 累计并可能降级**（L153-L162）：计数达到 3 时若配置了 fallback 且当前不是
   fallback，就切换模型并把计数清零。切换发生在异常处理阶段，所以**下一次**请求才用
   fallback——测试断言的模型序列正是 `["primary", "primary", "primary", "fallback"]`
   （test_s11.py L456-L459）。
4. **429 打断 529 序列**（L165）：不能把不连续的过载拼成一次错误降级。
5. **最后一次不 sleep**（L167-L169）：`attempt + 1 >= max_attempts` 直接 break，避免一
   次没有后续请求的无效等待。

退避公式在 `retry_delay()`（L113-L123）：

```python
base = min(BASE_DELAY_SECONDS * (2**attempt), MAX_DELAY_SECONDS)   # 0.5 → 32
return base + random_fn(0.0, base * 0.25)
```

attempt 从 0 起，因此依次约 0.5、1、2、4、8、16、32 秒后保持 32 秒基线；jitter 减少多个
客户端同步重试造成的冲击。`retry_after_seconds()`（L98）能从异常或 response 头解析出秒
数时优先返回，且**不再叠加本地 jitter**（L120-L121）。尝试耗尽抛
`MaxRetriesExceeded`（L182），并用 `from last_error` 保留原始异常链。

## 五、输出截断状态机

```text
8K 请求
  ├─ 完整 → 正常处理 assistant / tool_calls
  └─ 截断 → 丢弃片段，预算提到 64K，原历史重放          agent_loop.py L323-L331
                ├─ 完整 → 正常处理
                └─ 截断 → 保存纯文本片段 + continuation   L333-L366
                              ├─ 完整 → 正常结束
                              └─ 截断 → 最多 3 次，之后返回最后片段  L340-L353
```

**第一次为什么必须丢弃片段？** 如果先写进历史再重放原请求，模型会同时看到半截回答和原
任务，容易重复输出。更严重的是截断点可能落在 JSON 工具参数中间：把不完整的 `tool_calls`
写回历史，就制造了一个永远配不上 `role=tool` 的协议组。测试对这一点有双重断言——片段既
不在最终 `messages` 里，也不在第二次请求实际发送的消息里（test_s11.py L510-L520）。

**64K 后为什么改成续写？** 同一预算已经证明不足，简单重放只会再截断一次。此时保存纯文本
片段（L333-L337）并追加 `CONTINUATION_PROMPT`（error_recovery.py L19-L23），才能把一份
超长结果拆成多段。这条提示明确要求"不要道歉、不要复述、直接从断点继续"，避免每段开头浪
费 token。

**计数语义是"三条 continuation user 消息"。** 上限判定（L340）在追加片段之后、追加
continuation 之前，因此首次 64K 截断追加第 1 条，第 3 条之后若仍截断，就返回那次的片段
而不再追加第 4 条。测试用五次连续截断验证：`messages` 中恰好 3 条 continuation，共发出
5 次请求，返回值是 `partial-four`（test_s11.py L554-L562）。

片段为空时（例如模型只吐了一半工具参数），返回一条显式错误文本并同样写回 assistant
（L341-L348），保证 CLI 不会收到空字符串。

截断响应里的工具调用一律不执行：`is_output_truncated` 分支在 `assistant_payload()`
（L369）之前 `continue`，残缺参数根本没有机会进入 `_execute_tool_batch()`。

## 六、Prompt 过长与 s08 Compact 的关系

s08 本来就有两个入口：`prepare()`（context_compact.py L315）每次请求前按预算执行
L3/L1/L2，必要时 L4 摘要；`reactive_compact()`（L288）在供应商仍拒绝时用更激进方式，只
保留 system 前缀、摘要和当前请求。

s11 不复制任何压缩算法，只决定 reactive 入口在一个 turn 内可以用一次
（agent_loop.py L300-L312）：第一次命中就压缩并 `continue`，第二次直接
`_append_failure_result()` 收口。

这里**不能复用手动 compact 的 `manual_compactions` 计数**。手动 compact 是模型主动调用
的控制工具，它的计数决定下一轮是否继续暴露 `compact` schema（L278 →
`_visible_parent_tools` L158）；reactive compact 是 API 失败恢复，触发源和状态语义完全
不同，两者各自独立计数。

分类函数从 s10 的 `context_compact.py` 移到了 `error_recovery.py`：压缩模块只回答"怎么
压"，分类模块回答"这是什么错"。搬迁时同时补了 `prompt_is_too_long` 与
`max_context_window` 两个标记（error_recovery.py L81、L85）。

## 七、不可恢复错误为什么写回 assistant

交互式 CLI 一旦让异常穿透，用户看到的是 traceback，当前进程与会话历史一起中断。
`_append_failure_result()`（agent_loop.py L219-L232）把异常收口成一条最终 assistant
消息：

```text
Error: RuntimeError: invalid request
```

三个细节：

- 文本由 `format_unrecoverable_error()`（error_recovery.py L187）生成，详情截断到 1000
  字符（L191-L192），避免把巨大的服务端响应原样塞回上下文；
- 消息同时写入主历史与 `extraction_messages`（L229-L230），保持双写不变量；
- 这个出口**不触发 Memory 提取**——它在 `return` 前就退出了 L386-L387 那条路径，失败信息
  通常不是值得跨会话保存的用户事实。

## 八、Provider 适配

`completion_request()`（provider.py L32）新增两个 keyword-only 覆盖参数：

| 参数 | 由谁决定 | 落到请求 |
| --- | --- | --- |
| `model` | `RecoveryState.current_model` | `request["model"]` L43 |
| `max_tokens` | 8K/64K 状态机 | 仅非 None 时写入 L47-L48 |

模型专属参数仍集中在 Provider，且改成按 `selected_model` 判定（L50-L51）：切换到
`kimi/kimi-k3` 后仍会补 `reasoning_effort=max`，Agent Loop 不需要理解供应商 extra body。
`fallback_model` 作为普通配置项进入 `Settings`（config.py L29、L51），来源是
`FALLBACK_MODEL_ID`，未设置时为 `None`，此时 529 只退避不降级。

Anthropic 教程的截断信号是 `stop_reason=max_tokens`；百炼的 OpenAI-compatible 响应通常
是 `finish_reason=length`，部分兼容服务返回 `max_tokens`，所以分类函数同时接受两者
（error_recovery.py L95）。

## 九、父循环怎么把十一章串起来

`__init__`（agent_loop.py L53-L116）仍是五步装配，s11 只在签名上多了两个可注入函数
（L58-L59）并保存为 `_sleep_fn` / `_random_fn`（L63-L64）。一个 turn 的骨架：

```text
agent_loop(messages, active_request, compactor)            L234
  → latest_user_request 兜底                                L243
  → extraction_messages = deepcopy(messages[-12:])          L247
  → refresh_system_prompts(messages)                        L250
  → memory.load_memories / inject_recalled_memories         L251-L252
  → recovery = RecoveryState(settings.model)                L261
  while True:
      → 连续 3 轮未 todo_write 则注入 reminder               L266-L273
      → refresh_system_prompts(messages)                     L276
      → compactor.prepare(messages, active_request)          L277
      → 本 turn 压缩过则从 tools 中移除 compact              L278
      → with_retry(lambda model: create(**completion_request))L282-L296
            except:
              prompt too long 且未压缩 ? reactive + continue  L300-L312
              否则 _append_failure_result → return            L313
      → finish_reason 截断 ?                                  L322
            未升级 ? max_tokens=64K + continue                L323-L331
            已升级 → 存片段 → 达上限 ? return : continuation   L333-L366
      → assistant 消息双写主历史与提取快照                    L369-L371
      → 无 tool_calls ?                                       L374
            Stop Hook 要求继续 ? 追加 user 并 continue         L378-L383
            否则 extract_memories → consolidate → return       L386-L388
      → _execute_tool_batch 逐个执行并配对 role=tool           L392-L397
      → 整批写完后才 compact_history                           L399-L402
```

三处顺序约束是这一章最容易忽略的实现细节：

1. **刷新在压缩之前**（L276 → L277），否则 s08 按旧 Prompt 体积估算预算，这条继承自 s10。
2. **恢复分支都用 `continue` 回到循环顶部**，因此每次重试都会重新刷新 Prompt 并重新
   `prepare`。8K→64K 的"重放原请求"严格说是"用同一份消息历史重新走一遍请求准备"。
3. **截断判定在 `assistant_payload()` 之前**，完整性检查先于任何历史写入。

## 十、行为不变量

`tests/test_s11.py` 26 项测试分两组，同时覆盖新能力与旧约束。

s10 结构回归（`SystemPromptTest`，14 项）：

| 断言 | 位置 |
| --- | --- |
| 无 Skill / Memory 时只有四个固定 section | L130 |
| 工具行来自真实注册表，父 9 子 6 | L141 |
| Skill 出现后 skills section 才出现 | L153 |
| memory section 只放目录，不含正文 | L167 |
| context 不变则复用缓存 / 变化则重建 | L181、L189 |
| 刷新只替换首条 system 消息 | L197 |
| Agent Loop 真正发送组装结果 | L204 |
| 十一个能力模块存在、旧碎片文件名消失 | L227 |
| 父工具路由私有、工具批次返回控制信号 | L259、L280 |
| SubAgent 工具隔离、工作区与 Todo 规则仍生效 | L312、L324 |

s11 恢复路径（`ErrorRecoveryTest`，12 项）：

| 断言 | 位置 |
| --- | --- |
| 分类函数与退避公式（0.5 / 32 / Retry-After） | L368 |
| 429 退避序列恰好 `[0.5, 1.0]` | L382 |
| `Retry-After` 优先且不叠 jitter | L409 |
| 三次 529 后第四次请求用 fallback | L430 |
| 重试有界；非瞬态只尝试一次 | L462 |
| 首次截断不污染历史，8K→64K | L490 |
| 64K 截断后追加片段与 continuation | L522 |
| continuation 恰好 3 条，共 5 次请求 | L540 |
| 空片段返回显式错误并写回 assistant | L564 |
| prompt too long 压缩恰好一次后恢复 | L582 |
| 第二次溢出变成 assistant 错误 | L598 |
| 非瞬态错误返回文本而不是 traceback | L614 |

故障用脚本化 `ScriptedCompletions`（test_s11.py L54）注入，`HttpError`（L88）伪造
`status_code` 与 headers，`RecoveryCompactor`（L79）统计 reactive 次数。整套测试 0.7 秒
内跑完，不依赖真实服务恰好出错。

## 十一、权衡与已知遗留

### 1. 只包裹父循环主请求

未纳入 `with_retry` 的 side call 有三类：Memory 的选择 / 提取 / 整理、Compact 的摘要模型
调用、SubAgent 自己的内部循环。它们各自已有降级或错误返回边界，统一重试会引入不同的幂等
性和消息写回问题。后续扩展应复用 `error_recovery.py` 的纯策略，但为每类调用单独定义恢复
语义。

### 2. 供应商相关限制

- 某个模型若不接受 `max_tokens=64000`，第二次请求会被当作不可恢复的参数错误返回；
- prompt-too-long 依赖错误文本匹配（error_recovery.py L77-L89），新供应商文案需要补分类
  测试；
- `retry_after_seconds()` 只解析秒数（L107），不解析 HTTP-date 形式。

这些都是显式边界，不影响恢复次数的有界性。

### 3. 从前章继承的遗留

`memory.py` 仍使用模块级配置（memory.py L39），同一进程内不适合并存两个不同记忆目录的
Harness；`skill_loading.py` 每轮扫盘；`SystemPromptAssembler.invalidate()`
（system_prompt.py L99）仍无调用方；`memory.build_memory_system_prompt`（memory.py L321）
自 s10 起已被 memory section 取代。

## 十二、验证

```bash
python3 -m pytest -q tests/test_s11.py
# 26 passed
```

`python3 s11_error_recovery/code.py` 启动时打印
`model=... fallback=... workdir=... sections=...`，可直接确认 fallback 是否配置成功。恢复
路径本身在真实运行中会打印带颜色的单行日志：`[429 rate limit] retry n/10`、
`[529 overloaded] switching to fallback model ...`、`[max_tokens] escalating 8000 ->
64000`、`[prompt too long] reactive compact completed`、`[recovery exhausted] ...`，据此可
以在不读代码的情况下判断当前走的是哪条分支。
