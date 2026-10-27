"""循环护栏：轮次、截止时间、无进度、连续失败、重复调用五道闸。

这个类的唯一职责是**在继续之前说不**。它不执行工具、不调模型，只观察
「第几轮、调了什么、有没有产出新信息、失败了几次」，一旦越过策略阈值就抛
LoopHalted。把判定从提示词挪到代码里，是因为"再试一次"的诱惑在长循环里
永远存在，而模型自己数不清第几次。

典型接法：

    guard = LoopGuard(policy)
    while True:
        guard.begin_round()
        result = call_tool(...)              # 你的工具层
        guard.record_tool_call("read", {"path": p}, ok=result.ok)
        if result.ok:
            guard.mark_progress(result.signature)
        guard.end_round()
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .policy import LoopPolicy, Policy

__all__ = ["LoopHalted", "LoopGuard", "fingerprint"]


def fingerprint(tool: str, arguments: Any = None) -> str:
    """工具名 + 规范化参数 → 稳定指纹。用于识别"换个写法再调一次"。"""
    payload = json.dumps(
        {"tool": tool, "args": arguments},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


class LoopHalted(RuntimeError):
    """护栏判定必须停机。带原因、细节和当时的完整快照，便于写进报告。"""

    def __init__(self, reason: str, detail: str, snapshot: Mapping[str, Any] | None = None) -> None:
        self.reason = reason
        self.detail = detail
        self.snapshot = dict(snapshot or {})
        super().__init__(f"循环已停止[{reason}]: {detail}")

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "detail": self.detail, "snapshot": self.snapshot}


@dataclass
class LoopGuard:
    policy: LoopPolicy
    clock: Callable[[], float] = time.monotonic
    started_at: float | None = None

    round: int = 0
    tool_calls: int = 0
    consecutive_failures: int = 0
    total_failures: int = 0
    no_progress_rounds: int = 0
    halted: bool = False

    counts: Counter = field(default_factory=Counter)
    signatures: set[str] = field(default_factory=set)
    events: list[dict[str, Any]] = field(default_factory=list)

    _round_new_info: bool = False
    _round_started: float = 0.0

    def __post_init__(self) -> None:
        if self.started_at is None:
            self.started_at = self.clock()

    @classmethod
    def from_policy(cls, policy: Policy, **kwargs: Any) -> "LoopGuard":
        return cls(policy=policy.loop, **kwargs)

    # -- 时间 ---------------------------------------------------------------
    @property
    def elapsed(self) -> float:
        return max(0.0, self.clock() - float(self.started_at))

    # -- 轮次 ---------------------------------------------------------------
    def begin_round(self) -> int:
        """进入新一轮。越界即停，不进入。"""
        if self.halted:
            raise LoopHalted("already_halted", "护栏已停机，需人工处理后重置", self.snapshot())
        self._check_deadline()
        if self.round >= self.policy.max_rounds:
            self._halt(
                "max_rounds",
                f"已达到最大轮次 {self.policy.max_rounds}，继续只会重复已有信息",
            )
        if self.no_progress_rounds >= self.policy.no_progress_rounds:
            self._halt(
                "no_progress",
                f"连续 {self.no_progress_rounds} 轮没有产出新信息",
            )
        if self.consecutive_failures >= self.policy.max_consecutive_failures:
            self._halt(
                "consecutive_failures",
                f"连续失败 {self.consecutive_failures} 次，按纪律停止重试",
            )
        if self.tool_calls >= self.policy.max_tool_calls:
            self._halt(
                "max_tool_calls",
                f"工具调用总数达到上限 {self.policy.max_tool_calls}",
            )
        self.round += 1
        self._round_new_info = False
        self._round_started = self.clock()
        self.events.append({"at": self.elapsed, "event": "round_begin", "round": self.round})
        return self.round

    # -- 工具调用 -----------------------------------------------------------
    def record_tool_call(self, tool: str, arguments: Any = None, *, ok: bool = True) -> str:
        """登记一次工具调用。重复调用 / 连续失败 / 超量都在这里熔断。"""
        if self.halted:
            raise LoopHalted("already_halted", "护栏已停机，需人工处理后重置", self.snapshot())

        fp = fingerprint(tool, arguments)
        attempts = self.counts[fp]
        if attempts + 1 >= self.policy.duplicate_call_threshold:
            self._halt(
                "duplicate_call",
                (
                    f"工具 {tool!r} 以相同参数第 {attempts + 1} 次调用；"
                    f"阈值 {self.policy.duplicate_call_threshold} 次，大概率是空转"
                ),
            )
        self.counts[fp] += 1
        self.tool_calls += 1

        if ok:
            self.consecutive_failures = 0
            if fp not in self.signatures:
                self.signatures.add(fp)
                self._round_new_info = True
        else:
            self.consecutive_failures += 1
            self.total_failures += 1

        self.events.append(
            {
                "at": self.elapsed,
                "event": "tool_call",
                "round": self.round,
                "tool": tool,
                "ok": ok,
                "fingerprint": fp,
                "tool_calls": self.tool_calls,
                "consecutive_failures": self.consecutive_failures,
            }
        )

        if self.consecutive_failures >= self.policy.max_consecutive_failures:
            self._halt(
                "consecutive_failures",
                (
                    f"工具 {tool!r} 连续失败 {self.consecutive_failures} 次；"
                    "按 AGENTS.md 纪律停止重试，改为说明原因并询问用户"
                ),
            )
        if self.tool_calls > self.policy.max_tool_calls:
            self._halt(
                "max_tool_calls",
                f"工具调用总数 {self.tool_calls} 超过上限 {self.policy.max_tool_calls}",
            )
        self._check_deadline()
        return fp

    # -- 进度 ---------------------------------------------------------------
    def mark_progress(self, signature: str | None = None) -> None:
        """显式声明"这一轮有新信息"。文件变更、新结论、新证据都算。"""
        if signature:
            if signature not in self.signatures:
                self.signatures.add(signature)
                self._round_new_info = True
        else:
            self._round_new_info = True

    def end_round(self) -> int:
        """结束一轮并结算进度。返回连续空转轮数。"""
        if self._round_new_info:
            self.no_progress_rounds = 0
        else:
            self.no_progress_rounds += 1
        self.events.append(
            {
                "at": self.elapsed,
                "event": "round_end",
                "round": self.round,
                "new_info": self._round_new_info,
                "no_progress_rounds": self.no_progress_rounds,
            }
        )
        if self.no_progress_rounds >= self.policy.no_progress_rounds:
            self._halt(
                "no_progress",
                (
                    f"连续 {self.no_progress_rounds} 轮没有产出新信息；"
                    "按纪律结束循环，改为向用户汇报现状"
                ),
            )
        return self.no_progress_rounds

    # -- 内部 ---------------------------------------------------------------
    def _check_deadline(self) -> None:
        if self.elapsed > self.policy.deadline_seconds:
            self._halt(
                "deadline",
                f"已运行 {self.elapsed:.0f}s，超过截止时间 {self.policy.deadline_seconds:.0f}s",
            )

    def _halt(self, reason: str, detail: str) -> None:
        self.halted = True
        self.events.append({"at": self.elapsed, "event": "halt", "reason": reason, "detail": detail})
        raise LoopHalted(reason, detail, self.snapshot())

    def snapshot(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "tool_calls": self.tool_calls,
            "elapsed_seconds": round(self.elapsed, 3),
            "consecutive_failures": self.consecutive_failures,
            "total_failures": self.total_failures,
            "no_progress_rounds": self.no_progress_rounds,
            "unique_calls": len(self.counts),
            "halted": self.halted,
        }

    def report(self) -> str:
        snap = self.snapshot()
        return (
            f"轮次 {snap['round']}/{self.policy.max_rounds}，"
            f"工具调用 {snap['tool_calls']}/{self.policy.max_tool_calls}，"
            f"耗时 {snap['elapsed_seconds']}s/{self.policy.deadline_seconds:.0f}s，"
            f"连续失败 {snap['consecutive_failures']}，"
            f"空转 {snap['no_progress_rounds']}/{self.policy.no_progress_rounds}"
        )
