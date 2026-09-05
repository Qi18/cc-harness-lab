"""s18 自治队友：团队通信、任务板接入和 WORK / IDLE 生命周期。"""

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
from .autonomous_agents import AutonomousWorker, IDLE_POLL_INTERVAL, IDLE_TIMEOUT
from .task_system import TaskManager
from .worktree_isolation import WorktreeManager, WorktreeContext
from .provider import assistant_payload, completion_request
from .team_protocols import SUBMIT_PLAN_TOOL, TeamProtocolManager

if TYPE_CHECKING:
    # ToolExecutor 只用于类型标注：运行时不导入，团队模块与 tool_use 才不成环。
    from .tool_use import ToolExecutor


# 队友是自治循环，必须自带停机条件：轮次上限把失控队友收敛成一条 error 消息。
MAX_TEAMMATE_ROUNDS = 10
# 单条消息上限，避免一名队友用一封长邮件挤爆 Lead 的上下文。
MAX_MESSAGE_CHARS = 100_000
# Agent 名会拼进邮箱文件名，限制字符集才能防止越权写到邮箱目录之外。
VALID_AGENT_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
VALID_MESSAGE_TYPE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# lead 是 Lead 自己的邮箱名，agent 留作通用别名，都不允许被队友占用。
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


# Lead 与队友唯一共享的工具就是发消息，因此 schema 提成模块级常量给两张表复用。
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

# 三个 schema 在本模块自建、由 tool_use.py 反向导入，并且只进 PARENT_TOOLS：
# 只有 Lead 能组队，SubAgent 与队友都不能再派生新的 Agent。
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

# s18 在 s16 的五种能力上增加看板查询、认领和完成，不开放派生 Agent。
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
    SUBMIT_PLAN_TOOL,
    _team_tool("list_tasks", "Inspect the shared task board.", {}),
    _team_tool(
        "claim_task", "Claim a ready task for yourself.",
        {"task_id": {"type": "string", "minLength": 1}}, ["task_id"],
    ),
    _team_tool(
        "complete_task", "Complete your own task after verification.",
        {"task_id": {"type": "string", "minLength": 1}}, ["task_id"],
    ),
]


