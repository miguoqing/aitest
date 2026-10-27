import random

import pytest

from governance.policy import BreakerPolicy, RetryPolicy
from governance.retry import CircuitBreaker, ErrorClass, RetryExhausted, classify_error, compute_delay, run_with_retry


def test_transient_error_is_retried_then_succeeds():
    attempts = []
    sleeps = []

    def flaky(attempt):
        attempts.append(attempt)
        if attempt < 3:
            raise TimeoutError("ETIMEDOUT")
        return "ok"

    result = run_with_retry(flaky, policy=RetryPolicy(max_attempts=3), sleep=sleeps.append)
    assert result == "ok"
    assert attempts == [1, 2, 3]
    assert len(sleeps) == 2


def test_validation_error_is_not_retried():
    attempts = []

    def bad(attempt):
        attempts.append(attempt)
        raise ValueError("invalid schema: missing field 'path'")

    with pytest.raises(RetryExhausted) as excinfo:
        run_with_retry(bad, policy=RetryPolicy(max_attempts=5), sleep=lambda _: None)
    assert excinfo.value.error_class is ErrorClass.VALIDATION
    assert excinfo.value.retryable is False
    assert attempts == [1]


def test_exhaustion_reports_attempts_and_history():
    with pytest.raises(RetryExhausted) as excinfo:
        run_with_retry(
            lambda attempt: (_ for _ in ()).throw(ConnectionError("ECONNRESET")),
            policy=RetryPolicy(max_attempts=2, jitter_ratio=0),
            sleep=lambda _: None,
        )
    assert excinfo.value.attempts == 2
    assert len(excinfo.value.history) == 2
    assert excinfo.value.retryable is True


def test_delay_grows_exponentially_and_is_capped():
    policy = RetryPolicy(base_delay_seconds=1, max_delay_seconds=5, jitter_ratio=0)
    assert compute_delay(1, policy) == 1
    assert compute_delay(2, policy) == 2
    assert compute_delay(3, policy) == 4
    assert compute_delay(10, policy) == 5


def test_jitter_stays_within_band():
    policy = RetryPolicy(base_delay_seconds=1, max_delay_seconds=10, jitter_ratio=0.5)
    rng = random.Random(7)
    for _ in range(50):
        delay = compute_delay(1, policy, rng)
        assert 0.5 <= delay <= 1.5


def test_breaker_opens_and_recovers_after_cooldown():
    now = [0.0]
    breaker = CircuitBreaker(BreakerPolicy(failure_threshold=2, cooldown_seconds=10), clock=lambda: now[0])
    breaker.record_failure()
    assert breaker.allow() is True
    breaker.record_failure()
    assert breaker.allow() is False
    now[0] = 11
    assert breaker.allow() is True
    assert breaker.state == "half_open"
    breaker.record_success()
    assert breaker.state == "closed"
    assert breaker.failures == 0


def test_open_breaker_short_circuits_retry():
    now = [0.0]
    breaker = CircuitBreaker(BreakerPolicy(failure_threshold=1, cooldown_seconds=60), clock=lambda: now[0])
    calls = []

    def always_fail(attempt):
        calls.append(attempt)
        raise TimeoutError("timeout")

    with pytest.raises(RetryExhausted) as first:
        run_with_retry(always_fail, policy=RetryPolicy(max_attempts=1), breaker=breaker, sleep=lambda _: None)
    assert first.value.reason == "retryable_exhausted"
    with pytest.raises(RetryExhausted) as second:
        run_with_retry(always_fail, policy=RetryPolicy(max_attempts=1), breaker=breaker, sleep=lambda _: None)
    assert second.value.reason == "circuit_open"
    assert second.value.attempts == 0
    assert calls == [1]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (PermissionError("denied"), ErrorClass.PERMISSION),
        (FileNotFoundError("missing"), ErrorClass.NOT_FOUND),
        (TimeoutError("timed out"), ErrorClass.TRANSIENT),
        ("HTTP 429 rate limit", ErrorClass.RATE_LIMIT),
        ("invalid json argument", ErrorClass.VALIDATION),
        ("MCP server exited with code 1", ErrorClass.TRANSIENT),
        ("something odd", ErrorClass.UNKNOWN),
    ],
)
def test_error_classification(error, expected):
    assert classify_error(error) is expected
