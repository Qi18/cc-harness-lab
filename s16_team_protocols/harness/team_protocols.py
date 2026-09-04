"""s16 Team Protocols：团队请求状态、消息路由与响应关联。"""

from __future__ import annotations

import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any


VALID_REQUEST_ID = re.compile(r"^req_[A-Za-z0-9_-]{1,64}$")
PROTOCOL_TYPES = {"shutdown", "plan_approval"}
EXPECTED_RESPONSE = {
    "shutdown": "shutdown_response",
    "plan_approval": "plan_approval_response",
}


def _protocol_tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    """构造协议工具 schema，避免与团队和工具执行模块形成循环导入。"""

    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


PROTOCOL_TOOLS = [
    _protocol_tool(
        "request_shutdown",
        "Ask an active teammate to shut down gracefully.",
        {"teammate": {"type": "string", "minLength": 1}},
        ["teammate"],
    ),
    _protocol_tool(
        "request_plan",
        "Ask an active teammate to submit a plan for a task.",
        {
            "teammate": {"type": "string", "minLength": 1},
            "task": {"type": "string", "minLength": 1},
        },
        ["teammate", "task"],
    ),
    _protocol_tool(
        "review_plan",
        "Approve or reject a teammate plan by request ID.",
        {
            "request_id": {"type": "string", "minLength": 1},
            "approve": {"type": "boolean"},
            "feedback": {"type": "string"},
        },
        ["request_id", "approve"],
    ),
]

SUBMIT_PLAN_TOOL = _protocol_tool(
    "submit_plan",
    "Submit a plan to the Lead for approval before risky work.",
    {"plan": {"type": "string", "minLength": 1}},
    ["plan"],
)


@dataclass
class ProtocolState:
    """一条可追踪的请求状态。"""

    request_id: str
    type: str
    sender: str
    target: str
    status: str
    payload: str
    created_at: float


@dataclass(frozen=True)
class ProtocolDispatch:
    """队友处理一封信后的控制信号。"""

    handled: bool
    stop: bool = False
    event: dict[str, Any] | None = None


