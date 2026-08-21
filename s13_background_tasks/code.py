#!/usr/bin/env python3
"""s13 CLI entrypoint; implementation lives in the local ``harness`` package."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


MODULE_DIR = Path(__file__).resolve().parent
REPO_ROOT = MODULE_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s13_background_tasks.harness.agent import AgentHarness  # noqa: E402
from s13_background_tasks.harness.config import Settings, create_client  # noqa: E402
from s13_background_tasks.harness.tasks import (  # noqa: E402
    TASK_ID_PATTERN,
    TASK_TOOLS,
    TaskManager,
    TaskRecord,
    TaskStore,
)
from s13_background_tasks.harness.tools import (  # noqa: E402
    PARENT_TOOLS as TOOLS,
    SUB_TOOLS,
)


def main() -> None:
    settings = Settings.from_env()
    client = create_client(settings)
    harness = AgentHarness(client, settings)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": harness.system_prompt}
    ]

    print("s13: Background Tasks (Alibaba Cloud Bailian)")
    print(
        f"model={settings.model} workdir={settings.workdir} "
        f"skills={len(harness.skills.registry)}"
    )
    print("输入任务，回车发送；输入 q、exit 或空行退出。\n")

    try:
        while True:
            try:
                query = input("\033[36ms13 >> \033[0m")
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
    finally:
        harness.close()


if __name__ == "__main__":
    main()
