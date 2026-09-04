"""s15 后台工具执行、生命周期状态和完成通知。"""

from __future__ import annotations

import html
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any


# 状态名同时出现在通知正文、终态判定和测试断言里，因此固定为模块常量。
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
# 只有终态任务会被收集；running 的任务留在表里等下一个模型边界。
TERMINAL_STATUSES = {COMPLETED, FAILED}
# 通知只带摘要，一份完整的构建日志就足以把上下文顶满。
DEFAULT_SUMMARY_CHARS = 1_000

# 教学版只把“可能明显超过几十秒”的 Bash 命令作为启发式兜底。
# 模型显式传入 run_in_background 时，该布尔值始终优先于关键词判断。
SLOW_COMMAND_HINTS = (
    "install",
    "build",
    "test",
    "deploy",
    "compile",
    "docker build",
    "pip install",
    "npm install",
    "cargo build",
    "pytest",
    "make",
)


@dataclass(frozen=True)
class BackgroundTask:
    """一个进程内后台任务的不可变状态快照。"""

    id: str
    # 只用于内部诊断：通知不公开它，避免后续代码把通知重新按 ID
    # 配对成第二个 tool result。
    original_tool_call_id: str
    tool_name: str
    command: str
    status: str
    result: str = ""
    started_at: float = 0.0
    # None 表示仍在运行；worker 结束时才连同状态一起写入新快照。
    finished_at: float | None = None


