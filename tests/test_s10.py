import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT))

from s10_system_prompt.harness import memory
from s10_system_prompt.harness.agent_loop import AgentHarness
from s10_system_prompt.harness.config import Settings
from s10_system_prompt.harness.subagent import SubagentRunner
from s10_system_prompt.harness.system_prompt import (
    SystemPromptAssembler,
    registered_tool_names,
)
from s10_system_prompt.harness.todo_write import TodoManager
from s10_system_prompt.harness.tool_use import PARENT_TOOLS, SUB_TOOLS, ToolExecutor


class FakeMessage:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = None

    def model_dump(self, exclude_none=True):
        payload = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            payload["tool_calls"] = self.tool_calls
        return payload


class ScriptedCompletions:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        message = response if isinstance(response, FakeMessage) else FakeMessage(response)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")]
        )


def fake_client(*responses):
    completions = ScriptedCompletions(responses)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


class IdentityCompactor:
    def prepare(self, messages, active_request):
        return messages


class SystemPromptTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self):
        self.temporary.cleanup()

    def settings(self):
        return Settings(
            workdir=self.root,
            skills_dir=self.root / "skills",
            transcript_dir=self.root / ".transcripts",
            tool_results_dir=self.root / ".task_outputs" / "tool-results",
            memory_dir=self.root / ".memory",
            tasks_dir=self.root / ".tasks",
            model="qwen-plus",
        )

    def harness(self, *responses):
        client, completions = fake_client(*responses)
        return AgentHarness(client, self.settings()), completions

    def test_named_sections_include_identity_tools_and_workspace(self):
        harness, _ = self.harness()
        prompt = harness.system_prompt
        self.assertIn("You are a coding agent", prompt)
        self.assertIn(f"Working directory: {self.root}", prompt)
        self.assertIn("Available tools:", prompt)
        self.assertEqual(
            ("identity", "guidance", "tools", "workspace"),
            harness.parent_prompt.last_sections,
        )

    def test_tool_section_comes_from_real_registries(self):
        harness, _ = self.harness()
        parent_names = registered_tool_names(PARENT_TOOLS)
        child_names = registered_tool_names(SUB_TOOLS)
        self.assertEqual(9, len(parent_names))
        self.assertEqual(6, len(child_names))
        self.assertIn("todo_write", harness.system_prompt)
        self.assertIn("task", harness.system_prompt)
        self.assertNotIn("todo_write", harness.sub_system_prompt)
        self.assertNotIn("compact", harness.sub_system_prompt)
        self.assertNotIn("create_task", harness.system_prompt)

    def test_skill_section_is_loaded_only_when_skill_exists(self):
        harness, _ = self.harness()
        self.assertNotIn("skills", harness.parent_prompt.last_sections)

        skill = self.root / "skills" / "review" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            "---\nname: review\ndescription: Review code safely.\n---\n# Review",
            encoding="utf-8",
        )
        harness.refresh_system_prompts()
        self.assertIn("skills", harness.parent_prompt.last_sections)
        self.assertIn("review: Review code safely.", harness.system_prompt)

    def test_memory_section_tracks_real_index_state(self):
        harness, _ = self.harness()
        self.assertNotIn("memory", harness.parent_prompt.last_sections)
        memory.write_memory_file(
            "project-layout",
            "project",
            "Project layout",
            "Source code lives under src.",
        )
        harness.refresh_system_prompts()
        self.assertIn("memory", harness.parent_prompt.last_sections)
        self.assertIn("Memory catalog (metadata only)", harness.system_prompt)
        self.assertNotIn("Source code lives under src.", harness.system_prompt)

    def test_cache_hits_when_context_is_unchanged(self):
        harness, _ = self.harness()
        assemblies = harness.parent_prompt.assembly_count
        harness.refresh_system_prompts()
        harness.refresh_system_prompts()
        self.assertEqual(assemblies, harness.parent_prompt.assembly_count)
        self.assertGreaterEqual(harness.parent_prompt.cache_hits, 2)

    def test_changed_context_invalidates_cache(self):
        assembler = SystemPromptAssembler("identity", "guidance")
        base = {"workspace": "/repo", "enabled_tools": ["bash"]}
        first = assembler.get(base)
        second = assembler.get({**base, "enabled_tools": ["bash", "read_file"]})
        self.assertNotEqual(first, second)
        self.assertEqual(2, assembler.assembly_count)

    def test_refresh_updates_existing_system_message(self):
        harness, _ = self.harness()
        messages = [{"role": "system", "content": "stale"}, {"role": "user", "content": "hi"}]
        harness.refresh_system_prompts(messages)
        self.assertEqual(harness.system_prompt, messages[0]["content"])
        self.assertEqual(2, len(messages))

    def test_agent_loop_sends_assembled_prompt(self):
        harness, completions = self.harness("done", "[]")
        messages = [
            {"role": "system", "content": "stale"},
            {"role": "user", "content": "Inspect the project."},
        ]
        answer = harness.agent_loop(
            messages,
            active_request="Inspect the project.",
            compactor=IdentityCompactor(),
        )
        self.assertEqual("done", answer)
        sent = completions.requests[0]["messages"][0]["content"]
        self.assertEqual(harness.system_prompt, sent)
        self.assertIn("Available tools:", sent)

    def test_major_concerns_are_real_modules(self):
        harness, _ = self.harness()
        self.assertIsInstance(harness.executor, ToolExecutor)
        self.assertIsInstance(harness.subagent, SubagentRunner)
        self.assertIsInstance(harness.todo, TodoManager)


    def test_modules_follow_course_capability_names(self):
        harness_dir = REPO_ROOT / "s10_system_prompt" / "harness"
        module_names = {path.name for path in harness_dir.glob("*.py")}
        expected = {
            "agent_loop.py",
            "tool_use.py",
            "permission.py",
            "hooks.py",
            "todo_write.py",
            "subagent.py",
            "skill_loading.py",
            "context_compact.py",
            "memory.py",
            "system_prompt.py",
        }
        obsolete = {
            "agent.py",
            "client.py",
            "compact_tool.py",
            "compaction.py",
            "execution.py",
            "prompt.py",
            "protocol.py",
            "schemas.py",
            "skills.py",
            "todo.py",
            "tools.py",
        }
        self.assertTrue(expected <= module_names)
        self.assertTrue(obsolete.isdisjoint(module_names))

    def test_agent_harness_keeps_tool_routing_private(self):
        harness, _ = self.harness()
        legacy = {
            "execute_with_handlers",
            "execute_tool",
            "execute_sub_tool",
            "spawn_subagent",
            "request_manual_compact",
        }
        self.assertTrue(legacy.isdisjoint(dir(harness)))
        self.assertIn("task", harness._parent_handlers)
        self.assertNotIn("task", harness.subagent.handlers)
        visible_before = {
            tool["function"]["name"] for tool in harness._visible_parent_tools(False)
        }
        visible_after = {
            tool["function"]["name"] for tool in harness._visible_parent_tools(True)
        }
        self.assertIn("compact", visible_before)
        self.assertNotIn("compact", visible_after)

    def test_tool_batch_returns_control_signals_and_pairs_results(self):
        harness, _ = self.harness()

        def tool_call(call_id, name, arguments):
            return SimpleNamespace(
                id=call_id,
                function=SimpleNamespace(name=name, arguments=arguments),
            )

        calls = [
            tool_call("todo-1", "todo_write", '{"todos": []}'),
            tool_call("compact-1", "compact", "{}"),
            tool_call("compact-2", "compact", "{}"),
        ]
        messages = []
        extraction_messages = []
        used_todo, compact_requested = harness._execute_tool_batch(
            calls,
            messages,
            extraction_messages,
            already_compacted=False,
        )
        self.assertTrue(used_todo)
        self.assertTrue(compact_requested)
        self.assertEqual(
            ["todo-1", "compact-1", "compact-2"],
            [item["tool_call_id"] for item in messages],
        )
        self.assertIn("already completed", messages[-1]["content"])
        self.assertEqual(messages, extraction_messages)
        self.assertIsNot(messages[0], extraction_messages[0])

    def test_subagent_runner_uses_isolated_tool_set(self):
        harness, completions = self.harness("subagent done")
        self.assertEqual("subagent done", harness.subagent.run("Inspect one file."))
        request = completions.requests[0]
        names = [tool["function"]["name"] for tool in request["tools"]]
        self.assertEqual(
            ["bash", "read_file", "write_file", "edit_file", "glob", "load_skill"],
            names,
        )
        self.assertEqual("system", request["messages"][0]["role"])
        self.assertNotIn("todo_write", request["messages"][0]["content"])

    def test_previous_boundary_and_todo_rules_survive(self):
        harness, _ = self.harness()
        self.assertTrue(harness.builtins.run_read("../outside").startswith("Error:"))
        result = harness.todo.update(
            [
                {"content": "one", "status": "in_progress"},
                {"content": "two", "status": "in_progress"},
            ]
        )
        self.assertIn("at most one", result)


if __name__ == "__main__":
    unittest.main()
