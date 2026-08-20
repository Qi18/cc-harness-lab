import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


CODE_PATH = Path(__file__).parents[1] / "s09_memory" / "code.py"
SPEC = importlib.util.spec_from_file_location("s09_code", CODE_PATH)
s09 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = s09
assert SPEC.loader is not None
SPEC.loader.exec_module(s09)


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


class ScriptedCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(
            choices=[SimpleNamespace(message=FakeMessage(response))]
        )


def fake_client(*responses):
    completions = ScriptedCompletions(responses)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return client, completions


class IdentityCompactor:
    def prepare(self, messages, active_request):
        return messages


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.saved = {
            "WORKDIR": s09.WORKDIR,
            "MEMORY_DIR": s09.MEMORY_DIR,
            "MEMORY_INDEX": s09.MEMORY_INDEX,
            "ACTIVE_CLIENT": s09.ACTIVE_CLIENT,
        }
        s09.WORKDIR = self.root
        s09.MEMORY_DIR = self.root / ".memory"
        s09.MEMORY_INDEX = s09.MEMORY_DIR / "MEMORY.md"

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(s09, name, value)
        self.temporary.cleanup()

    def write_record(self, name="tabs", description="Use tabs", body="Use tabs."):
        return s09.write_memory_file(name, "user", description, body)

    def test_write_memory_creates_frontmatter_and_index(self):
        path = self.write_record()

        metadata, body = s09.parse_memory_frontmatter(
            path.read_text(encoding="utf-8")
        )
        self.assertEqual("tabs", metadata["name"])
        self.assertEqual("user", metadata["type"])
        self.assertEqual("Use tabs.", body.strip())
        self.assertIn("[tabs](tabs.md) — Use tabs", s09.read_memory_index())

    def test_memory_path_rejects_traversal_and_index_as_record(self):
        with self.assertRaises(ValueError):
            s09.memory_path("../outside.md")
        with self.assertRaises(ValueError):
            s09.memory_path("MEMORY.md")

    def test_memory_store_must_stay_inside_workdir(self):
        s09.MEMORY_DIR = self.root.parent / "outside-memory"
        s09.MEMORY_INDEX = s09.MEMORY_DIR / "MEMORY.md"
        with self.assertRaises(ValueError):
            s09.memory_path("record.md")

    def test_model_selects_relevant_memory_indices(self):
        self.write_record("tabs", "Python indentation preference", "Use tabs.")
        s09.write_memory_file(
            "deploy", "project", "Production deploy notes", "Use canary deploys."
        )
        client, completions = fake_client("Result: [1, 1, 0]")

        selected = s09.select_relevant_memories(
            client,
            [{"role": "user", "content": "How should I deploy?"}],
            max_items=2,
        )

        self.assertEqual(["tabs.md", "deploy.md"], selected)
        self.assertNotIn("tools", completions.requests[0])

    def test_selection_falls_back_to_catalog_keywords(self):
        self.write_record(
            "python-tabs",
            "Python indentation preference",
            "Use tabs in Python.",
        )
        client, _ = fake_client(RuntimeError("offline"))

        selected = s09.select_relevant_memories(
            client,
            [{"role": "user", "content": "Python indentation"}],
        )

        self.assertEqual(["python-tabs.md"], selected)

    def test_load_memories_obeys_recall_budget(self):
        self.write_record("one", "one description", "A" * 200)
        client, _ = fake_client("[0]")
        with patch.object(s09, "MEMORY_RECALL_CHAR_LIMIT", 80):
            recalled = json.loads(
                s09.load_memories(
                    client,
                    [{"role": "user", "content": "one description"}],
                )
            )

        self.assertEqual("one.md", recalled[0]["source"])
        self.assertLessEqual(len(recalled[0]["content"]), 80)

    def test_recall_is_attached_to_latest_user_turn_as_background(self):
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "current request"},
        ]

        s09.inject_recalled_memories(messages, '[{"source":"tabs.md"}]')

        self.assertEqual("first", messages[1]["content"])
        self.assertIn("Background knowledge only", messages[3]["content"])
        self.assertEqual(
            "current request",
            s09.without_recalled_memory(messages[3]["content"]),
        )

    def test_extract_stores_only_persistent_non_secret_records(self):
        response = json.dumps(
            [
                {
                    "name": "tabs",
                    "type": "user",
                    "scope": "persistent",
                    "description": "Use tabs",
                    "body": "Use tabs for indentation.",
                },
                {
                    "name": "temporary",
                    "type": "project",
                    "scope": "current_task",
                    "description": "Current task path",
                    "body": "Use /tmp/work for this task.",
                },
                {
                    "name": "credential",
                    "type": "reference",
                    "scope": "persistent",
                    "description": "API key",
                    "body": "api_key=sk-abcdefghijklmnopqrstuvwxyz",
                },
            ]
        )
        client, _ = fake_client(response)

        stored = s09.extract_memories(
            client,
            [
                {"role": "user", "content": "Remember that I prefer tabs."},
                {"role": "assistant", "content": "Understood."},
            ],
        )

        self.assertEqual(1, stored)
        self.assertEqual(["tabs.md"], [r["filename"] for r in s09.list_memory_files()])

    def test_extract_deduplicates_existing_records(self):
        self.write_record()
        response = json.dumps(
            [
                {
                    "name": "tabs",
                    "type": "user",
                    "scope": "persistent",
                    "description": "Use tabs",
                    "body": "Use tabs.",
                }
            ]
        )
        client, _ = fake_client(response)

        stored = s09.extract_memories(
            client,
            [{"role": "user", "content": "I still prefer tabs."}],
        )

        self.assertEqual(0, stored)
        self.assertEqual(1, len(s09.list_memory_files()))

    def test_consolidation_waits_for_threshold(self):
        self.write_record()
        client, completions = fake_client()

        self.assertEqual(0, s09.consolidate_memories(client))
        self.assertEqual([], completions.requests)

    def test_consolidation_replaces_crowded_store(self):
        for index in range(s09.MEMORY_CONSOLIDATE_THRESHOLD):
            s09.write_memory_file(
                f"record-{index}",
                "project",
                f"description {index}",
                f"body {index}",
            )
        response = json.dumps(
            [
                {
                    "name": "merged-project",
                    "type": "project",
                    "description": "Merged project facts",
                    "body": "The durable merged facts.",
                },
                {
                    "name": "user-style",
                    "type": "user",
                    "description": "User style",
                    "body": "Prefer concise answers.",
                },
            ]
        )
        client, _ = fake_client(response)

        count = s09.consolidate_memories(client)

        self.assertEqual(2, count)
        self.assertEqual(
            ["merged-project.md", "user-style.md"],
            [record["filename"] for record in s09.list_memory_files()],
        )

    def test_system_prompt_contains_index_but_not_full_body(self):
        self.write_record(body="PRIVATE FULL MEMORY BODY")

        prompt = s09.build_memory_system_prompt()

        self.assertIn("[tabs](tabs.md)", prompt)
        self.assertNotIn("PRIVATE FULL MEMORY BODY", prompt)

    def test_agent_loop_recalls_then_extracts_after_final_answer(self):
        self.write_record("tabs", "Python indentation", "Use tabs in Python.")
        client, completions = fake_client("[0]", "done", "[]")
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "Write Python with my indentation."},
        ]

        answer = s09.agent_loop(
            client,
            messages,
            active_request="Write Python with my indentation.",
            compactor=IdentityCompactor(),
        )

        self.assertEqual("done", answer)
        main_request = completions.requests[1]
        self.assertIn("tools", main_request)
        recalled_turn = next(
            message
            for message in main_request["messages"]
            if s09.MEMORY_CONTEXT_START in str(message.get("content", ""))
        )
        self.assertIn("tabs.md", recalled_turn["content"])
        extraction_prompt = completions.requests[2]["messages"][-1]["content"]
        self.assertNotIn(s09.MEMORY_CONTEXT_START, extraction_prompt)

    def test_s09_preserves_s08_tools_and_boundaries(self):
        parent = {tool["function"]["name"] for tool in s09.TOOLS}
