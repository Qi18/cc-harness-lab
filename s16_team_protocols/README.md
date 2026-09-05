# s16 Team Protocols：让团队请求有状态、有回应

s15 已经让多个 Agent 可以并行工作、通过邮箱通信。s16 进一步规定：怎样请求队友关闭，怎样提交和审批计划，以及怎样判断一封响应对应哪次请求。

本章的核心是 **请求 ID、协议状态和消息路由**。它还改变了队友的生命周期：完成一轮任务后进入 idle，等待新消息，收到关闭请求后再退出。

本文介绍本仓库的实际行为。前置概念见 [s15 Agent Teams](../s15_agent_teams/README.md)，逐函数接入关系见 [调用图](./CALLGRAPH.md)。

## 1. 普通消息为什么不够？

Lead 可以发一句“请退出”，队友也可以回复“好的”。但程序只看到两段文字：它们是否属于同一次请求？对方是否明确接受？旧响应能否误改新请求的状态？

s16 使用结构化协议消息，例如：

```json
{
  "from": "lead",
  "to": "alice",
  "type": "shutdown_request",
  "content": "Please shut down gracefully.",
  "ts": 1788535800.0,
  "metadata": {"request_id": "req_example"}
}
```

响应使用相同的 ID，并明确给出结果：

```json
{
  "from": "alice",
  "to": "lead",
  "type": "shutdown_response",
  "content": "Shutting down gracefully.",
  "ts": 1788535801.0,
  "metadata": {"request_id": "req_example", "approve": true}
}
```

程序根据 `type` 选择处理逻辑，根据 `request_id` 查找请求，再校验通信双方和状态。它不需要通过自然语言猜测“好的”到底同意了什么。

## 2. 一次完整协作是什么样？

假设 Alice 要修改接口，Lead 希望先看计划。

1. Lead 创建 Alice，或者向仍在岗的 Alice 调用 `request_plan`，说明任务。
2. Alice 调用 `submit_plan`，Harness 创建计划请求并发给 Lead。
3. Lead 收到计划，调用 `review_plan`，批准或拒绝并附上反馈。
4. Alice 收到响应，模型上下文中出现审批结论；若此前已经 idle，收到消息会唤醒它。
5. Alice 完成这一轮工作，发送 `result`，再次进入 idle。
6. Lead 调用 `request_shutdown`。Alice 在收信点处理请求、回信，然后退出；Lead 消费响应，更新关闭请求状态。

这是期望的协作过程，不是代码强制执行的审批关卡。提交计划不会自动阻塞后续工具调用；这一边界在第 5 节具体说明。

## 3. 四个新增工具分别归谁？

| 调用方 | 工具 | 作用 |
| --- | --- | --- |
| Lead | `request_shutdown(teammate)` | 创建关闭请求并发送协议消息 |
| Lead | `request_plan(teammate, task)` | 发普通消息，请队友提交计划 |
| Lead | `review_plan(request_id, approve, feedback)` | 审批指定计划并发送响应 |
| 队友 | `submit_plan(plan)` | 创建计划审批请求并发送给 Lead |

**request_plan 本身不会创建 ProtocolState。** 它只是提出要求，真正带请求 ID 的计划状态在队友 `submit_plan` 时建立。

队友保留 s15 的 Bash、文件读写和发信工具，加上 `submit_plan` 共五个工具。Lead 的三个新工具由协议模块注册，队友不因此获得创建新 Agent 的工具入口。

## 4. 请求状态与队友状态是两件事

`ProtocolState` 记录一条请求：

| 字段 | 含义 |
| --- | --- |
| `request_id` | 请求标识 |
| `type` | `shutdown` 或 `plan_approval` |
| `sender`、`target` | 发起者与被请求者 |
| `status` | `pending`、`approved`、`rejected` |
| `payload` | 请求内容，例如计划正文 |
| `created_at` | 创建时间 |

```text
请求：pending → approved / rejected
队友：working → idle → working
                    └─ 处理关闭请求 → stopping → done
      运行异常 → failed
```

请求已批准，不表示队友已完成任务；队友 idle，也不表示它已经退出。

当前有效关闭请求由队友的程序逻辑自动同意，不由模型判断是否拒绝。虽然通用状态支持 rejected，但常规关闭处理路径发送的是 `approve=true`。

## 5. 计划审批怎样流转？

```text
Alice: submit_plan
  → 创建 plan_approval，pending
  → plan_approval_request 发往 Lead
Lead: review_plan(request_id, approve, feedback)
  → 将状态设为 approved / rejected
  → plan_approval_response 发往 Alice
Alice: 消费响应
  → 校验 ID、双方、结论与已记录状态
  → 将审批结论写入自己的模型历史
```

与关闭流程不同，计划状态在 Lead 调用 `review_plan` 时就已经更新。队友接收响应时主要验证并注入结论，不再把 pending 推进一次。发送审批响应失败时，代码会尝试把状态回退到 pending。

同一条已审批计划不能再次审批；需要修改计划时，可以重新提交，产生新的请求 ID。

批准时，当前队友注入的是通用提示 `[Plan approved] Proceed with the task.`；拒绝时会附带 feedback。批准响应虽然携带反馈正文，但当前处理器不会把该正文一起注入模型历史。

