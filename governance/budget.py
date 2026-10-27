"""预算账本：token / 成本 / 并行度 / agent 数量的硬上限。

为什么单独一个类：循环失控和成本爆炸往往不是同一处代码引起的，但都需要
一个"动手之前先记账"的地方。所有消耗走 `charge()`，所有扇出走 `slot()`，
超限即抛 BudgetExceeded，由调用方决定降级还是收口。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Mapping

from .policy import BudgetPolicy, Policy

__all__ = ["BudgetExceeded", "BudgetLedger"]


class BudgetExceeded(RuntimeError):
    def __init__(self, reason: str, *, used: Mapping[str, float] | None = None) -> None:
        self.reason = reason
        self.used = dict(used or {})
        super().__init__(f"预算超限（{reason}）: {self.used}")


@dataclass
class BudgetLedger:
    policy: BudgetPolicy
    tokens_used: int = 0
    cost_used: float = 0.0
    agents_started: int = 0
    active_slots: int = 0
    peak_active_slots: int = 0
    charges: list[dict] = field(default_factory=list)

    @classmethod
    def from_policy(cls, policy: Policy | BudgetPolicy) -> "BudgetLedger":
        return cls(policy=policy.budget if isinstance(policy, Policy) else policy)

    # -- 记账 ---------------------------------------------------------------
    def charge(self, *, tokens: int = 0, cost: float = 0.0, label: str = "") -> None:
        if tokens < 0 or cost < 0:
            raise ValueError("tokens/cost 不能为负数")
        self.tokens_used += int(tokens)
        self.cost_used += float(cost)
        self.charges.append({"label": label, "tokens": int(tokens), "cost": float(cost), "at": time.time()})
        self.check()

    def check(self) -> None:
        if self.tokens_used > self.policy.max_tokens:
            raise BudgetExceeded("token", used=self.snapshot())
        if self.cost_used > self.policy.max_cost_usd:
            raise BudgetExceeded("cost_usd", used=self.snapshot())
        if self.agents_started > self.policy.max_agents:
            raise BudgetExceeded("agents", used=self.snapshot())

    # -- 并发 ---------------------------------------------------------------
    @contextmanager
    def slot(self) -> Iterator["BudgetLedger"]:
        """占用一个并发槽；进入时记账，退出时释放（异常也释放）。"""
        self.acquire()
        try:
            yield self
        finally:
            self.release()

    def acquire(self) -> None:
        if self.active_slots >= self.policy.max_parallel_agents:
            raise BudgetExceeded("parallel_agents", used=self.snapshot())
        if self.agents_started >= self.policy.max_agents:
            raise BudgetExceeded("agents", used=self.snapshot())
        self.active_slots += 1
        self.agents_started += 1
        self.peak_active_slots = max(self.peak_active_slots, self.active_slots)

    def release(self) -> None:
        self.active_slots = max(0, self.active_slots - 1)

    def remaining(self) -> dict[str, float]:
        return {
            "tokens": max(0, self.policy.max_tokens - self.tokens_used),
            "cost_usd": max(0.0, round(self.policy.max_cost_usd - self.cost_used, 6)),
            "agents": max(0, self.policy.max_agents - self.agents_started),
            "parallel_agents": max(0, self.policy.max_parallel_agents - self.active_slots),
        }

    def snapshot(self) -> dict[str, float]:
        return {
            "tokens": self.tokens_used,
            "cost_usd": round(self.cost_used, 6),
            "agents": self.agents_started,
            "active_slots": self.active_slots,
        }
