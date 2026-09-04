"""Alibaba Cloud Bailian client construction."""

from __future__ import annotations

import os
from typing import Any

from openai import OpenAI

from .config import Settings


def create_client(settings: Settings) -> OpenAI:
    """Create an OpenAI-compatible client without logging credentials."""
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Missing DASHSCOPE_API_KEY. Export it or place it in a local .env file."
        )
    return OpenAI(api_key=api_key, base_url=settings.base_url, timeout=150.0)


def assistant_payload(assistant: Any) -> dict[str, Any]:
    """Serialize an OpenAI-compatible assistant message."""
    payload = assistant.model_dump(exclude_none=True)
    reasoning = getattr(assistant, "reasoning_content", None)
    if reasoning:
        payload["reasoning_content"] = reasoning
    return payload


def completion_request(
    settings: Settings,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    model: str | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Build one provider request, including model-specific extensions."""
    selected_model = model or settings.model
    request: dict[str, Any] = {
        "model": selected_model,
        "messages": messages,
        "tools": tools,
    }
    if max_tokens is not None:
        request["max_tokens"] = max_tokens
    # 将供应商和模型特有参数收口，避免父子循环感知这些差异。
    if selected_model == "kimi/kimi-k3":
        request["extra_body"] = {"reasoning_effort": "max"}
    return request
