"""Composable building blocks for the s19 MCP Tools harness."""

from .agent_loop import AgentHarness
from .mcp_plugin import MCPClient, MCPManager
from .worktree_isolation import WorktreeManager
from .autonomous_agents import AutonomousWorker
from .agent_teams import AgentTeamManager, MessageBus, TeamMessage, TeammateRecord
from .background_tasks import BackgroundTask, BackgroundTaskManager
from .config import Settings
from .cron_scheduler import CronJob, CronScheduler
from .error_recovery import MaxRetriesExceeded, RecoveryState
from .provider import create_client
from .subagent import SubagentRunner
from .system_prompt import SystemPromptAssembler
from .task_system import TaskManager, TaskRecord
from .team_protocols import ProtocolState, TeamProtocolManager
from .todo_write import TodoManager
from .tool_use import ToolExecutor

__all__ = [
    "AgentHarness",
    "MCPClient",
    "MCPManager",
    "WorktreeManager",
    "AutonomousWorker",
    "AgentTeamManager",
    "BackgroundTask",
    "BackgroundTaskManager",
    "CronJob",
    "CronScheduler",
    "MaxRetriesExceeded",
    "MessageBus",
    "RecoveryState",
    "Settings",
    "SubagentRunner",
    "SystemPromptAssembler",
    "TaskManager",
    "TaskRecord",
    "TeamMessage",
    "ProtocolState",
    "TeamProtocolManager",
    "TeammateRecord",
    "TodoManager",
    "ToolExecutor",
    "create_client",
]
