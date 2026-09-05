# s19 MCP Tools：把动态发现的工具接到同一个 Agent Loop

s18 隔离队友的工作目录；s19 扩展 Lead 的工具来源。模型先连接一个已登记的服务，Harness 发现工具定义，把它们转换成 OpenAI function calling schema，再把模型调用路由回对应服务。

**本章是进程内 mock 工具桥，不是真实 MCP 网络或 stdio 客户端。** 名为 MCPClient 的类模拟发现与调用边界，没有发送 JSON-RPC 请求。docs 使用固定文档数据，deploy 只修改内存状态，绝不触发真实部署。

本文对应当前仓库代码。前置内容见 [s18 Worktree Isolation](../s18_worktree_isolation/README.md)，课程入口为 [s19 MCP Tools](https://learn.shareai.run/zh/s19/)。

## 1. 为什么工具不能一直是固定列表？

原来新增工具需要同时修改 schema 表和 handler 表。工具来源变多后，Agent Loop 不应该为每个外部系统增加一条 if 分支。

本章把变化放到工具池组装层：

```text
连接前：27 个内置 Lead 工具，其中包含 connect_mcp
连接 docs 后：27 + search/get = 29
再连接 deploy：29 + deploy/status = 31

Subagent：仍为 6 个工具
Teammate：仍为 8 个工具，不继承 MCP 连接
```

这里的数字来自本仓库累计实现，不直接沿用课程示例的工具总数。新增 MCP schema 和 handler 都只用于 Lead；即使队友自行构造 mcp__ 名称，自己的 handler 表里也找不到执行入口。

## 2. 一次“连接，再搜索”怎样运行？

```text
模型第 1 轮
  connect_mcp(name="docs")
    ↓
ToolExecutor：解析参数、PreToolUse
    ↓
AgentHarness._connect_mcp
    ↓
MCPManager：查受信任 factory → 创建 MCPClient → 发现工具定义
    ↓
校验整个候选工具池，没有名称冲突才发布连接
    ↓
刷新 Prompt，返回发现的工具名
    ↓
PostToolUse → 原 tool_call_id 对应的 tool result

模型第 2 轮：请求中已携带 mcp__docs__search / mcp__docs__get
  mcp__docs__search(query="worktree")
    ↓
ToolExecutor → 动态 handler
    ↓
MCPClient.call_tool("search", {"query": "worktree"})
    ↓
mock search → 结果 JSON 字符串 → tool result

模型第 3 轮
  根据搜索结果回答用户
```

模型不直接连接服务，也不负责把远端 schema 转成自己的协议。它只从当轮提供的工具列表中选择工具，连接与转换由 Harness 完成。

自动化测试中的完整循环覆盖了上述三轮：第一轮没有搜索工具，第二轮 schema 与 system prompt 都出现新工具，第三轮能看到搜索结果。

## 3. 模块边界在哪里？

| 模块 | 职责 |
| --- | --- |
| [mcp_plugin.py](./harness/mcp_plugin.py) | MCPClient、mock 服务、名称规范化、连接登记、工具池组装与调用适配 |
| [tool_use.py](./harness/tool_use.py) | 静态内置表增加 connect_mcp，继续提供统一 ToolExecutor |
| [agent_loop.py](./harness/agent_loop.py) | 装配 MCPManager，在 Prompt、模型请求和分发时使用动态工具池 |
| [system_prompt.py](./harness/system_prompt.py) | 告知 mock 边界、Lead-only 范围和不可信结果处理原则 |
| [code.py](./code.py) | CLI 入口，显式显示 in-process mock |
| [test_s19.py](../tests/test_s19.py) | 累计能力回归与 MCP 专项测试 |

MCPManager 不调用模型，也不读写对话消息；MCPClient 不组装 System Prompt；Agent Loop 不理解 docs 搜索和 deploy 状态的业务实现。

## 4. schema 和 handler 是怎样配对的？

每项发现的定义包含 name、description、inputSchema 和可选 annotations。桥接后：

| 服务侧定义 | 模型侧表示 |
| --- | --- |
| 服务 docs，工具 search | function.name = mcp__docs__search |
| inputSchema | function.parameters |
| description 与 annotation hints | function.description |
| 原始工具名 search | 被 handler 闭包捕获，用于 call_tool |

模型可见名称和原始名称不能混淆。服务若登记 search.docs，模型侧会规范化为 search_docs，但真正调用时仍使用原始 search.docs。

每次组装返回独立 schema 快照和 handler 字典。闭包捕获当前 client 与原始工具名，避免循环结束后所有工具都错误地指向最后一项。

内置工具维持原顺序，已连接服务的工具追加在后。组装过程不修改原来的 PARENT_TOOLS。

## 5. 名称规范化为什么还不够？

normalize_mcp_name 把非英文字母、数字、下划线和短横线替换成下划线。但 a.b 和 a/b 都会得到 a_b，因此仅规范化不能保证唯一。

当前实现额外检查：

- 服务名规范化后是否与已连接服务冲突。
- 最终 mcp__server__tool 名称是否与内置 schema、handler 或其他 MCP 工具冲突。
- 最终名称是否超过 64 字符。
- 服务提供的工具名是否重复，是否有对应的可调用 handler。
- inputSchema 是否为 object，description、annotations 的基本类型是否正确。

候选连接全部检查通过后才写入已连接表。失败时旧工具池保留，不发布半份服务，也不静默覆盖已有工具。

注册会检查基本结构与 JSON 可序列化性，但没有实现完整 JSON Schema 校验器；不能将它视为完整 MCP schema 合规验证。

## 6. 动态工具与 Prompt 缓存如何协调？

本仓库原来的 SystemPromptAssembler 缓存键已经包含 enabled_tools，因此这里保留缓存：

1. 连接成功后，刷新 system prompt。
2. 每轮请求按当前连接重新组装工具 schema。
3. 新工具名使缓存键改变，Prompt 自然重新生成。
4. 工具名及其他上下文不变时，仍可命中缓存。

这与课程直接移除缓存的简化路径不同，但同样解决旧 Prompt 看不到新工具的问题。schema 每次重新组装，不会依赖 Prompt 缓存保存工具定义；description 或参数变化也不会被旧 schema 缓存遮住。

每次 Lead 工具分发也读取当前连接，所以同一响应批次中先 connect_mcp、后调用新工具可以正确路由。正常体验仍推荐分两轮：先收到发现结果，再选择新工具。

Compact 的一次性隐藏规则保持不变：用过 compact 后只过滤它本身，不会把动态 MCP 工具一起丢掉。

## 7. mock 服务提供哪些能力？

| 服务 | 工具 | 实际行为 |
| --- | --- | --- |
| docs | search(query) | 在两条固定文档中匹配关键词，返回结果列表 |
| docs | get(document_id) | 获取 worktree 或 mcp 文档 |
| deploy | deploy(service) | 在该连接内记录 simulated，并返回“未执行真实部署” |
| deploy | status(service) | 返回 not_started 或 simulated |

返回数据带 mock=true，避免把演示结果误认为真实外部系统状态。

connect_mcp 只能选择受信任 Python 代码登记的 factory。模型不能传 URL、shell 命令、token 或任意服务配置来扩展连接范围。不存在的名称返回可用服务列表；重复连接返回 already connected，不会清空该连接的模拟状态。

## 8. 错误和权限边界

MCP 工具沿用普通工具管线：参数 JSON 解析 → PreToolUse → handler → PostToolUse。未知工具、错误参数和 handler 异常作为工具结果返回，不应直接终止整个 Agent turn。

readOnlyHint 和 destructiveHint 被转换为描述中的 (readOnly)、(destructive)。这些是提示，不是用户授权，也没有自动形成 MCP 专属权限拦截。默认 Hooks 仍会运行，但目前不知道如何对任意外部业务做精细授权。

连接的 schema、说明和工具结果都应当作不可信数据。本章 Prompt 明确提醒不要把它们当成更高优先级指令，但仅靠提醒不构成注入防护方案。

mock 调用为同步执行，尚无调用超时、取消、连接重试或断线恢复。接入真实副作用服务前，需要先完善传输、认证、授权和失败语义，不能直接把 mock deploy 换成生产部署接口就投入使用。

## 9. 运行和测试

在仓库根目录，使用已安装项目依赖的 Python 环境：

```bash
python3 -m unittest tests.test_s19
python3 s19_mcp_plugin/code.py
```

CLI 沿用已有百炼环境变量或本地 .env；不要把 API Key 写入 prompt 或仓库。mock 服务本身不需要外部凭据，CLI 中的模型调用仍需要模型凭据。

可以输入：

> 连接 docs mock 服务，搜索 worktree，再读取匹配的文档，解释任务完成和分支合并有什么区别。

或者：

> 连接 docs 和 deploy，列出新发现的工具。只在 mock 中模拟 api 服务部署，再查询模拟状态；不要执行任何真实部署。

2026-09-05 在 L20 完成验证：本章 125 项测试、全项目 790 项测试均通过，Python 编译检查通过。

当前 MCP 专项测试共 16 项，覆盖多服务路由、重复连接、未知服务、名称冲突、长度限制、内置保护、注册错误、handler 异常、factory 失败、并发连接、快照隔离、Prompt 缓存变化、Compact 过滤、父子能力隔离和完整 Agent Loop。

本章验证使用模拟模型响应与进程内 mock 服务，没有真实 MCP Server、stdio/HTTP 或真实部署调用。不要把上一章 s18 的真实模型记录算作本章验证证据。

## 10. 后续接入真正 MCP 还缺什么？

- initialize 握手、协议版本和 capabilities 协商。
- 真正的 tools/list、tools/call JSON-RPC 请求与结果结构，而不只是 Python 方法。
- stdio 子进程或 Streamable HTTP 传输，以及超时、取消和资源释放。
- tools/list 分页、列表变更通知、重连和缓存失效。
- MCP content blocks、isError、结构化结果与多模态内容转换。
- 认证与密钥管理、独立授权检查、审计和可信服务配置。
- 子 Agent 配置继承与调用范围控制。

本章完成的是“动态发现 → schema 适配 → 统一分发”的教学路径，不是一个已可连接任意 MCP Server 的通用客户端。

推荐阅读顺序：MCPClient.register/call_tool → MCPManager.connect_mcp → _assemble → AgentHarness._connect_mcp/_visible_parent_tools/_dispatch_parent_tool → 完整循环测试。
