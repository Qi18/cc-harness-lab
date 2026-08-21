"""Tracked shell processes, background workers, and one-shot notifications."""

from __future__ import annotations

import atexit
import html
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path


MAX_OUTPUT_CHARS = 50_000


def format_shell_result(output: str, exit_code: int | None) -> str:
    if exit_code in (0, None):
        return output
    return f"Error: command exited with status {exit_code}\n{output}"


class ShellExecutor:
    """Run commands in isolated process groups and clean up descendants."""

    def __init__(self, workdir: Path, timeout: float = 120.0):
        self.workdir = workdir
        self.timeout = timeout
        self._processes: set[subprocess.Popen] = set()
        self._lock = threading.RLock()
        self._closed = False
        atexit.register(self.close)

    @staticmethod
    def _stop_process_group(process: subprocess.Popen) -> None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except (ProcessLookupError, OSError):
                return
            time.sleep(0.05)

    def run(self, command: str) -> tuple[str, int | None]:
        if not isinstance(command, str) or not command.strip():
            return "Error: command must be a non-empty string", None
        with self._lock:
            if self._closed:
                return "Error: shell executor is closed", None
        process: subprocess.Popen | None = None
        try:
            process = subprocess.Popen(
                command,
                shell=True,
                cwd=self.workdir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            with self._lock:
                if self._closed:
                    self._stop_process_group(process)
                    return "Error: shell executor is closed", None
                self._processes.add(process)
            stdout, stderr = process.communicate(timeout=self.timeout)
            output = (stdout + stderr).strip() or "(no output)"
            if len(output) > MAX_OUTPUT_CHARS:
                output = output[:MAX_OUTPUT_CHARS] + "\n... (output truncated)"
            return output, process.returncode
        except subprocess.TimeoutExpired:
            if process is not None:
                self._stop_process_group(process)
            return f"Error: command timed out after {self.timeout:g} seconds", None
        except OSError as exc:
            return f"Error: {type(exc).__name__}: {exc}", None
        finally:
            if process is not None:
                self._stop_process_group(process)
                try:
                    process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    pass
                with self._lock:
                    self._processes.discard(process)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            processes = list(self._processes)
        for process in processes:
            self._stop_process_group(process)


@dataclass
class BackgroundTask:
    id: str
    tool_call_id: str
    command: str
    status: str = "running"
    result: str = ""


class BackgroundManager:
    """Run explicit Bash calls on daemon threads and collect each result once."""

    def __init__(self, shell: ShellExecutor):
        self.shell = shell
        self.tasks: dict[str, BackgroundTask] = {}
        self._ready: list[str] = []
        self._threads: set[threading.Thread] = set()
        self._counter = 0
        self._lock = threading.RLock()
        self._closed = False

    def start(self, command: str, tool_call_id: str = "") -> str:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("Bash command cannot be empty")
        with self._lock:
            if self._closed:
                raise RuntimeError("Background manager is closed")
            self._counter += 1
            task_id = f"bg_{self._counter:04d}"
            self.tasks[task_id] = BackgroundTask(
                id=task_id,
                tool_call_id=tool_call_id,
                command=command,
            )
            thread = threading.Thread(
                target=self._run,
                args=(task_id,),
                daemon=True,
                name=f"cc-{task_id}",
            )
            self._threads.add(thread)
        try:
            thread.start()
        except Exception:
            with self._lock:
                self._threads.discard(thread)
                self.tasks.pop(task_id, None)
            raise
        print(f"  [background] started {task_id}: {command[:60]}")
        return task_id

    def _run(self, task_id: str) -> None:
        current = threading.current_thread()
        with self._lock:
            task = self.tasks.get(task_id)
        if task is None:
            return
        try:
            output, exit_code = self.shell.run(task.command)
            result = format_shell_result(output, exit_code)
            status = "completed" if exit_code == 0 else "failed"
        except Exception as exc:
            result = f"Error: {type(exc).__name__}: {exc}"
            status = "failed"
        with self._lock:
            task = self.tasks.get(task_id)
            if task is not None:
                task.status = status
                task.result = result
                self._ready.append(task_id)
            self._threads.discard(current)

    def collect(self) -> list[str]:
        with self._lock:
            ready = []
            for task_id in self._ready:
                task = self.tasks.pop(task_id, None)
                if task is not None:
                    ready.append(task)
            self._ready.clear()
        notifications = []
        for task in ready:
            notifications.append(
                "<task_notification>\n"
                f"  <task_id>{html.escape(task.id)}</task_id>\n"
                f"  <status>{html.escape(task.status)}</status>\n"
                f"  <command>{html.escape(task.command)}</command>\n"
                f"  <summary>{html.escape(task.result[:500])}</summary>\n"
                "</task_notification>"
            )
            print(f"  [background] collected {task.id}: {task.status}")
        return notifications

    def inject(self, messages: list[dict[str, object]]) -> int:
        notifications = self.collect()
        if not notifications:
            return 0
        payload = "\n\n".join(notifications)
        if (
            messages
            and messages[-1].get("role") == "user"
            and isinstance(messages[-1].get("content"), str)
        ):
            content = str(messages[-1]["content"])
            messages[-1]["content"] = f"{content}\n\n{payload}"
        else:
            messages.append({"role": "user", "content": payload})
        return len(notifications)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            threads = list(self._threads)
        self.shell.close()
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=0.2)
