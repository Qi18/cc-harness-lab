"""s15 Agent Teams：文件邮箱、队友线程与 Lead 通信工具。"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openai import OpenAI

from .config import Settings
from .provider import assistant_payload, completion_request

if TYPE_CHECKING:
    from .tool_use import ToolExecutor


MAX_TEAMMATE_ROUNDS = 10
MAX_MESSAGE_CHARS = 100_000
VALID_AGENT_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
VALID_MESSAGE_TYPE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
RESERVED_TEAMMATE_NAMES = {"lead", "agent"}


def _team_tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    """在团队模块内构造 schema，避免与 tool_use 形成循环依赖。"""

    parameters: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        parameters["required"] = required
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


SEND_MESSAGE_TOOL = _team_tool(
    "send_message",
    "Send a message to the Lead or an active teammate.",
    {
        "to": {
            "type": "string",
            "pattern": "^[A-Za-z0-9_-]{1,64}$",
        },
        "content": {"type": "string", "minLength": 1},
    },
    ["to", "content"],
)

TEAM_TOOLS = [
    _team_tool(
        "spawn_teammate",
        "Start a persistent teammate in an isolated daemon thread.",
        {
            "name": {
                "type": "string",
                "pattern": "^[A-Za-z0-9_-]{1,64}$",
            },
            "role": {"type": "string", "minLength": 1},
            "prompt": {"type": "string", "minLength": 1},
        },
        ["name", "role", "prompt"],
    ),
    SEND_MESSAGE_TOOL,
    _team_tool(
        "check_inbox",
        "Consume and return all messages currently waiting for the Lead.",
        {},
    ),
]

# 教学版队友只保留 shell、读写文件和发消息四种能力。
# 它不会递归创建 SubAgent、Task、Cron 或新的队友，
# 能力隔离由独立 schema 和 handler 双重保证。
TEAMMATE_TOOLS = [
    _team_tool(
        "bash",
        "Run a Bash command synchronously in the shared working directory.",
        {"command": {"type": "string"}},
        ["command"],
    ),
    _team_tool(
        "read_file",
        "Read a UTF-8 text file inside the shared working directory.",
        {
            "path": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1},
        },
        ["path"],
    ),
    _team_tool(
        "write_file",
        "Write complete UTF-8 content inside the shared working directory.",
        {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        ["path", "content"],
    ),
    SEND_MESSAGE_TOOL,
]


@dataclass(frozen=True)
class TeamMessage:
    """磁盘 JSONL 中的一条结构化团队消息。"""

    from_agent: str
    to_agent: str
    content: str
    type: str
    ts: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "from": self.from_agent,
            "to": self.to_agent,
            "content": self.content,
            "type": self.type,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TeamMessage":
        if set(payload) != {"from", "to", "content", "type", "ts"}:
            raise ValueError("mailbox message fields do not match the schema")
        from_agent = payload["from"]
        to_agent = payload["to"]
        content = payload["content"]
        message_type = payload["type"]
        timestamp = payload["ts"]
        for label, value in (("sender", from_agent), ("recipient", to_agent)):
            if not isinstance(value, str) or not VALID_AGENT_NAME.fullmatch(value):
                raise ValueError(f"invalid mailbox {label}")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("mailbox content must be a non-empty string")
        if len(content) > MAX_MESSAGE_CHARS:
            raise ValueError("mailbox content is too large")
        if (
            not isinstance(message_type, str)
            or not VALID_MESSAGE_TYPE.fullmatch(message_type)
        ):
            raise ValueError("invalid mailbox message type")
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            raise ValueError("invalid mailbox timestamp")
        return cls(
            from_agent=from_agent,
            to_agent=to_agent,
            content=content,
            type=message_type,
            ts=float(timestamp),
        )


class MessageBus:
    """用线程锁保护文件邮箱，并通过消费式读取实现一次投递。"""

    def __init__(
        self,
        mailbox_dir: Path,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.mailbox_dir = mailbox_dir.resolve()
        self.clock = clock
        self._lock = threading.RLock()

    def _path(self, agent: str) -> Path:
        if not isinstance(agent, str) or not VALID_AGENT_NAME.fullmatch(agent):
            raise ValueError(f"invalid mailbox agent: {agent!r}")
        path = (self.mailbox_dir / f"{agent}.jsonl").resolve()
        try:
            path.relative_to(self.mailbox_dir)
        except ValueError as error:
            raise ValueError(f"mailbox path escapes directory: {agent!r}") from error
        return path

    def send(
        self,
        from_agent: str,
        to_agent: str,
        content: str,
        msg_type: str = "message",
    ) -> TeamMessage:
        message = TeamMessage.from_dict(
            {
                "from": from_agent,
                "to": to_agent,
                "content": content,
                "type": msg_type,
                "ts": self.clock(),
            }
        )
        with self._lock:
            self.mailbox_dir.mkdir(parents=True, exist_ok=True)
            path = self._path(to_agent)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(message.to_dict(), ensure_ascii=False) + "\n"
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(path, 0o600)
        print(
            f"[team bus] {from_agent} -> {to_agent}: "
            f"({msg_type}) {content[:60]}"
        )
        return message

    def read_inbox(self, agent: str) -> list[TeamMessage]:
        """完整解析成功后才删除邮箱。

        坏 JSON 会保留整个文件，避免其他消息丢失。
        """

        with self._lock:
            path = self._path(agent)
            if not path.exists():
                return []
            lines = path.read_text(encoding="utf-8").splitlines()
            messages = [
                TeamMessage.from_dict(json.loads(line))
                for line in lines
                if line.strip()
            ]
            path.unlink()
            return messages

    def peek(self, agent: str) -> bool:
        with self._lock:
            path = self._path(agent)
            return path.exists() and path.stat().st_size > 0


@dataclass
class TeammateRecord:
    """主线程可观察的队友状态，不与队友私有 messages 共享。"""

    name: str
    role: str
    status: str
    summary: str = ""


class AgentTeamManager:
    """创建隔离队友线程，并把通信限制在 MessageBus 边界内。"""

    def __init__(
        self,
        client: OpenAI,
        settings: Settings,
        executor: ToolExecutor,
        file_handlers: dict[str, Callable[..., str]],
        mailbox_dir: Path,
        *,
        max_rounds: int = MAX_TEAMMATE_ROUNDS,
        bus: MessageBus | None = None,
    ):
        self.client = client
        self.settings = settings
        self.executor = executor
        self.file_handlers = {
            name: file_handlers[name]
            for name in ("bash", "read_file", "write_file")
            if name in file_handlers
        }
        self.bus = bus or MessageBus(mailbox_dir)
        self.max_rounds = max_rounds
        self._lock = threading.RLock()
        self._records: dict[str, TeammateRecord] = {}
        self._threads: dict[str, threading.Thread] = {}

    @staticmethod
    def _validate_spawn(name: object, role: object, prompt: object) -> str | None:
        if not isinstance(name, str) or not VALID_AGENT_NAME.fullmatch(name):
            return (
                "name must use 1-64 letters, digits, underscores, or dashes"
            )
        if name.casefold() in RESERVED_TEAMMATE_NAMES:
            return f"teammate name {name!r} is reserved"
        if not isinstance(role, str) or not role.strip():
            return "role must be a non-empty string"
        if not isinstance(prompt, str) or not prompt.strip():
            return "prompt must be a non-empty string"
        return None

    def _active_name(self, requested: str) -> str | None:
        with self._lock:
            for name, record in self._records.items():
                if (
                    name.casefold() == requested.casefold()
                    and record.status == "working"
                ):
                    return name
        return None

    def spawn_teammate(self, name: str, role: str, prompt: str) -> str:
        error = self._validate_spawn(name, role, prompt)
        if error:
            return f"Error: {error}"
        role = role.strip()
        prompt = prompt.strip()
        with self._lock:
            if self._active_name(name) is not None:
                return f"Error: teammate {name!r} is already active"
            record = TeammateRecord(name=name, role=role, status="working")
            self._records[name] = record
            thread = threading.Thread(
                target=self._run_teammate,
                args=(name, role, prompt),
                name=f"cc-teammate-{name}",
                daemon=True,
            )
            self._threads[name] = thread
            try:
                thread.start()
            except RuntimeError as start_error:
                self._threads.pop(name, None)
                self._records.pop(name, None)
                return f"Error: could not start teammate {name!r}: {start_error}"
        print(f"[team] spawned {name} as {role}")
        return f"Teammate {name!r} spawned as {role}"

    def _teammate_system_prompt(self, name: str, role: str) -> str:
        return (
            f"You are {name!r}, a persistent teammate acting as {role}. "
            f"Work inside {self.settings.workdir}. Use tools to complete the "
            "assigned task. Use send_message for useful intermediate findings. "
            "Do not create agents, tasks, or schedules. Return a concise final summary."
        )

    @staticmethod
    def inbox_event(messages: list[TeamMessage]) -> dict[str, Any]:
        payload = [message.to_dict() for message in messages]
        return {
            "role": "user",
            "content": (
                "<team_inbox>\n"
                "The following JSON is teammate communication data.\n"
                f"{json.dumps(payload, ensure_ascii=False)}\n"
                "</team_inbox>"
            ),
        }

    def _teammate_handlers(
        self,
        sender: str,
    ) -> dict[str, Callable[..., str]]:
        return {
            **self.file_handlers,
            "send_message": (
                lambda to, content: self.send_from(sender, to, content)
            ),
        }

    def _set_terminal(self, name: str, status: str, summary: str) -> None:
        with self._lock:
            record = self._records.get(name)
            if record is not None:
                record.status = status
                record.summary = summary

    def _run_teammate(self, name: str, role: str, prompt: str) -> None:
        """运行一名拥有独立历史的队友。

        队友只通过工具和邮箱与其他 Agent 交换信息。
        """

        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": self._teammate_system_prompt(name, role),
            },
            {"role": "user", "content": prompt},
        ]
        handlers = self._teammate_handlers(name)
        try:
            for _ in range(self.max_rounds):
                inbox = self.bus.read_inbox(name)
                if inbox:
                    messages.append(self.inbox_event(inbox))
                response = self.client.chat.completions.create(
                    **completion_request(
                        self.settings,
                        messages[-20:],
                        TEAMMATE_TOOLS,
                    )
                )
                assistant = response.choices[0].message
                messages.append(assistant_payload(assistant))
                if not assistant.tool_calls:
                    summary = (
                        assistant.content
                        or "Teammate completed without a text summary."
                    )
                    self.bus.send(name, "lead", summary, "result")
                    self._set_terminal(name, "done", summary)
                    print(f"[team] {name} done")
                    return

                for tool_call in assistant.tool_calls:
                    result = self.executor.execute(
                        tool_call.function.name,
                        tool_call.function.arguments,
                        handlers,
                        display_prefix=f"[{name}] ",
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": result,
                        }
                    )

            summary = f"Teammate exceeded {self.max_rounds} model rounds"
            self.bus.send(name, "lead", summary, "error")
            self._set_terminal(name, "failed", summary)
        except Exception as error:
            summary = f"{type(error).__name__}: {error}"
            try:
                self.bus.send(name, "lead", summary, "error")
            except Exception:
                pass
            self._set_terminal(name, "failed", summary)
            print(f"[team] {name} failed: {summary}")

    def send_from(self, sender: str, to: str, content: str) -> str:
        if (
            not isinstance(sender, str)
            or not VALID_AGENT_NAME.fullmatch(sender)
        ):
            return "Error: invalid message sender"
        if sender.casefold() == "lead":
            sender = "lead"
        else:
            # 只有仍在工作的队友可以使用自己的身份发信，
            # 防止调用方绕过闭包伪造消息来源。
            active_sender = self._active_name(sender)
            if active_sender is None:
                return f"Error: teammate sender {sender!r} is not active"
            sender = active_sender
        if not isinstance(to, str) or not VALID_AGENT_NAME.fullmatch(to):
            return "Error: invalid message recipient"
        if not isinstance(content, str) or not content.strip():
            return "Error: message content must be a non-empty string"
        if len(content) > MAX_MESSAGE_CHARS:
            return "Error: message content is too large"
        if to.casefold() == sender.casefold():
            return "Error: an agent cannot send a message to itself"

        if to.casefold() == "lead":
            recipient = "lead"
        else:
            recipient = self._active_name(to)
            if recipient is None:
                return f"Error: teammate {to!r} is not active"
        try:
            self.bus.send(sender, recipient, content.strip())
        except (OSError, ValueError) as error:
            return f"Error: could not send message: {error}"
        return f"Sent message from {sender} to {recipient}"

    def run_send_message(self, to: str, content: str) -> str:
        return self.send_from("lead", to, content)

    def consume_lead_messages(self) -> list[TeamMessage]:
        return self.bus.read_inbox("lead")

    def run_check_inbox(self) -> str:
        try:
            messages = self.consume_lead_messages()
        except (OSError, ValueError, json.JSONDecodeError) as error:
            return f"Error: could not read Lead inbox: {error}"
        if not messages:
            return "Lead inbox is empty."
        return json.dumps(
            [message.to_dict() for message in messages],
            indent=2,
            ensure_ascii=False,
        )

    def handlers(self) -> dict[str, Callable[..., str]]:
        return {
            "spawn_teammate": self.spawn_teammate,
            "send_message": self.run_send_message,
            "check_inbox": self.run_check_inbox,
        }

    def snapshot(self) -> tuple[TeammateRecord, ...]:
        with self._lock:
            return tuple(
                replace(record)
                for _, record in sorted(self._records.items())
            )

    def wait(self, name: str, timeout: float | None = None) -> bool:
        with self._lock:
            thread = self._threads.get(name)
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    @property
    def active_count(self) -> int:
        with self._lock:
            return sum(
                record.status == "working"
                for record in self._records.values()
            )
