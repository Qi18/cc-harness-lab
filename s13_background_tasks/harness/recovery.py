"""Classify model failures and choose bounded recovery actions."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any, Callable


DEFAULT_MAX_TOKENS = 8_000
ESCALATED_MAX_TOKENS = 64_000
MAX_CONTINUATIONS = 3
MAX_TRANSIENT_RETRIES = 10
BASE_DELAY_SECONDS = 0.5
MAX_DELAY_SECONDS = 32.0
MAX_CONSECUTIVE_529 = 3
CONTINUATION_PROMPT = (
    "Output token limit hit. Resume directly — no apology, no recap. "
    "Pick up mid-thought and break remaining work into smaller pieces."
)


class MaxRetriesExceeded(RuntimeError):
    """Raised after the bounded transient retry budget is exhausted."""


@dataclass
class RecoveryState:
    current_model: str
    has_escalated: bool = False
    continuation_count: int = 0
    consecutive_529: int = 0
    has_attempted_reactive_compact: bool = False


def error_status_code(error: Exception) -> int | None:
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def is_rate_limit_error(error: Exception) -> bool:
    name = type(error).__name__.lower()
    text = str(error).lower()
    return error_status_code(error) == 429 or "ratelimit" in name or "429" in text


def is_overloaded_error(error: Exception) -> bool:
    name = type(error).__name__.lower()
    text = str(error).lower()
    return (
        error_status_code(error) == 529
        or "overloaded" in name
        or "overloaded" in text
        or "529" in text
    )


def is_prompt_too_long_error(error: Exception) -> bool:
    text = str(error).lower()
    return (
        ("prompt" in text and "long" in text)
        or "prompt_is_too_long" in text
        or "context_length_exceeded" in text
        or "max_context_window" in text
    )


def retry_after_seconds(error: Exception) -> float | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None) or getattr(error, "headers", None)
    if headers is None:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, value)


def retry_delay(
    attempt: int,
    retry_after: float | None = None,
    random_fn: Callable[[float, float], float] = random.uniform,
) -> float:
    """Exponential backoff with 0-25% jitter; Retry-After wins."""
    if retry_after is not None:
        return retry_after
    base = min(BASE_DELAY_SECONDS * (2**attempt), MAX_DELAY_SECONDS)
    return base + random_fn(0.0, base * 0.25)


def with_retry(
    fn: Callable[[str], Any],
    state: RecoveryState,
    fallback_model: str | None = None,
    *,
    max_retries: int = MAX_TRANSIENT_RETRIES,
    sleep_fn: Callable[[float], None] = time.sleep,
    random_fn: Callable[[float, float], float] = random.uniform,
) -> Any:
    """Retry only 429/529 failures; non-transient errors bubble immediately."""
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            result = fn(state.current_model)
            state.consecutive_529 = 0
            return result
        except Exception as error:
            if not (is_rate_limit_error(error) or is_overloaded_error(error)):
                raise
            last_error = error
            if is_overloaded_error(error):
                state.consecutive_529 += 1
                if (
                    state.consecutive_529 >= MAX_CONSECUTIVE_529
                    and fallback_model
                    and state.current_model != fallback_model
                ):
                    state.current_model = fallback_model
                    state.consecutive_529 = 0
            delay = retry_delay(
                attempt,
                retry_after_seconds(error),
                random_fn=random_fn,
            )
            sleep_fn(delay)
    raise MaxRetriesExceeded(
        f"transient retry limit reached after {max_retries} attempts"
    ) from last_error


def is_output_truncated(finish_reason: str | None) -> bool:
    return finish_reason in {"length", "max_tokens"}
