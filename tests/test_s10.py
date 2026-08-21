import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


STAGE_DIR = Path(__file__).parents[1] / "s10_task_system"
sys.path.insert(0, str(STAGE_DIR))

from harness.agent import AgentHarness
from harness.config import Settings
from harness import memory
from harness.tasks import TaskManager, TaskStore
from harness.tools import PARENT_TOOLS, SUB_TOOLS


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


class TaskSystemTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.store = TaskStore(self.root / ".tasks", self.root)
        self.manager = TaskManager(self.store)

    def tearDown(self):
        self.temporary.cleanup()

    def task(self, subject):
        return self.store.create(subject, f"Description for {subject}")

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

    def test_create_persists_and_new_store_reloads(self):
        task = self.task("schema")
        self.assertRegex(task.id, r"^task_[0-9a-f]{8}$")
        self.assertTrue((self.root / ".tasks" / f"{task.id}.json").is_file())
        self.assertEqual(task, TaskStore(self.root / ".tasks", self.root).load(task.id))

    def test_create_validates_subject_and_description(self):
        with self.assertRaises(ValueError):
            self.store.create("   ")
        with self.assertRaises(ValueError):
            self.store.create("valid", None)

    def test_store_cannot_escape_workspace(self):
        outside = TaskStore(self.root.parent / "outside-tasks", self.root)
        with self.assertRaisesRegex(ValueError, "escapes"):
            outside.create("outside")

    def test_invalid_ids_are_rejected(self):
        for invalid in ("../task_deadbeef", "task_DEADBEEF", "task_short", "x"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.load(invalid)

    def test_two_phase_dependencies_are_deduplicated(self):
        schema = self.task("schema")
        api = self.task("api")
        updated = self.store.update_dependencies(api.id, [schema.id, schema.id])
        self.assertEqual([schema.id], updated.blockedBy)

    def test_missing_dependency_is_all_or_nothing(self):
        first = self.task("first")
        target = self.task("target")
        with self.assertRaisesRegex(ValueError, "Dependency not found"):
            self.store.update_dependencies(target.id, [first.id, "task_deadbeef"])
        self.assertEqual([], self.store.load(target.id).blockedBy)

    def test_self_dependency_and_transitive_cycle_are_rejected(self):
        first = self.task("first")
        second = self.task("second")
        third = self.task("third")
        with self.assertRaisesRegex(ValueError, "itself"):
            self.store.update_dependencies(first.id, [first.id])
        self.store.update_dependencies(second.id, [first.id])
        self.store.update_dependencies(third.id, [second.id])
        with self.assertRaisesRegex(ValueError, "cycle"):
            self.store.update_dependencies(first.id, [third.id])

    def test_blocked_claim_then_completion_unblocks(self):
        schema = self.task("schema")
        api = self.task("api")
        self.store.update_dependencies(api.id, [schema.id])
        self.assertIn("Blocked by", self.manager.claim(api.id))
        self.assertIn("Claimed", self.manager.claim(schema.id))
        self.assertIn("Unblocked: api", self.manager.complete(schema.id))
        self.assertTrue(self.manager.can_start(api.id))

    def test_lifecycle_and_owner_are_enforced(self):
        task = self.task("owned")
        self.assertIn("Claimed", self.manager.claim(task.id, "alice"))
        self.assertIn("in_progress", self.manager.claim(task.id, "alice"))
        self.assertIn("owned by alice", self.manager.complete(task.id, "bob"))
        self.assertIn("Completed", self.manager.complete(task.id, "alice"))
        self.assertIn("completed", self.manager.complete(task.id, "alice"))

    def test_dependencies_cannot_change_after_claim(self):
        dependency = self.task("dependency")
        target = self.task("target")
        self.manager.claim(target.id)
        with self.assertRaisesRegex(ValueError, "pending and unowned"):
            self.store.update_dependencies(target.id, [dependency.id])

    def test_missing_dependency_file_remains_blocked(self):
        dependency = self.task("dependency")
        target = self.task("target")
        self.store.update_dependencies(target.id, [dependency.id])
        (self.root / ".tasks" / f"{dependency.id}.json").unlink()
        self.assertFalse(self.manager.can_start(target.id))

    def test_list_get_and_error_adapters(self):
        task = self.task("中文任务")
        self.assertIn(f"[ ] {task.id}: 中文任务 [pending]", self.manager.run_list())
        self.assertEqual("中文任务", json.loads(self.manager.run_get(task.id))["subject"])
        self.assertTrue(self.manager.run_get("bad-id").startswith("Error:"))
        self.assertIn("cannot be empty", self.manager.run_create(" "))

    def test_corrupt_record_is_rejected(self):
        task = self.task("corrupt")
        path = self.root / ".tasks" / f"{task.id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["status"] = "mystery"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "status"):
            self.store.load(task.id)

    def test_task_tools_are_parent_only_and_previous_tools_remain(self):
        parent = {tool["function"]["name"] for tool in PARENT_TOOLS}
        child = {tool["function"]["name"] for tool in SUB_TOOLS}
        task_tools = {
            "create_task", "update_task", "list_tasks", "get_task",
            "claim_task", "complete_task",
        }
        previous = {
            "bash", "read_file", "write_file", "edit_file", "glob",
            "todo_write", "task", "load_skill", "compact",
        }
        self.assertEqual(previous | task_tools, parent)
        self.assertTrue(task_tools.isdisjoint(child))
        self.assertEqual(
            {"bash", "read_file", "write_file", "edit_file", "glob", "load_skill"},
            child,
        )

    def test_modular_harness_keeps_code_entrypoint_small(self):
        code_lines = (STAGE_DIR / "code.py").read_text(encoding="utf-8").splitlines()
        modules = {path.name for path in (STAGE_DIR / "harness").glob("*.py")}
        self.assertLess(len(code_lines), 100)
        self.assertTrue(
            {
                "agent.py", "compaction.py", "config.py", "hooks.py",
                "memory.py", "models.py", "skills.py", "tasks.py", "tools.py",
            }.issubset(modules)
        )

    def test_agent_loop_dispatches_task_tool_and_memory_side_queries(self):
        call = SimpleNamespace(
            id="call-create",
            function=SimpleNamespace(
                name="create_task",
                arguments=json.dumps(
                    {"subject": "schema", "description": "Create schema"}
                ),
            ),
        )
        client, completions = fake_client(
            "[]",
            FakeMessage(None, [call]),
            "task graph created",
            "[]",
        )
        harness = AgentHarness(client, self.settings())
        memory.write_memory_file(
            "schema-convention",
            "project",
            "Schema task convention",
            "Create a durable schema task before API work.",
        )
        messages = [
            {"role": "system", "content": harness.system_prompt},
            {"role": "user", "content": "Create a durable schema task."},
        ]
        answer = harness.agent_loop(
            messages,
            active_request="Create a durable schema task.",
            compactor=IdentityCompactor(),
        )
        self.assertEqual("task graph created", answer)
        self.assertEqual(["schema"], [task.subject for task in harness.task_store.list()])
        result = next(message for message in messages if message["role"] == "tool")
        self.assertIn("Created task_", result["content"])
        self.assertIn(
            "create_task",
            {tool["function"]["name"] for tool in completions.requests[1]["tools"]},
        )

    def test_previous_file_boundary_and_todo_rules_survive_composition(self):
        client, _ = fake_client()
        harness = AgentHarness(client, self.settings())
        self.assertTrue(harness.builtins.run_read("../outside").startswith("Error:"))
        result = harness.builtins.run_todo_write(
            [
                {"content": "one", "status": "in_progress"},
                {"content": "two", "status": "in_progress"},
            ]
        )
        self.assertIn("at most one", result)


if __name__ == "__main__":
    unittest.main()
