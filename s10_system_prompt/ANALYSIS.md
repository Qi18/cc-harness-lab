# s10 源码剖析：运行时 System Prompt

核心调用关系：

```text
AgentHarness.__init__
  -> SkillLoader.scan
  -> memory.configure
  -> refresh_system_prompts
       -> _prompt_context(PARENT_TOOLS / SUB_TOOLS)
       -> SystemPromptAssembler.get
            -> stable JSON context key
            -> cache hit 或 assemble(section list)

agent_loop
  -> refresh system message
  -> Memory selection / recall
  -> 每轮再次 refresh
  -> ContextCompactor.prepare
  -> LLM -> tools -> observation -> loop
```

设计边界：

1. Prompt 只描述 Harness 的真实能力，不能声明未注册工具。
2. Memory index 是 metadata；完整记录仍走 s09 的按需召回。
3. 父 Agent 和 SubAgent 使用独立 assembler 与工具列表，防止子 Agent 获得
   `todo_write`、`task` 或 `compact` 的虚假说明。
4. cache key 使用确定性 JSON，不使用进程随机化的 `hash()`。
5. Prompt refresh 只改变 system message，不改写历史 user/tool 记录。
