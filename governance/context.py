"""上下文治理：把"聊到后面越来越乱"变成可预算的操作。

三件事按顺序做，顺序不能反：

1. **去重**：同一个工具对同一输入返回的同样内容只留最后一份。长循环里重复
   读同一文件是上下文膨胀的头号来源。
2. **剪枝**：单条工具结果超过阈值就保留头尾（头是结论，尾是报错/合计），中间
   省略。原始全文仍在磁盘上，需要时可再读。
3. **预算内裁剪**：证据（逐字引用的原文）永久置顶且不参与裁剪——它是这套用例
   可信度的锚；其余按"新近优先"保留，超出的折叠成一条摘要。

注意估算的是 tokens 而不是字符：中文一字约一 token，英文四字符约一 token。
宁可估高，不要估低。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from .policy import ContextPolicy, Policy

__all__ = ["estimate_tokens", "Message", "ContextGovernor"]

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return max(1, math.ceil(cjk * 1.0 + other / 4.0))


@dataclass
class Message:
    role: str
    content: str
    pinned: bool = False
    kind: str = "text"          # text / tool / evidence / summary
    meta: dict[str, Any] = field(default_factory=dict)
    tokens: int = 0

    def __post_init__(self) -> None:
        if not self.tokens:
            self.tokens = estimate_tokens(self.content)


_STUB = "[重复输出已省略：与后文完全相同]"


@dataclass
class ContextStats:
    messages_in: int = 0
    messages_out: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    deduped: int = 0
    dropped: int = 0
    truncated: int = 0
    pinned_kept: int = 0
    over_budget: bool = False

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class ContextGovernor:
    def __init__(self, policy: ContextPolicy | Policy) -> None:
        self.policy: ContextPolicy = policy.context if isinstance(policy, Policy) else policy
        self.messages: list[Message] = []
        self.last_stats = ContextStats()

    # -- 写入 ---------------------------------------------------------------
    def add(self, role: str, content: str, *, pinned: bool = False, kind: str = "text", **meta: Any) -> Message:
        message = Message(role=role, content=content, pinned=pinned, kind=kind, meta=meta)
        self.messages.append(message)
        return message

    def add_tool_result(self, name: str, content: str, *, ok: bool = True) -> Message:
        text = content if isinstance(content, str) else str(content)
        original_length = len(text)
        if original_length > self.policy.tool_result_max_chars:
            head = text[: self.policy.tool_result_head_chars]
            tail = text[-self.policy.tool_result_tail_chars :]
            omitted = original_length - len(head) - len(tail)
            text = f"{head}\n…[工具结果省略 {omitted} 字符，原文见工具输出文件]…\n{tail}"
            self.last_stats.truncated += 1
        return self.add("tool", text, kind="tool", **{"name": name, "ok": ok})

    def add_evidence(self, quote: str, source: str = "") -> Message:
        """逐字证据：置顶、永不裁剪。"""
        return self.add("system", f"证据[{source}]: {quote}", pinned=True, kind="evidence", source=source)

    def reset(self) -> None:
        self.messages.clear()
        self.last_stats = ContextStats()

    def __len__(self) -> int:
        return len(self.messages)

    # -- 渲染 ---------------------------------------------------------------
    def render(self) -> list[dict[str, str]]:
        stats = ContextStats(messages_in=len(self.messages))
        stats.tokens_in = sum(item.tokens for item in self.messages)

        deduped = self._dedupe(stats)
        kept, dropped = self._apply_budget(deduped, stats)

        stats.messages_out = len(kept)
        stats.tokens_out = sum(item.tokens for item in kept)
        stats.pinned_kept = sum(1 for item in kept if item.pinned)
        stats.over_budget = stats.tokens_out > self.policy.max_tokens
        self.last_stats = stats

        rendered: list[dict[str, str]] = []
        if dropped:
            rendered.append(
                {
                    "role": "system",
                    "content": (
                        f"[上下文治理] 已省略 {len(dropped)} 条较早消息（约 {sum(m.tokens for m in dropped)} tokens）。"
                        "需要其中的细节时请重新读取对应文件，不要凭记忆推测。"
                    ),
                }
            )
        rendered.extend({"role": item.role, "content": item.content} for item in kept)
        return rendered

    # -- 内部 ---------------------------------------------------------------
    def _dedupe(self, stats: ContextStats) -> list[Message]:
        seen: dict[tuple[str, str], int] = {}
        for index, message in enumerate(self.messages):
            if message.pinned or message.kind == "evidence":
                continue
            if message.kind not in {"tool"}:
                continue
            key = (str(message.meta.get("name", "")), message.content)
            seen[key] = index

        if not seen:
            return list(self.messages)

        result: list[Message] = []
        for index, message in enumerate(self.messages):
            if (
                message.kind == "tool"
                and not message.pinned
                and seen.get((str(message.meta.get("name", "")), message.content)) != index
                and message.content != _STUB
            ):
                stats.deduped += 1
                continue
            result.append(message)
        return result

    def _apply_budget(self, messages: list[Message], stats: ContextStats) -> tuple[list[Message], list[Message]]:
        pinned = [item for item in messages if item.pinned]
        pinned_tokens = sum(item.tokens for item in pinned)
        reserve = self.policy.evidence_reserve_tokens
        # 证据既占预算也必须保留，所以先从可用额度里扣掉，再裁非证据内容
        budget = max(0, self.policy.max_tokens - reserve - pinned_tokens)

        # 最近 N 条无条件保留：刚发生的事比旧的更重要
        recent_start = max(0, len(messages) - self.policy.keep_recent_turns)
        keep: set[int] = set(range(recent_start, len(messages)))
        used = sum(messages[index].tokens for index in keep if not messages[index].pinned)

        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if message.pinned or index in keep:
                continue
            if used + message.tokens <= budget:
                keep.add(index)
                used += message.tokens

        kept: list[Message] = []
        dropped: list[Message] = []
        for index, message in enumerate(messages):
            (kept if index in keep or message.pinned else dropped).append(message)
        stats.dropped = len(dropped)
        # 已 pinned 的条目即使超预算也必须保留（证据不可丢），但要标记出来
        if pinned_tokens > self.policy.max_tokens or used + pinned_tokens > self.policy.max_tokens:
            stats.over_budget = True
        return kept, dropped

    # -- 观测 ---------------------------------------------------------------
    def report(self) -> str:
        stats = self.last_stats
        return (
            f"上下文 {stats.messages_out}/{stats.messages_in} 条，"
            f"约 {stats.tokens_out}/{self.policy.max_tokens} tokens，"
            f"去重 {stats.deduped}，裁剪 {stats.dropped}，截断 {stats.truncated}，"
            f"证据保留 {stats.pinned_kept}"
            + ("；已超预算" if stats.over_budget else "")
        )
