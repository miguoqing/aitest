"""Agent 循环治理工具箱。

七个故障面各自对应一个模块，想用哪个拿哪个，不强制全用：

    loop      guard.LoopGuard          轮次/截止/空转/重复调用熔断
    retry     retry.run_with_retry     错误分类 + 指数退避 + 熔断
    tool      toolcall.ToolRegistry    JSON 容错解析 + schema 校验 + 工具解析
    state     state.CheckpointStore    检查点 / 幂等 / 补偿日志
    context   context.ContextGovernor  去重 / 剪枝 / 预算内裁剪
    safety    safety.SafetyGate        危险命令硬拒绝 + 一次性审批令牌
    mcp       mcp.probe                MCP 预检 + 握手 + 诊断

命令行入口：python3 -m governance.cli --help
"""

from __future__ import annotations

from .audit import Finding, audit_project, format_findings
from .budget import BudgetExceeded, BudgetLedger
from .context import ContextGovernor, estimate_tokens
from .guard import LoopGuard, LoopHalted, fingerprint
from .policy import Policy, PolicyError, load_policy
from .retry import CircuitBreaker, ErrorClass, RetryExhausted, classify_error, run_with_retry
from .safety import ApprovalToken, Decision, Risk, SafetyGate
from .state import CheckpointStore, CompensationLog, ResumePoint
from .toolcall import SchemaViolation, ToolArgumentError, ToolCallResult, ToolRegistry, ToolSpec, UnknownToolError, extract_json

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "Policy",
    "PolicyError",
    "load_policy",
    "LoopGuard",
    "LoopHalted",
    "fingerprint",
    "BudgetLedger",
    "BudgetExceeded",
    "run_with_retry",
    "CircuitBreaker",
    "RetryExhausted",
    "ErrorClass",
    "classify_error",
    "ToolRegistry",
    "ToolSpec",
    "ToolCallResult",
    "ToolArgumentError",
    "SchemaViolation",
    "UnknownToolError",
    "extract_json",
    "CheckpointStore",
    "CompensationLog",
    "ResumePoint",
    "ContextGovernor",
    "estimate_tokens",
    "SafetyGate",
    "Risk",
    "Decision",
    "ApprovalToken",
    "Finding",
    "audit_project",
    "format_findings",
]