**这是一套教学用协作协议，没有强制审批门禁。** `submit_plan` 返回“等待审批”，不意味着线程立即进入专门的审批等待状态。队友仍可能继续请求工具；`write_file`、`bash` 的执行器也不检查计划是否获批。若要保证未审批不能执行特定操作，需要把审批状态接入工具权限判定。

## 6. 关闭请求为什么需要握手？

```text
Lead: request_shutdown
  → 创建 shutdown 请求，pending
  → 发送 shutdown_request
Alice: 在收信点处理请求
  → 校验并发送 shutdown_response
  → 返回 stop 信号，队友循环退出
Lead: 消费响应
  → match_response 校验并将请求设为 approved
```

Lead 发出请求后，状态不会立即变成 approved。队友回信后，Lead 也需要再次消费邮箱才会推进请求状态。

响应先写入邮箱，队友随后退出，因此 Lead 看到 approved 不等于已经完成线程 join。需要确认实际线程结束时，还要查看队友状态或使用内部等待接口。

关闭请求也不能打断正在进行的模型调用或工具批次。它在队友下一次收信时生效；这里的“优雅退出”表示协作式退出，不是强制杀线程或取消子进程。

## 7. Lead 为什么需要统一收信入口？

s15 已有主动 `check_inbox` 和主循环自动收信两条路径。如果只有其中一条会更新协议状态，另一条先把响应消费掉，请求就可能一直停留在 pending。

s16 让两条路径共用 `consume_lead_messages`：读取邮箱后先经过协议路由，再返回消息。

| 消息 | 主要处理位置 |
| --- | --- |
| `shutdown_response` | Lead 收信时关联请求并推进状态 |
| `plan_approval_request` | 返回给 Lead 模型，由它调用 review_plan |
| `shutdown_request` | 队友程序直接处理，产生停止信号 |
| `plan_approval_response` | 队友校验后转为模型可见的审批结论 |
| 普通 `message` | 作为团队通信内容进入上下文 |

状态推进检查请求 ID、响应类型、通信双方和当前状态。重复响应不会再次改写已结束请求；队友还通过已处理集合避免重复回复关闭请求或重复注入计划结论。

这些是正常协议路径上的关联与去重，不是端到端可靠投递或安全认证。

## 8. idle 怎样等待和唤醒？

s15 队友给出最终总结后退出。s16 则发送总结、设置 idle，然后等待邮箱消息。

发信入口在落盘后设置队友的唤醒 Event；队友也会检查邮箱。等待期间不持续调用模型，因此 idle 本身不会不断消耗模型 token。

收到普通消息或审批结论后，可以开始新的工作周期，保留之前的私有历史。每个活跃周期默认最多 10 轮；超轮次时发送 error 并回到 idle，而不是直接终止队友。模型请求等运行异常仍可能导致 failed。

队友仍使用最近 20 条消息切片，没有自己的 Compact、Memory 或父循环的 Error Recovery。idle 不改变这些能力边界，也不保证上下文可以无限保留。

## 9. 当前实现的边界

- 协议状态和去重集合存在进程内。邮箱文件虽在磁盘上，但重启不会恢复请求表、队友线程或对话历史，旧协议消息可能因查不到请求而被忽略。
- 没有基于 created_at 的超时处理、自动重发或请求取消接口。响应失败或队友异常后，请求可能长期 pending。
- 状态表与邮箱写入不是一个事务。局部回滚只能处理部分错误路径，不能提供崩溃恢复保证。
- Lead 保留 s15 的收信方式；有 idle 队友不意味着 Lead 会一直运行，收到响应也不会独立唤醒已经结束的 Lead turn。
- 队友共享运行身份和工作目录，仍有 Bash、文件写入能力。闭包、ID 和字段校验限定的是工具与协议处理路径，不是防篡改邮箱或恶意代码的沙箱。
- daemon 线程不会阻止主进程退出；关闭整个 CLI 不等于逐个完成 shutdown 握手。

## 10. 建议怎样读代码？

1. [team_protocols.py](./harness/team_protocols.py)：先看 ProtocolState 和四个工具方法，确定每条请求何时创建、何时结束。
2. 接着看 `match_response` 和 `dispatch_teammate_message`，理解 Lead 与队友两侧不同的路由职责。
3. [agent_teams.py](./harness/agent_teams.py)：看 `_run_teammate` 的外层等待循环，以及 `_process_teammate_inbox` 如何返回停止信号。
4. 同一文件的 `consume_lead_messages`：检查主动查信和主循环为何都能更新协议状态。
5. [test_s16.py](../tests/test_s16.py)：阅读请求关联、类型与发送方校验、重复响应、idle 唤醒和关闭握手测试。

从仓库根目录运行已有测试：

```bash
python3 -m unittest tests.test_s16
```

已配置模型访问时，可运行 `python3 s16_team_protocols/code.py`。交互验证时观察：计划提交是否生成 ID、审批是否使用同一个 ID、任务完成后是否 idle、关闭后是否 done，以及 Lead 消费响应前后请求状态是否改变。

读完后可以自问：为什么 request_plan 不创建协议状态？为什么计划 approved 与工具执行权限不同？为什么收到关闭响应仍不等于已经 join 完队友线程？
