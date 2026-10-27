"""状态与事务：让"前面成功、后面失败"变成可续跑，而不是从头再来。

三件东西：

- CheckpointStore：只追加的 JSONL 流水。每完成一级就落一条，断线后读最后一条
  成功记录即可定位续跑点；每条可带幂等键，防止重跑产生重复产物。
- run_step：上下文管理器。进入时记 started，正常退出记 ok，抛异常记 failed，
  保证"做过什么"永远有账可查。
- CompensationLog：补偿日志。多步副作用（写文件、发通知、改配置）失败时按
  LIFO 回滚已完成的步骤。回滚动作必须自身幂等，失败也只记录不抛。
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

__all__ = [
    "CheckpointStore",
    "ResumePoint",
    "CompensationLog",
    "file_digest",
    "new_run_id",
]


def new_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]


def file_digest(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


@dataclass
class ResumePoint:
    last_completed: str | None
    next_step: str | None
    completed: list[str]
    missing: list[str]

    @property
    def can_resume(self) -> bool:
        return self.next_step is not None


class CheckpointStore:
    def __init__(self, root: str | os.PathLike[str], *, run_id: str | None = None, filename: str = "checkpoints.jsonl") -> None:
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / filename
        self.run_id = run_id or new_run_id()

    # -- 写 -----------------------------------------------------------------
    def record(self, step: str, status: str = "ok", **fields: Any) -> dict[str, Any]:
        if not step:
            raise ValueError("step 不能为空")
        payload = {
            "ts": round(time.time(), 3),
            "run_id": self.run_id,
            "step": step,
            "status": status,
            **fields,
        }
        line = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")) + "\n"
        # 单次 O_APPEND 写入 + fsync：进程被杀也不会留下半行
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(descriptor, line.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return payload

    @contextmanager
    def run_step(self, step: str, **fields: Any) -> Iterator[dict[str, Any]]:
        """自动登记 started / ok / failed 的步骤包装。"""
        started = time.time()
        self.record(step, "started", **fields)
        try:
            yield fields
        except BaseException as exc:
            self.record(
                step,
                "failed",
                duration_seconds=round(time.time() - started, 3),
                error=f"{type(exc).__name__}: {exc}",
                **fields,
            )
            raise
        self.record(step, "ok", duration_seconds=round(time.time() - started, 3), **fields)

    # -- 读 -----------------------------------------------------------------
    def records(self, *, run_id: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        items: list[dict[str, Any]] = []
        for raw_line in self.path.read_text(encoding="utf-8").splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                item = json.loads(raw_line)
            except json.JSONDecodeError:
                continue  # 坏行跳过，不让一行脏数据毁掉续跑
            if run_id is None or item.get("run_id") == run_id:
                items.append(item)
        return items

    def completed_steps(self, *, run_id: str | None = None) -> list[str]:
        seen: list[str] = []
        for item in self.records(run_id=run_id):
            if item.get("status") == "ok" and item.get("step") not in seen:
                seen.append(item["step"])
        return seen

    def last_success(self, step: str | None = None, *, run_id: str | None = None) -> dict[str, Any] | None:
        for item in reversed(self.records(run_id=run_id)):
            if item.get("status") != "ok":
                continue
            if step is None or item.get("step") == step:
                return item
        return None

    def resume_point(self, pipeline: list[str], *, run_id: str | None = None) -> ResumePoint:
        completed = [step for step in self.completed_steps(run_id=run_id) if step in pipeline]
        missing = [step for step in pipeline if step not in completed]
        return ResumePoint(
            last_completed=completed[-1] if completed else None,
            next_step=missing[0] if missing else None,
            completed=completed,
            missing=missing,
        )

    def find_by_key(self, idempotency_key: str, *, run_id: str | None = None) -> dict[str, Any] | None:
        for item in reversed(self.records(run_id=run_id)):
            if item.get("idempotency_key") == idempotency_key and item.get("status") == "ok":
                return item
        return None

    # -- 幂等 ---------------------------------------------------------------
    def idempotent(self, key: str, action: Callable[[], Any], *, step: str | None = None) -> tuple[Any, bool]:
        """同一 key 只执行一次。返回 (结果, 是否本次执行)。"""
        if not key:
            raise ValueError("幂等键不能为空")
        existing = self.find_by_key(key)
        if existing is not None:
            return existing.get("result"), False

        value = action()
        try:
            json.dumps(value, default=str)
        except (TypeError, ValueError) as exc:  # pragma: no cover - 极端类型
            raise TypeError(f"幂等结果必须可 JSON 序列化: {exc}") from exc
        self.record(step or f"action:{key}", "ok", idempotency_key=key, result=value)
        return value, True


@dataclass
class CompensationLog:
    """按 LIFO 记录可撤销动作。回滚永远不抛，只把失败写进报告。"""

    store: CheckpointStore | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)

    def register(self, name: str, undo: Callable[[Any], Any], payload: Any = None) -> None:
        self.actions.append({"name": name, "undo": undo, "payload": payload})

    def __len__(self) -> int:
        return len(self.actions)

    def rollback(self, *, stop_on_error: bool = False) -> dict[str, Any]:
        attempted = 0
        succeeded = 0
        failures: list[dict[str, str]] = []
        while self.actions:
            action = self.actions.pop()
            attempted += 1
            try:
                action["undo"](action["payload"])
            except Exception as exc:  # noqa: BLE001
                failures.append({"name": action["name"], "error": f"{type(exc).__name__}: {exc}"})
                if stop_on_error:
                    break
                continue
            succeeded += 1
        report = {"attempted": attempted, "succeeded": succeeded, "failed": failures}
        if self.store is not None:
            self.store.record("rollback", "ok" if not failures else "failed", **report)
        return report
