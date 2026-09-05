"""s12 持久任务图、依赖检查和任务工具契约。"""

from __future__ import annotations

import json
import fcntl
import threading
from functools import wraps
import os
import re
import secrets
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


PENDING = "pending"
IN_PROGRESS = "in_progress"
COMPLETED = "completed"
VALID_STATUSES = {PENDING, IN_PROGRESS, COMPLETED}
# 任务 ID 会直接拼成文件名，因此限定前缀、字符集和长度，杜绝路径穿越。
SAFE_TASK_ID = re.compile(r"^task_[A-Za-z0-9][A-Za-z0-9_.-]{0,121}$")


def _task_tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    """构造 s12 工具 schema，并保持与 s02 function calling 契约一致。"""

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


# schema 在本模块自建并由 tool_use.py 反向导入，避免两个模块相互依赖。
TASK_TOOLS = [
    _task_tool(
        "create_task",
        (
            "Create one persistent project task. Use blockedBy for prerequisite "
            "task IDs; use todo_write instead for short execution steps."
        ),
        {
            "subject": {"type": "string", "minLength": 1},
            "description": {"type": "string"},
            "blockedBy": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "uniqueItems": True,
            },
        },
        ["subject"],
    ),
    _task_tool(
        "list_tasks",
        "List the persistent task board with ready, blocked, active, and done states.",
        {},
    ),
    _task_tool(
        "get_task",
        "Read the complete JSON record for one persistent task.",
        {"task_id": {"type": "string", "minLength": 1}},
        ["task_id"],
    ),
    _task_tool(
        "claim_task",
        (
            "Claim one pending task for an owner. The claim fails until every "
            "blockedBy dependency is completed."
        ),
        {
            "task_id": {"type": "string", "minLength": 1},
            "owner": {"type": "string", "minLength": 1},
        },
        ["task_id"],
    ),
    _task_tool(
        "complete_task",
        "Complete one in-progress task and report downstream tasks it unlocks.",
        {"task_id": {"type": "string", "minLength": 1}},
        ["task_id"],
    ),
]


class TaskError(RuntimeError):
    """任务记录、状态或依赖不满足约束。"""


class TaskNotFound(TaskError):
    """请求的任务文件不存在。"""