@dataclass(frozen=True)
class TeamMessage:
    """磁盘 JSONL 中的一条结构化团队消息。"""

    from_agent: str
    to_agent: str
    content: str
    # type 区分普通 message、队友结束的 result 和异常终止的 error。
    type: str
    ts: float
    # 协议字段全放 metadata：request_id / approve 不污染给人看的 content。
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """磁盘字段用 from / to：from 是关键字，不能直接做 dataclass 字段名。"""

        return {
            "from": self.from_agent,
            "to": self.to_agent,
            "content": self.content,
            "type": self.type,
            "ts": self.ts,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TeamMessage":
        """邮箱文件与模型参数都不是可信输入，构造前逐字段校验。"""

        if not isinstance(payload, dict):
            raise ValueError("mailbox message must be an object")
        required = {"from", "to", "content", "type", "ts"}
        allowed = required | {"metadata"}
        # metadata 可选：s15 写下的旧邮箱仍能读，但未知字段依旧拒绝。
        if not required.issubset(payload) or set(payload) - allowed:
            raise ValueError("mailbox message fields do not match the schema")
        from_agent = payload["from"]
        to_agent = payload["to"]
        content = payload["content"]
        message_type = payload["type"]
        timestamp = payload["ts"]
        metadata = payload.get("metadata", {})
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
        # bool 是 int 的子类，True 会静默通过数值检查，因此先单独排除。
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            raise ValueError("invalid mailbox timestamp")
        if not isinstance(metadata, dict) or not all(
            # 非字符串键在 JSON 往返后会静默变成字符串，先直接拒接。
            isinstance(key, str) for key in metadata
        ):
            raise ValueError(
                "mailbox metadata must be an object with string keys"
            )
        try:
            # JSON 往返同时验证嵌套值可持久化，并切断调用方后续修改。
            metadata_json = json.dumps(metadata, ensure_ascii=False)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "mailbox metadata must be JSON serializable"
            ) from error
        if len(metadata_json) > MAX_MESSAGE_CHARS:
            # metadata 与正文共用同一上限：协议字段不能成为绕过限额的后门。
            raise ValueError("mailbox metadata is too large")
        metadata_copy = json.loads(metadata_json)
        return cls(
            from_agent=from_agent,
            to_agent=to_agent,
            content=content,
            type=message_type,
            ts=float(timestamp),
            metadata=metadata_copy,
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
        # 可注入时钟让测试能断言固定的 ts，而不必容忍真实时间的拖动。
        self.clock = clock
        # 同一进程内多名队友并发写同一个邮箱，追写与读后删除都必须互斥。
        self._lock = threading.RLock()

    def _path(self, agent: str) -> Path:
        """邮箱路径由 Agent 名拼成，因此先校字符集再校目录归属。"""

        if not isinstance(agent, str) or not VALID_AGENT_NAME.fullmatch(agent):
            raise ValueError(f"invalid mailbox agent: {agent!r}")
        path = (self.mailbox_dir / f"{agent}.jsonl").resolve()
        try:
            # 即使正则已经排除了 / 与 ..，仍然再确认一次解析后的真实位置。
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
        metadata: dict[str, Any] | None = None,
    ) -> TeamMessage:
        """追写一行 JSONL；先经 from_dict 校验，坏消息不落盘。"""

        message = TeamMessage.from_dict(
            {
                "from": from_agent,
                "to": to_agent,
                "content": content,
                "type": msg_type,
                "ts": self.clock(),
                # 普通消息与协议消息共用一种落盘格式，缺省就是空字典。
                "metadata": (
                    {} if metadata is None else metadata
                ),
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
                # 队友随时可能被主线程判定超时，fsync 保证已发出的消息不丢。
                os.fsync(stream.fileno())
            # 邮箱内容是 Agent 间通信，收敛到仅所有者可读写。
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
            # 列表推导先整体求值：任何一行解析失败都会在 unlink 之前抛出。
            messages = [
                TeamMessage.from_dict(json.loads(line))
                for line in lines
                if line.strip()
            ]
            path.unlink()
            return messages

    def peek(self, agent: str) -> bool:
        """只判断是否有待读消息，不消费邮箱。"""

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
        idle_poll_interval: float = IDLE_POLL_INTERVAL,
        idle_timeout: float = IDLE_TIMEOUT,
        tasks: TaskManager | None = None,
        worktrees: WorktreeManager | None = None,
        protocols: TeamProtocolManager | None = None,
    ):
        self.client = client
        self.settings = settings
        self.executor = executor
        # 只从子 Agent handler 里挑这三个：队友拿不到 task / cron / todo，
        # 能力隔离因此不依赖模型自觉遵守 schema。
        self.file_handlers = {
            name: file_handlers[name]
            for name in ("bash", "read_file", "write_file")
            if name in file_handlers
        }
        self.bus = bus or MessageBus(mailbox_dir)
        self.max_rounds = max_rounds
        # 注入 Lead 的同一张任务板；独立构造管理器时使用相同持久目录。
        self.tasks = tasks or TaskManager(settings.tasks_dir)
        self.autonomy = AutonomousWorker(self.tasks, idle_timeout=idle_timeout)
        self.worktrees = worktrees or WorktreeManager(settings, self.tasks)
        # idle 队友的轮询间隔；下限 0.01 避免传 0 变成忙等。
        self.idle_poll_interval = max(0.01, idle_poll_interval)
        # 队友线程与主线程都会读写 _records，并且 spawn 内部还嵌套调 _active_name。
        self._lock = threading.RLock()
        # 主线程只看 record 快照，队友的 messages 历史始终留在各自线程栈上。
        self._records: dict[str, TeammateRecord] = {}
        self._threads: dict[str, threading.Thread] = {}
        # 每名队友一个 Event：发信方置位，idle 线程立即醒来而不必等满轮询。
        self._wake_events: dict[str, threading.Event] = {}
        # 协议层只拿到两个回调（发信、查在岗），因此不知道邮箱与线程的存在。
        self.protocols = (
            protocols
            or TeamProtocolManager(self._send_bus, self._active_name)
        )

    @staticmethod
    def _validate_spawn(name: object, role: object, prompt: object) -> str | None:
        """三个参数全部来自模型，建线程前先把错误变成可读回复。"""

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
        """不区分大小写匹配在岗队友，并返回登记在册的原名。"""

        # 名字可能直接来自模型参数，非字符串时当作不在岗。
        if not isinstance(requested, str):
            return None
        with self._lock:
            for name, record in self._records.items():
                if (
                    # 模型常把名字大小写写错；casefold 比较后统一回落到真实邮箱名。
                    name.casefold() == requested.casefold()
                    # idle 队友仍然在岗：能收信、能发信、也能被关机。
                    and record.status in {"working", "idle", "stopping"}
                ):
                    return name
        return None

    def _send_bus(
        self,
        from_agent: str,
        to_agent: str,
        content: str,
        msg_type: str = "message",
        metadata: dict[str, Any] | None = None,
    ) -> TeamMessage:
        """统一落盘并唤醒收件队友，协议消息和普通消息走同一路径。"""

        message = self.bus.send(
            from_agent,
            to_agent,
            content,
            msg_type,
            metadata,
        )
        if to_agent.casefold() != "lead":
            # Lead 没有待命线程，它的信由父循环在下一个收信点自取。
            with self._lock:
                wake_event = self._wake_events.get(to_agent)
            if wake_event is not None:
                wake_event.set()
        return message

    def _wait_for_mail(self, name: str, timeout: float | None = None) -> None:
        """idle 时用 Event 低延迟唤醒，同时保留文件轮询兜底。"""

        delay = self.idle_poll_interval if timeout is None else timeout
        with self._lock:
            wake_event = self._wake_events.get(name)
        if wake_event is None:
            # 队友已被移除登记时退回纯睡眠，不抛异常打断收尾。
            time.sleep(delay)
            return
        # 超时后仍会回到 peek 复查：即使错过一次置位也不会永久睡死。
        wake_event.wait(delay)
        wake_event.clear()

    def spawn_teammate(self, name: str, role: str, prompt: str) -> str:
        """启动一名拥有独立历史与最小工具集的常驻队友。"""

        error = self._validate_spawn(name, role, prompt)
        if error:
            return f"Error: {error}"
        role = role.strip()
        prompt = prompt.strip()
        with self._lock:
            # working / idle / stopping 都仍占用邮箱，不能启动同名线程。
            if self._active_name(name) is not None:
                return f"Error: teammate {name!r} is already active"
            record = TeammateRecord(name=name, role=role, status="working")
            self._records[name] = record
            # 先建好唤醒事件再起线程：队友第一次进 idle 时它已经存在。
            self._wake_events[name] = threading.Event()
            thread = threading.Thread(
                target=self._run_teammate,
                args=(name, role, prompt),
                # 给线程起可读名字，堆栈与日志里能直接定位到哪名队友。
                name=f"cc-teammate-{name}",
                # daemon 线程：主程序退出不被未完成的队友拖住。
                daemon=True,
            )
            self._threads[name] = thread
            try:
                thread.start()
            except RuntimeError as start_error:
                # start 失败必须回滚登记，否则这个名字会永久占着 working 位。
                self._threads.pop(name, None)
                self._records.pop(name, None)
                self._wake_events.pop(name, None)
                return f"Error: could not start teammate {name!r}: {start_error}"
        print(f"[team] spawned {name} as {role}")
        return f"Teammate {name!r} spawned as {role}"

    def _teammate_system_prompt(self, name: str, role: str) -> str:
        """队友不走 SystemPromptAssembler：它不需要 Skill / Memory 目录。"""

        return (
            f"You are {name!r}, a persistent teammate acting as {role}. "
            f"Work inside {self.settings.workdir}. Use tools to complete the "
            "assigned task. Use send_message for useful intermediate findings. "
            "Use submit_plan when Lead should review a risky plan. After a final "
            "summary, remain idle for new messages until shutdown is approved. "
            "Do not create agents, tasks, or schedules."
        )

    @staticmethod
    def inbox_event(messages: list[TeamMessage]) -> dict[str, Any]:
        """把一批消息包成单条 user 事件，不冒充工具返回值。"""

        payload = [message.to_dict() for message in messages]
        return {
            "role": "user",
            "content": (
                # 显式声明“这是数据”，降低队友消息里的文字被当指令执行的风险。
                "<team_inbox>\n"
                "The following JSON is teammate communication data.\n"
                f"{json.dumps(payload, ensure_ascii=False)}\n"
                "</team_inbox>"
            ),
        }

    def _teammate_handlers(
        self,
        sender: str,
        context: WorktreeContext | None = None,
    ) -> dict[str, Callable[..., str]]:
        """用闭包把 sender 固定成队友自己，模型无法传参改写发信人。"""

        def claim(task_id):
            result = self.tasks.claim_task(task_id, sender)
            if context is not None and result.startswith("Claimed "):
                context.select(self.tasks._load(task_id))
                result += f"\nWorking directory: {context.path}"
            return result

        # 闭包每次读取 context，自动或手动 claim 都能切换下一次工具的 cwd。
        files = self.file_handlers if context is None else {
            name: (lambda name: lambda **kwargs: context.file_handlers[name](**kwargs))(name)
            for name in self.file_handlers
        }
        return {
            **files,
            "send_message": (
                lambda to, content: self.send_from(sender, to, content)
            ),
            # 计划的提交者同样由闭包钉住，队友无法替别人递计划。
            "submit_plan": (
                lambda plan: self.protocols.submit_plan(sender, plan)
            ),
            "list_tasks": self.tasks.list_tasks,
            "claim_task": claim,
            "complete_task": lambda task_id: self.tasks.complete_task(task_id, sender),
        }

    def _set_record(
        self,
        name: str,
        status: str,
        summary: str | None = None,
    ) -> None:
        """队友线程写运行态，主线程读快照，因此赋值也要进锁。"""

        with self._lock:
            record = self._records.get(name)
            if record is not None:
                record.status = status
                if summary is not None:
                    record.summary = summary

    def _process_teammate_inbox(
        self,
        name: str,
        messages: list[dict[str, Any]],
    ) -> bool:
        """先路由协议消息，再把普通消息作为一批 user 事件注入。"""

        inbox = self.bus.read_inbox(name)
        ordinary: list[TeamMessage] = []
        for message in inbox:
            dispatch = self.protocols.dispatch_teammate_message(name, message)
            if dispatch.event is not None:
                # 审批结论与被忽略的协议都要写进历史，模型才知道发生了什么。
                messages.append(dispatch.event)
            if dispatch.stop:
                # 先落 stopping：此刻队友仍在岗，收尾期间的发信不会被拒。
                self._set_record(name, "stopping")
                return True
            if not dispatch.handled:
                # 协议信已被消费，只有普通消息才需要作为 inbox 事件注入。
                ordinary.append(message)
        if ordinary:
            # 一批消息合成一条 user 事件，避免一次唤醒插入多段历史。
            messages.append(self.inbox_event(ordinary))
        return False

    def _run_teammate(self, name: str, role: str, prompt: str) -> None:
        """队友完成一轮工作后进入 idle，直到收到新消息或关机请求。"""

        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    self._teammate_system_prompt(name, role)
                    + " When idle, the harness discovers and claims ready board tasks. "
                    "Use list_tasks to inspect work, claim_task only for yourself, "
                    "and complete_task only after verifying your owned task. "
                    "Do not mark work complete merely because you gave a summary."
                ),
            },
            {"role": "user", "content": prompt},
        ]
        context = WorktreeContext(
            self.worktrees, name, self.settings, self.executor, self.file_handlers
        )
        handlers = self._teammate_handlers(name, context)
        # summary 跨周期保留：再次被唤醒时仍能报告上一轮的结论。
        summary = ""
        try:
            shutdown_requested = False
            # 外层循环让队友常驻：只有关机请求获批才会跳出。
            while not shutdown_requested:
                if self._process_teammate_inbox(name, messages):
                    break
                self.autonomy.restore_identity(messages, name, role)
                self._set_record(name, "working", summary)
                # 标记这一周期是否以正常总结收尾，用来区分超轮次。
                completed_cycle = False

                # 上限只约束一次活跃工作周期；进入 idle 后可被新消息再次唤醒。
                for _ in range(self.max_rounds):
                    if self._process_teammate_inbox(name, messages):
                        shutdown_requested = True
                        break
                    # 手动 claim 也会在同一周期换目录，因此每次请求都刷新提示。
                    messages[0]["content"] = (
                        self._teammate_system_prompt(name, role)
                        + " When idle, the harness discovers and claims ready board tasks. "
                        "Use list_tasks to inspect work and claim_task only for yourself. "
                        "Use complete_task only after verifying your owned task, not merely summarizing. "
                        f"Current tool working directory: {context.path}. "
                        "Relative file paths and bash run there."
                    )
                    response = self.client.chat.completions.create(
                        **completion_request(
                            self.settings,
                            # 队友没有 Compact 子系统，用固定滑窗兜住上下文长度。
                            # 固定保留身份 system；滑窗不能把队友身份挤掉。
                            [messages[0], *messages[1:][-19:]],
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
                        self._send_bus(name, "lead", summary, "result")
                        # 延续 s16 的 idle 生命周期；s18 同时等待消息和扫描新任务。
                        self._set_record(name, "idle", summary)
                        completed_cycle = True
                        print(f"[team] {name} idle")
                        break

                    for tool_call in assistant.tool_calls:
                        result = context.executor.execute(
                            tool_call.function.name,
                            tool_call.function.arguments,
                            handlers,
                            # 队友仍复用统一 ToolExecutor，但 handler 能力保持隔离。
                            display_prefix=f"[{name}] ",
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": result,
                            }
                        )

                if shutdown_requested:
                    break
                if not completed_cycle:
                    summary = (
                        f"Teammate exceeded {self.max_rounds} "
                        "model rounds in one work cycle"
                    )
                    self._send_bus(name, "lead", summary, "error")
                    # 超轮次只结束这一周期而不判死：Lead 仍能改派任务或请求关机。
                    self._set_record(name, "idle", summary)

                # 不调用模型地等待，收到普通消息、审批响应或关机请求才继续。
                idle_result = self.autonomy.idle_poll(
                    name, messages,
                    has_mail=lambda: self.bus.peek(name),
                    wait=lambda delay: self._wait_for_mail(name, delay),
                    poll_interval=self.idle_poll_interval,
                    on_claim=context.select,
                )
                if idle_result == "timeout":
                    # 自动超时不是关闭协议的 approved，单独发出退出原因。
                    self._send_bus(
                        name, "lead",
                        f"Idle timeout; teammate exiting. Last summary: {summary}",
                        "result",
                    )
                    break

            # 关闭握手或 idle 超时都结束线程；未完成任务不会被自动标记完成。
            self._set_record(name, "done", summary)
            print(f"[team] {name} stopped")
        except Exception as error:
            # 队友在子线程里抛异常不会惊动 Lead，因此必须自己报一封 error。
            summary = f"{type(error).__name__}: {error}"
            try:
                self._send_bus(name, "lead", summary, "error")
            except Exception:
                # 连发信也失败时宁可失去通知，也要把终态落定为 failed。
                pass
            self._set_record(name, "failed", summary)
            print(f"[team] {name} failed: {summary}")
        finally:
            context.close()

    def send_from(self, sender: str, to: str, content: str) -> str:
        """全部发信的单一入口：先验身份，再验收件人。"""

        if (
            not isinstance(sender, str)
            or not VALID_AGENT_NAME.fullmatch(sender)
        ):
            return "Error: invalid message sender"
        if sender.casefold() == "lead":
            sender = "lead"
        else:
            # working、idle、stopping 都仍是在岗身份。
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
            # 自寄会形成“自己给自己发信→下轮又读到”的死循环。
            return "Error: an agent cannot send a message to itself"

        if to.casefold() == "lead":
            recipient = "lead"
        else:
            # 收件人同样只能是在岗队友：否则邮件会堆在永无人读的邮箱里。
            recipient = self._active_name(to)
            if recipient is None:
                return f"Error: teammate {to!r} is not active"
        try:
            self._send_bus(sender, recipient, content.strip())
        except (OSError, ValueError) as error:
            # 磁盘或校验失败返回可读文本，不让一次发信弄崩整个循环。
            return f"Error: could not send message: {error}"
        return f"Sent message from {sender} to {recipient}"

    def run_send_message(self, to: str, content: str) -> str:
        """Lead 侧 handler：sender 写死为 lead，模型无法冒名队友。"""

        return self.send_from("lead", to, content)

    def consume_lead_messages(self) -> list[TeamMessage]:
        """消费并路由 Lead 邮箱；主循环与 check_inbox 共用这个出口。"""

        messages = self.bus.read_inbox("lead")
        for message in messages:
            matched, reason = self.protocols.route_lead_message(message)
            # 只有响应类消息值得审计：没配上就是串单或重放，必须留一行可查。
            if message.type.endswith("_response") and not matched:
                print(
                    f"[protocol] ignored {message.type}: {reason}"
                )
        return messages

    def run_check_inbox(self) -> str:
        """主动查信工具：同一批消息不会再被主循环注入一次。"""

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
        """只挂到父 handler 表；队友的工具表另由 _teammate_handlers 给出。"""

        return {
            "spawn_teammate": self.spawn_teammate,
            "send_message": self.run_send_message,
            "check_inbox": self.run_check_inbox,
            # 协议三件工具由协议层给出：agent_loop 不必知道它们的存在。
            **self.protocols.handlers(),
        }

    def snapshot(self) -> tuple[TeammateRecord, ...]:
        """返回 record 拷贝，避免调用方改到队友线程正在写的对象。"""

        with self._lock:
            return tuple(
                # 名字排序使快照顺序稳定，Prompt 与测试都能直接断言。
                replace(record)
                for _, record in sorted(self._records.items())
            )

    def wait(self, name: str, timeout: float | None = None) -> bool:
        """供测试与关停使用；未知名字视为已结束。"""

        with self._lock:
            thread = self._threads.get(name)
        if thread is None:
            return True
        # join 在锁外调用：队友线程收尾时还需要拿同一把锁写终态。
        thread.join(timeout)
        return not thread.is_alive()

    @property
    def active_count(self) -> int:
        """供主循环判断是否还有队友在跑。"""

        with self._lock:
            return sum(
                # idle 队友也算在岗：主循环不能因为它暂时不动就当团队已散。
                record.status in {"working", "idle", "stopping"}
                for record in self._records.values()
            )
