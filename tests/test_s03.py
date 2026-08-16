import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


CODE_PATH = Path(__file__).parents[1] / "s03_permission" / "code.py"
SPEC = importlib.util.spec_from_file_location("s03_code", CODE_PATH)
s03 = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(s03)


class FakeMessage:
    def __init__(self, content, tool_calls):
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


class FakeCompletions:
    def __init__(self):
        self.calls = 0
        self.received_messages = []

    def create(self, **kwargs):
        self.calls += 1
        self.received_messages.append(list(kwargs["messages"]))
        if self.calls == 1:
            tool_call = SimpleNamespace(
                id="call-denied",
                function=SimpleNamespace(
                    name="bash",
                    arguments=json.dumps({"command": "sudo whoami"}),
                ),
            )
            message = FakeMessage(None, [tool_call])
        else:
            message = FakeMessage("request was denied", None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class PermissionTest(unittest.TestCase):
    def test_deny_list_blocks_forbidden_commands(self):
        self.assertIsNotNone(s03.check_deny_list("rm -rf /"))
        self.assertIsNotNone(s03.check_deny_list("sudo whoami"))
        self.assertIsNotNone(s03.check_deny_list("mkfs /dev/sda"))
        self.assertIsNone(s03.check_deny_list("rm -rf /tmp/build-cache"))

    def test_safe_read_is_allowed_without_prompt(self):
        with patch.object(
            s03,
            "ask_user",
            side_effect=AssertionError("safe read must not prompt"),
        ):
            allowed, reason = s03.check_permission(
                "read_file",
                {"path": "README.md"},
            )

        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_risky_bash_uses_user_decision(self):
        args = {"command": "rm old.txt"}
        with patch.object(s03, "ask_user", return_value="deny") as ask:
            denied, reason = s03.check_permission("bash", args)
        self.assertFalse(denied)
        self.assertEqual("Potentially destructive command", reason)
        ask.assert_called_once()

        with patch.object(s03, "ask_user", return_value="allow"):
            allowed, reason = s03.check_permission("bash", args)
        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_hard_deny_never_prompts(self):
        with patch.object(
            s03,
            "ask_user",
            side_effect=AssertionError("hard deny must not prompt"),
        ):
            allowed, reason = s03.check_permission(
                "bash",
                {"command": "sudo rm file.txt"},
            )

        self.assertFalse(allowed)
        self.assertIn("sudo", reason)

    def test_denied_tool_never_reaches_handler(self):
        handler = Mock(return_value="should not run")
        with patch.dict(s03.TOOL_HANDLERS, {"bash": handler}):
            result = s03.execute_tool(
                "bash",
                json.dumps({"command": "sudo whoami"}),
            )

        handler.assert_not_called()
        self.assertIn("Permission denied", result)

    def test_approved_tool_reaches_handler(self):
        handler = Mock(return_value="deleted")
        with (
            patch.dict(s03.TOOL_HANDLERS, {"bash": handler}),
            patch.object(s03, "ask_user", return_value="allow"),
        ):
            result = s03.execute_tool(
                "bash",
                json.dumps({"command": "rm old.txt"}),
            )

        handler.assert_called_once_with(command="rm old.txt")
        self.assertEqual("deleted", result)

    def test_approval_does_not_expand_path_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory) / "work"
            workdir.mkdir()
            outside = Path(directory) / "outside.txt"
            with (
                patch.object(s03, "WORKDIR", workdir.resolve()),
                patch.object(s03, "ask_user", return_value="allow") as ask,
            ):
                result = s03.execute_tool(
                    "write_file",
                    json.dumps({"path": str(outside), "content": "nope"}),
                )

        ask.assert_called_once()
        self.assertIn("path escapes working directory", result)
        self.assertFalse(outside.exists())

    def test_agent_loop_feeds_denial_back_to_model(self):
        completions = FakeCompletions()
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )
        messages = [{"role": "user", "content": "run sudo"}]
        handler = Mock(return_value="should not run")

        with patch.dict(s03.TOOL_HANDLERS, {"bash": handler}):
            answer = s03.agent_loop(client, messages)

        self.assertEqual("request was denied", answer)
        handler.assert_not_called()
        tool_messages = [m for m in messages if m["role"] == "tool"]
        self.assertEqual(1, len(tool_messages))
        self.assertIn("Permission denied", tool_messages[0]["content"])
        self.assertEqual("call-denied", tool_messages[0]["tool_call_id"])

    def test_ask_user_defaults_to_deny_on_eof(self):
        with patch("builtins.input", side_effect=EOFError):
            decision = s03.ask_user("bash", {"command": "rm x"}, "risky")
        self.assertEqual("deny", decision)


if __name__ == "__main__":
    unittest.main()
