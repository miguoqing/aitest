"""安全闸：把"工具权限过大"收敛成硬规则 + 一次性审批令牌。

分两层，缺一不可：

- **硬拒绝**：命令匹配危险模式（rm -rf /、git push --force、DROP TABLE、
  curl | sh、sudo…）直接不允许，不进入审批流程。这类操作没有一个"确认"是
  合理的默认值。
- **写路径约束**：写操作必须落在 allow_write_roots 内，`.git`、`node_modules`
  等目录一律拒绝；大小超限先拒。
- **一次性审批**：写/删/网络/改配置默认需要审批。令牌绑定具体动作（工具名+
  参数指纹）并带过期时间，用过即废——防止"批准一次，后面顺手做十次"。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import pathlib
import re
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping

from .policy import Policy, SafetyPolicy

__all__ = ["Risk", "Decision", "SafetyGate", "ApprovalToken", "action_fingerprint"]


class Risk(str, Enum):
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    NETWORK = "network"
    CONFIG = "config"
    EXEC = "exec"


_RISK_KEYWORDS: list[tuple[Risk, tuple[str, ...]]] = [
    (Risk.DELETE, ("delete", "remove", "rm ", "drop", "truncate", "purge", "unlink")),
    (Risk.CONFIG, ("config", "settings", "permission", "profile", "env")),
    (Risk.NETWORK, ("http", "fetch", "curl", "request", "upload", "post", "web", "download", "push")),
    (Risk.WRITE, ("write", "save", "create", "update", "edit", "patch", "apply", "move", "rename")),
]


def action_fingerprint(name: str, arguments: Any = None) -> str:
    from .guard import fingerprint  # 延迟导入，避免循环依赖

    return fingerprint(name, arguments)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    risk: Risk
    requires_approval: bool
    reason: str
    matched: str | None = None

    def __bool__(self) -> bool:
        return self.allowed and not self.requires_approval

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "risk": self.risk.value,
            "requires_approval": self.requires_approval,
            "reason": self.reason,
            "matched": self.matched,
        }


class SafetyGate:
    def __init__(self, policy: SafetyPolicy | Policy, workspace_root: str | os.PathLike[str] | None = None) -> None:
        self.policy: SafetyPolicy = policy.safety if isinstance(policy, Policy) else policy
        self.workspace_root = pathlib.Path(workspace_root or os.getcwd()).resolve()
        self._deny = [(pattern, re.compile(pattern, re.IGNORECASE)) for pattern in self.policy.deny_command_patterns]
        self.roots = [(self.workspace_root / root).resolve() if root != "." else self.workspace_root for root in self.policy.allow_write_roots]

    # -- 命令 ---------------------------------------------------------------
    def check_command(self, command: str, *, risk: Risk = Risk.EXEC) -> Decision:
        if not isinstance(command, str) or not command.strip():
            return Decision(False, risk, False, "空命令")
        for pattern, compiled in self._deny:
            if compiled.search(command):
                return Decision(False, risk, False, f"命中危险命令模式 {pattern!r}", matched=pattern)
        if self.policy.mode == "read-only" and re.search(r"(^|\s)(rm|mv|cp|mkdir|touch|tee)\s|>>?\s*\S", command):
            return Decision(False, Risk.WRITE, False, "只读模式禁止写操作")
        needs_approval = risk.value in self.policy.require_approval
        return Decision(True, risk, needs_approval, "允许执行" + ("（需审批）" if needs_approval else ""))

    # -- 路径 ---------------------------------------------------------------
    def check_write(self, path: str | os.PathLike[str], *, size_bytes: int = 0) -> Decision:
        if self.policy.mode == "read-only":
            return Decision(False, Risk.WRITE, False, "只读模式禁止写入")
        try:
            resolved = pathlib.Path(path).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            return Decision(False, Risk.WRITE, False, f"路径无法解析: {exc}")

        for part in resolved.parts:
            if part in self.policy.deny_path_parts:
                return Decision(False, Risk.WRITE, False, f"路径命中禁写目录 {part!r}")

        if not any(self._is_within(resolved, root) for root in self.roots):
            return Decision(False, Risk.WRITE, False, f"写路径 {resolved} 不在允许范围内 {list(map(str, self.roots))}")

        if size_bytes and size_bytes > self.policy.max_write_bytes:
            return Decision(False, Risk.WRITE, False, f"写入 {size_bytes} 字节超过上限 {self.policy.max_write_bytes}")

        needs_approval = Risk.WRITE.value in self.policy.require_approval or Risk.DELETE.value in self.policy.require_approval
        return Decision(True, Risk.WRITE, needs_approval, "允许写入" + ("（需审批）" if needs_approval else ""))

    def check_delete(self, path: str | os.PathLike[str]) -> Decision:
        if self.policy.mode != "danger-full-access":
            return Decision(False, Risk.DELETE, False, "非完全访问模式下禁止删除；请改为归档或移入回收目录")
        decision = self.check_write(path)
        return Decision(decision.allowed, Risk.DELETE, True, decision.reason) if decision.allowed else Decision(False, Risk.DELETE, False, decision.reason)

    # -- 工具 ---------------------------------------------------------------
    def check_tool(self, name: str, arguments: Any = None) -> Decision:
        risk = self.infer_risk(name, arguments)
        if risk == Risk.DELETE:
            path = _first_path(arguments)
            if path:
                return self.check_delete(path)
            return Decision(True, Risk.DELETE, True, "删除类工具需要审批")
        if risk == Risk.WRITE:
            path = _first_path(arguments)
            if path:
                return self.check_write(path)
            return Decision(True, Risk.WRITE, Risk.WRITE.value in self.policy.require_approval, "写入类工具")
        if risk == Risk.NETWORK:
            return Decision(True, Risk.NETWORK, Risk.NETWORK.value in self.policy.require_approval, "网络类工具需要审批")
        return Decision(True, risk, risk.value in self.policy.require_approval, "读取类工具")

    def infer_risk(self, name: str, arguments: Any = None) -> Risk:
        lowered = f"{name} {arguments if isinstance(arguments, str) else ''}".lower()
        for risk, keywords in _RISK_KEYWORDS:
            if any(keyword in lowered for keyword in keywords):
                return risk
        return Risk.READ

    @staticmethod
    def _is_within(path: pathlib.Path, root: pathlib.Path) -> bool:
        try:
            return path == root or path.is_relative_to(root)
        except AttributeError:  # pragma: no cover - Python < 3.9
            return str(path).startswith(str(root) + os.sep)


def _first_path(arguments: Any) -> str | None:
    if isinstance(arguments, Mapping):
        for key in ("path", "file", "target", "destination", "dest", "source", "filename"):
            value = arguments.get(key)
            if isinstance(value, str):
                return value
    return None


class ApprovalToken:
    """HMAC 签名的一次性审批令牌，绑定动作指纹。"""

    def __init__(
        self,
        secret: str | bytes,
        *,
        ttl_seconds: float = 300.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        if not secret:
            raise ValueError("审批令牌必须有密钥")
        self.secret = secret
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._used: set[str] = set()
        self._issued: dict[str, dict[str, Any]] = {}

    def issue(self, action: str, *, ttl_seconds: float | None = None) -> str:
        payload = {
            "a": action,
            "exp": self.clock() + (ttl_seconds if ttl_seconds is not None else self.ttl_seconds),
            "n": secrets.token_hex(8),
        }
        encoded = _b64encode(_compact_json(payload))
        return f"{encoded}.{self._sign(encoded)}"

    def verify(self, token: str, action: str) -> bool:
        parsed = self._parse(token)
        if parsed is None:
            return False
        payload, nonce = parsed
        if payload["a"] != action:
            return False
        if self.clock() > payload["exp"]:
            return False
        return nonce not in self._used

    def consume(self, token: str, action: str) -> bool:
        if not self.verify(token, action):
            return False
        parsed = self._parse(token)
        assert parsed is not None
        self._used.add(parsed[1])
        return True

    # -- 内部 ---------------------------------------------------------------
    def _parse(self, token: str) -> tuple[dict[str, Any], str] | None:
        if not isinstance(token, str) or token.count(".") != 1:
            return None
        encoded, signature = token.split(".", 1)
        if not hmac.compare_digest(signature, self._sign(encoded)):
            return None
        try:
            import json

            payload = json.loads(_b64decode(encoded))
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(payload, dict) or not {"a", "exp", "n"} <= set(payload):
            return None
        return payload, str(payload["n"])

    def _sign(self, encoded: str) -> str:
        return hmac.new(self.secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()


def _compact_json(payload: Mapping[str, Any]) -> bytes:
    import json

    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)
