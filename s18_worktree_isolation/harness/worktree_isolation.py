"""s18 Git worktree 生命周期与队友工作目录上下文。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import Settings
from .task_system import TaskManager, TaskRecord, TaskError, _task_tool

WORKTREE_TOOLS = [
    _task_tool(
        "create_worktree", "Create a separate branch and worktree, optionally bind a pending task.",
        {"name": {"type": "string"}, "task_id": {"type": "string"}}, ["name"],
    ),
    _task_tool(
        "keep_worktree", "Keep a managed worktree and branch for later review.",
        {"name": {"type": "string"}}, ["name"],
    ),
    _task_tool(
        "remove_worktree",
        "Remove an inactive managed worktree. Refuse changes unless discard_changes is true.",
        {"name": {"type": "string"}, "discard_changes": {"type": "boolean"}}, ["name"],
    ),
]


def validate_worktree_name(name: str) -> str:
    """名称只能是单段，禁止路径穿越、选项注入和 Git 的特殊引用形式。"""

    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}", name)
        or ".." in name
        or name.endswith((".", ".lock"))
    ):
        raise ValueError("invalid worktree name")
    return name


class WorktreeManager:
    """只管理自己创建并登记的 worktree，不操作主目录或任意 Git 工作区。"""

    def __init__(self, settings: Settings, tasks: TaskManager):
        self.root = settings.workdir.resolve()
        self.directory = self.root / ".worktrees"
        settings.assert_inside_workdir(self.directory, "worktree directory")
        self.index_path = self.directory / "index.json"
        self.tasks = tasks
        self._lock = threading.RLock()
        # 活跃上下文是删除屏障：complete_task 后仍可能有本周期的文件操作。
        self._users: dict[str, set[str]] = {}

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["git", *args], cwd=cwd or self.root, capture_output=True,
            text=True, timeout=60,
        )
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip())
        return result.stdout.strip()

    def _path(self, name: str) -> Path:
        validate_worktree_name(name)
        path = self.directory / name
        # 拒绝改成符号链接的容器或目录；删除目标永远是受控目录的直接子项。
        if self.directory.is_symlink() or self.directory.resolve() != self.directory:
            raise ValueError("worktree directory changed or is a symlink")
        if path.is_symlink() or path.resolve().parent != self.directory:
            raise ValueError("worktree path escapes managed directory")
        return path

    def _index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return {}
        value = json.loads(self.index_path.read_text())
        if not isinstance(value, dict):
            raise ValueError("invalid worktree index")
        return value

    def _save_index(self, index: dict[str, Any]) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.directory / f".index-{os.getpid()}-{threading.get_ident()}.tmp"
        try:
            with temporary.open("w") as stream:
                os.chmod(temporary, 0o600)
                json.dump(index, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.index_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _event(self, kind: str, name: str, task_id: str = "") -> None:
        with (self.directory / "events.jsonl").open("a") as stream:
            os.chmod(stream.name, 0o600)
            stream.write(json.dumps({
                "type": kind, "worktree": name, "task_id": task_id, "ts": time.time(),
            }) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _record(self, name: str) -> tuple[Path, dict[str, Any]]:
        path = self._path(name)
        record = self._index().get(name)
        if not isinstance(record, dict) or not isinstance(record.get("base"), str):
            raise ValueError("worktree is not managed by this harness")
        # 索引不是唯一凭据，还要与 Git 登记、当前分支和仓库归属互相核对。
        registered = self._git("-c", "core.quotePath=false", "worktree", "list", "--porcelain").splitlines()
        if f"worktree {path}" not in registered:
            raise ValueError("worktree is missing from Git registration")
        if self._git("symbolic-ref", "--short", "HEAD", cwd=path) != f"wt/{name}":
            raise ValueError("worktree branch changed; refusing operation")
        common = Path(self._git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=path))
        original = Path(self._git("rev-parse", "--path-format=absolute", "--git-common-dir"))
        if common.resolve() != original.resolve():
            raise ValueError("worktree belongs to another repository")
        return path, record

    def create_worktree(self, name: str, task_id: str = "") -> str:
        with self._lock, self.tasks.transaction():
            try:
                path = self._path(name)
                top = Path(self._git("rev-parse", "--show-toplevel")).resolve()
                if top != self.root:
                    raise ValueError("workdir must be the repository root")
                index = self._index()
                if name in index or path.exists():
                    raise ValueError("worktree name or directory already exists")
                if task_id:
                    task = self.tasks._load(task_id)
                    if task.status != "pending" or task.worktree:
                        raise ValueError("only an unbound pending task can be bound")
                base = self._git("rev-parse", "--verify", "HEAD")
                self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                # 使用 argv，不拼接 shell；新分支从当前已提交 HEAD 创建。
                self._git("worktree", "add", "-b", f"wt/{name}", str(path), base)
                try:
                    index[name] = {"base": base, "task_id": task_id}
                    self._save_index(index)
                    if task_id:
                        self.tasks.bind_task_to_worktree(task_id, name)
                except Exception:
                    # 新目录尚未交给队友；若已发生改动，Git 默认 remove 会拒绝。
                    self._git("worktree", "remove", str(path))
                    self._git("branch", "-D", f"wt/{name}")
                    index.pop(name, None)
                    self._save_index(index)
                    raise
                self._event("create", name, task_id)
                return f"Worktree {name!r} created at {path} (branch: wt/{name})"
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
                return f"Error: {error}"

    def bind_task_to_worktree(self, task_id: str, name: str) -> str:
        """内部绑定入口；不推进任务状态，也不自动认领。"""

        with self._lock:
            try:
                self._record(name)
                self.tasks.bind_task_to_worktree(task_id, name)
                return f"Bound {task_id} to {name}"
            except (OSError, ValueError, RuntimeError) as error:
                return f"Error: {error}"

    def keep_worktree(self, name: str) -> str:
        with self._lock:
            try:
                self._record(name)
                self._event("keep", name)
                return f"Worktree {name!r} kept for review (branch: wt/{name})"
            except (OSError, ValueError, RuntimeError) as error:
                return f"Error: {error}"

    def remove_worktree(self, name: str, discard_changes: bool = False) -> str:
        with self._lock, self.tasks.transaction():
            try:
                if type(discard_changes) is not bool:
                    raise ValueError("discard_changes must be a boolean")
                path, record = self._record(name)
                if self._users.get(name):
                    raise ValueError("worktree is in use by a teammate")
                bound = [t for t in self.tasks._all() if t.worktree == name]
                if any(t.status == "in_progress" for t in bound):
                    raise ValueError("worktree has an in-progress task")
                # 连 ignored 文件也计入：默认删除不能悄悄丢掉未跟踪的产物。
                dirty = self._git("status", "--porcelain", "--untracked-files=all", "--ignored", cwd=path)
                commits = int(self._git("rev-list", "--count", f"{record['base']}..HEAD", cwd=path))
                if not discard_changes and (dirty or commits):
                    raise ValueError("worktree has file changes or new commits; keep it or explicitly discard_changes=true")
                args = ["worktree", "remove"]
                if discard_changes:
                    args.append("--force")
                self._git(*args, str(path))
                # 非强制路径已确认没有相对创建点的新提交；不会触及任意分支名。
                self._git("branch", "-D", f"wt/{name}")
                self.tasks.unbind_worktree(name)
                index = self._index()
                index.pop(name, None)
                self._save_index(index)
                self._event("remove", name)
                return f"Removed worktree {name!r}; task statuses unchanged"
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
                return f"Error: {error}"

    def acquire(self, name: str, actor: str) -> Path:
        with self._lock:
            path, _ = self._record(name)
            self._users.setdefault(name, set()).add(actor)
            return path

    def switch(self, name: str | None, actor: str) -> Path:
        """校验、释放旧引用和获取新引用在同一个锁内，删除不能插入其间。"""

        with self._lock:
            path = self._record(name)[0] if name else self.root
            self.release(actor)
            if name:
                self._users.setdefault(name, set()).add(actor)
            return path

    def release(self, actor: str) -> None:
        with self._lock:
            for users in self._users.values():
                users.discard(actor)

    def handlers(self):
        return {
            "create_worktree": self.create_worktree,
            "keep_worktree": self.keep_worktree,
            "remove_worktree": self.remove_worktree,
        }


class WorktreeContext:
    """一名队友的局部 cwd；禁止 os.chdir 改变其他线程的工作目录。"""

    def __init__(self, manager, actor, settings, executor, file_handlers):
        self.manager = manager
        self.actor = actor
        self.settings = settings
        self.base_executor = executor
        self.base_handlers = file_handlers
        self.executor = executor
        self.file_handlers = file_handlers
        self.path = settings.workdir

    def select(self, task: TaskRecord) -> None:
        # 缺失绑定目录时直接失败，不回退到主目录执行本应隔离的任务。
        path = self.manager.switch(task.worktree, self.actor)
        from .hooks import HookManager, install_default_hooks
        from .skill_loading import SkillLoader
        from .tool_use import BuiltinTools, ToolExecutor

        if task.worktree:
            local = replace(self.settings, workdir=path)
            hooks = HookManager()
            install_default_hooks(hooks, local)
            self.executor = ToolExecutor(hooks)
            builtins = BuiltinTools(local, SkillLoader(self.settings.skills_dir))
            self.file_handlers = {
                name: handler for name, handler in builtins.handlers().items()
                if name in {"bash", "read_file", "write_file"}
            }
        else:
            self.executor = self.base_executor
            self.file_handlers = self.base_handlers
        self.path = path

    def close(self):
        self.manager.release(self.actor)
