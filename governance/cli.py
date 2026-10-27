#!/usr/bin/env python3
"""治理工具箱命令行。

常用：
    python3 -m governance.cli audit                 # 按 7 类故障审计当前工程
    python3 -m governance.cli audit --fail-on high  # CI 门禁
    python3 -m governance.cli policy --json         # 打印生效阈值
    python3 -m governance.cli mcp                   # MCP 预检 + 握手
    python3 -m governance.cli checkpoint --dir runs/x --pipeline a,b,c
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from .audit import _SEVERITY_ORDER, audit_project, findings_to_json, format_findings, max_severity
from .policy import PolicyError, load_policy
from .state import CheckpointStore
from .trace import build_items, filter_items, find_session, follow, load_events, render_items, summarize


def _cmd_audit(args: argparse.Namespace) -> int:
    root = pathlib.Path(args.root).resolve()
    try:
        policy = load_policy()
    except PolicyError as exc:
        sys.stderr.write(f"策略加载失败: {exc}\n")
        return 2
    findings = audit_project(root, policy=policy)
    if args.json:
        print(findings_to_json(findings))
    else:
        print(format_findings(findings, root))
    threshold = args.fail_on if args.fail_on is not None else policy.audit.fail_on
    if threshold != "none" and _SEVERITY_ORDER[max_severity(findings)] >= _SEVERITY_ORDER[threshold]:
        return 1
    return 0


def _cmd_policy(args: argparse.Namespace) -> int:
    try:
        policy = load_policy(args.path)
    except PolicyError as exc:
        sys.stderr.write(f"策略加载失败: {exc}\n")
        return 2
    data = policy.to_dict()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"策略版本 {data['version']}")
        print(f"循环: 最多 {data['loop']['max_rounds']} 轮 / {data['loop']['max_tool_calls']} 次工具 / {data['loop']['deadline_seconds']}s")
        print(f"     连续失败 {data['loop']['max_consecutive_failures']} 次停，重复调用阈值 {data['loop']['duplicate_call_threshold']}")
        print(f"预算: {data['budget']['max_agents']} agents / {data['budget']['max_parallel_agents']} 并行 / {data['budget']['max_tokens']} tokens / ${data['budget']['max_cost_usd']}")
        print(f"上下文: {data['context']['max_tokens']} tokens，证据预留 {data['context']['evidence_reserve_tokens']}")
        print(f"安全: {data['safety']['mode']}，需审批 {', '.join(data['safety']['require_approval'])}")
    return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp import check_servers, load_servers_from_dsh_config, preflight

    config = pathlib.Path(args.config)
    if not config.exists():
        sys.stderr.write(f"配置不存在: {config}\n")
        return 2
    try:
        specs = load_servers_from_dsh_config(config)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"解析 MCP 配置失败: {exc}\n")
        return 2
    if not specs:
        print("配置里没有 MCP server。")
        return 0
    results = [preflight(spec) for spec in specs] if args.no_probe else check_servers(specs)
    failed = 0
    for result in results:
        print(result.summary())
        failed += 0 if result.ok else 1
    if args.json:
        print(json.dumps([item.to_dict() for item in results], ensure_ascii=False, indent=2))
    return 1 if failed else 0


def _cmd_checkpoint(args: argparse.Namespace) -> int:
    store = CheckpointStore(args.dir)
    pipeline = [item.strip() for item in args.pipeline.split(",") if item.strip()]
    if pipeline:
        point = store.resume_point(pipeline)
        payload = {
            "last_completed": point.last_completed,
            "next_step": point.next_step,
            "completed": point.completed,
            "missing": point.missing,
        }
    else:
        payload = {"completed": store.completed_steps(), "records": len(store.records())}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_trace(args: argparse.Namespace) -> int:
    try:
        path = find_session(session=args.session, cwd=args.cwd)
    except FileNotFoundError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2

    color = sys.stdout.isatty() and not args.json
    if args.follow:
        try:
            follow(path, interval=args.interval, full=args.full, include_jobs=args.jobs, color=color, from_start=args.from_start)
        except KeyboardInterrupt:
            return 0
        return 0

    try:
        events = load_events(path)
    except (RuntimeError, OSError) as exc:
        sys.stderr.write(f"读取会话失败: {exc}\n")
        return 2

    items = filter_items(
        build_items(events, full=args.full, include_jobs=args.jobs),
        errors_only=args.errors,
        tool=args.tool,
        turn=args.turn,
    )
    if args.last and args.last > 0:
        items = items[-args.last :]

    if args.json:
        print(json.dumps([item.to_dict() for item in items], ensure_ascii=False, indent=2))
        return 0

    print(f"# {path}")
    for line in render_items(items, color=color):
        print(line)
    print("\n" + summarize(items))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="governance", description="Agent 循环治理工具箱")
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit", help="按 7 类故障审计工程配置")
    audit.add_argument("--root", default=".", help="工程根目录")
    audit.add_argument("--json", action="store_true")
    audit.add_argument("--fail-on", choices=["none", "low", "medium", "high"], default=None)
    audit.set_defaults(func=_cmd_audit)

    policy = sub.add_parser("policy", help="打印生效策略")
    policy.add_argument("--path", default=None)
    policy.add_argument("--json", action="store_true")
    policy.set_defaults(func=_cmd_policy)

    mcp = sub.add_parser("mcp", help="MCP server 预检与握手")
    mcp.add_argument("--config", default="dsh-web-config.yml")
    mcp.add_argument("--no-probe", action="store_true", help="只做静态预检，不启动进程")
    mcp.add_argument("--json", action="store_true")
    mcp.set_defaults(func=_cmd_mcp)

    trace = sub.add_parser("trace", help="按步查看会话执行日志")
    trace.add_argument("--session", default=None, help="会话文件或目录（默认当前工程最近一次）")
    trace.add_argument("--cwd", default=".", help="按工程目录找会话")
    trace.add_argument("--last", type=int, default=0, help="只看最后 N 条")
    trace.add_argument("--errors", action="store_true", help="只看失败的调用")
    trace.add_argument("--tool", default=None, help="只看某个工具（如 bash/read）")
    trace.add_argument("--turn", type=int, default=None, help="只看某一轮")
    trace.add_argument("--jobs", action="store_true", help="显示后台 job 生命周期")
    trace.add_argument("--full", action="store_true", help="不截断参数与输出")
    trace.add_argument("--json", action="store_true")
    trace.add_argument("--follow", action="store_true", help="持续跟踪新事件")
    trace.add_argument("--from-start", action="store_true", help="follow 时先回放历史")
    trace.add_argument("--interval", type=float, default=1.5, help="follow 轮询间隔秒")
    trace.set_defaults(func=_cmd_trace)

    checkpoint = sub.add_parser("checkpoint", help="查看续跑点")
    checkpoint.add_argument("--dir", required=True)
    checkpoint.add_argument("--pipeline", default="")
    checkpoint.set_defaults(func=_cmd_checkpoint)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
