import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).parents[1]
STAGE_DIR = REPO_ROOT / "s13_background_tasks"
sys.path.insert(0, str(REPO_ROOT))

from s13_background_tasks.harness.agent import AgentHarness
from s13_background_tasks.harness.background import (
    BackgroundManager,
    ShellExecutor,
    format_shell_result,
)
from s13_background_tasks.harness.config import Settings
from s13_background_tasks.harness.tools import PARENT_TOOLS, SUB_TOOLS


class FakeMessage:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = None

    def model_dump(self, exclude_none=True):
        payload = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            payload["tool_calls"] = [
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
        return payload


class ScriptedCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        message = response if isinstance(response, FakeMessage) else FakeMessage(response)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def fake_client(*responses):
    completions = ScriptedCompletions(responses)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


class IdentityCompactor:
    def prepare(self, messages, active_request):
        return messages


def wait_finished(manager, task_id, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = manager.tasks.get(task_id)
        if task is not None and task.status != "running":
            return task
        time.sleep(0.01)
    raise AssertionError(f"background task {task_id} did not finish")


class ImmediateBackground:
    def __init__(self):
        self.ready = False
        self.started = []
        self.closed = False

    def start(self, command, tool_call_id=""):
        self.started.append((command, tool_call_id))
        self.ready = True
        return "bg_0001"

    def inject(self, messages):
        if not self.ready:
            return 0
        self.ready = False
        messages.append(
            {
                "role": "user",
                "content": (
                    "<task_notification>\n"
                    "  <task_id>bg_0001</task_id>\n"
                    "  <status>completed</status>\n"
                    "  <summary>done</summary>\n"
                    "</task_notification>"
                ),
            }
        )
        return 1

    def close(self):
        self.closed = True


class BackgroundTasksTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.shell = ShellExecutor(self.root, timeout=1.0)
        self.manager = BackgroundManager(self.shell)

    def tearDown(self):
        self.manager.close()
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

    def test_shell_executor_reports_success_and_nonzero_exit(self):
        output, code = self.shell.run("printf success")
        self.assertEqual(("success", 0), (output, code))

        output, code = self.shell.run("printf failure; exit 7")
        self.assertEqual(7, code)
        self.assertIn("status 7", format_shell_result(output, code))

    def test_shell_timeout_returns_failure_without_waiting_for_command(self):
        shell = ShellExecutor(self.root, timeout=0.05)
        started = time.monotonic()
        output, code = shell.run("sleep 2")
        elapsed = time.monotonic() - started
        shell.close()

        self.assertIsNone(code)
        self.assertIn("timed out", output)
        self.assertLess(elapsed, 1.0)

    def test_background_start_returns_id_and_collects_once(self):
        started = time.monotonic()
        task_id = self.manager.start("sleep 0.1; printf done", "call-1")
        self.assertEqual("bg_0001", task_id)
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual([], self.manager.collect())

        task = wait_finished(self.manager, task_id)
        self.assertEqual("completed", task.status)
        notifications = self.manager.collect()
        self.assertEqual(1, len(notifications))
        self.assertIn("<summary>done</summary>", notifications[0])
        self.assertEqual([], self.manager.collect())
        self.assertNotIn(task_id, self.manager.tasks)

    def test_background_failure_and_xml_escaping(self):
        failed = self.manager.start("printf '&lt;bad&gt;&amp;'; exit 3")
        wait_finished(self.manager, failed)
        notification = self.manager.collect()[0]
        self.assertIn("<status>failed</status>", notification)
        self.assertIn("status 3", notification)
        self.assertIn("&amp;lt;bad&amp;gt;&amp;amp;", notification)

    def test_inject_appends_user_notification_after_tool_result(self):
        task_id = self.manager.start("printf ready")
        wait_finished(self.manager, task_id)
        messages = [{"role": "tool", "content": "placeholder"}]

        self.assertEqual(1, self.manager.inject(messages))
        self.assertEqual("user", messages[-1]["role"])
        self.assertIn("<task_notification>", messages[-1]["content"])

    def test_inject_merges_with_existing_user_message(self):
        task_id = self.manager.start("printf ready")
        wait_finished(self.manager, task_id)
        messages = [{"role": "user", "content": "current request"}]

        self.manager.inject(messages)
        self.assertEqual(1, len(messages))
        self.assertTrue(messages[0]["content"].startswith("current request"))

    def test_parent_bash_schema_has_flag_but_subagent_bash_does_not(self):
        parent_bash = next(
            tool for tool in PARENT_TOOLS if tool["function"]["name"] == "bash"
        )
        child_bash = next(
            tool for tool in SUB_TOOLS if tool["function"]["name"] == "bash"
        )
        self.assertIn(
            "run_in_background",
            parent_bash["function"]["parameters"]["properties"],
        )
        self.assertNotIn(
            "run_in_background",
            child_bash["function"]["parameters"]["properties"],
        )
        self.assertEqual(15, len(PARENT_TOOLS))

    def test_harness_intercepts_only_explicit_true(self):
        client, _ = fake_client()
        harness = AgentHarness(client, self.settings())
        fake_background = ImmediateBackground()
        harness.background = fake_background

        sync = harness.execute_tool("bash", json.dumps({"command": "printf sync"}))
        invalid = harness.execute_tool(
            "bash",
            json.dumps({"command": "printf no", "run_in_background": "true"}),
        )
        async_result = harness.execute_tool(
            "bash",
            json.dumps({"command": "sleep 1", "run_in_background": True}),
            tool_call_id="call-bg",
        )

        self.assertEqual("sync", sync)
        self.assertIn("must be a boolean", invalid)
        self.assertIn("Background task bg_0001 started", async_result)
        self.assertEqual([("sleep 1", "call-bg")], fake_background.started)
        harness.close()

    def test_permission_runs_before_background_start(self):
        client, _ = fake_client()
        harness = AgentHarness(client, self.settings())
        fake_background = ImmediateBackground()
        harness.background = fake_background

        result = harness.execute_tool(
            "bash",
            json.dumps({"command": "sudo echo no", "run_in_background": True}),
        )

        self.assertIn("Permission denied", result)
        self.assertEqual([], fake_background.started)
        harness.close()

    def test_subagent_execution_cannot_start_background_work(self):
        client, _ = fake_client()
        harness = AgentHarness(client, self.settings())
        fake_background = ImmediateBackground()
        harness.background = fake_background

        result = harness.execute_sub_tool(
            "bash",
            json.dumps({"command": "printf child", "run_in_background": True}),
        )

        self.assertEqual("child", result)
        self.assertEqual([], fake_background.started)
        harness.close()

    def test_agent_loop_returns_placeholder_then_injects_notification(self):
        call = SimpleNamespace(
            id="call-bg",
            function=SimpleNamespace(
                name="bash",
                arguments=json.dumps(
                    {"command": "slow-test", "run_in_background": True}
                ),
            ),
        )
        client, completions = fake_client(
            FakeMessage(None, [call]),
            "background result observed",
            "[]",
        )
        harness = AgentHarness(client, self.settings())
        fake_background = ImmediateBackground()
        harness.background = fake_background
        messages = [
            {"role": "system", "content": harness.system_prompt},
            {"role": "user", "content": "Run the slow test in background."},
        ]

        answer = harness.agent_loop(
            messages,
            active_request="Run the slow test in background.",
            compactor=IdentityCompactor(),
        )

        self.assertEqual("background result observed", answer)
        tool_result = next(item for item in messages if item["role"] == "tool")
        notification = next(
            item
            for item in messages
            if item["role"] == "user"
            and str(item["content"]).startswith("<task_notification>")
        )
        self.assertIn("Background task bg_0001 started", tool_result["content"])
        self.assertNotIn("tool_call_id", notification)
        self.assertIn("<status>completed</status>", notification["content"])
        self.assertEqual(3, len(completions.requests))
        harness.close()

    def test_s12_task_system_remains_available(self):
        client, _ = fake_client()
        harness = AgentHarness(client, self.settings())
        result = harness.execute_tool(
            "create_task",
            json.dumps({"subject": "persisted", "description": "from s10"}),
        )
        self.assertIn("Created task_", result)
        self.assertEqual("persisted", harness.task_store.list()[0].subject)
        harness.close()


if __name__ == "__main__":
    unittest.main()