class TeamProtocolManager:
    """维护请求 FSM，并对 Lead 与队友收件箱执行协议路由。"""

    def __init__(
        self,
        send_message: Callable[..., Any],
        resolve_active: Callable[[str], str | None],
        *,
        clock: Callable[[], float] = time.time,
        request_id_factory: Callable[[], str] | None = None,
    ):
        self._send_message = send_message
        self._resolve_active = resolve_active
        self._clock = clock
        self._request_id_factory = (
            request_id_factory
            or (lambda: f"req_{secrets.token_hex(4)}")
        )
        self._lock = threading.RLock()
        self._requests: dict[str, ProtocolState] = {}
        # shutdown_response 尚未被 Lead 消费前，请求仍是 pending。
        # 本地记账可以防止同一 shutdown_request 重放两次。
        self._handled_requests: set[str] = set()
        self._delivered_responses: set[str] = set()

    def _new_request_id(self) -> str:
        for _ in range(100):
            request_id = self._request_id_factory()
            if (
                isinstance(request_id, str)
                and VALID_REQUEST_ID.fullmatch(request_id)
                and request_id not in self._requests
            ):
                return request_id
        raise RuntimeError("could not allocate a unique protocol request ID")

    def _create_state(
        self,
        protocol_type: str,
        sender: str,
        target: str,
        payload: str,
    ) -> ProtocolState:
        if protocol_type not in PROTOCOL_TYPES:
            raise ValueError(f"unsupported protocol type: {protocol_type}")
        with self._lock:
            state = ProtocolState(
                request_id=self._new_request_id(),
                type=protocol_type,
                sender=sender,
                target=target,
                status="pending",
                payload=payload,
                created_at=self._clock(),
            )
            self._requests[state.request_id] = state
            return state

    def _discard_pending(self, request_id: str) -> None:
        with self._lock:
            state = self._requests.get(request_id)
            if state is not None and state.status == "pending":
                self._requests.pop(request_id, None)

    @staticmethod
    def _request_id(message: Any) -> str:
        metadata = getattr(message, "metadata", {})
        request_id = (
            metadata.get("request_id")
            if isinstance(metadata, dict)
            else None
        )
        return request_id if isinstance(request_id, str) else ""

    @staticmethod
    def _approve(message: Any) -> bool | None:
        metadata = getattr(message, "metadata", {})
        approve = (
            metadata.get("approve") if isinstance(metadata, dict) else None
        )
        return approve if isinstance(approve, bool) else None

    def request_shutdown(self, teammate: str) -> str:
        target = self._resolve_active(teammate)
        if target is None:
            return f"Error: teammate {teammate!r} is not active"
        with self._lock:
            duplicate = next(
                (
                    state
                    for state in self._requests.values()
                    if state.type == "shutdown"
                    and state.target.casefold() == target.casefold()
                    and state.status == "pending"
                ),
                None,
            )
            if duplicate is not None:
                return (
                    f"Shutdown request already pending for {target} "
                    f"({duplicate.request_id})"
                )
            state = self._create_state("shutdown", "lead", target, "")
        try:
            self._send_message(
                "lead",
                target,
                "Please shut down gracefully.",
                "shutdown_request",
                {"request_id": state.request_id},
            )
        except (OSError, ValueError) as error:
            self._discard_pending(state.request_id)
            return f"Error: could not request shutdown: {error}"
        return f"Shutdown request sent to {target} ({state.request_id})"

    def request_plan(self, teammate: str, task: str) -> str:
        target = self._resolve_active(teammate)
        if target is None:
            return f"Error: teammate {teammate!r} is not active"
        if not isinstance(task, str) or not task.strip():
            return "Error: task must be a non-empty string"
        try:
            self._send_message(
                "lead",
                target,
                f"Please submit a plan for: {task.strip()}",
                "message",
                {},
            )
        except (OSError, ValueError) as error:
            return f"Error: could not request plan: {error}"
        return f"Asked {target} to submit a plan"

    def submit_plan(self, sender: str, plan: str) -> str:
        teammate = self._resolve_active(sender)
        if teammate is None:
            return f"Error: teammate sender {sender!r} is not active"
        if not isinstance(plan, str) or not plan.strip():
            return "Error: plan must be a non-empty string"
        state = self._create_state(
            "plan_approval",
            teammate,
            "lead",
            plan.strip(),
        )
        try:
            self._send_message(
                teammate,
                "lead",
                plan.strip(),
                "plan_approval_request",
                {"request_id": state.request_id},
            )
        except (OSError, ValueError) as error:
            self._discard_pending(state.request_id)
            return f"Error: could not submit plan: {error}"
        return f"Plan submitted ({state.request_id}). Waiting for approval."

    def review_plan(
        self,
        request_id: str,
        approve: bool,
        feedback: str = "",
    ) -> str:
        if not isinstance(approve, bool):
            return "Error: approve must be a boolean"
        with self._lock:
            state = self._requests.get(request_id)
            if state is None:
                return f"Error: request {request_id!r} not found"
            if state.type != "plan_approval":
                return f"Error: request {request_id} is not a plan approval"
            if state.status != "pending":
                return f"Error: request {request_id} already {state.status}"
            state.status = "approved" if approve else "rejected"
            sender = state.sender

        content = (
            feedback.strip()
            if isinstance(feedback, str) and feedback.strip()
            else ("Approved" if approve else "Rejected")
        )
        try:
            self._send_message(
                "lead",
                sender,
                content,
                "plan_approval_response",
                {"request_id": request_id, "approve": approve},
            )
        except (OSError, ValueError) as error:
            with self._lock:
                current = self._requests.get(request_id)
                if current is not None and current.status in {
                    "approved",
                    "rejected",
                }:
                    current.status = "pending"
            return f"Error: could not deliver plan review: {error}"
        result = "approved" if approve else "rejected"
        return f"Plan {result} ({request_id})"

    def match_response(self, message: Any) -> tuple[bool, str]:
        """通过 ID、类型和通信双方共同匹配响应，拒绝串单与重放。"""

        request_id = self._request_id(message)
        approve = self._approve(message)
        if not request_id or approve is None:
            return False, "missing request_id or boolean approve"
        with self._lock:
            state = self._requests.get(request_id)
            if state is None:
                return False, f"unknown request_id: {request_id}"
            expected = EXPECTED_RESPONSE[state.type]
            if getattr(message, "type", "") != expected:
                return False, f"type mismatch: expected {expected}"
            if (
                getattr(message, "from_agent", "").casefold()
                != state.target.casefold()
                or getattr(message, "to_agent", "").casefold()
                != state.sender.casefold()
            ):
                return False, "response sender or target does not match request"
            if state.status != "pending":
                return False, f"request already {state.status}"
            state.status = "approved" if approve else "rejected"
            return True, state.status

    def route_lead_message(self, message: Any) -> tuple[bool, str]:
        if getattr(message, "type", "") not in {
            "shutdown_response",
            "plan_approval_response",
        }:
            return False, "not a protocol response"
        return self.match_response(message)

    def dispatch_teammate_message(
        self,
        teammate: str,
        message: Any,
    ) -> ProtocolDispatch:
        msg_type = getattr(message, "type", "message")
        if msg_type == "shutdown_request":
            return self._handle_shutdown_request(teammate, message)
        if msg_type == "plan_approval_response":
            return self._handle_plan_response(teammate, message)
        return ProtocolDispatch(handled=False)

    def _handle_shutdown_request(
        self,
        teammate: str,
        message: Any,
    ) -> ProtocolDispatch:
        request_id = self._request_id(message)
        with self._lock:
            state = self._requests.get(request_id)
            valid = (
                state is not None
                and state.type == "shutdown"
                and state.status == "pending"
                and state.sender.casefold()
                == getattr(message, "from_agent", "").casefold()
                and state.target.casefold() == teammate.casefold()
                and state.target.casefold()
                == getattr(message, "to_agent", "").casefold()
                and request_id not in self._handled_requests
            )
            if not valid:
                return ProtocolDispatch(
                    handled=True,
                    event={
                        "role": "user",
                        "content": (
                            f"[Protocol ignored] Invalid shutdown request "
                            f"{request_id or '(missing ID)'}."
                        ),
                    },
                )
            self._handled_requests.add(request_id)

        try:
            self._send_message(
                teammate,
                "lead",
                "Shutting down gracefully.",
                "shutdown_response",
                {"request_id": request_id, "approve": True},
            )
        except (OSError, ValueError) as error:
            with self._lock:
                self._handled_requests.discard(request_id)
            return ProtocolDispatch(
                handled=True,
                event={
                    "role": "user",
                    "content": f"[Protocol error] Shutdown reply failed: {error}",
                },
            )
        return ProtocolDispatch(handled=True, stop=True)

    def _handle_plan_response(
        self,
        teammate: str,
        message: Any,
    ) -> ProtocolDispatch:
        request_id = self._request_id(message)
        approve = self._approve(message)
        with self._lock:
            state = self._requests.get(request_id)
            if approve is True:
                expected_status = "approved"
            elif approve is False:
                expected_status = "rejected"
            else:
                expected_status = ""
            valid = (
                state is not None
                and state.type == "plan_approval"
                and state.sender.casefold() == teammate.casefold()
                and state.target.casefold()
                == getattr(message, "from_agent", "").casefold()
                and teammate.casefold()
                == getattr(message, "to_agent", "").casefold()
                and state.status == expected_status
                and request_id not in self._delivered_responses
            )
            if not valid:
                return ProtocolDispatch(
                    handled=True,
                    event={
                        "role": "user",
                        "content": (
                            f"[Protocol ignored] Invalid plan response "
                            f"{request_id or '(missing ID)'}."
                        ),
                    },
                )
            self._delivered_responses.add(request_id)

        if approve:
            content = "[Plan approved] Proceed with the task."
        else:
            feedback = getattr(message, "content", "") or "No feedback."
            content = f"[Plan rejected] Feedback: {feedback}"
        return ProtocolDispatch(
            handled=True,
            event={"role": "user", "content": content},
        )

    def get(self, request_id: str) -> ProtocolState | None:
        with self._lock:
            state = self._requests.get(request_id)
            return replace(state) if state is not None else None

    def snapshot(self) -> tuple[ProtocolState, ...]:
        with self._lock:
            return tuple(
                replace(state)
                for _, state in sorted(self._requests.items())
            )

    def handlers(self) -> dict[str, Callable[..., str]]:
        return {
            "request_shutdown": self.request_shutdown,
            "request_plan": self.request_plan,
            "review_plan": self.review_plan,
        }
