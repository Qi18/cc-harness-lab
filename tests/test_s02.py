import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


CODE_PATH = Path(__file__).parents[1] / "s02_tool_use" / "code.py"
SPEC = importlib.util.spec_from_file_location("s02_code", CODE_PATH)
s02 = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(s02)


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

    def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            calls = [
                SimpleNamespace(
                    id="call-read",
                    function=SimpleNamespace(
                        name="read_file",
                        arguments=json.dumps({"path": "a.txt"}),
                    ),
                ),
                SimpleNamespace(
                    id="call-glob",
                    function=SimpleNamespace(
                        name="glob",
                        arguments=json.dumps({"pattern": "*.txt"}),
                    ),
                ),
            ]
            message = FakeMessage(None, calls)
        else:
            message = FakeMessage("done", None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class ToolUseTest(unittest.TestCase):
    def test_all_five_tools_are_declared_and_registered(self):
        declared = {tool["function"]["name"] for tool in s02.TOOLS}
        self.assertEqual(
            {"bash", "read_file", "write_file", "edit_file", "glob"},
            declared,
        )
        self.assertEqual(declared, set(s02.TOOL_HANDLERS))

    def test_safe_path_blocks_escape_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory) / "work"
            outside = Path(directory) / "outside"
            workdir.mkdir()
            outside.mkdir()
            (workdir / "link").symlink_to(outside, target_is_directory=True)

            with patch.object(s02, "WORKDIR", workdir.resolve()):
                with self.assertRaisesRegex(ValueError, "escapes"):
                    s02.safe_path("../outside/secret.txt")
                with self.assertRaisesRegex(ValueError, "escapes"):
                    s02.safe_path("link/secret.txt")

    def test_read_write_edit_and_glob(self):
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            with patch.object(s02, "WORKDIR", workdir):
                written = s02.run_write("hello.py", "hello\nsecond\nthird\n")
                first_two = s02.run_read("hello.py", limit=2)
                edited = s02.run_edit("hello.py", "hello", "hi")
                matches = s02.run_glob("**/*.py")
                final = (workdir / "hello.py").read_text()

        self.assertIn("Wrote", written)
        self.assertEqual("hello\nsecond", first_two)
        self.assertEqual("Edited hello.py", edited)
        self.assertEqual("hello.py", matches)
        self.assertTrue(final.startswith("hi\n"))

    def test_glob_rejects_parent_traversal(self):
        self.assertTrue(s02.run_glob("../*.py").startswith("Error:"))

    def test_dispatch_handles_invalid_calls(self):
        self.assertIn("unknown tool", s02.execute_tool("missing", "{}"))
        self.assertIn("invalid tool arguments", s02.execute_tool("glob", "{"))
        self.assertIn(
            "must be a JSON object",
            s02.execute_tool("glob", json.dumps(["*.py"])),
        )

    def test_agent_loop_executes_multiple_tools_in_order(self):
        completions = FakeCompletions()
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )
        messages = [{"role": "user", "content": "inspect files"}]
        calls = []

        def fake_execute(name, arguments):
            calls.append((name, json.loads(arguments)))
            return f"result from {name}"

        with patch.object(s02, "execute_tool", side_effect=fake_execute):
            answer = s02.agent_loop(client, messages)

        self.assertEqual("done", answer)
        self.assertEqual(
            [
                ("read_file", {"path": "a.txt"}),
                ("glob", {"pattern": "*.txt"}),
            ],
            calls,
        )
        tool_messages = [m for m in messages if m["role"] == "tool"]
        self.assertEqual(["call-read", "call-glob"], [m["tool_call_id"] for m in tool_messages])


if __name__ == "__main__":
    unittest.main()
