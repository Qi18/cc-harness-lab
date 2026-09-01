"""Two-level skill discovery and on-demand loading."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import SkillRecord


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return {}, text
    closing = next(
        (
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.rstrip("\r\n") == "---"
        ),
        None,
    )
    if closing is None:
        return {}, text
    try:
        metadata = yaml.safe_load("".join(lines[1:closing])) or {}
    except yaml.YAMLError:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return metadata, "".join(lines[closing + 1 :]).strip()


def first_content_line(body: str) -> str:
    for line in body.splitlines():
        candidate = line.strip().lstrip("#").strip()
        if candidate:
            return " ".join(candidate.split())
    return "No description provided."


class SkillLoader:
    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir
        self.registry = self.scan()

    def scan(self) -> dict[str, SkillRecord]:
        registry: dict[str, SkillRecord] = {}
        if not self.skills_dir.is_dir():
            return registry
        root = self.skills_dir.resolve()
        for directory in sorted(self.skills_dir.iterdir(), key=lambda path: path.name):
            manifest = directory / "SKILL.md"
            if not directory.is_dir() or not manifest.is_file():
                continue
            try:
                # resolve 后再次检查根目录，阻止符号链接把 SKILL.md 指向目录外。
                resolved = manifest.resolve()
                resolved.relative_to(root)
                raw = resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeError, ValueError):
                continue
            metadata, body = parse_frontmatter(raw)
            raw_name = metadata.get("name")
            name = raw_name.strip() if isinstance(raw_name, str) else ""
            name = name or directory.name
            raw_description = metadata.get("description")
            description = (
                raw_description.strip() if isinstance(raw_description, str) else ""
            )
            # 注册表保留完整正文，但 catalog() 只暴露名称和描述。
            registry[name] = {
                "name": name,
                "description": " ".join(description.split())
                or first_content_line(body),
                "content": raw,
                "path": str(resolved),
            }
        return registry

    def catalog(self) -> str:
        if not self.registry:
            return "(no skills found)"
        return "\n".join(
            f"- {record['name']}: {record['description']}"
            for record in self.registry.values()
        )

    def load(self, name: str) -> str:
        if not isinstance(name, str) or not name.strip():
            return "Error: skill name must be a non-empty string"
        record = self.registry.get(name.strip())
        if record is None:
            available = ", ".join(self.registry) or "none"
            return f"Error: skill not found: {name.strip()}. Available: {available}"
        # 只有显式调用 load_skill 时，完整 SKILL.md 才进入模型上下文。
        return record["content"]