@dataclass(frozen=True)
class TaskRecord:
    """磁盘中的稳定任务结构；blockedBy 在 Python 内部使用蛇形字段。"""

    id: str
    subject: str
    description: str
    status: str
    owner: str | None
    blocked_by: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """回写磁盘时恢复教程公开的 blockedBy 字段名。"""

        return {
            "id": self.id,
            "subject": self.subject,
            "description": self.description,
            "status": self.status,
            "owner": self.owner,
            "blockedBy": list(self.blocked_by),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TaskRecord":
        """磁盘不是可信输入：每次读取都重新校验全部字段。"""

        task_id = payload.get("id")
        subject = payload.get("subject")
        description = payload.get("description")
        status = payload.get("status")
        owner = payload.get("owner")
        blocked_by = payload.get("blockedBy")

        if not isinstance(task_id, str) or not SAFE_TASK_ID.fullmatch(task_id):
            raise TaskError("task record has an invalid id")
        if not isinstance(subject, str) or not subject.strip():
            raise TaskError(f"task {task_id} has an invalid subject")
        if not isinstance(description, str):
            raise TaskError(f"task {task_id} has an invalid description")
        if status not in VALID_STATUSES:
            raise TaskError(f"task {task_id} has an invalid status")
        # owner 只能是 null 或非空字符串：空串等于没有归属，却能骗过状态检查。
        if owner is not None and (not isinstance(owner, str) or not owner.strip()):
            raise TaskError(f"task {task_id} has an invalid owner")
        if not isinstance(blocked_by, list) or not all(
            isinstance(item, str) and SAFE_TASK_ID.fullmatch(item)
            for item in blocked_by
        ):
            raise TaskError(f"task {task_id} has invalid blockedBy entries")
        # 重复依赖不会改变语义，但会让 blocker 列表出现重复项。
        if len(blocked_by) != len(set(blocked_by)):
            raise TaskError(f"task {task_id} has duplicate blockedBy entries")

        return cls(
            id=task_id,
            subject=subject.strip(),
            description=description,
            status=status,
            owner=owner.strip() if owner else None,
            blocked_by=tuple(blocked_by),
        )


def board_locked(method):
    """共享看板的读改写事务：线程锁配合 flock，避免跨管理器重复认领。"""

    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._board_lock:
            # 锁文件独立于原子替换的任务 JSON，不能锁一个将被 replace 的 inode。
            with (self.tasks_dir / ".board.lock").open("a") as handle:
                os.fchmod(handle.fileno(), 0o600)
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    return method(self, *args, **kwargs)
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return guarded


class TaskManager:
    """管理 .tasks/{id}.json，并执行有界的任务状态转换。"""

    def __init__(
        self,
        tasks_dir: Path,
        *,
        id_factory: Callable[[], str] | None = None,
    ):
        """建立任务目录；id_factory 可注入，让测试拿到确定的 ID。"""

        self.tasks_dir = tasks_dir.expanduser().resolve()
        # mode 700：任务正文可能包含项目细节，只对当前用户可见。
        self.tasks_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._id_factory = id_factory or self._default_id
        self._board_lock = threading.RLock()

    @staticmethod
    def _default_id() -> str:
        # 时间提供可读顺序，随机后缀避免同一时刻创建任务发生碰撞。
        return f"task_{time.time_ns()}_{secrets.token_hex(4)}"

    @staticmethod
    def _validate_id(task_id: str) -> str:
        """任何进入路径拼接的 ID 都必须先过白名单正则。"""

        if not isinstance(task_id, str) or not SAFE_TASK_ID.fullmatch(task_id):
            raise TaskError("task_id must be a filesystem-safe identifier")
        return task_id

    def _path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{self._validate_id(task_id)}.json"

    def _new_id(self) -> str:
        # 生产 ID 几乎不会碰撞；循环上限防止测试或错误 factory 无限重试。
        for _ in range(10):
            task_id = self._validate_id(self._id_factory())
            if not self._path(task_id).exists():
                return task_id
        raise TaskError("could not allocate a unique task id")

    def _load(self, task_id: str) -> TaskRecord:
        """读取单个任务，并确认文件名与记录里的 id 指向同一个任务。"""

        path = self._path(task_id)
        if not path.is_file():
            raise TaskNotFound(f"task {task_id!r} does not exist")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TaskError(f"cannot read task {task_id!r}: {exc}") from exc
        if not isinstance(payload, dict):
            raise TaskError(f"task {task_id!r} must contain a JSON object")
        task = TaskRecord.from_dict(payload)
        # 复制或手改文件后，get_task("A") 不应该返回任务 B。
        if task.id != task_id:
            raise TaskError(
                f"task file {task_id!r} contains mismatched id {task.id!r}"
            )
        return task

    def _save(self, task: TaskRecord) -> None:
        """先写同目录临时文件再原子替换，避免中途崩溃留下半个 JSON。"""

        self.tasks_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{task.id}.",
            suffix=".tmp",
            dir=self.tasks_dir,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    task.to_dict(),
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            # 临时文件与目标同目录，os.replace 才是同一文件系统上的原子替换。
            os.replace(temporary, self._path(task.id))
            self._path(task.id).chmod(0o600)
        finally:
            # 替换成功后已无文件可删；中途失败则不留下 .tmp 残留。
            temporary.unlink(missing_ok=True)

    def _all(self) -> list[TaskRecord]:
        """按文件名顺序读整张任务板；单个坏文件会让本次调用报错。"""

        records = []
        for path in sorted(self.tasks_dir.glob("task_*.json")):
            records.append(self._load(path.stem))
        return records

    def blocking_dependencies(self, task: TaskRecord) -> list[str]:
        """缺失依赖与未完成依赖都算 blocker，不能因坏引用直接开工。"""

        blocked = []
        for dependency_id in task.blocked_by:
            try:
                dependency = self._load(dependency_id)
            except TaskNotFound:
                blocked.append(dependency_id)
                continue
            if dependency.status != COMPLETED:
                blocked.append(dependency_id)
        return blocked

    def can_start(self, task_id: str) -> bool:
        """看板显示的 ready 与 claim 的前置检查共用同一判定。"""

        return not self.blocking_dependencies(self._load(task_id))

    @staticmethod
    def _normalize_dependencies(blocked_by: Any) -> tuple[str, ...]:
        """创建时清理依赖输入：校验 ID 并按首次出现顺序去重。"""

        if blocked_by is None:
            return ()
        if not isinstance(blocked_by, list):
            raise TaskError("blockedBy must be an array of task IDs")
        normalized = []
        for dependency_id in blocked_by:
            if (
                not isinstance(dependency_id, str)
                or not SAFE_TASK_ID.fullmatch(dependency_id)
            ):
                raise TaskError("blockedBy contains an invalid task ID")
            if dependency_id not in normalized:
                normalized.append(dependency_id)
        return tuple(normalized)

    @board_locked
    def create_task(
        self,
        subject: str,
        description: str = "",
        blockedBy: list[str] | None = None,
    ) -> str:
        """创建 pending 任务；失败以 Error: 文本回给模型而不抛出。"""

        try:
            if not isinstance(subject, str) or not subject.strip():
                raise TaskError("subject must be a non-empty string")
            if not isinstance(description, str):
                raise TaskError("description must be a string")
            dependencies = self._normalize_dependencies(blockedBy)
            task_id = self._new_id()
            # 本章唯一的环检测：公开工具无法修改已有依赖，只需挡住自环。
            if task_id in dependencies:
                raise TaskError("a task cannot block itself")
            task = TaskRecord(
                id=task_id,
                subject=subject.strip(),
                description=description,
                status=PENDING,
                owner=None,
                blocked_by=dependencies,
            )
            self._save(task)
            return (
                f"Created {task.id} ({task.subject})\n"
                + json.dumps(task.to_dict(), ensure_ascii=False, indent=2)
            )
        except (OSError, TaskError) as exc:
            return f"Error: {exc}"

    def list_tasks(self) -> str:
        """渲染任务看板；pending 的 ready / blocked 由依赖实时判定。"""

        try:
            records = self._all()
            if not records:
                return "(no tasks)"
            lines = []
            for task in records:
                if task.status == PENDING:
                    blockers = self.blocking_dependencies(task)
                    state = (
                        f"blocked by {', '.join(blockers)}"
                        if blockers
                        else "ready"
                    )
                elif task.status == IN_PROGRESS:
                    state = f"in_progress owner={task.owner or '-'}"
                else:
                    state = COMPLETED
                lines.append(f"{task.id} [{state}] {task.subject}")
            return "\n".join(lines)
        except (OSError, TaskError) as exc:
            return f"Error: {exc}"

    def get_task(self, task_id: str) -> str:
        """返回完整记录，让任务描述按需读取而不必常驻上下文。"""

        try:
            task = self._load(task_id)
            return json.dumps(task.to_dict(), ensure_ascii=False, indent=2)
        except (OSError, TaskError) as exc:
            return f"Error: {exc}"

    @board_locked
    def claim_task(self, task_id: str, owner: str = "agent") -> str:
        """pending → in_progress 的唯一入口，依赖未完成时拒绝认领。"""

        try:
            task = self._load(task_id)
            # 状态检查先于依赖检查，已认领的任务错误原因才稳定。
            if task.status != PENDING:
                raise TaskError(
                    f"task {task_id} is {task.status}, cannot claim"
                )
            if task.owner is not None:
                raise TaskError(f"task {task_id} already owned by {task.owner}")
            if not isinstance(owner, str) or not owner.strip():
                raise TaskError("owner must be a non-empty string")
            blockers = self.blocking_dependencies(task)
            if blockers:
                raise TaskError(
                    f"task {task_id} is blocked by: {', '.join(blockers)}"
                )
            claimed = replace(
                task,
                status=IN_PROGRESS,
                owner=owner.strip(),
            )
            self._save(claimed)
            return f"Claimed {task_id} ({task.subject}) for {claimed.owner}"
        except (OSError, TaskError) as exc:
            return f"Error: {exc}"

    @board_locked
    def complete_task(self, task_id: str, owner: str | None = None) -> str:
        """in_progress → completed，并报告本次真正解锁的下游任务。"""

        try:
            task = self._load(task_id)
            if task.status != IN_PROGRESS:
                raise TaskError(
                    f"task {task_id} is {task.status}, cannot complete"
                )
            # Lead 保留管理入口；队友 handler 固定传入自身身份，不能完成别人的任务。
            if owner is not None and task.owner != owner:
                raise TaskError(f"task {task_id} is owned by {task.owner}, not {owner}")
            # 先落盘再扫描，下游判定读到的就是新的 completed 状态。
            self._save(replace(task, status=COMPLETED))

            # 只报告真正依赖本任务且现在无 blocker 的下游，避免把早已 ready
            # 的无关任务误报成“刚刚解锁”。
            unblocked = [
                candidate
                for candidate in self._all()
                if candidate.status == PENDING
                and task_id in candidate.blocked_by
                and not self.blocking_dependencies(candidate)
            ]
            result = f"Completed {task_id} ({task.subject})"
            if unblocked:
                result += "\nUnblocked: " + ", ".join(
                    f"{item.id} ({item.subject})" for item in unblocked
                )
            return result
        except (OSError, TaskError) as exc:
            return f"Error: {exc}"

    def scan_unclaimed_tasks(self) -> list[TaskRecord]:
        """按文件名扫描：pending、无 owner、依赖全部完成才是候选。"""

        return [
            task for task in self._all()
            if task.status == PENDING
            and task.owner is None
            and not self.blocking_dependencies(task)
        ]

    def claim_next(self, owner: str) -> TaskRecord | None:
        """扫描允许过期；真正的 claim 在锁内重读，竞争失败就试下一个。"""

        for candidate in self.scan_unclaimed_tasks():
            result = self.claim_task(candidate.id, owner)
            if result.startswith("Claimed "):
                return self._load(candidate.id)
        return None

    def handlers(self) -> dict[str, Callable[..., str]]:
        """只并入父 Agent handler 表；SubAgent 拿不到这五个入口。"""

        return {
            "create_task": self.create_task,
            "list_tasks": self.list_tasks,
            "get_task": self.get_task,
            "claim_task": self.claim_task,
            "complete_task": self.complete_task,
        }
