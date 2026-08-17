import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


CODE_PATH = Path(__file__).parents[1] / "s04_hooks" / "code.py"
SPEC = importlib.util.spec_from_file_location("s04_code", CODE_PATH)
s04 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = s04
assert SPEC.loader is not None
SPEC.loader.exec_module(s04)


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


class StopCompletions:
    def __init__(self):
        self.calls = 0
        self.requests = []

    def create(self, **kwargs):
        self.calls += 1
        self.requests.append(list(kwargs["messages"]))
        content = "first answer" if self.calls == 1 else "final answer"
        message = FakeMessage(content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class ToolCompletions:
    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            tool_call = SimpleNamespace(
                id="call-read",
                function=SimpleNamespace(
                    name="read_file",
                    arguments=json.dumps({"path": "README.md"}),
                ),
            )
            message = FakeMessage(None, [tool_call])
        else:
            message = FakeMessage("done")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class HooksTest(unittest.TestCase):
    def setUp(self):
        self.saved_hooks = {
            event: list(callbacks)
            for event, callbacks in s04.HOOKS.items()
        }

    def tearDown(self):
        for event, callbacks in self.saved_hooks.items():
            s04.HOOKS[event][:] = callbacks

    def clear_hooks(self):
        for callbacks in s04.HOOKS.values():
            callbacks.clear()

    def test_default_hooks_cover_all_four_events(self):
        self.assertEqual(1, len(s04.HOOKS["UserPromptSubmit"]))
        self.assertEqual(2, len(s04.HOOKS["PreToolUse"]))
        self.assertEqual(1, len(s04.HOOKS["PostToolUse"]))
        self.assertEqual(1, len(s04.HOOKS["Stop"]))

    def test_hooks_run_in_order_and_stop_on_non_none(self):
        self.clear_hooks()
        calls = []

        def first(value):
            calls.append(("first", value))
            return None

        def blocker(value):
            calls.append(("blocker", value))
            return "blocked"

        def unreachable(value):
            calls.append(("unreachable", value))
            return None

        s04.register_hook("PreToolUse", first)
        s04.register_hook("PreToolUse", blocker)
        s04.register_hook("PreToolUse", unreachable)

        result = s04.trigger_hooks("PreToolUse", "request")

        self.assertEqual("blocked", result)
        self.assertEqual(
            [("first", "request"), ("blocker", "request")],
            calls,
        )

    def test_unknown_hook_event_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown hook event"):
            s04.register_hook("Missing", lambda: None)
        with self.assertRaisesRegex(ValueError, "Unknown hook event"):
            s04.trigger_hooks("Missing")

    def test_pre_hook_can_block_handler(self):
        self.clear_hooks()
        handler = Mock(return_value="must not run")
        s04.register_hook("PreToolUse", lambda request: "blocked by test")

        with patch.dict(s04.TOOL_HANDLERS, {"bash": handler}):
            result = s04.execute_tool(
                "bash",
                json.dumps({"command": "printf hello"}),
            )

        handler.assert_not_called()
        self.assertEqual("blocked by test", result)

    def test_pre_handler_post_order(self):
        self.clear_hooks()
        calls = []

        def pre(request):
            calls.append(("pre", request.name))
            return None

        def handler(command):
            calls.append(("handler", command))
            return "handler output"

        def post(request, output):
            calls.append(("post", request.name, output))
            return None

        s04.register_hook("PreToolUse", pre)
        s04.register_hook("PostToolUse", post)

        with patch.dict(s04.TOOL_HANDLERS, {"bash": handler}):
            result = s04.execute_tool(
                "bash",
                json.dumps({"command": "printf hello"}),
            )

        self.assertEqual("handler output", result)
        self.assertEqual(
            [
                ("pre", "bash"),
                ("handler", "printf hello"),
                ("post", "bash", "handler output"),
            ],
            calls,
        )

    def test_permission_is_a_pre_tool_hook(self):
        handler = Mock(return_value="must not run")
        with patch.dict(s04.TOOL_HANDLERS, {"bash": handler}):
            result = s04.execute_tool(
                "bash",
                json.dumps({"command": "sudo whoami"}),
            )

        handler.assert_not_called()
        self.assertIn("Permission denied", result)

    def test_large_output_post_hook_warns(self):
        request = s04.ToolRequest("read_file", {"path": "large.txt"})
        with (
            patch.object(s04, "LARGE_OUTPUT_CHARS", 3),
            patch("builtins.print") as print_mock,
        ):
            result = s04.large_output_hook(request, "1234")

        self.assertIsNone(result)
        self.assertTrue(
            any("large output" in str(call) for call in print_mock.call_args_list)
        )

    def test_summary_hook_counts_openai_tool_messages(self):
        messages = [
            {"role": "user", "content": "task"},
            {"role": "tool", "content": "one"},
            {"role": "assistant", "content": "more"},
            {"role": "tool", "content": "two"},
        ]
        with patch("builtins.print") as print_mock:
            result = s04.summary_hook(messages)

        self.assertIsNone(result)
        self.assertIn("2 tool calls", str(print_mock.call_args))

    def test_stop_hook_can_force_one_more_model_turn(self):
        self.clear_hooks()
        stop_calls = 0

        def force_once(messages):
            nonlocal stop_calls
            stop_calls += 1
            return "Check your answer once more." if stop_calls == 1 else None

        s04.register_hook("Stop", force_once)
        completions = StopCompletions()
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )
        messages = [{"role": "user", "content": "answer carefully"}]

        answer = s04.agent_loop(client, messages)

        self.assertEqual("final answer", answer)
        self.assertEqual(2, completions.calls)
        self.assertEqual(2, stop_calls)
        self.assertIn(
            {"role": "user", "content": "Check your answer once more."},
            messages,
        )

    def test_agent_loop_runs_pre_post_and_stop_hooks(self):
        self.clear_hooks()
        events = []
        s04.register_hook(
            "PreToolUse",
            lambda request: events.append(("pre", request.name)),
        )
        s04.register_hook(
            "PostToolUse",
            lambda request, output: events.append(("post", request.name)),
        )
        s04.register_hook(
            "Stop",
            lambda messages: events.append(("stop", len(messages))),
        )
        completions = ToolCompletions()
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )
        messages = [{"role": "user", "content": "read"}]

        with patch.object(s04, "run_read", return_value="content"):
            with patch.dict(s04.TOOL_HANDLERS, {"read_file": s04.run_read}):
                answer = s04.agent_loop(client, messages)

        self.assertEqual("done", answer)
        self.assertEqual("pre", events[0][0])
        self.assertEqual("post", events[1][0])
        self.assertEqual("stop", events[2][0])


if __name__ == "__main__":
    unittest.main()
