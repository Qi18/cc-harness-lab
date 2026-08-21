import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT))

from s11_error_recovery.harness.agent import AgentHarness
from s11_error_recovery.harness.config import Settings
from s11_error_recovery.harness.recovery import (
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
from s11_error_recovery.harness.tools import PARENT_TOOLS


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
        self.message = FakeMessage(content, tool_calls)
        self.finish_reason = finish_reason


class ScriptedCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, FakeChoice):
            choice = response
        else:
            choice = FakeChoice(response)
        return SimpleNamespace(choices=[choice])


def fake_client(*responses):
    completions = ScriptedCompletions(responses)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


class IdentityCompactor:
    def __init__(self):
        self.reactive_calls = 0

    def prepare(self, messages, active_request):
        return messages

    def reactive_compact(self, messages, active_request):
        self.reactive_calls += 1
        return [
            messages[0],
            {"role": "user", "content": f"[compact] {active_request}"},
        ]


class ErrorRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self):
        self.temporary.cleanup()

    def settings(self, fallback="qwen-turbo"):
        return Settings(
            workdir=self.root,
            skills_dir=self.root / "skills",
            transcript_dir=self.root / ".transcripts",
            tool_results_dir=self.root / ".task_outputs" / "tool-results",
            memory_dir=self.root / ".memory",
            tasks_dir=self.root / ".tasks",
            model="qwen-plus",
            fallback_model=fallback,
        )

    def harness(self, *responses, fallback="qwen-turbo"):
        client, completions = fake_client(*responses)
        sleeps = []
        harness = AgentHarness(
            client,
            self.settings(fallback),
            sleep_fn=sleeps.append,
            random_fn=lambda low, high: low,
        )
        return harness, completions, sleeps

    def messages(self):
        return [
            {"role": "system", "content": "stale"},
            {"role": "user", "content": "finish the task"},
        ]

    def test_retry_delay_uses_exponential_cap_and_retry_after(self):
        self.assertEqual(0.5, retry_delay(0, random_fn=lambda low, high: low))
        self.assertEqual(32.0, retry_delay(20, random_fn=lambda low, high: low))
        self.assertEqual(7.0, retry_delay(3, retry_after=7.0))

    def test_prompt_and_output_error_classification(self):
        self.assertTrue(is_prompt_too_long_error(Exception("context_length_exceeded")))
        self.assertTrue(is_output_truncated("length"))
        self.assertTrue(is_output_truncated("max_tokens"))
        self.assertFalse(is_output_truncated("stop"))

    def test_with_retry_retries_429_without_real_sleep(self):
        attempts = []
        sleeps = []
        state = RecoveryState("primary")

        def call(model):
            attempts.append(model)
            if len(attempts) < 3:
                raise RuntimeError("HTTP 429 rate limit")
            return "ok"

        result = with_retry(
            call,
            state,
            sleep_fn=sleeps.append,
            random_fn=lambda low, high: low,
        )
        self.assertEqual("ok", result)
        self.assertEqual(["primary", "primary", "primary"], attempts)
        self.assertEqual([0.5, 1.0], sleeps)

    def test_three_529_errors_switch_to_fallback(self):
        attempts = []
        state = RecoveryState("primary")

        def call(model):
            attempts.append(model)
            if len(attempts) <= 3:
                raise RuntimeError("HTTP 529 overloaded")
            return model

        result = with_retry(
            call,
            state,
            "fallback",
            sleep_fn=lambda delay: None,
            random_fn=lambda low, high: low,
        )
        self.assertEqual("fallback", result)
        self.assertEqual(["primary", "primary", "primary", "fallback"], attempts)

    def test_non_transient_error_is_not_retried(self):
        attempts = []
        state = RecoveryState("primary")
        with self.assertRaisesRegex(ValueError, "invalid"):
            with_retry(
                lambda model: attempts.append(model) or (_ for _ in ()).throw(
                    ValueError("invalid request")
                ),
                state,
                sleep_fn=lambda delay: None,
            )
        self.assertEqual(["primary"], attempts)

    def test_retry_budget_is_bounded(self):
        state = RecoveryState("primary")
        with self.assertRaises(MaxRetriesExceeded):
            with_retry(
                lambda model: (_ for _ in ()).throw(RuntimeError("429")),
                state,
                max_retries=2,
                sleep_fn=lambda delay: None,
                random_fn=lambda low, high: low,
            )

    def test_first_truncation_escalates_without_appending_partial(self):
        harness, completions, _ = self.harness(
            FakeChoice("discard-me", "length"),
            FakeChoice("complete", "stop"),
            "[]",
        )
        messages = self.messages()
        answer = harness.agent_loop(
            messages,
            active_request="finish the task",
            compactor=IdentityCompactor(),
        )
        main_requests = [request for request in completions.requests if "tools" in request]
        self.assertEqual("complete", answer)
        self.assertEqual(
            [DEFAULT_MAX_TOKENS, ESCALATED_MAX_TOKENS],
            [request["max_tokens"] for request in main_requests],
        )
        self.assertNotIn("discard-me", [item.get("content") for item in messages])

    def test_second_truncation_adds_continuation(self):
        harness, _, _ = self.harness(
            FakeChoice("first-partial", "length"),
            FakeChoice("second-partial", "length"),
            FakeChoice("complete", "stop"),
            "[]",
        )
        messages = self.messages()
        answer = harness.agent_loop(
            messages,
            active_request="finish the task",
            compactor=IdentityCompactor(),
        )
        self.assertEqual("complete", answer)
        self.assertNotIn("first-partial", [item.get("content") for item in messages])
        self.assertIn("second-partial", [item.get("content") for item in messages])
        self.assertIn(CONTINUATION_PROMPT, [item.get("content") for item in messages])

    def test_prompt_too_long_compacts_once_then_recovers(self):
        harness, _, _ = self.harness(
            RuntimeError("context_length_exceeded"),
            FakeChoice("recovered", "stop"),
            "[]",
        )
        compactor = IdentityCompactor()
        answer = harness.agent_loop(
            self.messages(),
            active_request="finish the task",
            compactor=compactor,
        )
        self.assertEqual("recovered", answer)
        self.assertEqual(1, compactor.reactive_calls)

    def test_unrecoverable_error_becomes_assistant_result(self):
        harness, completions, _ = self.harness(ValueError("bad credentials"))
        messages = self.messages()
        answer = harness.agent_loop(
            messages,
            active_request="finish the task",
            compactor=IdentityCompactor(),
        )
        self.assertIn("ValueError", answer)
        self.assertEqual("assistant", messages[-1]["role"])
        self.assertEqual(1, len(completions.requests))

    def test_dynamic_prompt_and_previous_tools_remain(self):
        harness, _, _ = self.harness()
        names = {tool["function"]["name"] for tool in PARENT_TOOLS}
        self.assertEqual(
            {
                "bash", "read_file", "write_file", "edit_file", "glob",
                "todo_write", "task", "load_skill", "compact",
            },
            names,
        )
        self.assertIn("Available tools:", harness.system_prompt)
        self.assertNotIn("create_task", harness.system_prompt)


if __name__ == "__main__":
    unittest.main()
