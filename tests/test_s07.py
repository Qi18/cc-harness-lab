import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


CODE_PATH = Path(__file__).parents[1] / "s07_skill_loading" / "code.py"
SPEC = importlib.util.spec_from_file_location("s07_code", CODE_PATH)
s07 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = s07
assert SPEC.loader is not None
SPEC.loader.exec_module(s07)


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
    arguments = (
        {"todos": [{"content": "update plan", "status": "in_progress"}]}
        if name == "todo_write"
        else {"pattern": "*.py"}
    )
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
        self.tool_requests.append(list(kwargs["tools"]))
        index = self.calls - 1
        if index < len(self.tool_names):
            call = tool_call(self.tool_names[index], f"call-{self.calls}")
            message = FakeMessage(None, [call])
        else:
            message = FakeMessage("done")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class SkillLoadingTest(unittest.TestCase):
    def setUp(self):
        self.saved_todos = list(s07.CURRENT_TODOS)
        self.saved_client = s07.ACTIVE_CLIENT
        self.saved_registry = dict(s07.SKILL_REGISTRY)
        self.saved_hooks = {
            event: list(callbacks)
            for event, callbacks in s07.HOOKS.items()
        }

    def tearDown(self):
        s07.CURRENT_TODOS = self.saved_todos
        s07.ACTIVE_CLIENT = self.saved_client
        s07.SKILL_REGISTRY = self.saved_registry
        for event, callbacks in self.saved_hooks.items():
            s07.HOOKS[event][:] = callbacks

    def clear_hooks(self):
        for callbacks in s07.HOOKS.values():
            callbacks.clear()

    def test_parent_and_subagent_tools_have_expected_boundaries(self):
        declared = {tool["function"]["name"] for tool in s07.TOOLS}
        self.assertIn("todo_write", declared)
        self.assertIn("task", declared)
        self.assertIn("load_skill", declared)
        self.assertIs(s07.run_todo_write, s07.TOOL_HANDLERS["todo_write"])
        self.assertIs(s07.spawn_subagent, s07.TOOL_HANDLERS["task"])
        self.assertIs(s07.run_load_skill, s07.TOOL_HANDLERS["load_skill"])
        self.assertEqual(8, len(declared))

        sub_declared = {
            tool["function"]["name"] for tool in s07.SUB_TOOLS
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
        self.assertNotIn("task", s07.SUB_TOOL_HANDLERS)
        self.assertNotIn("todo_write", s07.SUB_TOOL_HANDLERS)
        self.assertIn("load_skill", s07.SUB_TOOL_HANDLERS)

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

        metadata, body = s07.parse_skill_frontmatter(raw)

        self.assertEqual("review", metadata["name"])
        self.assertEqual("Review changes for correctness.", metadata["description"])
        self.assertEqual("# Instructions\n\nCheck tests.", body)

    def test_text_without_frontmatter_is_its_own_body(self):
        raw = "# Plain Skill\n\nDo the work."
        metadata, body = s07.parse_skill_frontmatter(raw)

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

            registry = s07.scan_skills(skills_dir)

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

        prompt = s07.build_system_prompt("base", registry)

        self.assertIn("- review: Review code.", prompt)
        self.assertNotIn("SECRET FULL INSTRUCTIONS", prompt)

    def test_load_skill_returns_full_manifest_by_registered_name(self):
        raw = "---\nname: review\ndescription: Review code.\n---\nFull body"
        s07.SKILL_REGISTRY = {
            "review": {
                "name": "review",
                "description": "Review code.",
                "content": raw,
                "path": "/skills/review/SKILL.md",
            }
        }

        self.assertEqual(raw, s07.run_load_skill("review"))
        self.assertIn("Available: review", s07.run_load_skill("missing"))

    def test_load_skill_uses_normal_dispatch_and_hooks(self):
        events = []
        self.clear_hooks()
        s07.SKILL_REGISTRY = {
            "review": {
                "name": "review",
                "description": "Review code.",
                "content": "full instructions",
                "path": "/skills/review/SKILL.md",
            }
        }
        s07.register_hook(
            "PreToolUse",
            lambda request: events.append(("pre", request.name)),
        )
        s07.register_hook(
            "PostToolUse",
            lambda request, output: events.append(("post", request.name)),
        )

        result = s07.execute_tool("load_skill", json.dumps({"name": "review"}))

        self.assertEqual("full instructions", result)
        self.assertEqual(
            [("pre", "load_skill"), ("post", "load_skill")],
            events,
        )

    def test_subagent_gets_fresh_messages_and_returns_only_conclusion(self):
        self.clear_hooks()
        completions = ScriptedCompletions(["glob"])
        s07.ACTIVE_CLIENT = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )

        with patch.object(s07, "execute_sub_tool", return_value="a.py"):
            result = s07.spawn_subagent("inspect the project")

        self.assertEqual("done", result)
        self.assertEqual(2, completions.calls)
        self.assertEqual(
            [
                {"role": "system", "content": s07.SUB_SYSTEM},
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
        s07.register_hook(
            "PreToolUse",
            lambda request: events.append(("pre", request.name)),
        )
        s07.register_hook(
            "PostToolUse",
            lambda request, output: events.append(("post", request.name)),
        )

        result = s07.execute_sub_tool("glob", json.dumps({"pattern": "*.py"}))

        self.assertIsInstance(result, str)
        self.assertEqual([("pre", "glob"), ("post", "glob")], events)

    def test_subagent_cannot_dispatch_task_recursively(self):
        self.clear_hooks()
        result = s07.execute_sub_tool(
            "task",
            json.dumps({"description": "spawn another child"}),
        )

        self.assertEqual("Error: unknown tool 'task'", result)

    def test_subagent_stops_at_round_limit(self):
        self.clear_hooks()
        completions = ScriptedCompletions(["glob", "glob", "glob"])
        s07.ACTIVE_CLIENT = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )

        with (
            patch.object(s07, "MAX_SUBAGENT_ROUNDS", 2),
            patch.object(s07, "execute_sub_tool", return_value="ok"),
        ):
            result = s07.spawn_subagent("keep looking")

        self.assertEqual("Error: subagent exceeded 2 rounds", result)
        self.assertEqual(2, completions.calls)

    def test_subagent_requires_active_parent_client(self):
        s07.ACTIVE_CLIENT = None
        self.assertEqual(
            "Error: no active model client for subagent",
            s07.spawn_subagent("inspect"),
        )

    def test_valid_todos_replace_state(self):
        todos = [
            {"content": "inspect", "status": "completed"},
            {"content": "implement", "status": "in_progress"},
            {"content": "verify", "status": "pending"},
        ]

        with patch("builtins.print") as print_mock:
            result = s07.run_todo_write(todos)

        self.assertEqual(todos, s07.CURRENT_TODOS)
        self.assertEqual("Updated 3 tasks (1 completed)", result)
        rendered = str(print_mock.call_args)
        self.assertIn("[✓] inspect", rendered)
        self.assertIn("[▸] implement", rendered)
        self.assertIn("[ ] verify", rendered)

    def test_input_is_copied_and_normalized(self):
        todos = [{"content": "  inspect  ", "status": "pending"}]
        s07.run_todo_write(todos)
        todos[0]["content"] = "mutated outside"

        self.assertEqual("inspect", s07.CURRENT_TODOS[0]["content"])

    def test_multiple_in_progress_is_rejected_atomically(self):
        original = [{"content": "keep", "status": "pending"}]
        s07.CURRENT_TODOS = list(original)

        result = s07.run_todo_write(
            [
                {"content": "one", "status": "in_progress"},
                {"content": "two", "status": "in_progress"},
            ]
        )

        self.assertIn("at most one", result)
        self.assertEqual(original, s07.CURRENT_TODOS)

    def test_invalid_item_is_rejected_atomically(self):
        original = [{"content": "keep", "status": "pending"}]
        s07.CURRENT_TODOS = list(original)

        invalid_lists = [
            [{"content": "", "status": "pending"}],
            [{"content": "x", "status": "unknown"}],
            [{"content": "x", "status": "pending", "extra": True}],
            ["not an object"],
        ]
        for todos in invalid_lists:
            with self.subTest(todos=todos):
                result = s07.run_todo_write(todos)
                self.assertTrue(result.startswith("Error:"))
                self.assertEqual(original, s07.CURRENT_TODOS)

    def test_pending_without_active_task_returns_guidance(self):
        result = s07.run_todo_write(
            [
                {"content": "inspect", "status": "completed"},
                {"content": "implement", "status": "pending"},
            ]
        )

        self.assertIn("mark one pending task in_progress", result)

    def test_empty_list_clears_plan(self):
        s07.CURRENT_TODOS = [{"content": "old", "status": "completed"}]
        result = s07.run_todo_write([])

        self.assertEqual([], s07.CURRENT_TODOS)
        self.assertEqual("Updated 0 tasks (0 completed)", result)

    def test_execute_tool_updates_plan_through_dispatch(self):
        self.clear_hooks()
        payload = {
            "todos": [
                {"content": "plan", "status": "in_progress"},
                {"content": "work", "status": "pending"},
            ]
        }

        result = s07.execute_tool("todo_write", json.dumps(payload))

        self.assertIn("Updated 2 tasks", result)
        self.assertEqual(payload["todos"], s07.CURRENT_TODOS)

    def test_three_non_todo_rounds_inject_reminder(self):
        self.clear_hooks()
        completions = ScriptedCompletions(["glob", "glob", "glob"])
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )
        messages = [{"role": "user", "content": "long task"}]

        with patch.object(s07, "execute_tool", return_value="ok"):
            answer = s07.agent_loop(client, messages)

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

        with patch.object(s07, "execute_tool", return_value="ok"):
            answer = s07.agent_loop(client, messages)

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
        s07.register_hook(
            "PreToolUse",
            lambda request: events.append(("pre", request.name)),
        )
        s07.register_hook(
            "PostToolUse",
            lambda request, output: events.append(("post", request.name)),
        )

        s07.execute_tool(
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