class BackgroundTaskManager:
    """在线程中执行已批准的工具调用，并一次性投递完成通知。"""

    def __init__(
        self,
        *,
        summary_chars: int = DEFAULT_SUMMARY_CHARS,
        time_fn: Callable[[], float] = time.monotonic,
    ):
        # 0 或负数会让摘要恒为空，属于配置错误而不是边界情况。
        if summary_chars < 1:
            raise ValueError("summary_chars must be positive")
        self.summary_chars = summary_chars
        # 时钟可注入，测试不必真的等待就能断言耗时与完成顺序。
        self._time_fn = time_fn
        # 三张表共用同一把锁：ID 计数、任务快照和线程句柄总是一起变化，
        # 任何一次读改写都应当在同一个临界区内完成。
        self._lock = threading.Lock()
        self._counter = 0
        self._tasks: dict[str, BackgroundTask] = {}
        self._threads: dict[str, threading.Thread] = {}

    @staticmethod
    def is_slow_operation(tool_name: str, arguments: dict[str, Any]) -> bool:
        """判断未显式选择执行方式的调用是否适合后台运行。"""

        if tool_name != "bash":
            return False
        command = arguments.get("command")
        if not isinstance(command, str):
            return False
        # 纯子串匹配会误判 git test-branch 这类命令，代价只是多一条通知。
        lowered = command.lower()
        return any(hint in lowered for hint in SLOW_COMMAND_HINTS)

    @classmethod
    def should_run_background(
        cls,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> bool:
        """显式布尔值优先；未提供时才使用慢命令启发式。"""

        # 只有 Bash 有后台形态；其余工具即便带上该字段也走同步路径。
        if tool_name != "bash":
            return False
        if "run_in_background" in arguments:
            # 用 is True 而不是真值判断：“false”、1 这类畸形值退回同步执行，
            # 不会意外产生一个模型并不打算追踪的后台任务。
            return arguments["run_in_background"] is True
        return cls.is_slow_operation(tool_name, arguments)

    def _next_id(self) -> str:
        # ID 分配与任务注册共用同一把锁，多个模型调用并发分发时也不会重复。
        self._counter += 1
        return f"bg_{self._counter:04d}"

    def start(
        self,
        *,
        original_tool_call_id: str,
        tool_name: str,
        command: str,
        runner: Callable[[], str],
    ) -> str:
        """启动 daemon worker，并立即返回可供模型追踪的 bg_id。"""

        # 这两个检查面向调用方而不是模型：模型的畸形输入在
        # should_run_background 就已经退回同步执行，根本到不了这里，
        # 所以用异常暴露 Harness 内部约定被破坏，而不是返回 Error: 文本。
        if tool_name != "bash":
            raise ValueError("only bash can run in the background")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("background command must be a non-empty string")

        # 先在锁内登记记录和线程，再到锁外启动：worker 一旦跑完就要写回
        # self._tasks，那个条目必须已经存在。
        with self._lock:
            bg_id = self._next_id()
            task = BackgroundTask(
                id=bg_id,
                original_tool_call_id=original_tool_call_id,
                tool_name=tool_name,
                command=command,
                status=RUNNING,
                started_at=self._time_fn(),
            )
            self._tasks[bg_id] = task

            def worker() -> None:
                status = COMPLETED
                try:
                    result = str(runner())
                except Exception as exc:  # worker 不能把异常泄漏到线程边界之外
                    status = FAILED
                    result = (
                        "Error: background task raised "
                        f"{type(exc).__name__}: {exc}"
                    )
                # 先采样再取锁，等锁的时间不会被算进任务耗时。
                finished_at = self._time_fn()
                with self._lock:
                    current = self._tasks.get(bg_id)
                    # collect 只会移除终态任务；正常情况下 current 一定存在。
                    if current is not None:
                        self._tasks[bg_id] = replace(
                            current,
                            status=status,
                            result=result,
                            finished_at=finished_at,
                        )

            thread = threading.Thread(
                target=worker,
                # 线程名带 bg_id：既方便诊断，也让测试能断言哪个 Hook
                # 运行在父线程、哪个运行在 worker 线程。
                name=f"cc-background-{bg_id}",
                # daemon 让 CLI 不被未完成的命令拖住，代价是退出即丢任务。
                daemon=True,
            )
            self._threads[bg_id] = thread

        # start() 必须在锁外：worker 结束时要重新获得这把不可重入的锁。
        thread.start()
        print(f"\033[33m[background] dispatched {bg_id}: {command[:80]}\033[0m")
        return bg_id

    @staticmethod
    def placeholder(bg_id: str, command: str) -> str:
        """完成原始 tool_call 配对；真实结果稍后以独立通知进入消息历史。"""

        # 同时给出 bg_id 和命令原文，模型才知道稍后会有一条通知，
        # 而不是把这条占位结果当成命令失败并重跑一次。
        return (
            f"[Background task {bg_id} started] Command: {command}. "
            "Result will be delivered as a task_notification when complete."
        )

    def _result_summary(self, result: str) -> str:
        # 截断只影响通知文本，task.result 仍保存完整输出。
        if len(result) <= self.summary_chars:
            return result
        omitted = len(result) - self.summary_chars
        return (
            result[: self.summary_chars]
            + f"\n... ({omitted} chars omitted from notification)"
        )

    def _format_notification(self, task: BackgroundTask) -> str:
        # 命令和输出都来自不可信边界，先转义再放入 XML，避免伪造闭合标签。
        return (
            "<task_notification>\n"
            f"  <task_id>{html.escape(task.id)}</task_id>\n"
            f"  <status>{html.escape(task.status)}</status>\n"
            f"  <command>{html.escape(task.command)}</command>\n"
            f"  <summary>{html.escape(self._result_summary(task.result))}</summary>\n"
            "</task_notification>"
        )

    def collect_notifications(self) -> list[str]:
        """原子取走所有终态任务；同一完成事件最多投递一次。"""

        # 锁内只做字典操作：移除即代表“已投递”，因此不需要额外的
        # notified 标记，紧邻的两次调用也只有第一次能拿到同一个任务。
        with self._lock:
            ready_ids = sorted(
                bg_id
                for bg_id, task in self._tasks.items()
                if task.status in TERMINAL_STATUSES
            )
            ready = [self._tasks.pop(bg_id) for bg_id in ready_ids]
            for bg_id in ready_ids:
                self._threads.pop(bg_id, None)

        # 渲染和打印放在锁外，持锁时间与输出长度无关。
        notifications = []
        for task in ready:
            notifications.append(self._format_notification(task))
            print(
                "\033[32m[background done] "
                f"{task.id}: {task.command[:80]} "
                f"({len(task.result)} chars, {task.status})\033[0m"
            )
        return notifications

    def snapshot(self) -> tuple[BackgroundTask, ...]:
        """返回排序后的不可变快照，供状态展示和测试读取。"""

        # 已投递的任务在 collect 时就离开了字典，这里看不到它们。
        with self._lock:
            return tuple(self._tasks[key] for key in sorted(self._tasks))

    def wait(self, bg_id: str, timeout: float | None = None) -> bool:
        """等待指定 worker，主要用于受控关闭和确定性测试。"""

        # 锁内只取句柄，join 放在锁外，否则等待期间没人能更新任务表。
        with self._lock:
            thread = self._threads.get(bg_id)
        # 线程缺失有两种原因：从未存在，或已被 collect 清理；对调用方
        # 而言都意味着没有需要继续等待的 worker。
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def wait_for_all(self, timeout: float | None = None) -> bool:
        """在共享总超时内等待当前所有 worker，不启动轮询线程。"""

        # 先算统一 deadline，再逐个 join 剩余时间：总等待受同一个 timeout
        # 约束，而不是每个线程各等一遍。
        deadline = None if timeout is None else self._time_fn() + timeout
        with self._lock:
            threads = list(self._threads.values())
        for thread in threads:
            remaining = (
                None
                if deadline is None
                else max(0.0, deadline - self._time_fn())
            )
            thread.join(remaining)
        return all(not thread.is_alive() for thread in threads)

    @property
    def running_count(self) -> int:
        # 统计的是仍未被收集的运行中任务，已完成的记录此时已经被取走。
        with self._lock:
            return sum(task.status == RUNNING for task in self._tasks.values())
