import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


CODE_PATH = Path(__file__).parents[1] / "s05_todo_write" / "code.py"
SPEC = importlib.util.spec_from_file_location("s05_code", CODE_PATH)
s05 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = s05
assert SPEC.loader is not None
SPEC.loader.exec_module(s05)


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

    def create(self, **kwargs):
        self.calls += 1
        self.requests.append(list(kwargs["messages"]))
        index = self.calls - 1
        if index < len(self.tool_names):
            call = tool_call(self.tool_names[index], f"call-{self.calls}")
            message = FakeMessage(None, [call])
        else:
            message = FakeMessage("done")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class TodoWriteTest(unittest.TestCase):
    def setUp(self):
        self.saved_todos = list(s05.CURRENT_TODOS)
        self.saved_hooks = {
            event: list(callbacks)
            for event, callbacks in s05.HOOKS.items()
        }

    def tearDown(self):
        s05.CURRENT_TODOS = self.saved_todos
        for event, callbacks in self.saved_hooks.items():
            s05.HOOKS[event][:] = callbacks

    def clear_hooks(self):
        for callbacks in s05.HOOKS.values():
            callbacks.clear()

    def test_todo_tool_is_declared_and_registered(self):
        declared = {tool["function"]["name"] for tool in s05.TOOLS}
        self.assertIn("todo_write", declared)
        self.assertIs(s05.run_todo_write, s05.TOOL_HANDLERS["todo_write"])
        self.assertEqual(6, len(declared))

    def test_valid_todos_replace_state(self):
        todos = [
            {"content": "inspect", "status": "completed"},
            {"content": "implement", "status": "in_progress"},
            {"content": "verify", "status": "pending"},
        ]

        with patch("builtins.print") as print_mock:
            result = s05.run_todo_write(todos)

        self.assertEqual(todos, s05.CURRENT_TODOS)
        self.assertEqual("Updated 3 tasks (1 completed)", result)
        rendered = str(print_mock.call_args)
        self.assertIn("[✓] inspect", rendered)
        self.assertIn("[▸] implement", rendered)
        self.assertIn("[ ] verify", rendered)

    def test_input_is_copied_and_normalized(self):
        todos = [{"content": "  inspect  ", "status": "pending"}]
        s05.run_todo_write(todos)
        todos[0]["content"] = "mutated outside"

        self.assertEqual("inspect", s05.CURRENT_TODOS[0]["content"])

    def test_multiple_in_progress_is_rejected_atomically(self):
        original = [{"content": "keep", "status": "pending"}]
        s05.CURRENT_TODOS = list(original)

        result = s05.run_todo_write(
            [
                {"content": "one", "status": "in_progress"},
                {"content": "two", "status": "in_progress"},
            ]
        )

        self.assertIn("at most one", result)
        self.assertEqual(original, s05.CURRENT_TODOS)

    def test_invalid_item_is_rejected_atomically(self):
        original = [{"content": "keep", "status": "pending"}]
        s05.CURRENT_TODOS = list(original)

        invalid_lists = [
            [{"content": "", "status": "pending"}],
            [{"content": "x", "status": "unknown"}],
            [{"content": "x", "status": "pending", "extra": True}],
            ["not an object"],
        ]
        for todos in invalid_lists:
            with self.subTest(todos=todos):
                result = s05.run_todo_write(todos)
                self.assertTrue(result.startswith("Error:"))
                self.assertEqual(original, s05.CURRENT_TODOS)

    def test_pending_without_active_task_returns_guidance(self):
        result = s05.run_todo_write(
            [
                {"content": "inspect", "status": "completed"},
                {"content": "implement", "status": "pending"},
            ]
        )

        self.assertIn("mark one pending task in_progress", result)

    def test_empty_list_clears_plan(self):
        s05.CURRENT_TODOS = [{"content": "old", "status": "completed"}]
        result = s05.run_todo_write([])

        self.assertEqual([], s05.CURRENT_TODOS)
        self.assertEqual("Updated 0 tasks (0 completed)", result)

    def test_execute_tool_updates_plan_through_dispatch(self):
        self.clear_hooks()
        payload = {
            "todos": [
                {"content": "plan", "status": "in_progress"},
                {"content": "work", "status": "pending"},
            ]
        }

        result = s05.execute_tool("todo_write", json.dumps(payload))

        self.assertIn("Updated 2 tasks", result)
        self.assertEqual(payload["todos"], s05.CURRENT_TODOS)

    def test_three_non_todo_rounds_inject_reminder(self):
        self.clear_hooks()
        completions = ScriptedCompletions(["glob", "glob", "glob"])
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )
        messages = [{"role": "user", "content": "long task"}]

        with patch.object(s05, "execute_tool", return_value="ok"):
            answer = s05.agent_loop(client, messages)

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

        with patch.object(s05, "execute_tool", return_value="ok"):
            answer = s05.agent_loop(client, messages)

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
        s05.register_hook(
            "PreToolUse",
            lambda request: events.append(("pre", request.name)),
        )
        s05.register_hook(
            "PostToolUse",
            lambda request, output: events.append(("post", request.name)),
        )

        s05.execute_tool(
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
