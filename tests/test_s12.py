import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT))

from s12_task_system.harness import memory
from s12_task_system.harness.agent_loop import AgentHarness
from s12_task_system.harness.config import Settings
from s12_task_system.harness.error_recovery import (
    CONTINUATION_PROMPT,
    DEFAULT_MAX_TOKENS,
    ESCALATED_MAX_TOKENS,
    MaxRetriesExceeded,
    RecoveryState,
    is_output_truncated,
    is_prompt_too_long_error,
    retry_delay,
    with_retry,
)
from s12_task_system.harness.subagent import SubagentRunner
from s12_task_system.harness.system_prompt import (
    SystemPromptAssembler,
    registered_tool_names,
)
from s12_task_system.harness.task_system import TaskManager
from s12_task_system.harness.todo_write import TodoManager
from s12_task_system.harness.tool_use import PARENT_TOOLS, SUB_TOOLS, ToolExecutor


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


class FakeChoice:
    def __init__(self, content, finish_reason="stop", tool_calls=None):
        self.message = FakeMessage(content, tool_calls=tool_calls)
        self.finish_reason = finish_reason


class ScriptedCompletions:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        # 每次请求保存快照，避免后续 Agent Loop 修改同一个 messages 列表。
        self.requests.append(copy.deepcopy(kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        choice = response if isinstance(response, FakeChoice) else FakeChoice(response)
        return SimpleNamespace(choices=[choice])


def fake_client(*responses):
    completions = ScriptedCompletions(responses)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


class IdentityCompactor:
    def prepare(self, messages, active_request):
        return messages


class RecoveryCompactor(IdentityCompactor):
    def __init__(self):
        self.reactive_calls = 0

    def reactive_compact(self, messages, active_request):
        self.reactive_calls += 1
        return messages


class HttpError(RuntimeError):
    def __init__(self, status_code, message, headers=None):
        super().__init__(message)
        self.status_code = status_code
        self.response = SimpleNamespace(
            status_code=status_code,
            headers=headers or {},
        )


class SystemPromptTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self):
        self.temporary.cleanup()

    def settings(self, fallback_model=None):
        return Settings(
            workdir=self.root,
            skills_dir=self.root / "skills",
            transcript_dir=self.root / ".transcripts",
            tool_results_dir=self.root / ".task_outputs" / "tool-results",
            memory_dir=self.root / ".memory",
            tasks_dir=self.root / ".tasks",
            model="qwen-plus",
            fallback_model=fallback_model,
        )

    def harness(self, *responses, fallback_model=None):
        client, completions = fake_client(*responses)
        return (
            AgentHarness(
                client,
                self.settings(fallback_model),
                sleep_fn=lambda _delay: None,
                random_fn=lambda low, _high: low,
            ),
            completions,
        )

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
        self.assertEqual(14, len(parent_names))
        self.assertEqual(6, len(child_names))
        self.assertIn("todo_write", harness.system_prompt)
        self.assertIn("task", harness.system_prompt)
        self.assertNotIn("todo_write", harness.sub_system_prompt)
        self.assertNotIn("compact", harness.sub_system_prompt)
        self.assertIn("create_task", harness.system_prompt)
        self.assertIn("complete_task", harness.system_prompt)
        self.assertNotIn("create_task", harness.sub_system_prompt)

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
        self.assertIsInstance(harness.tasks, TaskManager)


    def test_modules_follow_course_capability_names(self):
        harness_dir = REPO_ROOT / "s12_task_system" / "harness"
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
            "error_recovery.py",
            "task_system.py",
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
        self.assertIn("create_task", harness._parent_handlers)
        self.assertNotIn("task", harness.subagent.handlers)
        self.assertNotIn("create_task", harness.subagent.handlers)
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


class ErrorRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self):
        self.temporary.cleanup()

    def settings(self, fallback_model=None):
        return Settings(
            workdir=self.root,
            skills_dir=self.root / "skills",
            transcript_dir=self.root / ".transcripts",
            tool_results_dir=self.root / ".task_outputs" / "tool-results",
            memory_dir=self.root / ".memory",
            tasks_dir=self.root / ".tasks",
            model="qwen-plus",
            fallback_model=fallback_model,
        )

    def harness(self, *responses, fallback_model=None):
        client, completions = fake_client(*responses)
        return (
            AgentHarness(
                client,
                self.settings(fallback_model),
                sleep_fn=lambda _delay: None,
                random_fn=lambda low, _high: low,
            ),
            completions,
        )

    def test_error_classification_and_retry_delay(self):
        self.assertTrue(is_output_truncated("length"))
        self.assertTrue(is_output_truncated("max_tokens"))
        self.assertFalse(is_output_truncated("stop"))
        self.assertTrue(
            is_prompt_too_long_error(
                RuntimeError("maximum context length exceeded")
            )
        )
        no_jitter = lambda low, _high: low
        self.assertEqual(0.5, retry_delay(0, random_fn=no_jitter))
        self.assertEqual(32.0, retry_delay(99, random_fn=no_jitter))
        self.assertEqual(7.0, retry_delay(0, retry_after=7.0))

    def test_rate_limit_retries_with_exponential_backoff(self):
        outcomes = [
            HttpError(429, "rate limit"),
            HttpError(429, "rate limit"),
            "ok",
        ]
        models = []
        sleeps = []

        def request(model):
            models.append(model)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        result = with_retry(
            request,
            RecoveryState("primary"),
            max_attempts=3,
            sleep_fn=sleeps.append,
            random_fn=lambda low, _high: low,
        )
        self.assertEqual("ok", result)
        self.assertEqual(["primary", "primary", "primary"], models)
        self.assertEqual([0.5, 1.0], sleeps)

    def test_retry_after_header_takes_priority(self):
        sleeps = []
        outcomes = [
            HttpError(429, "rate limit", {"Retry-After": "7"}),
            "ok",
        ]

        def request(_model):
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with_retry(
            request,
            RecoveryState("primary"),
            sleep_fn=sleeps.append,
            random_fn=lambda low, _high: low,
        )
        self.assertEqual([7.0], sleeps)

    def test_three_consecutive_529_switch_to_fallback(self):
        outcomes = [
            HttpError(529, "overloaded"),
            HttpError(529, "overloaded"),
            HttpError(529, "overloaded"),
            "ok",
        ]
        models = []

        def request(model):
            models.append(model)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        state = RecoveryState("primary")
        result = with_retry(
            request,
            state,
            fallback_model="fallback",
            max_attempts=4,
            sleep_fn=lambda _delay: None,
            random_fn=lambda low, _high: low,
        )
        self.assertEqual("ok", result)
        self.assertEqual(
            ["primary", "primary", "primary", "fallback"],
            models,
        )
        self.assertEqual("fallback", state.current_model)

    def test_retry_is_bounded_and_non_transient_errors_are_not_retried(self):
        attempts = []

        def exhausted(model):
            attempts.append(model)
            raise HttpError(429, "rate limit")

        with self.assertRaises(MaxRetriesExceeded):
            with_retry(
                exhausted,
                RecoveryState("primary"),
                max_attempts=3,
                sleep_fn=lambda _delay: None,
                random_fn=lambda low, _high: low,
            )
        self.assertEqual(3, len(attempts))

        attempts.clear()
        with self.assertRaisesRegex(RuntimeError, "invalid request"):
            with_retry(
                lambda model: (
                    attempts.append(model),
                    (_ for _ in ()).throw(RuntimeError("invalid request")),
                )[1],
                RecoveryState("primary"),
            )
        self.assertEqual(["primary"], attempts)

    def test_first_truncation_escalates_without_saving_partial_output(self):
        harness, completions = self.harness(
            FakeChoice("discard-me", finish_reason="length"),
            "complete",
            "[]",
        )
        messages = [{"role": "user", "content": "Write a long answer."}]
        answer = harness.agent_loop(
            messages,
            active_request="Write a long answer.",
            compactor=IdentityCompactor(),
        )
        self.assertEqual("complete", answer)
        model_requests = [
            request for request in completions.requests if "tools" in request
        ]
        self.assertEqual(
            [DEFAULT_MAX_TOKENS, ESCALATED_MAX_TOKENS],
            [request["max_tokens"] for request in model_requests],
        )
        self.assertNotIn(
            "discard-me",
            [item.get("content") for item in messages],
        )
        self.assertNotIn(
            "discard-me",
            [
                item.get("content")
                for item in model_requests[1]["messages"]
            ],
        )

    def test_second_truncation_appends_partial_and_continuation(self):
        harness, _ = self.harness(
            FakeChoice("discard-me", finish_reason="length"),
            FakeChoice("partial-one", finish_reason="length"),
            "complete",
            "[]",
        )
        messages = [{"role": "user", "content": "Write a long answer."}]
        answer = harness.agent_loop(
            messages,
            active_request="Write a long answer.",
            compactor=IdentityCompactor(),
        )
        self.assertEqual("complete", answer)
        contents = [item.get("content") for item in messages]
        self.assertIn("partial-one", contents)
        self.assertIn(CONTINUATION_PROMPT, contents)

    def test_continuation_attempts_are_bounded(self):
        harness, completions = self.harness(
            FakeChoice("discard-me", finish_reason="length"),
            FakeChoice("partial-one", finish_reason="length"),
            FakeChoice("partial-two", finish_reason="length"),
            FakeChoice("partial-three", finish_reason="length"),
            FakeChoice("partial-four", finish_reason="length"),
        )
        messages = [{"role": "user", "content": "Write a huge answer."}]
        answer = harness.agent_loop(
            messages,
            active_request="Write a huge answer.",
            compactor=IdentityCompactor(),
        )
        self.assertEqual("partial-four", answer)
        self.assertEqual(
            3,
            sum(
                item.get("content") == CONTINUATION_PROMPT
                for item in messages
            ),
        )
        self.assertEqual(5, len(completions.requests))

    def test_empty_truncated_tool_output_returns_explicit_error(self):
        harness, _ = self.harness(
            FakeChoice(None, finish_reason="length"),
            FakeChoice(None, finish_reason="length"),
            FakeChoice(None, finish_reason="length"),
            FakeChoice(None, finish_reason="length"),
            FakeChoice(None, finish_reason="length"),
        )
        messages = [{"role": "user", "content": "Call a very large tool."}]
        answer = harness.agent_loop(
            messages,
            active_request="Call a very large tool.",
            compactor=IdentityCompactor(),
        )
        self.assertIn("output remained truncated", answer)
        self.assertEqual("assistant", messages[-1]["role"])
        self.assertEqual(answer, messages[-1]["content"])

    def test_prompt_too_long_compacts_once_then_recovers(self):
        harness, _ = self.harness(
            RuntimeError("maximum context length exceeded"),
            "recovered",
            "[]",
        )
        compactor = RecoveryCompactor()
        messages = [{"role": "user", "content": "Inspect everything."}]
        answer = harness.agent_loop(
            messages,
            active_request="Inspect everything.",
            compactor=compactor,
        )
        self.assertEqual("recovered", answer)
        self.assertEqual(1, compactor.reactive_calls)

    def test_second_prompt_too_long_becomes_assistant_error(self):
        harness, _ = self.harness(
            RuntimeError("maximum context length exceeded"),
            RuntimeError("maximum context length exceeded again"),
        )
        compactor = RecoveryCompactor()
        messages = [{"role": "user", "content": "Inspect everything."}]
        answer = harness.agent_loop(
            messages,
            active_request="Inspect everything.",
            compactor=compactor,
        )
        self.assertIn("Error: RuntimeError", answer)
        self.assertEqual(1, compactor.reactive_calls)
        self.assertEqual("assistant", messages[-1]["role"])

    def test_unrecoverable_error_is_returned_instead_of_crashing(self):
        harness, _ = self.harness(RuntimeError("invalid request"))
        messages = [{"role": "user", "content": "Run."}]
        answer = harness.agent_loop(
            messages,
            active_request="Run.",
            compactor=IdentityCompactor(),
        )
        self.assertEqual(
            "Error: RuntimeError: invalid request",
            answer,
        )
        self.assertEqual(answer, messages[-1]["content"])


class TaskSystemTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.generated_ids = iter(
            [f"task_{index:03d}" for index in range(1, 30)]
        )
        self.manager = TaskManager(
            self.root / ".tasks",
            id_factory=lambda: next(self.generated_ids),
        )

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def task_payload(result: str) -> dict:
        return json.loads(result.split("\n", 1)[1])

    def settings(self, tasks_dir=None):
        return Settings(
            workdir=self.root,
            skills_dir=self.root / "skills",
            transcript_dir=self.root / ".transcripts",
            tool_results_dir=self.root / ".task_outputs" / "tool-results",
            memory_dir=self.root / ".memory",
            tasks_dir=tasks_dir or self.root / ".tasks",
            model="qwen-plus",
        )

    def test_create_persists_complete_json_with_private_mode(self):
        result = self.manager.create_task(
            "Set up schema",
            "Create the initial tables.",
        )
        payload = self.task_payload(result)
        self.assertEqual("task_001", payload["id"])
        self.assertEqual("pending", payload["status"])
        self.assertIsNone(payload["owner"])
        self.assertEqual([], payload["blockedBy"])

        task_file = self.root / ".tasks" / "task_001.json"
        self.assertEqual(payload, json.loads(task_file.read_text()))
        self.assertEqual(0o600, task_file.stat().st_mode & 0o777)
        self.assertEqual([], list((self.root / ".tasks").glob("*.tmp")))

    def test_dependencies_control_ready_and_blocked_board_states(self):
        self.manager.create_task("Set up schema")
        self.manager.create_task(
            "Create API",
            blockedBy=["task_001"],
        )
        board = self.manager.list_tasks()
        self.assertIn("task_001 [ready]", board)
        self.assertIn("task_002 [blocked by task_001]", board)
        self.assertFalse(self.manager.can_start("task_002"))
        self.assertIn(
            "blocked by: task_001",
            self.manager.claim_task("task_002", "api-agent"),
        )

    def test_missing_dependency_is_a_blocker_not_a_crash(self):
        self.manager.create_task(
            "Wait for imported task",
            blockedBy=["task_missing"],
        )
        self.assertFalse(self.manager.can_start("task_001"))
        self.assertIn("task_missing", self.manager.list_tasks())
        self.assertIn(
            "blocked by: task_missing",
            self.manager.claim_task("task_001", "agent"),
        )

    def test_claim_and_complete_enforce_state_machine(self):
        self.manager.create_task("Implement endpoint")
        self.assertIn(
            "is pending, cannot complete",
            self.manager.complete_task("task_001"),
        )
        self.assertIn(
            "Claimed task_001",
            self.manager.claim_task("task_001", "alice"),
        )
        active = json.loads(self.manager.get_task("task_001"))
        self.assertEqual("in_progress", active["status"])
        self.assertEqual("alice", active["owner"])
        self.assertIn(
            "is in_progress, cannot claim",
            self.manager.claim_task("task_001", "bob"),
        )

        self.assertIn("Completed task_001", self.manager.complete_task("task_001"))
        completed = json.loads(self.manager.get_task("task_001"))
        self.assertEqual("completed", completed["status"])
        self.assertIn(
            "is completed, cannot complete",
            self.manager.complete_task("task_001"),
        )

    def test_completion_reports_only_newly_unblocked_downstream_tasks(self):
        self.manager.create_task("Set up schema")
        self.manager.create_task("Create API", blockedBy=["task_001"])
        self.manager.create_task("Write docs", blockedBy=["task_001"])
        self.manager.create_task("Independent cleanup")

        self.manager.claim_task("task_001", "db-agent")
        result = self.manager.complete_task("task_001")
        self.assertIn("task_002 (Create API)", result)
        self.assertIn("task_003 (Write docs)", result)
        self.assertNotIn("Independent cleanup", result)
        board = self.manager.list_tasks()
        self.assertIn("task_002 [ready]", board)
        self.assertIn("task_003 [ready]", board)

    def test_task_board_survives_new_manager_instance(self):
        self.manager.create_task("Persist me", "Across sessions")
        restarted = TaskManager(self.root / ".tasks")
        board = restarted.list_tasks()
        self.assertIn("task_001 [ready] Persist me", board)
        payload = json.loads(restarted.get_task("task_001"))
        self.assertEqual("Across sessions", payload["description"])

    def test_invalid_inputs_and_path_traversal_return_errors(self):
        self.assertTrue(self.manager.create_task(" ").startswith("Error:"))
        self.assertIn(
            "blockedBy must be an array",
            self.manager.create_task("Bad deps", blockedBy="task_001"),
        )
        self.assertTrue(self.manager.get_task("../secret").startswith("Error:"))

        self_ref = TaskManager(
            self.root / "self-tasks",
            id_factory=lambda: "task_self",
        )
        self.assertIn(
            "cannot block itself",
            self_ref.create_task("Cycle", blockedBy=["task_self"]),
        )

    def test_corrupt_task_file_is_reported_without_partial_rewrite(self):
        task_file = self.root / ".tasks" / "task_bad.json"
        task_file.write_text("{broken", encoding="utf-8")
        result = self.manager.list_tasks()
        self.assertIn("Error: cannot read task", result)
        self.assertEqual("{broken", task_file.read_text(encoding="utf-8"))

    def test_agent_harness_rejects_tasks_directory_outside_workspace(self):
        client, _ = fake_client()
        outside = self.root.parent / "outside-task-board"
        settings = self.settings(tasks_dir=outside)
        with self.assertRaisesRegex(ValueError, "tasks directory"):
            AgentHarness(client, settings)

    def test_task_tools_run_through_parent_executor_and_hooks(self):
        client, _ = fake_client()
        harness = AgentHarness(client, self.settings())
        call = SimpleNamespace(
            id="create-1",
            function=SimpleNamespace(
                name="create_task",
                arguments=json.dumps(
                    {
                        "subject": "Integration task",
                        "description": "Created through ToolExecutor",
                    }
                ),
            ),
        )
        messages = []
        extraction_messages = []
        used_todo, compact_requested = harness._execute_tool_batch(
            [call],
            messages,
            extraction_messages,
            already_compacted=False,
        )
        self.assertFalse(used_todo)
        self.assertFalse(compact_requested)
        self.assertIn("Created task_", messages[0]["content"])
        self.assertEqual(messages, extraction_messages)
        self.assertEqual(
            1,
            len(list((self.root / ".tasks").glob("task_*.json"))),
        )


if __name__ == "__main__":
    unittest.main()
