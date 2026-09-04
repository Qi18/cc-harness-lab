"""s14 本地 Cron 调度、持久化和待投递队列。"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


# Cron ID 会进入持久化文件和取消工具参数，限制字符集可避免异常记录污染状态。
SAFE_CRON_ID = re.compile(r"^cron_[A-Za-z0-9_-]{1,64}$")


def _cron_tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    """在能力模块内定义 schema，避免 cron_scheduler 与 tool_use 循环导入。"""

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


CRON_TOOLS = [
    _cron_tool(
        "schedule_cron",
        "Schedule a prompt with a five-field cron expression in local time.",
        {
            "cron": {"type": "string", "minLength": 1},
            "prompt": {"type": "string", "minLength": 1},
            "recurring": {"type": "boolean"},
            "durable": {"type": "boolean"},
        },
        ["cron", "prompt"],
    ),
    _cron_tool(
        "list_crons",
        "List scheduled cron jobs and their delivery state.",
        {},
    ),
    _cron_tool(
        "cancel_cron",
        "Cancel a cron job by ID.",
        {"job_id": {"type": "string", "minLength": 1}},
        ["job_id"],
    ),
]


@dataclass
class CronJob:
    """一个调度定义，以及它是否正在等待 Agent 确认接收。"""

    id: str
    cron: str
    prompt: str
    recurring: bool
    durable: bool
    pending_delivery: bool = False
    last_fired: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CronJob":
        """持久化文件不是可信输入，恢复前逐字段校验。"""

        expected = {
            "id",
            "cron",
            "prompt",
            "recurring",
            "durable",
            "pending_delivery",
            "last_fired",
        }
        if set(payload) != expected:
            raise ValueError("saved job fields do not match the cron schema")
        job = cls(**payload)
        if not isinstance(job.id, str) or not SAFE_CRON_ID.fullmatch(job.id):
            raise ValueError("invalid job ID")
        if not isinstance(job.cron, str):
            raise ValueError("cron must be a string")
        error = validate_cron(job.cron)
        if error:
            raise ValueError(error)
        if not isinstance(job.prompt, str) or not job.prompt.strip():
            raise ValueError("prompt cannot be empty")
        if type(job.recurring) is not bool or type(job.durable) is not bool:
            raise ValueError("recurring and durable must be booleans")
        if type(job.pending_delivery) is not bool:
            raise ValueError("pending_delivery must be a boolean")
        if job.last_fired is not None and (
            not isinstance(job.last_fired, str)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", job.last_fired)
        ):
            raise ValueError("last_fired has an invalid minute marker")
        return job


def _cron_field_matches(field: str, value: int) -> bool:
    if field == "*":
        return True
    if field.startswith("*/"):
        return value % int(field[2:]) == 0
    if "," in field:
        return any(
            _cron_field_matches(part.strip(), value) for part in field.split(",")
        )
    if "-" in field:
        start, end = field.split("-", 1)
        return int(start) <= value <= int(end)
    return value == int(field)


def cron_matches(cron_expr: str, moment: datetime) -> bool:
    """匹配五段 Cron；日期与星期同时受限时遵循标准 OR 语义。"""

    if validate_cron(cron_expr) is not None:
        return False
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, day, month, weekday = fields
    cron_weekday = (moment.weekday() + 1) % 7
    if not (
        _cron_field_matches(minute, moment.minute)
        and _cron_field_matches(hour, moment.hour)
        and _cron_field_matches(month, moment.month)
    ):
        return False

    day_matches = _cron_field_matches(day, moment.day)
    weekday_matches = _cron_field_matches(weekday, cron_weekday)
    if day == "*" and weekday == "*":
        return True
    if day == "*":
        return weekday_matches
    if weekday == "*":
        return day_matches
    return day_matches or weekday_matches


def _validate_cron_field(field: str, minimum: int, maximum: int) -> str | None:
    if field == "*":
        return None
    if field.startswith("*/"):
        step = field[2:]
        if not step.isdigit() or int(step) <= 0:
            return f"Invalid step: {field}"
        return None
    if "," in field:
        for part in field.split(","):
            error = _validate_cron_field(part.strip(), minimum, maximum)
            if error:
                return error
        return None
    if "-" in field:
        start, end = field.split("-", 1)
        if not start.isdigit() or not end.isdigit():
            return f"Invalid range: {field}"
        start_value, end_value = int(start), int(end)
        if start_value > end_value:
            return f"Range start is greater than end: {field}"
        if start_value < minimum or end_value > maximum:
            return f"Range {field} is outside [{minimum}-{maximum}]"
        return None
    if not field.isdigit():
        return f"Invalid field: {field}"
    value = int(field)
    if value < minimum or value > maximum:
        return f"Value {value} is outside [{minimum}-{maximum}]"
    return None


def validate_cron(cron_expr: str) -> str | None:
    if not isinstance(cron_expr, str):
        return "Cron expression must be a string"
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return f"Expected 5 fields, got {len(fields)}"
    rules = (
        ("minute", 0, 59),
        ("hour", 0, 23),
        ("day-of-month", 1, 31),
        ("month", 1, 12),
        ("day-of-week", 0, 6),
    )
    for field, (name, minimum, maximum) in zip(fields, rules):
        error = _validate_cron_field(field, minimum, maximum)
        if error:
            return f"{name}: {error}"
    return None


class CronScheduler:
    """管理任务定义、原子持久化、到期队列和一秒轮询线程。"""

    def __init__(
        self,
        storage_path: Path,
        *,
        id_factory: Callable[[], str] | None = None,
        now_fn: Callable[[], datetime] = datetime.now,
        poll_interval: float = 1.0,
    ):
        self.storage_path = storage_path.resolve()
        self.id_factory = id_factory
        self.now_fn = now_fn
        self.poll_interval = poll_interval
        self.jobs: dict[str, CronJob] = {}
        self.queue: list[CronJob] = []
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loaded = False

    def _save_durable_jobs(self) -> None:
        """临时文件落盘后原子替换，避免中断留下半截 JSON。"""

        with self.lock:
            payload = [asdict(job) for job in self.jobs.values() if job.durable]
            temporary = self.storage_path.with_name(
                f"{self.storage_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    0o600,
                )
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, indent=2, ensure_ascii=False)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.storage_path)
                os.chmod(self.storage_path, 0o600)
            finally:
                temporary.unlink(missing_ok=True)

    def load_durable_jobs(self) -> int:
        """幂等恢复持久任务；单条坏记录不会阻止其他任务加载。"""

        with self.lock:
            if self._loaded:
                return 0
            self._loaded = True
        if not self.storage_path.exists():
            return 0
        try:
            payload = json.loads(self.storage_path.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                raise ValueError("expected a JSON list")
        except (OSError, json.JSONDecodeError, ValueError) as error:
            print(f"[cron] could not load {self.storage_path.name}: {error}")
            return 0

        loaded = 0
        with self.lock:
            queued_ids = {job.id for job in self.queue}
            for item in payload:
                try:
                    if not isinstance(item, dict):
                        raise ValueError("saved job must be an object")
                    job = CronJob.from_dict(item)
                    if not job.durable:
                        raise ValueError("saved job must be durable")
                    if job.id in self.jobs:
                        raise ValueError(f"duplicate job ID: {job.id}")
                except (TypeError, ValueError) as error:
                    print(f"[cron] skipped invalid saved job: {error}")
                    continue
                self.jobs[job.id] = job
                if job.pending_delivery and job.id not in queued_ids:
                    self.queue.append(job)
                    queued_ids.add(job.id)
                loaded += 1
        if loaded:
            print(f"[cron] loaded {loaded} durable job(s)")
        return loaded

    def _new_id(self) -> str:
        for _ in range(100):
            candidate = (
                self.id_factory() if self.id_factory else f"cron_{secrets.token_hex(4)}"
            )
            if not isinstance(candidate, str) or not SAFE_CRON_ID.fullmatch(candidate):
                raise ValueError("ID factory returned an invalid cron ID")
            if candidate not in self.jobs:
                return candidate
        raise RuntimeError("could not allocate a unique cron job ID")

    def schedule_job(
        self,
        cron: str,
        prompt: str,
        recurring: bool = True,
        durable: bool = True,
    ) -> CronJob | str:
        error = validate_cron(cron)
        if error:
            return error
        if not isinstance(prompt, str) or not prompt.strip():
            return "Prompt cannot be empty"
        if type(recurring) is not bool or type(durable) is not bool:
            return "recurring and durable must be booleans"

        with self.lock:
            job = CronJob(
                id=self._new_id(),
                cron=cron.strip(),
                prompt=prompt.strip(),
                recurring=recurring,
                durable=durable,
            )
            self.jobs[job.id] = job
            try:
                if durable:
                    self._save_durable_jobs()
            except OSError as error:
                self.jobs.pop(job.id, None)
                return f"Could not persist cron job: {error}"
        print(f"[cron] scheduled {job.id}: {job.cron} -> {job.prompt[:60]}")
        return job

    def cancel_job(self, job_id: str) -> str:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return f"Job {job_id} not found"
            previous_queue = list(self.queue)
            self.jobs.pop(job_id)
            self.queue[:] = [queued for queued in self.queue if queued.id != job_id]
            try:
                if job.durable:
                    self._save_durable_jobs()
            except OSError as error:
                self.jobs[job_id] = job
                self.queue[:] = previous_queue
                return f"Could not persist cancellation: {error}"
        print(f"[cron] cancelled {job_id}")
        return f"Cancelled {job_id}"

    def _enqueue_due_job(
        self,
        job: CronJob,
        minute_marker: str | None = None,
    ) -> None:
        old_pending = job.pending_delivery
        old_last_fired = job.last_fired
        job.pending_delivery = True
        if minute_marker is not None:
            job.last_fired = minute_marker
        try:
            if job.durable:
                self._save_durable_jobs()
        except Exception:
            job.pending_delivery = old_pending
            job.last_fired = old_last_fired
            raise
        self.queue.append(job)

    def poll_due_jobs(self, moment: datetime | None = None) -> int:
        moment = moment or self.now_fn()
        minute_marker = moment.strftime("%Y-%m-%d %H:%M")
        enqueued = 0
        with self.lock:
            for job in list(self.jobs.values()):
                try:
                    if job.pending_delivery or job.last_fired == minute_marker:
                        continue
                    if cron_matches(job.cron, moment):
                        self._enqueue_due_job(job, minute_marker)
                        enqueued += 1
                        print(f"[cron] due {job.id}: {job.prompt[:60]}")
                except Exception as error:
                    print(f"[cron] could not enqueue {job.id}: {error}")
        return enqueued

    def consume_queue(self) -> list[CronJob]:
        with self.lock:
            jobs = list(self.queue)
            self.queue.clear()
        return jobs

    def acknowledge(self, jobs: list[CronJob]) -> None:
        """模型首次成功响应后确认投递；一次性任务在此刻删除。"""

        changed: list[tuple[CronJob, bool]] = []
        removed: list[CronJob] = []
        with self.lock:
            for delivered in jobs:
                current = self.jobs.get(delivered.id)
                if current is None:
                    continue
                changed.append((current, current.pending_delivery))
                if current.recurring:
                    current.pending_delivery = False
                else:
                    removed.append(current)
                    self.jobs.pop(current.id)
            try:
                if any(job.durable for job, _ in changed):
                    self._save_durable_jobs()
            except Exception:
                for job in removed:
                    self.jobs[job.id] = job
                for job, pending in changed:
                    job.pending_delivery = pending
                self.restore(jobs)
                raise

    def restore(self, jobs: list[CronJob]) -> None:
        """模型请求失败时重新排队，保证任务不会静默丢失。"""

        with self.lock:
            queued_ids = {job.id for job in self.queue}
            for delivered in jobs:
                current = self.jobs.get(delivered.id)
                if current is None:
                    continue
                current.pending_delivery = True
                if current.id not in queued_ids:
                    self.queue.append(current)
                    queued_ids.add(current.id)

    def has_queue(self) -> bool:
        with self.lock:
            return bool(self.queue)

    def run_schedule_cron(
        self,
        cron: str,
        prompt: str,
        recurring: bool = True,
        durable: bool = True,
    ) -> str:
        result = self.schedule_job(cron, prompt, recurring, durable)
        if isinstance(result, str):
            return f"Error: {result}"
        return f"Scheduled {result.id}: {result.cron} -> {result.prompt}"

    def run_list_crons(self) -> str:
        with self.lock:
            jobs = list(self.jobs.values())
        if not jobs:
            return "No cron jobs."
        lines = []
        for job in jobs:
            frequency = "recurring" if job.recurring else "one-shot"
            storage = "durable" if job.durable else "session"
            delivery = ", pending" if job.pending_delivery else ""
            lines.append(
                f"{job.id}: {job.cron} -> {job.prompt[:60]} "
                f"[{frequency}, {storage}{delivery}]"
            )
        return "\n".join(lines)

    def run_cancel_cron(self, job_id: str) -> str:
        return self.cancel_job(job_id)

    def handlers(self) -> dict[str, Callable[..., str]]:
        return {
            "schedule_cron": self.run_schedule_cron,
            "list_crons": self.run_list_crons,
            "cancel_cron": self.run_cancel_cron,
        }

    def _scheduler_loop(self) -> None:
        while not self._stop.wait(self.poll_interval):
            self.poll_due_jobs()

    def start(self) -> None:
        """幂等启动持久化恢复和调度线程。"""

        with self.lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self.load_durable_jobs()
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._scheduler_loop,
                name="cron-scheduler",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        with self.lock:
            thread = self._thread
            self._thread = None
            self._stop.set()
        if thread is not None:
            thread.join(timeout=max(1.0, self.poll_interval * 2))

    @property
    def is_running(self) -> bool:
        with self.lock:
            return self._thread is not None and self._thread.is_alive()
