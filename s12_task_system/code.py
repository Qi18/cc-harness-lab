#!/usr/bin/env python3
"""s12 CLI entrypoint; implementation lives in the local ``harness`` package."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


MODULE_DIR = Path(__file__).resolve().parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

from harness.agent import AgentHarness  # noqa: E402
from harness.config import Settings, create_client  # noqa: E402
from harness.tasks import (  # noqa: E402
    TASK_ID_PATTERN,
    TASK_TOOLS,
    TaskManager,
    TaskRecord,
    TaskStore,
)
from harness.tools import PARENT_TOOLS as TOOLS, SUB_TOOLS  # noqa: E402


def main() -> None:
    settings = Settings.from_env()
    client = create_client(settings)
    harness = AgentHarness(client, settings)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": harness.system_prompt}
    ]

    print("s12: Modular Persistent Task System (Alibaba Cloud Bailian)")
    print(
        f"model={settings.model} workdir={settings.workdir} "
        f"skills={len(harness.skills.registry)}"
    )
    print("输入任务，回车发送；输入 q、exit 或空行退出。\n")

    while True:
        try:
            query = input("\033[36ms12 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if query.strip().lower() in {"", "q", "exit"}:
            break
        harness.hooks.trigger("UserPromptSubmit", query)
        messages.append({"role": "user", "content": query})
        answer = harness.agent_loop(messages, active_request=query)
        if answer:
            print(answer)
        print()


if __name__ == "__main__":
    main()
