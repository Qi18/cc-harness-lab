"""Persistent task graph, lifecycle, and OpenAI tool adapters."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


TASK_ID_PATTERN = re.compile(r"^task_[0-9a-f]{8}$")


@dataclass
class TaskRecord:
    id: str
    subject: str
    description: str
    status: str
    owner: str | None
    blockedBy: list[str]


class TaskStore:
    """Store each validated task as ``.tasks/{id}.json``."""

    VALID_STATUSES = {"pending", "in_progress", "completed"}

    def __init__(self, directory: Path, workdir: Path):
        self.directory = directory
        self.workdir = workdir.resolve()

    def _root(self, create: bool = False) -> Path:
        if create:
            self.directory.mkdir(parents=True, exist_ok=True)
        root = self.directory.resolve()
        try:
            root.relative_to(self.workdir)
        except ValueError as exc:
            raise ValueError("Task store escapes the working directory") from exc
        return root

    def _path(self, task_id: str, create_root: bool = False) -> Path:
        if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
            raise ValueError(f"Invalid task ID: {task_id!r}")
        root = self._root(create=create_root)
        path = (root / f"{task_id}.json").resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"Invalid task ID: {task_id!r}") from exc
        return path

    @staticmethod
    def validate_text(value: object, field: str, allow_empty: bool = False) -> str:
        if not isinstance(value, str):
            raise ValueError(f"Task {field} must be a string")
        value = value.strip()
        if not allow_empty and not value:
            raise ValueError(f"Task {field} cannot be empty")
        return value

    def _validate_record(self, task: TaskRecord) -> None:
        if not TASK_ID_PATTERN.fullmatch(task.id):
            raise ValueError(f"Invalid task ID: {task.id!r}")
        self.validate_text(task.subject, "subject")
        if not isinstance(task.description, str):
            raise ValueError("Task description must be a string")
        if task.status not in self.VALID_STATUSES:
            raise ValueError(f"Invalid task status: {task.status!r}")
        if task.owner is not None and (
            not isinstance(task.owner, str) or not task.owner.strip()
        ):
            raise ValueError("Task owner must be null or a non-empty string")
        if not isinstance(task.blockedBy, list):
            raise ValueError("Task blockedBy must be a list")
        if len(task.blockedBy) != len(set(task.blockedBy)):
            raise ValueError("Task blockedBy contains duplicate IDs")
        for dependency in task.blockedBy:
            if not isinstance(dependency, str) or not TASK_ID_PATTERN.fullmatch(
                dependency
            ):
                raise ValueError(f"Invalid dependency ID: {dependency!r}")
            if dependency == task.id:
                raise ValueError("Task cannot depend on itself")

    def exists(self, task_id: str) -> bool:
        return self._path(task_id).is_file()

    def create(self, subject: str, description: str = "") -> TaskRecord:
        subject = self.validate_text(subject, "subject")
        if not isinstance(description, str):
            raise ValueError("Task description must be a string")
        self._root(create=True)
        for _ in range(100):
            task = TaskRecord(
                id=f"task_{uuid.uuid4().hex[:8]}",
                subject=subject,
                description=description.strip(),
                status="pending",
                owner=None,
                blockedBy=[],
            )
            try:
                with self._path(task.id, create_root=True).open(
                    "x", encoding="utf-8"
                ) as handle:
                    json.dump(asdict(task), handle, indent=2, ensure_ascii=False)
                    handle.write("\n")
                return task
            except FileExistsError:
                continue
        raise RuntimeError("Could not allocate a unique task ID")

    def save(self, task: TaskRecord) -> None:
        self._validate_record(task)
        path = self._path(task.id, create_root=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(asdict(task), handle, indent=2, ensure_ascii=False)
                handle.write("\n")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self, task_id: str) -> TaskRecord:
        try:
            data = json.loads(self._path(task_id).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(f"Task not found: {task_id}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid task file: {task_id}") from exc
        expected = {"id", "subject", "description", "status", "owner", "blockedBy"}
        if not isinstance(data, dict) or set(data) != expected:
            raise ValueError(f"Invalid task fields: {task_id}")
        try:
            task = TaskRecord(**data)
        except TypeError as exc:
            raise ValueError(f"Invalid task file: {task_id}") from exc
        self._validate_record(task)
        if task.id != task_id:
            raise ValueError(f"Task file ID does not match {task_id}")
        return task

    def list(self) -> list[TaskRecord]:
        if not self.directory.exists():
            return []
        root = self._root()
        return [self.load(path.stem) for path in sorted(root.glob("task_*.json"))]

    def _depends_on(self, task_id: str, target_id: str) -> bool:
        pending = [task_id]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target_id:
                return True
            if current in visited:
                continue
            visited.add(current)
            pending.extend(self.load(current).blockedBy)
        return False

    def update_dependencies(
        self, task_id: str, add_blocked_by: list[str]
    ) -> TaskRecord:
        if not isinstance(add_blocked_by, list) or not add_blocked_by:
            raise ValueError("addBlockedBy must be a non-empty list of task IDs")
        task = self.load(task_id)
        if task.status != "pending" or task.owner is not None:
            raise ValueError(
                f"Task {task_id} dependencies can only be updated while "
                "pending and unowned"
            )
        dependencies = list(dict.fromkeys(add_blocked_by))
        for dependency in dependencies:
            if not isinstance(dependency, str) or not TASK_ID_PATTERN.fullmatch(
                dependency
            ):
                raise ValueError(f"Invalid dependency ID: {dependency!r}")
            if dependency == task_id:
                raise ValueError("Task cannot depend on itself")
            if not self.exists(dependency):
                raise ValueError(f"Dependency not found: {dependency}")
            if dependency not in task.blockedBy and self._depends_on(
                dependency, task_id
            ):
                raise ValueError(
                    f"Dependency cycle detected: {task_id} -> {dependency}"
                )
        task.blockedBy.extend(
            dependency
            for dependency in dependencies
            if dependency not in task.blockedBy
        )
        self.save(task)
        return task


class TaskManager:
    """Lifecycle operations and model-facing string adapters."""

    def __init__(self, store: TaskStore, owner: str = "agent"):
        self.store = store
        self.owner = owner

    def incomplete_dependencies(self, task: TaskRecord) -> list[str]:
        incomplete = []
        for dependency in task.blockedBy:
            try:
                if self.store.load(dependency).status != "completed":
                    incomplete.append(dependency)
            except ValueError:
                incomplete.append(dependency)
        return incomplete

    def can_start(self, task_id: str) -> bool:
        return not self.incomplete_dependencies(self.store.load(task_id))

    def claim(self, task_id: str, owner: str | None = None) -> str:
        owner = TaskStore.validate_text(owner or self.owner, "owner")
        task = self.store.load(task_id)
        if task.status != "pending":
            return f"Task {task_id} is {task.status}, cannot claim"
        dependencies = self.incomplete_dependencies(task)
        if dependencies:
            return f"Blocked by: {dependencies}"
        task.owner = owner
        task.status = "in_progress"
        self.store.save(task)
        print(f"  [claim] {task.subject} -> in_progress (owner: {owner})")
        return f"Claimed {task.id} ({task.subject})"

    def complete(self, task_id: str, owner: str | None = None) -> str:
        owner = owner or self.owner
        task = self.store.load(task_id)
        if task.status != "in_progress":
            return f"Task {task_id} is {task.status}, cannot complete"
        if task.owner != owner:
            return f"Task {task_id} is owned by {task.owner}, not {owner}"
        ready_before = {
            candidate.id
            for candidate in self.store.list()
            if candidate.status == "pending"
            and candidate.blockedBy
            and self.can_start(candidate.id)
        }
        task.status = "completed"
        self.store.save(task)
        unblocked = [
            candidate.subject
            for candidate in self.store.list()
            if candidate.status == "pending"
            and candidate.blockedBy
            and candidate.id not in ready_before
            and self.can_start(candidate.id)
        ]
        print(f"  [complete] {task.subject}")
        result = f"Completed {task.id} ({task.subject})"
        if unblocked:
            result += f"\nUnblocked: {', '.join(unblocked)}"
            print(f"  [unblocked] {', '.join(unblocked)}")
        return result

    @staticmethod
    def _observe(operation: Callable[[], str]) -> str:
        try:
            return operation()
        except (OSError, RuntimeError, ValueError) as exc:
            return f"Error: {exc}"

    def run_create(self, subject: str, description: str = "") -> str:
        def operation() -> str:
            task = self.store.create(subject, description)
            print(f"  [create] {task.subject}")
            return f"Created {task.id}: {task.subject}"

        return self._observe(operation)

    def run_update(self, task_id: str, addBlockedBy: list[str]) -> str:
        def operation() -> str:
            task = self.store.update_dependencies(task_id, addBlockedBy)
            dependencies = ", ".join(task.blockedBy) or "(none)"
            print(f"  [update] {task.subject} blockedBy: {dependencies}")
            return f"Updated {task.id} blockedBy: {dependencies}"

        return self._observe(operation)

    def run_list(self) -> str:
        def operation() -> str:
            tasks = self.store.list()
            if not tasks:
                return "No tasks. Use create_task to add some."
            markers = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
            lines = []
            for task in tasks:
                dependencies = (
                    f" (blockedBy: {', '.join(task.blockedBy)})"
                    if task.blockedBy
                    else ""
                )
                owner = f" [{task.owner}]" if task.owner else ""
                lines.append(
                    f"{markers[task.status]} {task.id}: {task.subject} "
                    f"[{task.status}]{owner}{dependencies}"
                )
            return "\n".join(lines)

        return self._observe(operation)

    def run_get(self, task_id: str) -> str:
        return self._observe(
            lambda: json.dumps(
                asdict(self.store.load(task_id)), indent=2, ensure_ascii=False
            )
        )

    def run_claim(self, task_id: str) -> str:
        return self._observe(lambda: self.claim(task_id))

    def run_complete(self, task_id: str) -> str:
        return self._observe(lambda: self.complete(task_id))


def _id_property() -> dict[str, str]:
    return {"type": "string", "pattern": "^task_[0-9a-f]{8}$"}


TASK_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": "Create a durable task and return its generated ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "minLength": 1},
                    "description": {"type": "string"},
                },
                "required": ["subject"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_task",
            "description": (
                "Add dependencies using IDs returned by create_task. "
                "Create every node before adding dependency edges."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": _id_property(),
                    "addBlockedBy": {
                        "type": "array",
                        "items": _id_property(),
                        "minItems": 1,
                    },
                },
                "required": ["task_id", "addBlockedBy"],
                "additionalProperties": False,
            },
        },
    },
    *[
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": {"task_id": _id_property()},
                    "required": ["task_id"],
                    "additionalProperties": False,
                },
            },
        }
        for name, description in (
            ("get_task", "Read one complete durable task record by ID."),
            ("claim_task", "Claim a pending task after all dependencies complete."),
            (
                "complete_task",
                "Complete a task owned by this agent and report newly unblocked work.",
            ),
        )
    ],
    {
        "type": "function",
        "function": {
            "name": "list_tasks",
            "description": "List durable tasks with status, owner, and dependencies.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
]
