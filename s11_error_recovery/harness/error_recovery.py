"""s11 错误分类、退避重试和有界恢复状态。"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


DEFAULT_MAX_TOKENS = 8_000
ESCALATED_MAX_TOKENS = 64_000
MAX_CONTINUATIONS = 3
MAX_TRANSIENT_ATTEMPTS = 10
BASE_DELAY_SECONDS = 0.5
MAX_DELAY_SECONDS = 32.0
MAX_CONSECUTIVE_529 = 3
CONTINUATION_PROMPT = (
    "Output token limit hit. Resume directly — no apology, no recap of what "
    "you were doing. Pick up mid-thought if that is where the cut happened. "
    "Break remaining work into smaller pieces."
)


class MaxRetriesExceeded(RuntimeError):
    """瞬态错误超过最大尝试次数。"""


@dataclass
class RecoveryState:
    """记录当前 user turn 已经使用过哪些恢复动作。"""

    current_model: str
    max_tokens: int = DEFAULT_MAX_TOKENS
    has_escalated: bool = False
    continuation_count: int = 0
    consecutive_529: int = 0
    has_attempted_reactive_compact: bool = False


def error_status_code(error: Exception) -> int | None:
    """兼容 OpenAI SDK 异常本身或 response 上的状态码。"""

    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def is_rate_limit_error(error: Exception) -> bool:
    name = type(error).__name__.lower()
    text = str(error).lower()
    return (
        error_status_code(error) == 429
        or "ratelimit" in name
        or "rate limit" in text
        or "429" in text
    )


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
    return any(
        marker in text
        for marker in (
            "prompt_too_long",
            "prompt_is_too_long",
            "too many tokens",
            "maximum context length",
            "context_length_exceeded",
            "max_context_window",
            "request too large",
            "http 413",
        )
    )


def is_output_truncated(finish_reason: str | None) -> bool:
    """OpenAI 兼容接口通常返回 length，部分供应商返回 max_tokens。"""

    return finish_reason in {"length", "max_tokens"}


def retry_after_seconds(error: Exception) -> float | None:
    """读取服务端 Retry-After 秒数；无法解析时退回本地退避。"""

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
    """计算 0.5s 起步、32s 封顶并带 0–25% 抖动的退避时间。"""

    if retry_after is not None:
        return retry_after
    base = min(BASE_DELAY_SECONDS * (2**attempt), MAX_DELAY_SECONDS)
    return base + random_fn(0.0, base * 0.25)


def with_retry(
    fn: Callable[[str], Any],
    state: RecoveryState,
    fallback_model: str | None = None,
    *,
    max_attempts: int = MAX_TRANSIENT_ATTEMPTS,
    sleep_fn: Callable[[float], None] = time.sleep,
    random_fn: Callable[[float, float], float] = random.uniform,
) -> Any:
    """只重试 429/529；其他异常立即交给 Agent Loop 外层分类。"""

    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")

    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            result = fn(state.current_model)
            state.consecutive_529 = 0
            return result
        except Exception as error:
            rate_limited = is_rate_limit_error(error)
            overloaded = is_overloaded_error(error)
            if not (rate_limited or overloaded):
                raise
            last_error = error

            if overloaded:
                state.consecutive_529 += 1
                if state.consecutive_529 >= MAX_CONSECUTIVE_529:
                    if fallback_model and state.current_model != fallback_model:
                        state.current_model = fallback_model
                        print(
                            "\033[31m[529 overloaded] "
                            f"switching to fallback model {fallback_model}\033[0m"
                        )
                    state.consecutive_529 = 0
            else:
                # 429 打断连续 529 序列，不能跨错误类型累计 fallback 阈值。
                state.consecutive_529 = 0

            # 最后一次失败后直接报 exhausted，不做一次没有后续请求的无效 sleep。
            if attempt + 1 >= max_attempts:
                break
            delay = retry_delay(
                attempt,
                retry_after_seconds(error),
                random_fn=random_fn,
            )
            label = "429 rate limit" if rate_limited else "529 overloaded"
            print(
                f"\033[33m[{label}] retry {attempt + 1}/{max_attempts}, "
                f"wait {delay:.2f}s\033[0m"
            )
            sleep_fn(delay)

    raise MaxRetriesExceeded(
        f"transient retry limit reached after {max_attempts} attempts"
    ) from last_error


def format_unrecoverable_error(error: Exception) -> str:
    """生成可回写消息历史的有界错误文本，避免异常内容无限膨胀。"""

    detail = str(error).strip() or "no additional details"
    if len(detail) > 1_000:
        detail = detail[:1_000] + "..."
    return f"Error: {type(error).__name__}: {detail}"
