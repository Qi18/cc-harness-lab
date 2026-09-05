"""s18 Team Protocols：团队请求状态、消息路由与响应关联。"""

from __future__ import annotations

import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any


# 请求 ID 会随消息写入邮箱文件，因此和 Agent 名字一样按白名单字符校验。
VALID_REQUEST_ID = re.compile(r"^req_[A-Za-z0-9_-]{1,64}$")
PROTOCOL_TYPES = {"shutdown", "plan_approval"}
# 每种请求只接受一种响应类型：类型对不上就不是这条请求的回复。
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


# 三件协议工具只属于 Lead：队友既不能命令别人关机，也不能自己批自己的计划。
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

# 队友侧唯一的协议工具，由 agent_teams.py 的 TEAMMATE_TOOLS 引入。
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
    # sender / target 记录“谁问谁”，响应必须原样反向才算配对成功。
    sender: str
    target: str
    # pending → approved / rejected，只允许推进一次。
    status: str
    payload: str
    created_at: float


@dataclass(frozen=True)
class ProtocolDispatch:
    """队友处理一封信后的控制信号。"""

    # handled 表示协议层已消费这封信，不再作为普通消息注入历史。
    handled: bool
    # stop 只由通过校验的 shutdown_request 置位，队友据此退出主循环。
    stop: bool = False
    # event 是可选的一条 user 消息：审批结果和被忽略的协议都要让模型看见。
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
        # 发信与在岗查询都靠注入：协议层因此不导入 agent_teams，也就不成环。
        self._send_message = send_message
        self._resolve_active = resolve_active
        self._clock = clock
        # 默认随机 ID；测试注入固定序列后才能直接断言 request_id。
        self._request_id_factory = (
            request_id_factory
            or (lambda: f"req_{secrets.token_hex(4)}")
        )
        # Lead 主线程与多个队友线程共用同一张请求表。
        self._lock = threading.RLock()
        self._requests: dict[str, ProtocolState] = {}
        # shutdown_response 尚未被 Lead 消费前，请求仍是 pending。
        # 本地记账可以防止同一 shutdown_request 重放两次。
        self._handled_requests: set[str] = set()
        self._delivered_responses: set[str] = set()

    def _new_request_id(self) -> str:
        """ID 由外部工厂给出，因此格式与唯一性都要在这里兜住。"""

        for _ in range(100):
            request_id = self._request_id_factory()
            if (
                isinstance(request_id, str)
                and VALID_REQUEST_ID.fullmatch(request_id)
                and request_id not in self._requests
            ):
                return request_id
        # 工厂反复返回重复或非法 ID 时宁可报错，也不让两条请求共用一个 ID。
        raise RuntimeError("could not allocate a unique protocol request ID")

    def _create_state(
        self,
        protocol_type: str,
        sender: str,
        target: str,
        payload: str,
    ) -> ProtocolState:
        """登记一条 pending 请求；分配 ID 与写入请求表必须在同一把锁内。"""

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
        """发信失败时回收请求；只删仍 pending 的，不抹掉已推进的状态。"""

        with self._lock:
            state = self._requests.get(request_id)
            if state is not None and state.status == "pending":
                self._requests.pop(request_id, None)

    @staticmethod
    def _request_id(message: Any) -> str:
        """metadata 来自邮箱文件，不是可信输入：类型不对就当没有。"""

        metadata = getattr(message, "metadata", {})
        request_id = (
            metadata.get("request_id")
            if isinstance(metadata, dict)
            else None
        )
        return request_id if isinstance(request_id, str) else ""

    @staticmethod
    def _approve(message: Any) -> bool | None:
        """只接受真正的 bool：字符串“false”之类一律视为缺失。"""

        metadata = getattr(message, "metadata", {})
        approve = (
            metadata.get("approve") if isinstance(metadata, dict) else None
        )
        return approve if isinstance(approve, bool) else None

    def request_shutdown(self, teammate: str) -> str:
        """优雅关机是请求-响应：只发信不杀线程，停不停由队友自己决定。"""

        target = self._resolve_active(teammate)
        if target is None:
            return f"Error: teammate {teammate!r} is not active"
        with self._lock:
            # 同一名队友的 pending 关机只能有一条：否则两条请求互相抢同一封响应。
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
            # 信没发出去就不能留下 pending 记录，否则这名队友永远无法再被关机。
            self._discard_pending(state.request_id)
            return f"Error: could not request shutdown: {error}"
        return f"Shutdown request sent to {target} ({state.request_id})"

    def request_plan(self, teammate: str, task: str) -> str:
        """讨计划只是一句普通消息：真正的请求状态由队友 submit_plan 时建立。"""

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
        """队友侧入口：sender 由闭包传入并经在岗校验，模型改不了身份。"""

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
            # 计划没递到 Lead 手上，就不应留下一条永远等不到审批的 pending。
            self._discard_pending(state.request_id)
            return f"Error: could not submit plan: {error}"
        return f"Plan submitted ({state.request_id}). Waiting for approval."

    def review_plan(
        self,
        request_id: str,
        approve: bool,
        feedback: str = "",
    ) -> str:
        """Lead 侧审批：先在锁内把状态推进一步，再把结论发回队友。"""

        if not isinstance(approve, bool):
            return "Error: approve must be a boolean"
        with self._lock:
            state = self._requests.get(request_id)
            if state is None:
                return f"Error: request {request_id!r} not found"
            if state.type != "plan_approval":
                return f"Error: request {request_id} is not a plan approval"
            if state.status != "pending":
                # 一条计划只能被评审一次，重复审批直接报错而不发第二封信。
                return f"Error: request {request_id} already {state.status}"
            state.status = "approved" if approve else "rejected"
            sender = state.sender

        content = (
            # 没写 feedback 时自动补一句结论，队友不会收到空正文的审批信。
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
                # 发信失败就回退到 pending：队友没看到结果，状态就不能当已审批。
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
                # 响应必须是“当初的被问方→提问方”，否则就是别人在串单。
                getattr(message, "from_agent", "").casefold()
                != state.target.casefold()
                or getattr(message, "to_agent", "").casefold()
                != state.sender.casefold()
            ):
                return False, "response sender or target does not match request"
            if state.status != "pending":
                # 已结案的请求不再接受第二封响应，重放因此无法改写结果。
                return False, f"request already {state.status}"
            state.status = "approved" if approve else "rejected"
            return True, state.status

    def route_lead_message(self, message: Any) -> tuple[bool, str]:
        """Lead 每次收信都先过这里：只有响应类消息才去关联请求。"""

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
        """队友侧路由：协议信交给专用处理，其余仍当普通消息。"""

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
        """收到关机请求：先回一封响应，再告知调用方可以停了。"""

        request_id = self._request_id(message)
        with self._lock:
            state = self._requests.get(request_id)
            valid = (
                # 下面几项必须同时成立：请求存在且仍 pending、发信方是 Lead、
                # 收信方就是正在处理的这名队友，且这个 ID 还没被处理过。
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
                # 不合法就不停，但也不默默丢弃：给模型一条可读的忽略说明。
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
            # 先记账再发信：同一封 shutdown_request 重放时不会回第二封响应。
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
                # 响应没发出去就撤销记账，否则 Lead 重发关机会被当成重放。
                self._handled_requests.discard(request_id)
            return ProtocolDispatch(
                handled=True,
                event={
                    "role": "user",
                    "content": f"[Protocol error] Shutdown reply failed: {error}",
                },
            )
        # 只有回信成功才置 stop：关机对 Lead 而言才是可观测的。
        return ProtocolDispatch(handled=True, stop=True)

    def _handle_plan_response(
        self,
        teammate: str,
        message: Any,
    ) -> ProtocolDispatch:
        """收到审批结果：校验通过后转成一条明确的 user 提示。"""

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
                # metadata 里的 approve 必须与 Lead 落定的状态一致：
                # 否则伪造一封 approve=true 就能把被驳回的计划变成通过。
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
                # 同样给一条忽略说明：队友不会因为一封坏信就盲等审批。
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
            # 一条审批只注入一次，不让重复投递把同一结论说两遍。
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
        """返回状态拷贝，避免调用方改到协议层正在维护的对象。"""

        with self._lock:
            state = self._requests.get(request_id)
            return replace(state) if state is not None else None

    def snapshot(self) -> tuple[ProtocolState, ...]:
        """按 request_id 排序返回全部请求，供测试与人工检查。"""

        with self._lock:
            return tuple(
                replace(state)
                for _, state in sorted(self._requests.items())
            )

    def handlers(self) -> dict[str, Callable[..., str]]:
        """只给 Lead 三个 handler；submit_plan 由队友工具表单独提供。"""

        return {
            "request_shutdown": self.request_shutdown,
            "request_plan": self.request_plan,
            "review_plan": self.review_plan,
        }
