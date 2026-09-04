import copy
import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT))

from s14_cron_scheduler.harness import memory
from s14_cron_scheduler.harness.background_tasks import (
    COMPLETED,
    FAILED,
    BackgroundTaskManager,
)
from s14_cron_scheduler.harness.agent_loop import AgentHarness
from s14_cron_scheduler.harness.config import Settings
from s14_cron_scheduler.harness.cron_scheduler import (
    CronJob,
    CronScheduler,
    cron_matches,
    validate_cron,
)
from s14_cron_scheduler.harness.error_recovery import (
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
from s14_cron_scheduler.harness.subagent import SubagentRunner
from s14_cron_scheduler.harness.system_prompt import (
    SystemPromptAssembler,
    registered_tool_names,
)
from s14_cron_scheduler.harness.task_system import TaskManager
from s14_cron_scheduler.harness.todo_write import TodoManager
from s14_cron_scheduler.harness.tool_use import PARENT_TOOLS, SUB_TOOLS, ToolExecutor


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
        self.assertEqual(17, len(parent_names))
        self.assertEqual(6, len(child_names))
        self.assertIn("todo_write", harness.system_prompt)
        self.assertIn("task", harness.system_prompt)
        self.assertNotIn("todo_write", harness.sub_system_prompt)
        self.assertNotIn("compact", harness.sub_system_prompt)
        self.assertIn("create_task", harness.system_prompt)
        self.assertIn("complete_task", harness.system_prompt)
        parent_bash = PARENT_TOOLS[0]["function"]["parameters"]["properties"]
        child_bash = SUB_TOOLS[0]["function"]["parameters"]["properties"]
        self.assertIn("run_in_background", parent_bash)
        self.assertNotIn("run_in_background", child_bash)
        self.assertIn("run_in_background=true", harness.system_prompt)
        self.assertNotIn(
            "run_in_background=true",
            harness.sub_system_prompt,
        )
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
        self.assertIsInstance(harness.background, BackgroundTaskManager)
        self.assertIsInstance(harness.cron, CronScheduler)
        self.assertIsInstance(harness.executor, ToolExecutor)
        self.assertIsInstance(harness.subagent, SubagentRunner)
        self.assertIsInstance(harness.todo, TodoManager)
        self.assertIsInstance(harness.tasks, TaskManager)


    def test_modules_follow_course_capability_names(self):
        harness_dir = REPO_ROOT / "s14_cron_scheduler" / "harness"
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
            "background_tasks.py",
            "cron_scheduler.py",
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


class BackgroundTasksTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self):
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

    @staticmethod
    def tool_call(call_id, command, run_in_background=None):
        arguments = {"command": command}
        if run_in_background is not None:
            arguments["run_in_background"] = run_in_background
        return SimpleNamespace(
            id=call_id,
            function=SimpleNamespace(
                name="bash",
                arguments=json.dumps(arguments),
            ),
        )

    def harness(self, *responses):
        client, completions = fake_client(*responses)
        return AgentHarness(client, self.settings()), completions

    def test_explicit_choice_precedes_slow_command_heuristic(self):
        manager = BackgroundTaskManager()
        self.assertTrue(
            manager.should_run_background(
                "bash", {"command": "echo hello", "run_in_background": True}
            )
        )
        self.assertFalse(
            manager.should_run_background(
                "bash", {"command": "pytest -q", "run_in_background": False}
            )
        )
        self.assertTrue(
            manager.should_run_background("bash", {"command": "pytest -q"})
        )
        self.assertFalse(
            manager.should_run_background(
                "read_file", {"command": "pytest -q", "run_in_background": True}
            )
        )

    def test_completed_notification_is_escaped_bounded_and_delivered_once(self):
        manager = BackgroundTaskManager(summary_chars=10)
        bg_id = manager.start(
            original_tool_call_id="call-secret",
            tool_name="bash",
            command="build <unsafe>",
            runner=lambda: "<result>" + "x" * 20,
        )
        self.assertTrue(manager.wait(bg_id, timeout=2))
        self.assertEqual(COMPLETED, manager.snapshot()[0].status)

        notifications = manager.collect_notifications()
        self.assertEqual(1, len(notifications))
        notification = notifications[0]
        self.assertIn("<task_id>bg_0001</task_id>", notification)
        self.assertIn("<status>completed</status>", notification)
        self.assertIn("build &lt;unsafe&gt;", notification)
        self.assertIn("&lt;result&gt;", notification)
        self.assertIn("chars omitted", notification)
        self.assertNotIn("call-secret", notification)
        self.assertEqual([], manager.collect_notifications())

    def test_worker_exception_becomes_failed_notification(self):
        manager = BackgroundTaskManager()

        def fail():
            raise RuntimeError("boom")

        bg_id = manager.start(
            original_tool_call_id="call-1",
            tool_name="bash",
            command="make failure",
            runner=fail,
        )
        self.assertTrue(manager.wait(bg_id, timeout=2))
        self.assertEqual(FAILED, manager.snapshot()[0].status)
        notification = manager.collect_notifications()[0]
        self.assertIn("<status>failed</status>", notification)
        self.assertIn("RuntimeError: boom", notification)

    def test_concurrent_dispatch_allocates_unique_ids(self):
        manager = BackgroundTaskManager()
        release = threading.Event()
        ids = []

        def dispatch(index):
            ids.append(
                manager.start(
                    original_tool_call_id=f"call-{index}",
                    tool_name="bash",
                    command=f"test {index}",
                    runner=lambda: (release.wait(2), "done")[1],
                )
            )

        dispatchers = [
            threading.Thread(target=dispatch, args=(index,))
            for index in range(5)
        ]
        for thread in dispatchers:
            thread.start()
        for thread in dispatchers:
            thread.join(2)

        self.assertEqual(
            [f"bg_{index:04d}" for index in range(1, 6)],
            sorted(ids),
        )
        self.assertEqual(5, manager.running_count)
        release.set()
        self.assertTrue(manager.wait_for_all(timeout=2))
        self.assertEqual(5, len(manager.collect_notifications()))

    def test_dispatch_keeps_pre_hook_on_parent_and_post_hook_on_worker(self):
        harness, _ = self.harness()
        harness._parent_handlers["bash"] = lambda command: f"ran: {command}"
        events = []
        harness.hooks.register(
            "PreToolUse",
            lambda request: events.append(
                ("pre", threading.current_thread().name, request.arguments)
            ),
        )
        harness.hooks.register(
            "PostToolUse",
            lambda request, result: events.append(
                ("post", threading.current_thread().name, result)
            ),
        )

        result = harness._dispatch_parent_tool(
            self.tool_call("call-1", "echo hello", True)
        )
        self.assertIn("Background task bg_0001 started", result)
        self.assertTrue(harness.background.wait_for_all(timeout=2))
        self.assertEqual("pre", events[0][0])
        self.assertEqual(threading.current_thread().name, events[0][1])
        self.assertEqual("post", events[1][0])
        self.assertEqual("cc-background-bg_0001", events[1][1])
        self.assertEqual("ran: echo hello", events[1][2])

    def test_explicit_false_runs_synchronously_and_strips_control_argument(self):
        harness, _ = self.harness()
        received = []
        harness._parent_handlers["bash"] = (
            lambda command: received.append(command) or "foreground"
        )
        result = harness._dispatch_parent_tool(
            self.tool_call("call-1", "pytest -q", False)
        )
        self.assertEqual("foreground", result)
        self.assertEqual(["pytest -q"], received)
        self.assertEqual(0, harness.background.running_count)

    def test_permission_rejection_does_not_start_worker(self):
        harness, _ = self.harness()
        result = harness._dispatch_parent_tool(
            self.tool_call("call-1", "sudo true", True)
        )
        self.assertIn("Permission denied", result)
        self.assertEqual((), harness.background.snapshot())

    def test_notification_is_an_independent_user_event(self):
        harness, _ = self.harness()
        bg_id = harness.background.start(
            original_tool_call_id="original-call",
            tool_name="bash",
            command="build docs",
            runner=lambda: "ready",
        )
        self.assertTrue(harness.background.wait(bg_id, timeout=2))
        messages = []
        extraction_messages = []
        self.assertEqual(
            1,
            harness._inject_background_notifications(
                messages, extraction_messages
            ),
        )
        self.assertEqual("user", messages[0]["role"])
        self.assertNotIn("tool_call_id", messages[0])
        self.assertIn("<task_notification>", messages[0]["content"])
        self.assertNotIn("original-call", messages[0]["content"])
        self.assertEqual(messages, extraction_messages)
        self.assertIsNot(messages[0], extraction_messages[0])

    def test_previous_turn_completion_is_visible_to_first_model_request(self):
        harness, completions = self.harness("handled", "[]")
        bg_id = harness.background.start(
            original_tool_call_id="old-call",
            tool_name="bash",
            command="build package",
            runner=lambda: "package ready",
        )
        self.assertTrue(harness.background.wait(bg_id, timeout=2))
        messages = [
            {"role": "system", "content": "stale"},
            {"role": "user", "content": "What is the status?"},
        ]
        answer = harness.agent_loop(
            messages,
            active_request="What is the status?",
            compactor=IdentityCompactor(),
        )
        self.assertEqual("handled", answer)
        sent = completions.requests[0]["messages"]
        notifications = [
            item
            for item in sent
            if item.get("role") == "user"
            and "<task_notification>" in item.get("content", "")
        ]
        self.assertEqual(1, len(notifications))
        self.assertIn("package ready", notifications[0]["content"])

    def test_completion_during_model_response_forces_notification_round(self):
        release = threading.Event()

        class CompletingCompletions:
            def __init__(self):
                self.requests = []
                self.harness = None

            def create(self, **kwargs):
                self.requests.append(copy.deepcopy(kwargs))
                index = len(self.requests)
                if index == 1:
                    release.set()
                    self.harness.background.wait_for_all(timeout=2)
                    return SimpleNamespace(choices=[FakeChoice("too early")])
                if index == 2:
                    return SimpleNamespace(
                        choices=[FakeChoice("handled notification")]
                    )
                return SimpleNamespace(choices=[FakeChoice("[]")])

        completions = CompletingCompletions()
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )
        harness = AgentHarness(client, self.settings())
        completions.harness = harness
        harness.background.start(
            original_tool_call_id="call-1",
            tool_name="bash",
            command="build package",
            runner=lambda: (release.wait(2), "finished")[1],
        )
        messages = [
            {"role": "system", "content": "stale"},
            {"role": "user", "content": "Continue other work."},
        ]
        answer = harness.agent_loop(
            messages,
            active_request="Continue other work.",
            compactor=IdentityCompactor(),
        )
        self.assertEqual("handled notification", answer)
        second_request = completions.requests[1]["messages"]
        self.assertTrue(
            any(
                "<task_notification>" in item.get("content", "")
                for item in second_request
            )
        )




class CronSchedulerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.ids = iter(
            ["cron_test_1", "cron_test_2", "cron_test_3", "cron_test_4"]
        )

    def tearDown(self):
        self.temporary.cleanup()

    def scheduler(self):
        return CronScheduler(
            self.root / ".scheduled_tasks.json",
            id_factory=lambda: next(self.ids),
        )

    def test_cron_parser_supports_wildcard_step_list_range_and_exact(self):
        monday = datetime(2026, 9, 7, 9, 30)
        self.assertTrue(cron_matches("*/15 9 * 9 1", monday))
        self.assertTrue(cron_matches("30 8-10 * 9 0,1,2", monday))
        self.assertFalse(cron_matches("31 9 * 9 1", monday))
        self.assertFalse(cron_matches("30 10 * 9 1", monday))

    def test_day_of_month_and_weekday_use_or_semantics(self):
        monday_the_seventh = datetime(2026, 9, 7, 9, 0)
        self.assertTrue(cron_matches("0 9 7 * 0", monday_the_seventh))
        self.assertTrue(cron_matches("0 9 8 * 1", monday_the_seventh))
        self.assertFalse(cron_matches("0 9 8 * 0", monday_the_seventh))

    def test_invalid_cron_expressions_are_rejected(self):
        self.assertIn("Expected 5 fields", validate_cron("* * * *"))
        self.assertIn("minute", validate_cron("60 * * * *"))
        self.assertIn("hour", validate_cron("* 24 * * *"))
        self.assertIn("Range start", validate_cron("* * 8-2 * *"))
        self.assertIn("Invalid step", validate_cron("*/0 * * * *"))
        self.assertFalse(cron_matches("*/0 * * * *", datetime(2026, 9, 4)))
        self.assertIsNone(validate_cron("0,15,30,45 9-17 * * 1-5"))

    def test_durable_and_session_jobs_have_distinct_storage(self):
        scheduler = self.scheduler()
        durable = scheduler.schedule_job("0 9 * * 1-5", "run tests")
        session = scheduler.schedule_job(
            "30 9 * * 1-5",
            "review logs",
            durable=False,
        )
        self.assertIsInstance(durable, CronJob)
        self.assertIsInstance(session, CronJob)

        payload = json.loads(scheduler.storage_path.read_text(encoding="utf-8"))
        self.assertEqual([durable.id], [item["id"] for item in payload])
        self.assertEqual(0o600, scheduler.storage_path.stat().st_mode & 0o777)
        listing = scheduler.run_list_crons()
        self.assertIn("[recurring, durable]", listing)
        self.assertIn("[recurring, session]", listing)

    def test_loading_skips_bad_records_and_requeues_pending_delivery(self):
        valid = {
            "id": "cron_saved",
            "cron": "* * * * *",
            "prompt": "resume work",
            "recurring": True,
            "durable": True,
            "pending_delivery": True,
            "last_fired": "2026-09-04 10:30",
        }
        invalid = {**valid, "id": "../escape"}
        path = self.root / ".scheduled_tasks.json"
        path.write_text(json.dumps([invalid, valid]), encoding="utf-8")

        scheduler = self.scheduler()
        self.assertEqual(1, scheduler.load_durable_jobs())
        self.assertEqual(["cron_saved"], list(scheduler.jobs))
        self.assertEqual(["cron_saved"], [job.id for job in scheduler.consume_queue()])
        self.assertEqual(0, scheduler.load_durable_jobs())

    def test_poll_is_once_per_minute_and_date_aware(self):
        scheduler = self.scheduler()
        job = scheduler.schedule_job("* * * * *", "tick", durable=False)
        self.assertIsInstance(job, CronJob)
        first = datetime(2026, 9, 4, 10, 30, 1)
        self.assertEqual(1, scheduler.poll_due_jobs(first))
        self.assertEqual(0, scheduler.poll_due_jobs(first.replace(second=50)))
        delivered = scheduler.consume_queue()
        scheduler.acknowledge(delivered)
        self.assertFalse(job.pending_delivery)
        self.assertEqual(0, scheduler.poll_due_jobs(first.replace(second=59)))
        self.assertEqual(1, scheduler.poll_due_jobs(datetime(2026, 9, 5, 10, 30)))

    def test_acknowledge_removes_one_shot_and_releases_recurring_job(self):
        scheduler = self.scheduler()
        recurring = scheduler.schedule_job("* * * * *", "repeat", durable=False)
        one_shot = scheduler.schedule_job(
            "* * * * *",
            "once",
            recurring=False,
            durable=False,
        )
        self.assertEqual(2, scheduler.poll_due_jobs(datetime(2026, 9, 4, 10, 30)))
        delivered = scheduler.consume_queue()
        scheduler.acknowledge(delivered)
        self.assertIn(recurring.id, scheduler.jobs)
        self.assertFalse(recurring.pending_delivery)
        self.assertNotIn(one_shot.id, scheduler.jobs)

    def test_restore_and_cancel_preserve_delivery_guarantees(self):
        scheduler = self.scheduler()
        job = scheduler.schedule_job("* * * * *", "retry me", durable=False)
        scheduler.poll_due_jobs(datetime(2026, 9, 4, 10, 30))
        delivered = scheduler.consume_queue()
        scheduler.restore(delivered)
        scheduler.restore(delivered)
        self.assertEqual([job.id], [queued.id for queued in scheduler.queue])
        self.assertEqual(f"Cancelled {job.id}", scheduler.cancel_job(job.id))
        self.assertFalse(scheduler.has_queue())
        self.assertNotIn(job.id, scheduler.jobs)

    def test_failed_persistence_rolls_back_creation(self):
        scheduler = self.scheduler()

        def fail():
            raise OSError("disk full")

        scheduler._save_durable_jobs = fail
        result = scheduler.schedule_job("* * * * *", "must persist")
        self.assertIn("Could not persist", result)
        self.assertEqual({}, scheduler.jobs)


class CronIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self):
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

    def harness(self, *responses):
        client, completions = fake_client(*responses)
        harness = AgentHarness(
            client,
            self.settings(),
            sleep_fn=lambda _delay: None,
            random_fn=lambda low, _high: low,
        )
        return harness, completions

    def test_cron_tools_are_parent_only_and_use_shared_executor(self):
        harness, _ = self.harness()
        parent_names = registered_tool_names(PARENT_TOOLS)
        child_names = registered_tool_names(SUB_TOOLS)
        self.assertEqual(
            ["schedule_cron", "list_crons", "cancel_cron"],
            [name for name in parent_names if "cron" in name],
        )
        self.assertNotIn("schedule_cron", child_names)
        self.assertIn("schedule_cron", harness._parent_handlers)

        result = harness.executor.execute(
            "schedule_cron",
            json.dumps(
                {
                    "cron": "* * * * *",
                    "prompt": "inspect status",
                    "durable": False,
                }
            ),
            harness._parent_handlers,
        )
        self.assertIn("Scheduled cron_", result)
        self.assertIn("inspect status", harness.cron.run_list_crons())

    def test_successful_model_response_acknowledges_one_shot(self):
        harness, completions = self.harness("scheduled done", "[]")
        job = harness.cron.schedule_job(
            "* * * * *",
            "run scheduled check",
            recurring=False,
            durable=False,
        )
        harness.cron.poll_due_jobs(datetime(2026, 9, 4, 10, 30))
        messages = [{"role": "system", "content": harness.system_prompt}]

        answer = harness.agent_loop(messages, compactor=IdentityCompactor())

        self.assertEqual("scheduled done", answer)
        self.assertNotIn(job.id, harness.cron.jobs)
        sent = completions.requests[0]["messages"]
        scheduled = [
            item for item in sent if item.get("content") == "[Scheduled] run scheduled check"
        ]
        self.assertEqual(1, len(scheduled))
        self.assertEqual("user", scheduled[0]["role"])
        self.assertNotIn("tool_call_id", scheduled[0])

    def test_unrecoverable_model_error_restores_job_and_removes_injection(self):
        harness, _ = self.harness(RuntimeError("invalid request"))
        job = harness.cron.schedule_job(
            "* * * * *",
            "retry scheduled check",
            recurring=False,
            durable=False,
        )
        harness.cron.poll_due_jobs(datetime(2026, 9, 4, 10, 30))
        messages = [{"role": "system", "content": harness.system_prompt}]

        answer = harness.agent_loop(messages, compactor=IdentityCompactor())

        self.assertIn("invalid request", answer)
        self.assertEqual([job.id], [queued.id for queued in harness.cron.queue])
        self.assertFalse(
            any(
                item.get("content") == "[Scheduled] retry scheduled check"
                for item in messages
            )
        )

    def test_explicit_user_request_keeps_priority_over_scheduled_prompt(self):
        class RecordingCompactor(IdentityCompactor):
            def __init__(self):
                self.active_requests = []

            def prepare(self, messages, active_request):
                self.active_requests.append(active_request)
                return messages

        harness, _ = self.harness("done", "[]")
        harness.cron.schedule_job("* * * * *", "scheduled work", durable=False)
        harness.cron.poll_due_jobs(datetime(2026, 9, 4, 10, 30))
        compactor = RecordingCompactor()
        messages = [{"role": "user", "content": "human request"}]
        harness.agent_loop(
            messages,
            active_request="human request",
            compactor=compactor,
        )
        self.assertEqual("human request", compactor.active_requests[0])

    def test_queue_processor_waits_until_agent_lock_is_free(self):
        harness, completions = self.harness("automatic answer", "[]")
        harness.cron.schedule_job(
            "* * * * *",
            "automatic task",
            recurring=False,
            durable=False,
        )
        harness.cron.poll_due_jobs(datetime(2026, 9, 4, 10, 30))
        messages = [{"role": "system", "content": harness.system_prompt}]
        answers = []
        delivered = threading.Event()
        lock_held = harness._agent_lock.acquire()
        self.assertTrue(lock_held)
        try:
            harness.start_cron_runtime(
                messages,
                lambda answer: (answers.append(answer), delivered.set()),
            )
            time.sleep(0.35)
            self.assertEqual([], completions.requests)
        finally:
            harness._agent_lock.release()

        try:
            self.assertTrue(delivered.wait(2))
        finally:
            harness.stop_cron_runtime()
        self.assertEqual(["automatic answer"], answers)
        self.assertFalse(harness.cron.is_running)

    def test_scheduled_turn_cannot_open_interactive_permission_prompt(self):
        harness, _ = self.harness()
        results = []

        def check_in_worker():
            request = SimpleNamespace(
                name="bash",
                arguments={"command": "rm build.tmp"},
            )
            results.append(harness.hooks.trigger("PreToolUse", request))

        worker = threading.Thread(target=check_in_worker)
        worker.start()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(
            ["Permission denied: scheduled turns cannot request interactive approval"],
            results,
        )


if __name__ == "__main__":
    unittest.main()
