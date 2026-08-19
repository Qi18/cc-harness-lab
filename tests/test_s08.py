import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


CODE_PATH = Path(__file__).parents[1] / "s08_context_compact" / "code.py"
SPEC = importlib.util.spec_from_file_location("s08_code", CODE_PATH)
s08 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = s08
assert SPEC.loader is not None
SPEC.loader.exec_module(s08)


class FakeMessage:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = None

    def model_dump(self, exclude_none=True):
        message = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in self.tool_calls
            ]
        return message


def tool_call(name, call_id):
    if name == "todo_write":
        arguments = {
            "todos": [{"content": "update plan", "status": "in_progress"}]
        }
    elif name == "compact":
        arguments = {}
    else:
        arguments = {"pattern": "*.py"}
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments),
        ),
    )


class ScriptedCompletions:
    def __init__(self, tool_names):
        self.tool_names = list(tool_names)
        self.calls = 0
        self.requests = []
        self.tool_requests = []

    def create(self, **kwargs):
        self.calls += 1
        self.requests.append(list(kwargs["messages"]))
        self.tool_requests.append(list(kwargs.get("tools", [])))
        index = self.calls - 1
        if index < len(self.tool_names):
            call = tool_call(self.tool_names[index], f"call-{self.calls}")
            message = FakeMessage(None, [call])
        else:
            message = FakeMessage("done")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class ContextCompactTest(unittest.TestCase):
    def make_compactor(self, root, summary="summary"):
        completions = ScriptedCompletions([])
        completions.tool_names = []

        def summarize(**kwargs):
            completions.calls += 1
            completions.requests.append(list(kwargs["messages"]))
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(message=FakeMessage(summary))
                ]
            )

        completions.create = summarize
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        return s08.ContextCompactor(
            client,
            "test-model",
            Path(root) / "transcripts",
            Path(root) / "tool-results",
        )

    def setUp(self):
        self.saved_todos = list(s08.CURRENT_TODOS)
        self.saved_client = s08.ACTIVE_CLIENT
        self.saved_registry = dict(s08.SKILL_REGISTRY)
        self.saved_hooks = {
            event: list(callbacks)
            for event, callbacks in s08.HOOKS.items()
        }

    def tearDown(self):
        s08.CURRENT_TODOS = self.saved_todos
        s08.ACTIVE_CLIENT = self.saved_client
        s08.SKILL_REGISTRY = self.saved_registry
        for event, callbacks in self.saved_hooks.items():
            s08.HOOKS[event][:] = callbacks

    def clear_hooks(self):
        for callbacks in s08.HOOKS.values():
            callbacks.clear()

    def test_parent_and_subagent_tools_have_expected_boundaries(self):
        declared = {tool["function"]["name"] for tool in s08.TOOLS}
        self.assertIn("todo_write", declared)
        self.assertIn("task", declared)
        self.assertIn("load_skill", declared)
        self.assertIn("compact", declared)
        self.assertIs(s08.run_todo_write, s08.TOOL_HANDLERS["todo_write"])
        self.assertIs(s08.spawn_subagent, s08.TOOL_HANDLERS["task"])
        self.assertIs(s08.run_load_skill, s08.TOOL_HANDLERS["load_skill"])
        self.assertEqual(9, len(declared))

        sub_declared = {
            tool["function"]["name"] for tool in s08.SUB_TOOLS
        }
        self.assertEqual(
            {
                "bash",
                "read_file",
                "write_file",
                "edit_file",
                "glob",
                "load_skill",
            },
            sub_declared,
        )
        self.assertNotIn("task", s08.SUB_TOOL_HANDLERS)
        self.assertNotIn("todo_write", s08.SUB_TOOL_HANDLERS)
        self.assertIn("load_skill", s08.SUB_TOOL_HANDLERS)
        self.assertNotIn("compact", s08.SUB_TOOL_HANDLERS)

    def test_tool_result_budget_persists_largest_trailing_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            compactor = self.make_compactor(temporary)
            messages = [
                {"role": "assistant", "content": None, "tool_calls": [{"id": "a"}]},
                {"role": "tool", "tool_call_id": "a", "content": "A" * 5_000},
                {"role": "tool", "tool_call_id": "b", "content": "B" * 5_000},
            ]

            result = compactor.tool_result_budget(messages, max_chars=6_000)
            persisted = list((Path(temporary) / "tool-results").glob("*.txt"))

            self.assertIs(result, messages)
            self.assertGreaterEqual(len(persisted), 1)
            self.assertIn("<persisted-output>", messages[1]["content"])
            persisted_contents = {
                path.read_text(encoding="utf-8") for path in persisted
            }
            self.assertIn("A" * 5_000, persisted_contents)

    def test_micro_compact_keeps_only_three_recent_tool_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            compactor = self.make_compactor(temporary)
            messages = [
                {
                    "role": "tool",
                    "tool_call_id": str(index),
                    "content": str(index) * 200,
                }
                for index in range(5)
            ]

            compactor.micro_compact(messages)

            self.assertIn("Earlier tool result compacted", messages[0]["content"])
            self.assertIn("Earlier tool result compacted", messages[1]["content"])
            self.assertEqual("2" * 200, messages[2]["content"])
            self.assertEqual("4" * 200, messages[4]["content"])

    def test_snip_compact_preserves_tool_call_result_groups(self):
        with tempfile.TemporaryDirectory() as temporary:
            compactor = self.make_compactor(temporary)
            messages = [{"role": "system", "content": "system"}]
            for index in range(30):
                messages.extend(
                    [
                        {"role": "user", "content": f"question {index}"},
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{"id": f"call-{index}"}],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": f"call-{index}",
                            "content": f"result {index}",
                        },
                    ]
                )

            compacted = compactor.snip_compact(messages, max_messages=20)

            self.assertTrue(
                any("snipped" in str(message.get("content")) for message in compacted)
            )
            for index, message in enumerate(compacted):
                if message.get("role") != "tool":
                    continue
                group_start = index - 1
                while group_start >= 0 and compacted[group_start].get("role") == "tool":
                    group_start -= 1
                self.assertGreaterEqual(group_start, 0)
                self.assertTrue(compactor.has_tool_use(compacted[group_start]))
            self.assertEqual(
                1,
                len(list((Path(temporary) / "transcripts").glob("*.jsonl"))),
            )

    def test_prepare_runs_budget_before_snip_before_micro(self):
        with tempfile.TemporaryDirectory() as temporary:
            compactor = self.make_compactor(temporary)
            events = []
            messages = [{"role": "user", "content": "small"}]

            with (
                patch.object(
                    compactor,
                    "tool_result_budget",
                    side_effect=lambda value: events.append("budget") or value,
                ),
                patch.object(
                    compactor,
                    "snip_compact",
                    side_effect=lambda value: events.append("snip") or value,
                ),
                patch.object(
                    compactor,
                    "micro_compact",
                    side_effect=lambda value: events.append("micro") or value,
                ),
            ):
                compactor.prepare(messages, "request")

            self.assertEqual(["budget", "snip", "micro"], events)

    def test_compact_history_keeps_system_and_archives_full_transcript(self):
        with tempfile.TemporaryDirectory() as temporary:
            compactor = self.make_compactor(temporary, summary="factual state")
            messages = [
                {"role": "system", "content": "system rules"},
                {"role": "user", "content": "original request"},
                {"role": "assistant", "content": "work in progress"},
            ]

            compacted = compactor.compact_history(messages, "current request")
            transcripts = list((Path(temporary) / "transcripts").glob("*.jsonl"))

            self.assertEqual(messages[0], compacted[0])
            self.assertEqual(2, len(compacted))
            self.assertIn("[Compacted]", compacted[1]["content"])
            self.assertIn("current request", compacted[1]["content"])
            self.assertIn("factual state", compacted[1]["content"])
            self.assertEqual(1, len(transcripts))
            self.assertEqual(3, len(transcripts[0].read_text().splitlines()))

    def test_manual_compact_tool_replaces_history_then_continues(self):
        self.clear_hooks()
        completions = ScriptedCompletions(["compact", "compact"])
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "compact now"},
        ]

        class FakeCompactor:
            def __init__(self):
                self.compact_calls = 0

            def prepare(self, value, active_request):
                return value

            def compact_history(self, value, active_request):
                self.compact_calls += 1
                return [value[0], {"role": "user", "content": "[Compacted]"}]

        compactor = FakeCompactor()
        answer = s08.agent_loop(
            client,
            messages,
            active_request="compact now",
            compactor=compactor,
        )

        self.assertEqual("done", answer)
        self.assertEqual(1, compactor.compact_calls)
        self.assertEqual(3, completions.calls)
        self.assertTrue(
            any(
                message.get("role") == "tool"
                and "already completed" in str(message.get("content"))
                for message in messages
            )
        )
        second_request_tools = {
            tool["function"]["name"]
            for tool in completions.tool_requests[1]
        }
        self.assertNotIn("compact", second_request_tools)

    def test_prompt_too_long_reactively_compacts_and_retries_once(self):
        self.clear_hooks()

        class OverflowThenDone:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("context_length_exceeded")
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=FakeMessage("done"))]
                )

        class FakeCompactor:
            def __init__(self):
                self.reactive_calls = 0

            def prepare(self, value, active_request):
                return value

            def reactive_compact(self, value, active_request):
                self.reactive_calls += 1
                return [value[0], {"role": "user", "content": "[Reactive compact]"}]

        completions = OverflowThenDone()
        compactor = FakeCompactor()
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "request"},
        ]

        answer = s08.agent_loop(
            client,
            messages,
            active_request="request",
            compactor=compactor,
        )

        self.assertEqual("done", answer)
        self.assertEqual(2, completions.calls)
        self.assertEqual(1, compactor.reactive_calls)

    def test_auto_compact_failure_breaker_raises_on_third_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            compactor = self.make_compactor(temporary)
            compactor.CONTEXT_CHAR_LIMIT = 1
            messages = [{"role": "user", "content": "large"}]

            with patch.object(
                compactor,
                "compact_history",
                side_effect=RuntimeError("summary failed"),
            ):
                self.assertIs(messages, compactor.prepare(messages, "request"))
                self.assertIs(messages, compactor.prepare(messages, "request"))
                with self.assertRaisesRegex(RuntimeError, "summary failed"):
                    compactor.prepare(messages, "request")

    def test_parse_frontmatter_returns_metadata_and_body(self):
        raw = (
            "---\n"
            "name: review\n"
            "description: >-\n"
            "  Review changes for\n"
            "  correctness.\n"
            "---\n"
            "# Instructions\n\nCheck tests.\n"
        )

        metadata, body = s08.parse_skill_frontmatter(raw)

        self.assertEqual("review", metadata["name"])
        self.assertEqual("Review changes for correctness.", metadata["description"])
        self.assertEqual("# Instructions\n\nCheck tests.", body)

    def test_text_without_frontmatter_is_its_own_body(self):
        raw = "# Plain Skill\n\nDo the work."
        metadata, body = s08.parse_skill_frontmatter(raw)

        self.assertEqual({}, metadata)
        self.assertEqual(raw, body)

    def test_scan_skills_builds_registry_and_skips_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            skills_dir = base / "skills"
            review = skills_dir / "review-folder"
            fallback = skills_dir / "fallback"
            external = base / "external"
            review.mkdir(parents=True)
            fallback.mkdir()
            external.mkdir()
            (review / "SKILL.md").write_text(
                "---\nname: code-review\ndescription: Review code safely.\n---\nBody",
                encoding="utf-8",
            )
            (fallback / "SKILL.md").write_text(
                "# Fallback description\n\nDetails",
                encoding="utf-8",
            )
            (external / "SKILL.md").write_text("Outside", encoding="utf-8")
            (skills_dir / "escaped").symlink_to(external, target_is_directory=True)

            registry = s08.scan_skills(skills_dir)

        self.assertEqual({"code-review", "fallback"}, set(registry))
        self.assertEqual("Review code safely.", registry["code-review"]["description"])
        self.assertEqual(
            "Fallback description",
            registry["fallback"]["description"],
        )

    def test_system_catalog_excludes_full_skill_body(self):
        registry = {
            "review": {
                "name": "review",
                "description": "Review code.",
                "content": "SECRET FULL INSTRUCTIONS",
                "path": "/skills/review/SKILL.md",
            }
        }

        prompt = s08.build_system_prompt("base", registry)

        self.assertIn("- review: Review code.", prompt)
        self.assertNotIn("SECRET FULL INSTRUCTIONS", prompt)

    def test_load_skill_returns_full_manifest_by_registered_name(self):
        raw = "---\nname: review\ndescription: Review code.\n---\nFull body"
        s08.SKILL_REGISTRY = {
            "review": {
                "name": "review",
                "description": "Review code.",
                "content": raw,
                "path": "/skills/review/SKILL.md",
            }
        }

        self.assertEqual(raw, s08.run_load_skill("review"))
        self.assertIn("Available: review", s08.run_load_skill("missing"))

    def test_load_skill_uses_normal_dispatch_and_hooks(self):
        events = []
        self.clear_hooks()
        s08.SKILL_REGISTRY = {
            "review": {
                "name": "review",
                "description": "Review code.",
                "content": "full instructions",
                "path": "/skills/review/SKILL.md",
            }
        }
        s08.register_hook(
            "PreToolUse",
            lambda request: events.append(("pre", request.name)),
        )
        s08.register_hook(
            "PostToolUse",
            lambda request, output: events.append(("post", request.name)),
        )

        result = s08.execute_tool("load_skill", json.dumps({"name": "review"}))

        self.assertEqual("full instructions", result)
        self.assertEqual(
            [("pre", "load_skill"), ("post", "load_skill")],
            events,
        )

    def test_subagent_gets_fresh_messages_and_returns_only_conclusion(self):
        self.clear_hooks()
        completions = ScriptedCompletions(["glob"])
        s08.ACTIVE_CLIENT = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )

        with patch.object(s08, "execute_sub_tool", return_value="a.py"):
            result = s08.spawn_subagent("inspect the project")

        self.assertEqual("done", result)
        self.assertEqual(2, completions.calls)
        self.assertEqual(
            [
                {"role": "system", "content": s08.SUB_SYSTEM},
                {"role": "user", "content": "inspect the project"},
            ],
            completions.requests[0],
        )
        self.assertEqual("tool", completions.requests[1][-1]["role"])
        exposed = {
            tool["function"]["name"]
            for tool in completions.tool_requests[0]
        }
        self.assertEqual(
            {
                "bash",
                "read_file",
                "write_file",
                "edit_file",
                "glob",
                "load_skill",
            },
            exposed,
        )

    def test_subagent_tool_calls_still_run_hooks(self):
        events = []
        self.clear_hooks()
        s08.register_hook(
            "PreToolUse",
            lambda request: events.append(("pre", request.name)),
        )
        s08.register_hook(
            "PostToolUse",
            lambda request, output: events.append(("post", request.name)),
        )

        result = s08.execute_sub_tool("glob", json.dumps({"pattern": "*.py"}))

        self.assertIsInstance(result, str)
        self.assertEqual([("pre", "glob"), ("post", "glob")], events)

    def test_subagent_cannot_dispatch_task_recursively(self):
        self.clear_hooks()
        result = s08.execute_sub_tool(
            "task",
            json.dumps({"description": "spawn another child"}),
        )

        self.assertEqual("Error: unknown tool 'task'", result)

    def test_subagent_stops_at_round_limit(self):
        self.clear_hooks()
        completions = ScriptedCompletions(["glob", "glob", "glob"])
        s08.ACTIVE_CLIENT = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )

        with (
            patch.object(s08, "MAX_SUBAGENT_ROUNDS", 2),
            patch.object(s08, "execute_sub_tool", return_value="ok"),
        ):
            result = s08.spawn_subagent("keep looking")

        self.assertEqual("Error: subagent exceeded 2 rounds", result)
        self.assertEqual(2, completions.calls)

    def test_subagent_requires_active_parent_client(self):
        s08.ACTIVE_CLIENT = None
        self.assertEqual(
            "Error: no active model client for subagent",
            s08.spawn_subagent("inspect"),
        )

    def test_valid_todos_replace_state(self):
        todos = [
            {"content": "inspect", "status": "completed"},
            {"content": "implement", "status": "in_progress"},
            {"content": "verify", "status": "pending"},
        ]

        with patch("builtins.print") as print_mock:
            result = s08.run_todo_write(todos)

        self.assertEqual(todos, s08.CURRENT_TODOS)
        self.assertEqual("Updated 3 tasks (1 completed)", result)
        rendered = str(print_mock.call_args)
        self.assertIn("[✓] inspect", rendered)
        self.assertIn("[▸] implement", rendered)
        self.assertIn("[ ] verify", rendered)

    def test_input_is_copied_and_normalized(self):
        todos = [{"content": "  inspect  ", "status": "pending"}]
        s08.run_todo_write(todos)
        todos[0]["content"] = "mutated outside"

        self.assertEqual("inspect", s08.CURRENT_TODOS[0]["content"])

    def test_multiple_in_progress_is_rejected_atomically(self):
        original = [{"content": "keep", "status": "pending"}]
        s08.CURRENT_TODOS = list(original)

        result = s08.run_todo_write(
            [
                {"content": "one", "status": "in_progress"},
                {"content": "two", "status": "in_progress"},
            ]
        )

        self.assertIn("at most one", result)
        self.assertEqual(original, s08.CURRENT_TODOS)

    def test_invalid_item_is_rejected_atomically(self):
        original = [{"content": "keep", "status": "pending"}]
        s08.CURRENT_TODOS = list(original)

        invalid_lists = [
            [{"content": "", "status": "pending"}],
            [{"content": "x", "status": "unknown"}],
            [{"content": "x", "status": "pending", "extra": True}],
            ["not an object"],
        ]
        for todos in invalid_lists:
            with self.subTest(todos=todos):
                result = s08.run_todo_write(todos)
                self.assertTrue(result.startswith("Error:"))
                self.assertEqual(original, s08.CURRENT_TODOS)

    def test_pending_without_active_task_returns_guidance(self):
        result = s08.run_todo_write(
            [
                {"content": "inspect", "status": "completed"},
                {"content": "implement", "status": "pending"},
            ]
        )

        self.assertIn("mark one pending task in_progress", result)

    def test_empty_list_clears_plan(self):
        s08.CURRENT_TODOS = [{"content": "old", "status": "completed"}]
        result = s08.run_todo_write([])

        self.assertEqual([], s08.CURRENT_TODOS)
        self.assertEqual("Updated 0 tasks (0 completed)", result)

    def test_execute_tool_updates_plan_through_dispatch(self):
        self.clear_hooks()
        payload = {
            "todos": [
                {"content": "plan", "status": "in_progress"},
                {"content": "work", "status": "pending"},
            ]
        }

        result = s08.execute_tool("todo_write", json.dumps(payload))

        self.assertIn("Updated 2 tasks", result)
        self.assertEqual(payload["todos"], s08.CURRENT_TODOS)

    def test_three_non_todo_rounds_inject_reminder(self):
        self.clear_hooks()
        completions = ScriptedCompletions(["glob", "glob", "glob"])
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )
        messages = [{"role": "user", "content": "long task"}]

        with patch.object(s08, "execute_tool", return_value="ok"):
            answer = s08.agent_loop(client, messages)

        self.assertEqual("done", answer)
        self.assertEqual(4, completions.calls)
        reminders = [
            message
            for message in completions.requests[3]
            if message.get("role") == "user"
            and "<reminder>" in str(message.get("content"))
        ]
        self.assertEqual(1, len(reminders))

    def test_todo_call_resets_reminder_counter(self):
        self.clear_hooks()
        sequence = ["glob", "glob", "todo_write", "glob", "glob"]
        completions = ScriptedCompletions(sequence)
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )
        messages = [{"role": "user", "content": "long task"}]

        with patch.object(s08, "execute_tool", return_value="ok"):
            answer = s08.agent_loop(client, messages)

        self.assertEqual("done", answer)
        self.assertEqual(6, completions.calls)
        reminders = [
            message
            for request in completions.requests
            for message in request
            if message.get("role") == "user"
            and "<reminder>" in str(message.get("content"))
        ]
        self.assertEqual([], reminders)

    def test_todo_round_does_not_remove_hooks(self):
        events = []
        self.clear_hooks()
        s08.register_hook(
            "PreToolUse",
            lambda request: events.append(("pre", request.name)),
        )
        s08.register_hook(
            "PostToolUse",
            lambda request, output: events.append(("post", request.name)),
        )

        s08.execute_tool(
            "todo_write",
            json.dumps(
                {"todos": [{"content": "plan", "status": "in_progress"}]}
            ),
        )

        self.assertEqual(
            [("pre", "todo_write"), ("post", "todo_write")],
            events,
        )


if __name__ == "__main__":
    unittest.main()
