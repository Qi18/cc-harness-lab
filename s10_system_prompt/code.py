#!/usr/bin/env python3
"""s10 CLI entrypoint: runtime-assembled system prompts."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s10_system_prompt.harness.agent_loop import AgentHarness  # noqa: E402
from s10_system_prompt.harness.config import Settings  # noqa: E402
from s10_system_prompt.harness.provider import create_client  # noqa: E402
from s10_system_prompt.harness.system_prompt import SystemPromptAssembler  # noqa: E402
from s10_system_prompt.harness.tool_use import (  # noqa: E402
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

    print("s10: Runtime System Prompt (Alibaba Cloud Bailian)")
    print(
        f"model={settings.model} workdir={settings.workdir} "
        f"sections={','.join(harness.parent_prompt.last_sections)}"
    )
    print("输入任务，回车发送；输入 q、exit 或空行退出。\n")

    while True:
        try:
            query = input("\033[36ms10 >> \033[0m")
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
