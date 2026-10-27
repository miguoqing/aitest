"""重试与熔断：区分"值得再试"和"再试一万次也一样"。

三条原则：
1. 只有瞬时错误（超时、连接重置、限流）才重试；参数错、权限错、工具不存在
   立刻停，并把修复建议交回模型——重试解决不了 schema 不匹配。
2. 退避必须带抖动。多个 agent 同时失败时，固定间隔会同步重试，形成重试风暴。
3. 熔断先于重试。某个工具连续失败到阈值就整体熔断，冷却后半开试探，避免
   一个坏掉的 MCP server 拖垮整轮。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from .policy import BreakerPolicy, RetryPolicy

__all__ = [
    "ErrorClass",
    "RetryExhausted",
    "CircuitBreaker",
    "classify_error",
    "compute_delay",
    "run_with_retry",
]


class ErrorClass(str, Enum):
    TRANSIENT = "transient"
    RATE_LIMIT = "rate_limit"
    VALIDATION = "validation"
    PERMISSION = "permission"
    NOT_FOUND = "not_found"
    FATAL = "fatal"
    UNKNOWN = "unknown"


RETRYABLE = {ErrorClass.TRANSIENT, ErrorClass.RATE_LIMIT}

_CLASS_PATTERNS: list[tuple[ErrorClass, tuple[str, ...]]] = [
    (ErrorClass.PERMISSION, ("permissionerror", "permission denied", "eacces", "eperm", "approval required", "not approved")),
    (ErrorClass.NOT_FOUND, ("filenotfounderror", "no such file", "unknown tool", "command not found", "not found", "enoent")),
    (ErrorClass.RATE_LIMIT, ("429", "rate limit", "too many requests", "quota exceeded", "overloaded")),
    (
        ErrorClass.TRANSIENT,
        (
            "timeout", "timed out", "timeouterror", "connectionerror", "connection reset",
            "econnreset", "econnrefused", "epipe", "temporarily unavailable", "server exited",
            "mcp", "502", "503", "504", "broken pipe", "unavailable",
        ),
    ),
    (ErrorClass.VALIDATION, ("validation", "invalid", "schema", "json", "parse", "unexpected token", "argument")),
]


class RetryExhausted(RuntimeError):
    def __init__(
        self,
        reason: str,
        *,
        attempts: int,
        error_class: ErrorClass,
        last_error: BaseException | None = None,
        retryable: bool = False,
        history: list[dict[str, Any]] | None = None,
    ) -> None:
        self.reason = reason
        self.attempts = attempts
        self.error_class = error_class
        self.last_error = last_error
        self.retryable = retryable
        self.history = list(history or [])
        suffix = f"；最后错误: {last_error}" if last_error else ""
        super().__init__(f"重试终止（{reason}/{error_class.value}，共 {attempts} 次）{suffix}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "attempts": self.attempts,
            "error_class": self.error_class.value,
            "retryable": self.retryable,
            "history": self.history,
        }


def classify_error(error: BaseException | str) -> ErrorClass:
    if isinstance(error, RetryExhausted):
        return error.error_class
    if isinstance(error, PermissionError):
        return ErrorClass.PERMISSION
    if isinstance(error, FileNotFoundError):
        return ErrorClass.NOT_FOUND
    if isinstance(error, TimeoutError):
        return ErrorClass.TRANSIENT
    text = f"{type(error).__name__} {error}".lower() if isinstance(error, BaseException) else str(error).lower()
    for error_class, needles in _CLASS_PATTERNS:
        if any(needle in text for needle in needles):
            return error_class
    return ErrorClass.UNKNOWN


@dataclass
class CircuitBreaker:
    """按工具维度的熔断器。closed → open → half_open → closed。"""

    policy: BreakerPolicy
    clock: Callable[[], float] = time.monotonic
    failures: int = 0
    state: str = "closed"
    opened_at: float = 0.0

    def allow(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            if self.clock() - self.opened_at >= self.policy.cooldown_seconds:
                self.state = "half_open"
                return True
            return False
        return True  # half_open 放一个请求过去试探

    def record_success(self) -> None:
        self.failures = 0
        self.state = "closed"
        self.opened_at = 0.0

    def record_failure(self) -> None:
        self.failures += 1
        if self.state == "half_open" or self.failures >= self.policy.failure_threshold:
            self.state = "open"
            self.opened_at = self.clock()


def compute_delay(attempt: int, policy: RetryPolicy, rng: random.Random | None = None) -> float:
    """attempt 从 1 开始。指数退避 + 抖动。"""
    if attempt < 1:
        raise ValueError("attempt 从 1 开始")
    raw = policy.base_delay_seconds * (2 ** (attempt - 1))
    capped = min(raw, policy.max_delay_seconds)
    if policy.jitter_ratio <= 0:
        return capped
    rand = rng or random
    factor = 1.0 + rand.uniform(-policy.jitter_ratio, policy.jitter_ratio)
    return max(0.0, min(capped, capped * factor))


def run_with_retry(
    fn: Callable[[int], Any],
    *,
    policy: RetryPolicy,
    tool_name: str = "tool",
    breaker: CircuitBreaker | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> Any:
    """执行 fn(attempt)，按策略重试；失败抛 RetryExhausted（保留错误分类）。"""
    history: list[dict[str, Any]] = []

    if breaker is not None and not breaker.allow():
        raise RetryExhausted(
            "circuit_open",
            attempts=0,
            error_class=ErrorClass.TRANSIENT,
            retryable=False,
            history=history,
        )

    for attempt in range(1, policy.max_attempts + 1):
        try:
            result = fn(attempt)
        except BaseException as exc:  # noqa: BLE001 - 需要连 KeyboardInterrupt 之外都分类
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            error_class = classify_error(exc)
            retryable = error_class in RETRYABLE
            history.append(
                {
                    "attempt": attempt,
                    "error_class": error_class.value,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            if retryable and breaker is not None:
                breaker.record_failure()

            can_retry = retryable and attempt < policy.max_attempts
            if not can_retry:
                raise RetryExhausted(
                    "retryable_exhausted" if retryable else "non_retryable",
                    attempts=attempt,
                    error_class=error_class,
                    last_error=exc,
                    retryable=retryable,
                    history=history,
                ) from exc

            delay = compute_delay(attempt, policy, rng)
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            sleep(delay)
            continue

        if breaker is not None:
            breaker.record_success()
        return result

    # 理论不可达：max_attempts>=1 且循环内必有 return/raise
    raise RetryExhausted("unreachable", attempts=0, error_class=ErrorClass.UNKNOWN, history=history)
