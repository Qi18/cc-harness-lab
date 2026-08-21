"""Environment-backed runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen-plus"


@dataclass
class Settings:
    """All mutable paths and model settings for one harness instance."""

    workdir: Path
    skills_dir: Path
    transcript_dir: Path
    tool_results_dir: Path
    memory_dir: Path
    tasks_dir: Path
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        workdir = Path(os.getenv("CC_WORKDIR", os.getcwd())).expanduser().resolve()
        return cls(
            workdir=workdir,
            skills_dir=Path(
                os.getenv("CC_SKILLS_DIR", str(workdir / "skills"))
            ).expanduser().resolve(),
            transcript_dir=workdir / ".transcripts",
            tool_results_dir=workdir / ".task_outputs" / "tool-results",
            memory_dir=Path(
                os.getenv("CC_MEMORY_DIR", str(workdir / ".memory"))
            ).expanduser().resolve(),
            tasks_dir=Path(
                os.getenv("CC_TASKS_DIR", str(workdir / ".tasks"))
            ).expanduser().resolve(),
            model=os.getenv("MODEL_ID", DEFAULT_MODEL),
            base_url=os.getenv("DASHSCOPE_BASE_URL", DEFAULT_BASE_URL),
        )

    def assert_inside_workdir(self, path: Path, label: str) -> Path:
        resolved = path.expanduser().resolve()
        try:
            resolved.relative_to(self.workdir.resolve())
        except ValueError as exc:
            raise ValueError(f"{label} escapes the working directory") from exc
        return resolved


def create_client(settings: Settings) -> OpenAI:
    """Create a Bailian OpenAI-compatible client without logging credentials."""
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Missing DASHSCOPE_API_KEY. Export it or place it in a local .env file."
        )
    return OpenAI(api_key=api_key, base_url=settings.base_url, timeout=150.0)
