"""按步看执行日志：把 DSH 的会话事件流渲染成时间线。

DSH 每一步都写进 `~/.dsh/sessions/<cwd>/<session>/session.v4.jsonl.zstd`，
事件是结构化的（`turn/start`、`step/start`、`tool/call`、`tool/result`、
`assistant/message`、`turn/end`…）。压缩 + 原始 JSON 不适合直接看，所以这里做三件事：

1. 找到"当前工程最近一次会话"（路径编码规则与 DSH 一致）；
2. 把 tool/call 与 tool/result 按 callId 配对，算出耗时；
3. 渲染成"轮次 → 步骤 → 工具调用 → 结果 → 模型输出"的缩进时间线。

用法：
    python3 -m governance.cli trace                 # 当前工程最近一次会话
    python3 -m governance.cli trace --follow        # 边跑边看
    python3 -m governance.cli trace --errors        # 只看失败
    python3 -m governance.cli trace --tool bash     # 只看某个工具
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping

__all__ = [
    "TraceItem",
    "find_session",
    "load_events",
    "build_items",
    "render_items",
    "summarize",
    "encode_cwd",
    "read_zstd",
]


# --------------------------------------------------------------------------
# 会话定位
# --------------------------------------------------------------------------
def dsh_home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("DSH_HOME") or (pathlib.Path.home() / ".dsh"))


def encode_cwd(cwd: str | os.PathLike[str]) -> str:
    """DSH 的 cwd 目录编码：/a/b → --a-b--。"""
    resolved = str(pathlib.Path(cwd).resolve())
    return "-" + resolved.replace(os.sep, "-") + "--"


def _session_files(root: pathlib.Path) -> list[pathlib.Path]:
    if not root.exists():
        return []
    return sorted(
        (path for path in root.rglob("session*.jsonl.zstd") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def find_session(
    *,
    session: str | os.PathLike[str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> pathlib.Path:
    """定位会话文件：显式路径 > 当前 cwd 的最新会话 > 全部会话里最新的一个。"""
    if session is not None:
        target = pathlib.Path(session).expanduser()
        if target.is_dir():
            candidates = _session_files(target)
            if not candidates:
                raise FileNotFoundError(f"目录里没有会话日志: {target}")
            return candidates[0]
        if not target.exists():
            raise FileNotFoundError(f"会话文件不存在: {target}")
        return target

    home = dsh_home() / "sessions"
    if cwd is not None:
        scoped = home / encode_cwd(cwd)
        candidates = _session_files(scoped)
        if candidates:
            return candidates[0]

    everything = _session_files(home)
    if everything:
        return everything[0]
    raise FileNotFoundError(
        f"没有找到会话日志（查找范围 {home}）。确认 DSH 会话持久化已启用，或用 --session 指定文件。"
    )


# --------------------------------------------------------------------------
# 读取（zstd / 纯 jsonl）
# --------------------------------------------------------------------------
def read_zstd(path: pathlib.Path) -> bytes:
    """优先用 Python zstandard；没有就退回 zstd CLI / node。截断文件按已解码部分返回。"""
    raw = path.read_bytes()
    try:
        import zstandard  # type: ignore[import-not-found]
    except ImportError:
        zstandard = None
    if zstandard is not None:
        import io

        with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw)) as reader:
            return reader.read()

    binary = shutil.which("zstd")
    if binary:
        completed = subprocess.run([binary, "-dc"], input=raw, capture_output=True)
        if completed.returncode == 0:
            return completed.stdout
        raise RuntimeError(f"zstd 解压失败: {completed.stderr.decode('utf-8', 'replace')[:200]}")

    node = shutil.which("node")
    if node:
        script = (
            "const fs=require('node:fs'),z=require('node:zlib');"
            "process.stdout.write(z.zstdDecompressSync(fs.readFileSync(process.argv[1])))"
        )
        completed = subprocess.run([node, "-e", script, str(path)], capture_output=True)
        if completed.returncode == 0:
            return completed.stdout

    raise RuntimeError("缺少 zstd 解压能力：pip install zstandard，或安装 zstd / node")


def load_events(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    target = pathlib.Path(path)
    raw = read_zstd(target) if target.suffix == ".zstd" else target.read_bytes()
    text = raw.decode("utf-8", "replace")
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # 正在写入的最后一帧可能不完整，跳过即可；下一轮 follow 会补上
            continue
        if isinstance(event, dict) and "type" in event:
            events.append(event)
    events.sort(key=lambda event: int(event.get("seq") or 0))
    return events


# --------------------------------------------------------------------------
# 时间线
# --------------------------------------------------------------------------
@dataclass
class TraceItem:
    seq: int
    time: float
    kind: str          # session / turn / step / user / assistant / tool_result / note
    text: str
    indent: int = 0
    status: str = ""    # ok / error / info
    duration_ms: int | None = None
    tokens: dict[str, int] | None = None
    tool: str | None = None
    call_id: str | None = None
    turn: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "seq": self.seq,
            "time": self.time,
            "kind": self.kind,
            "indent": self.indent,
            "status": self.status,
            "text": self.text,
        }
        if self.duration_ms is not None:
            payload["duration_ms"] = self.duration_ms
        if self.tokens:
            payload["tokens"] = self.tokens
        if self.tool:
            payload["tool"] = self.tool
        return payload


def _text_blocks(content: Any) -> tuple[str, int]:
    """拼接 text 块，返回 (文本, tool-call 数量)。"""
    parts: list[str] = []
    calls = 0
    if isinstance(content, str):
        return content, 0
    for block in content or []:
        if not isinstance(block, Mapping):
            continue
        block_type = block.get("type")
        if block_type == "text":
            parts.append(str(block.get("text") or ""))
        elif block_type in {"tool-call", "tool_call"}:
            calls += 1
    return "\n".join(part for part in parts if part), calls


def _preview(text: str, limit: int, full: bool) -> str:
    text = " ".join(str(text).split())
    if full or len(text) <= limit:
        return text
    return text[:limit] + "…"


def _summarize_args(name: str, raw: Any, limit: int, full: bool) -> str:
    args = raw
    if isinstance(raw, str):
        try:
            args = json.loads(raw)
        except json.JSONDecodeError:
            return _preview(raw, limit, full)
    if isinstance(args, Mapping):
        for key in ("command", "path", "file_path", "pattern", "question", "url", "description"):
            value = args.get(key)
            if isinstance(value, str) and value:
                return _preview(f"{key}={value}", limit, full)
        return _preview(json.dumps(args, ensure_ascii=False, default=str), limit, full)
    return _preview(str(args), limit, full)


def build_items(events: Iterable[Mapping[str, Any]], *, full: bool = False, include_jobs: bool = False) -> list[TraceItem]:
    items: list[TraceItem] = []
    pending: dict[str, dict[str, Any]] = {}
    args_limit = 400 if full else 160
    text_limit = 400 if full else 100

    for event in events:
        event_type = str(event.get("type"))
        data = event.get("data") or {}
        seq = int(event.get("seq") or 0)
        when = float(event.get("time") or 0) / 1000.0

        if event_type == "session":
            items.append(TraceItem(seq, when, "session", f"会话 {data.get('id', '?')}  cwd={data.get('cwd', '?')}", status="info"))
        elif event_type == "permission/preset":
            items.append(TraceItem(seq, when, "note", f"权限预设: {data.get('preset')}", status="info"))
        elif event_type == "sandbox/mode":
            items.append(TraceItem(seq, when, "note", f"沙箱模式: {data.get('mode')}", status="info"))
        elif event_type == "approval/policy":
            items.append(TraceItem(seq, when, "note", f"审批策略: {data.get('policy')}", status="info"))
        elif event_type == "model/selection":
            items.append(
                TraceItem(
                    seq,
                    when,
                    "note",
                    f"模型: {data.get('provider')}/{data.get('model')} ({data.get('reasoningEffort') or '-'})",
                    status="info",
                )
            )
        elif event_type == "session/title":
            items.append(TraceItem(seq, when, "note", f"标题: {data.get('title')}", status="info"))
        elif event_type == "turn/start":
            items.append(TraceItem(seq, when, "turn", f"turn {data.get('turn')} 开始", indent=0, turn=int(data.get("turn") or 0) or None))
        elif event_type == "turn/end":
            items.append(
                TraceItem(
                    seq, when, "turn", f"turn {data.get('turn')} 结束（{data.get('reason') or 'unknown'}）",
                    indent=0, status="info", turn=int(data.get("turn") or 0) or None,
                )
            )
        elif event_type == "step/start":
            items.append(
                TraceItem(
                    seq, when, "step", f"step {data.get('turn')}.{data.get('step')}",
                    indent=1, status="info", turn=int(data.get("turn") or 0) or None,
                )
            )
        elif event_type == "user/message":
            text, _ = _text_blocks(data.get("content"))
            items.append(TraceItem(seq, when, "user", f"用户: {_preview(text, text_limit, full)}", indent=2))
        elif event_type == "assistant/message":
            message = data.get("message") or {}
            text, calls = _text_blocks(message.get("content"))
            usage = data.get("usage") or {}
            tokens = {
                "in": int(usage.get("inputTokens") or 0),
                "out": int(usage.get("outputTokens") or 0),
                "cache_read": int(usage.get("cacheReadTokens") or 0),
            }
            label = f"模型: {_preview(text, text_limit, full)}" if text else f"模型: （仅工具调用 ×{calls}）"
            items.append(
                TraceItem(
                    seq,
                    when,
                    "assistant",
                    label,
                    indent=2,
                    tokens=tokens if any(tokens.values()) else None,
                    turn=int(data.get("turn") or 0) or None,
                )
            )
        elif event_type == "tool/call":
            call_id = str(data.get("callId") or "")
            name = str(data.get("name") or "?")
            summary = _summarize_args(name, data.get("arguments"), args_limit, full)
            pending[call_id] = {"time": when, "name": name, "seq": seq}
            items.append(
                TraceItem(
                    seq, when, "tool_call", f"→ {name}  {summary}",
                    indent=3, tool=name, call_id=call_id, turn=int(data.get("turn") or 0) or None,
                )
            )
        elif event_type == "tool/result":
            message = data.get("message") or {}
            call_id = str(message.get("toolCallId") or (message.get("source") or {}).get("callId") or "")
            meta = pending.pop(call_id, None)
            name = (meta or {}).get("name") or "?"
            duration = int(round((when - meta["time"]) * 1000)) if meta else None
            text, _ = _text_blocks(message.get("content"))
            is_error = bool(message.get("isError")) or bool(data.get("error"))
            code = (data.get("error") or {}).get("code") or (data.get("error") or {}).get("name") or ""
            prefix = "← 失败" if is_error else "← ok"
            suffix = f" {code}" if code else ""
            items.append(
                TraceItem(
                    seq,
                    when,
                    "tool_result",
                    f"{prefix}{suffix}  {_preview(text, text_limit, full)}",
                    indent=3,
                    status="error" if is_error else "ok",
                    duration_ms=duration,
                    tool=name,
                    call_id=call_id,
                    turn=int(data.get("turn") or 0) or None,
                )
            )
        elif event_type in {"job/registered", "job/settled"} and include_jobs:
            if event_type == "job/registered":
                items.append(
                    TraceItem(seq, when, "note", f"job 启动: {data.get('kind')} {data.get('label') or ''}".strip(), indent=3, status="info")
                )
            else:
                duration = int(float(data.get("finishedAt") or 0) - float(data.get("startedAt") or 0))
                items.append(
                    TraceItem(
                        seq,
                        when,
                        "note",
                        f"job {data.get('status')}: {data.get('kind')} ({duration}ms)",
                        indent=3,
                        status="info",
                    )
                )
        elif event_type == "todo/write":
            todos = data.get("todos") or []
            done = sum(1 for todo in todos if isinstance(todo, Mapping) and todo.get("status") == "completed")
            items.append(TraceItem(seq, when, "note", f"todo: {done}/{len(todos)} 完成", indent=1, status="info"))
        elif event_type == "system/message":
            text, _ = _text_blocks((data.get("message") or {}).get("content"))
            if text:
                items.append(TraceItem(seq, when, "note", f"系统: {_preview(text, text_limit, full)}", indent=1, status="info"))

    return items


def render_items(items: list[TraceItem], *, color: bool = False) -> Iterator[str]:
    for item in items:
        stamp = time.strftime("%H:%M:%S", time.localtime(item.time)) if item.time else "--:--:--"
        trail = []
        if item.duration_ms is not None:
            trail.append(f"{item.duration_ms}ms")
        if item.tokens:
            trail.append(f"in={item.tokens['in']} out={item.tokens['out']} cache={item.tokens['cache_read']}")
        suffix = f"  [{', '.join(trail)}]" if trail else ""
        line = f"{stamp}  {'  ' * item.indent}{item.text}{suffix}"
        if color and item.status == "error":
            line = f"\033[31m{line}\033[0m"
        elif color and item.kind == "turn":
            line = f"\033[1m{line}\033[0m"
        yield line


def summarize(items: list[TraceItem]) -> str:
    tools: dict[str, int] = {}
    steps = 0
    turns = 0
    errors = 0
    tokens_in = tokens_out = 0
    turn_ids: set[int] = set()
    for item in items:
        if item.turn is not None:
            turn_ids.add(item.turn)
        if item.kind == "step":
            steps += 1
        elif item.kind == "tool_call" and item.tool:
            tools[item.tool] = tools.get(item.tool, 0) + 1
        elif item.kind == "tool_result" and item.status == "error":
            errors += 1
        elif item.kind == "assistant" and item.tokens:
            tokens_in += item.tokens["in"]
            tokens_out += item.tokens["out"]
    turns = len(turn_ids)
    span = ""
    timed = [item.time for item in items if item.time]
    if len(timed) >= 2:
        span = f"，跨度 {int(timed[-1] - timed[0])}s"
    tool_text = ", ".join(f"{name}×{count}" for name, count in sorted(tools.items(), key=lambda pair: -pair[1]))
    return (
        f"turn {turns} / step {steps} / 工具 {sum(tools.values())} 次（{tool_text or '无'}）"
        f" / 失败 {errors} / token in={tokens_in} out={tokens_out}{span}"
    )


# --------------------------------------------------------------------------
# follow
# --------------------------------------------------------------------------
def follow(
    path: pathlib.Path,
    *,
    interval: float = 1.5,
    full: bool = False,
    include_jobs: bool = False,
    color: bool = False,
    from_start: bool = False,
) -> None:
    """轮询重读整个文件，只打印新 seq。会话文件是追加写的，重读成本很低。

    默认从"当前进度"开始（等价 tail -f）；from_start=True 时先回放历史。
    """
    seen = 0
    if not from_start:
        try:
            seen = max((int(event.get("seq") or 0) for event in load_events(path)), default=0)
        except Exception:  # noqa: BLE001
            seen = 0
    announced = False
    while True:
        try:
            events = load_events(path)
        except Exception as exc:  # noqa: BLE001 - 写入中可能短暂不可读
            print(f"[trace] 读取失败，重试: {exc}", file=sys.stderr)
            time.sleep(interval)
            continue
        if not announced:
            hint = "（回放历史）" if from_start else "（从当前进度开始，--from-start 可回放）"
            print(f"[trace] 跟踪 {path} {hint}\n", flush=True)
            announced = True
        fresh = [event for event in events if int(event.get("seq") or 0) > seen]
        if fresh:
            seen = max(int(event.get("seq") or 0) for event in fresh)
            for line in render_items(build_items(fresh, full=full, include_jobs=include_jobs), color=color):
                print(line, flush=True)
        time.sleep(interval)


def filter_items(
    items: list[TraceItem],
    *,
    errors_only: bool = False,
    tool: str | None = None,
    turn: int | None = None,
) -> list[TraceItem]:
    if not errors_only and not tool and turn is None:
        return items
    if turn is not None:
        items = [item for item in items if item.turn == turn or item.kind == "session"]
        if not errors_only and not tool:
            return items
    if errors_only:
        keep: list[TraceItem] = []
        emitted: set[int] = set()

        def add(item: TraceItem) -> None:
            if id(item) not in emitted:
                emitted.add(id(item))
                keep.append(item)

        context: TraceItem | None = None
        for index, item in enumerate(items):
            if item.kind in {"turn", "step"}:
                context = item
                continue
            if item.kind == "tool_result" and item.status == "error":
                if context is not None:
                    add(context)
                if index > 0 and items[index - 1].kind == "tool_call":
                    add(items[index - 1])
                add(item)
        items = keep
    if tool:
        wanted = tool.lower()
        items = [item for item in items if (item.tool or "").lower() == wanted or item.kind in {"turn", "step"}]
    return items
