"""Composable building blocks for the s12 persistent-task Agent Harness."""

from .agent_loop import AgentHarness
from .config import Settings
from .error_recovery import MaxRetriesExceeded, RecoveryState
from .provider import create_client
from .subagent import SubagentRunner
from .system_prompt import SystemPromptAssembler
from .task_system import TaskManager, TaskRecord
from .todo_write import TodoManager
from .tool_use import ToolExecutor

__all__ = [
    "AgentHarness",
    "MaxRetriesExceeded",
    "RecoveryState",
    "Settings",
    "SubagentRunner",
    "SystemPromptAssembler",
    "TaskManager",
    "TaskRecord",
    "TodoManager",
    "ToolExecutor",
    "create_client",
]
