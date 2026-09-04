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


# 三个 schema 在本模块自建、由 tool_use.py 反向导入，并且只进 PARENT_TOOLS：
# SubAgent 不应该给主会话留下会自动触发的定时任务。
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
    # recurring 决定确认投递后是保留还是删除，durable 决定是否写入磁盘。
    recurring: bool
    durable: bool
    # True 表示已经排队但模型还没确认收到：崩溃重启后据此重新投递。
    pending_delivery: bool = False
    # "YYYY-MM-DD HH:MM" 分钟标记，使一分钟内的多次轮询只触发一次。
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
    """匹配单个字段：`*`、`*/n`、逗号列表、区间或精确值。"""

    # 调用方已先跑过 validate_cron，因此这里可以直接 int() 而不再重复校验。
    if field == "*":
        return True
    if field.startswith("*/"):
        # 步进统一以 0 为基准取模：*/15 命中 0、15、30、45。
        return value % int(field[2:]) == 0
    if "," in field:
        # 列表元素本身还可以是区间或步进，所以递归而不是逐个 int()。
        return any(
            _cron_field_matches(part.strip(), value) for part in field.split(",")
        )
    if "-" in field:
        start, end = field.split("-", 1)
        return int(start) <= value <= int(end)
    return value == int(field)


def cron_matches(cron_expr: str, moment: datetime) -> bool:
    """匹配五段 Cron；日期与星期同时受限时遵循标准 OR 语义。"""

    # 非法表达式返回 False 而不是抛异常：轮询线程每秒调用一次，一条坏表达式
    # 不能连带阻塞其他任务的触发。
    if validate_cron(cron_expr) is not None:
        return False
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, day, month, weekday = fields
    # datetime.weekday() 以周一为 0，cron 以周日为 0，先换算再比较。
    cron_weekday = (moment.weekday() + 1) % 7
    if not (
        _cron_field_matches(minute, moment.minute)
        and _cron_field_matches(hour, moment.hour)
        and _cron_field_matches(month, moment.month)
    ):
        return False

    # 标准 cron 的特例：日期与星期都被限定时取并集（任一命中即触发），
    # 只有一侧为 * 时才退化成另一侧的单独判定。
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
    """校验单个字段，返回首个人类可读错误；None 表示合法。"""

    if field == "*":
        return None
    if field.startswith("*/"):
        step = field[2:]
        # 0 步进会让取模抛 ZeroDivisionError，负号与空串同样只能在这里拦下。
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
        # 倒序区间在匹配时恒不成立，属于静默失效，必须当成参数错误报出。
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
    """校验五段本地时区表达式；返回 None 表示合法，否则是错误说明。"""

    if not isinstance(cron_expr, str):
        return "Cron expression must be a string"
    fields = cron_expr.strip().split()
    # 先确认字段数，下面的 zip 才不会因为缺字段而静默少校验一段。
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
        # 错误里带上字段名：这段文本会作为 tool result 回到模型，它需要知道
        # 是哪一段写错了才能自行修正参数。
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
        # id_factory 与 now_fn 可注入：测试无需真的等待到某一分钟就能断言行为。
        self.id_factory = id_factory
        self.now_fn = now_fn
        self.poll_interval = poll_interval
        # jobs 是任务定义全集，queue 只放已到期、等待 Agent 消费的任务。
        self.jobs: dict[str, CronJob] = {}
        self.queue: list[CronJob] = []
        # 用 RLock 而不是 Lock：poll_due_jobs → _enqueue_due_job → _save_durable_jobs
        # 这条路径会在同一线程内重复取锁。
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # 保证磁盘恢复只做一次：start() 与显式调用都可能触发 load。
        self._loaded = False

    def _save_durable_jobs(self) -> None:
        """临时文件落盘后原子替换，避免中断留下半截 JSON。"""

        with self.lock:
            # 只有 durable 任务落盘；session 任务随进程结束消失。
            payload = [asdict(job) for job in self.jobs.values() if job.durable]
            # 临时文件名带 pid 与线程 id：多个进程或线程同时保存也不会互相覆盖
            # 对方写了一半的临时文件。
            temporary = self.storage_path.with_name(
                f"{self.storage_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                # 任务 prompt 属于用户内容，用 0600 创建，避免经由 umask 放宽权限。
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    0o600,
                )
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, indent=2, ensure_ascii=False)
                    stream.write("\n")
                    stream.flush()
                    # 先 fsync 再 replace：崩溃后要么是旧文件，要么是完整的新文件。
                    os.fsync(stream.fileno())
                os.replace(temporary, self.storage_path)
                os.chmod(self.storage_path, 0o600)
            finally:
                # 写入或替换失败时清掉临时文件，不在工作区留下垃圾。
                temporary.unlink(missing_ok=True)

    def load_durable_jobs(self) -> int:
        """幂等恢复持久任务；单条坏记录不会阻止其他任务加载。"""

        with self.lock:
            # _loaded 在锁内翻转，第二次调用直接返回 0，不会重复注册同一批任务。
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
            # 整个文件坏掉只打印并放弃恢复：调度能力降级，但 Harness 仍可用。
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
                    # 逐条容错：一条被手工改坏的记录不影响其余任务恢复。
                    print(f"[cron] skipped invalid saved job: {error}")
                    continue
                self.jobs[job.id] = job
                # 上次退出时还没确认投递的任务重新入队，这是“至少一次”投递的来源。
                if job.pending_delivery and job.id not in queued_ids:
                    self.queue.append(job)
                    queued_ids.add(job.id)
                loaded += 1
        if loaded:
            print(f"[cron] loaded {loaded} durable job(s)")
        return loaded

    def _new_id(self) -> str:
        """分配未占用的 cron ID；注入的 id_factory 同样要过字符集校验。"""

        for _ in range(100):
            candidate = (
                self.id_factory() if self.id_factory else f"cron_{secrets.token_hex(4)}"
            )
            if not isinstance(candidate, str) or not SAFE_CRON_ID.fullmatch(candidate):
                raise ValueError("ID factory returned an invalid cron ID")
            if candidate not in self.jobs:
                return candidate
        # 有界重试：一个总是返回同一个值的 factory 只会失败，不会死循环。
        raise RuntimeError("could not allocate a unique cron job ID")

    def schedule_job(
        self,
        cron: str,
        prompt: str,
        recurring: bool = True,
        durable: bool = True,
    ) -> CronJob | str:
        """校验并登记一个调度；失败时返回面向模型的错误文本而不抛异常。"""

        error = validate_cron(cron)
        if error:
            return error
        if not isinstance(prompt, str) or not prompt.strip():
            return "Prompt cannot be empty"
        # 用 type() is not bool 而不是真值判断：1 / "false" 这类畸形参数不能静默
        # 变成持久任务，否则会写进磁盘并在下次启动时被拒收。
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
                # 落盘失败就回滚内存登记：否则本进程会触发一个重启即消失的任务。
                self.jobs.pop(job.id, None)
                return f"Could not persist cron job: {error}"
        print(f"[cron] scheduled {job.id}: {job.cron} -> {job.prompt[:60]}")
        return job

    def cancel_job(self, job_id: str) -> str:
        """删除任务定义并清理待投递队列；失败时整体回滚。"""

        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return f"Job {job_id} not found"
            previous_queue = list(self.queue)
            self.jobs.pop(job_id)
            # 已到期但还没投递的副本也要从队列里移除，否则取消后仍会触发一次。
            # 用切片赋值原地改写，保持已持有 self.queue 引用的调用方看到同一个列表。
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
        """先把 pending 状态落盘再入队，使崩溃后仍能重新投递。"""

        old_pending = job.pending_delivery
        old_last_fired = job.last_fired
        job.pending_delivery = True
        if minute_marker is not None:
            job.last_fired = minute_marker
        try:
            if job.durable:
                self._save_durable_jobs()
        except Exception:
            # 落盘失败时回滚两个字段并向上抛，避免出现“队列里有、磁盘上没有”
            # 的任务；下一分钟的轮询会重试。
            job.pending_delivery = old_pending
            job.last_fired = old_last_fired
            raise
        self.queue.append(job)

    def poll_due_jobs(self, moment: datetime | None = None) -> int:
        """以分钟为粒度找出到期任务，返回本次新入队的数量。"""

        moment = moment or self.now_fn()
        minute_marker = moment.strftime("%Y-%m-%d %H:%M")
        enqueued = 0
        with self.lock:
            for job in list(self.jobs.values()):
                try:
                    # 两道去重：上一次投递还未确认的任务不重复入队；同一分钟内的
                    # 多次轮询（默认 1 秒一次）只能触发一次。
                    if job.pending_delivery or job.last_fired == minute_marker:
                        continue
                    if cron_matches(job.cron, moment):
                        self._enqueue_due_job(job, minute_marker)
                        enqueued += 1
                        print(f"[cron] due {job.id}: {job.prompt[:60]}")
                except Exception as error:
                    # 单个任务失败不能终止轮询线程，否则整个调度器会静默停止。
                    print(f"[cron] could not enqueue {job.id}: {error}")
        return enqueued

    def consume_queue(self) -> list[CronJob]:
        """原子取走整个队列；调用方必须 acknowledge 或 restore。"""

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
                # 投递期间可能已经被 cancel_cron 删掉，这时不再复活它。
                current = self.jobs.get(delivered.id)
                if current is None:
                    continue
                changed.append((current, current.pending_delivery))
                if current.recurring:
                    # 只清 pending：任务定义保留，等下一个到期分钟再入队。
                    current.pending_delivery = False
                else:
                    removed.append(current)
                    self.jobs.pop(current.id)
            try:
                if any(job.durable for job, _ in changed):
                    self._save_durable_jobs()
            except Exception:
                # 确认本质上是一次状态提交：落盘失败就把删除与清 pending 全部撤销，
                # 并重新排队，宁可重复投递也不静默丢任务。
                for job in removed:
                    self.jobs[job.id] = job
                for job, pending in changed:
                    job.pending_delivery = pending
                self.restore(jobs)
                raise

    def restore(self, jobs: list[CronJob]) -> None:
        """模型请求失败时重新排队，保证任务不会静默丢失。"""

        with self.lock:
            # queued_ids 去重：重复 restore 同一批任务不会让它在队列里排两次。
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
        """供 Queue Processor 在不取走任务的前提下轻量探测。"""

        with self.lock:
            return bool(self.queue)

    def run_schedule_cron(
        self,
        cron: str,
        prompt: str,
        recurring: bool = True,
        durable: bool = True,
    ) -> str:
        """schedule_cron handler：把错误文本结构成 Error: observation。"""

        result = self.schedule_job(cron, prompt, recurring, durable)
        if isinstance(result, str):
            return f"Error: {result}"
        return f"Scheduled {result.id}: {result.cron} -> {result.prompt}"

    def run_list_crons(self) -> str:
        """list_crons handler：一行一个任务，并标出投递与存储形态。"""

        with self.lock:
            jobs = list(self.jobs.values())
        if not jobs:
            return "No cron jobs."
        lines = []
        for job in jobs:
            frequency = "recurring" if job.recurring else "one-shot"
            storage = "durable" if job.durable else "session"
            delivery = ", pending" if job.pending_delivery else ""
            # prompt 只取前 60 字符：列表是状态概览，不能把上下文预算吃掉。
            lines.append(
                f"{job.id}: {job.cron} -> {job.prompt[:60]} "
                f"[{frequency}, {storage}{delivery}]"
            )
        return "\n".join(lines)

    def run_cancel_cron(self, job_id: str) -> str:
        """cancel_cron handler：任务不存在也是普通 observation，不是异常。"""

        return self.cancel_job(job_id)

    def handlers(self) -> dict[str, Callable[..., str]]:
        """只注入父 Agent handler 表，与 CRON_TOOLS 的三个 schema 一一对应。"""

        return {
            "schedule_cron": self.run_schedule_cron,
            "list_crons": self.run_list_crons,
            "cancel_cron": self.run_cancel_cron,
        }

    def _scheduler_loop(self) -> None:
        # 用 Event.wait 而不是 sleep：它同时是轮询间隔和退出信号，stop() 能立即
        # 唤醒线程，不必等完剩余间隔。
        while not self._stop.wait(self.poll_interval):
            self.poll_due_jobs()

    def start(self) -> None:
        """幂等启动持久化恢复和调度线程。"""

        with self.lock:
            if self._thread is not None and self._thread.is_alive():
                return
            # 先恢复再起线程：轮询不会看到只加载了一半的任务表。
            self.load_durable_jobs()
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._scheduler_loop,
                name="cron-scheduler",
                # daemon 让 CLI 退出不被轮询拖住；任务定义已在磁盘上。
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """停止轮询线程；内存任务与磁盘定义都保留。"""

        with self.lock:
            thread = self._thread
            self._thread = None
            self._stop.set()
        # join 放在锁外：否则正在 poll 的线程拿不到锁，两边互相等待。
        if thread is not None:
            thread.join(timeout=max(1.0, self.poll_interval * 2))

    @property
    def is_running(self) -> bool:
        # 给 CLI 与测试断言用：只反映轮询线程，与队列是否为空无关。
        with self.lock:
            return self._thread is not None and self._thread.is_alive()
