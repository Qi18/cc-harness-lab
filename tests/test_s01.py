import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


CODE_PATH = Path(__file__).parents[1] / "s01_agent_loop" / "code.py"
SPEC = importlib.util.spec_from_file_location("s01_code", CODE_PATH)
s01 = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(s01)


class FakeMessage:
    def __init__(self, content, tool_calls, reasoning_content=None):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = reasoning_content

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
        self.last_messages = None
        self.requests = []

    def create(self, **kwargs):
        self.calls += 1
        self.last_messages = kwargs["messages"]
        self.requests.append(kwargs)
        if self.calls == 1:
            tool_call = SimpleNamespace(
                id="call-1",
                function=SimpleNamespace(
                    name="bash",
                    arguments=json.dumps({"command": "printf s01-ok"}),
                ),
            )
            message = FakeMessage(None, [tool_call], reasoning_content="thinking")
        else:
            message = FakeMessage("done", None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class AgentLoopTest(unittest.TestCase):
    def test_create_client_requires_api_key(self):
        with patch.dict(s01.os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "DASHSCOPE_API_KEY"):
                s01.create_client()

    def test_run_bash_returns_exit_code_and_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(s01, "WORKDIR", Path(directory)):
                result = s01.run_bash("printf hello")

        self.assertIn("exit_code=0", result)
        self.assertIn("hello", result)

    def test_tool_result_is_fed_back_to_model(self):
        completions = FakeCompletions()
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )
        messages = [{"role": "user", "content": "run a command"}]

        with (
            patch.object(s01, "MODEL", "kimi/kimi-k3"),
            patch.object(s01, "execute_tool", return_value="exit_code=0"),
        ):
            answer = s01.agent_loop(client, messages)

        self.assertEqual("done", answer)
        self.assertEqual(2, completions.calls)
        self.assertEqual("tool", messages[-2]["role"])
        self.assertEqual("call-1", messages[-2]["tool_call_id"])
        self.assertEqual("thinking", messages[-3]["reasoning_content"])
        self.assertEqual(
            {"reasoning_effort": "max"},
            completions.requests[0]["extra_body"],
        )


if __name__ == "__main__":
    unittest.main()
