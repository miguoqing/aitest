#!/usr/bin/env python3
"""校验 cases.json：原文回扫 + 结构 + 覆盖度。

用法:
    python3 verify_grounding.py --input cases.json \
        --corpus wiki/attendance-management --corpus raw/attendance-management

退出码:
    0  全部通过（可能带 warning）
    1  存在硬失败（引用对不上、字段缺失、编号重复、等级非法）

为什么这么做: 用例的可信度来自"每条断言都能指回需求原文"。这条检查把
"引用"从写作习惯变成可执行的验收条件，转述和拼接会立刻暴露。
"""

import argparse
import json
import re
import sys
from pathlib import Path

SEV = {"S1", "S2", "S3", "S4", "S5"}
METHODS = {"场景法", "判定表", "边界值分析", "等价类划分", "错误推测",
           "状态迁移", "流程分析", "矩阵分析", "因果图"}
TAGS = {"正向", "异常", "边界", "权限", "数据准确性", "数据一致性",
        "冒烟", "安全", "集成", "导出", "回归", "待确认", "建议"}
REQUIRED = ["id", "fp", "scene", "point", "title", "methods", "tags",
            "severity", "preconditions", "steps", "expected", "context"]
WS = re.compile(r"\s+")


def norm(text):
    return WS.sub("", text or "")


def load_corpus(paths):
    chunks = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.suffix.lower() in {".md", ".txt", ".markdown"}:
                    chunks.append(f.read_text(encoding="utf-8", errors="ignore"))
        elif p.is_file():
            chunks.append(p.read_text(encoding="utf-8", errors="ignore"))
        else:
            print(f"error: corpus path not found: {raw}", file=sys.stderr)
            sys.exit(1)
    if not chunks:
        print("error: corpus is empty", file=sys.stderr)
        sys.exit(1)
    text = "".join(chunks)
    # flat: 去掉全部空白（容错换行与缩进）；spaced: 换行折成空格（用于发现跨行拼接）
    spaced = re.sub(r"[ \t\r]*\n[ \t\r]*", " ", text)
    spaced = re.sub(r"[ \t]+", " ", spaced)
    return norm(text), spaced


def main():
    ap = argparse.ArgumentParser(description="verify cases.json against source corpus")
    ap.add_argument("--input", default="cases.json")
    ap.add_argument("--corpus", action="append", required=True,
                    help="需求来源文件或目录，可重复")
    args = ap.parse_args()

    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    corpus, corpus_spaced = load_corpus(args.corpus)
    cases = data.get("cases", [])
    fps = data.get("functional_points", {})
    points = data.get("points", {})

    hard, warn = [], []
    seen = set()

    for case in cases:
        cid = case.get("id", "<no-id>")
        for field in REQUIRED:
            if field not in case or case[field] in ("", [], {}, None):
                hard.append(f"{cid}: 缺少字段 {field}")
        if cid in seen:
            hard.append(f"{cid}: 用例ID重复")
        seen.add(cid)
        if case.get("severity") not in SEV:
            hard.append(f"{cid}: 严重等级非法 {case.get('severity')!r}")
        if case.get("fp") not in fps:
            hard.append(f"{cid}: 功能点 {case.get('fp')!r} 未在 functional_points 中定义")
        for pid in case.get("covers", []):
            if pid not in points:
                hard.append(f"{cid}: covers 引用了未定义的判定点 {pid}")

        ctx = case.get("context") or {}
        quote = (ctx.get("quote") or "").strip()
        if not quote:
            hard.append(f"{cid}: context.quote 为空")
        elif not ctx.get("source"):
            hard.append(f"{cid}: context.source 为空，无法定位引用出处")
        else:
            if norm(quote) not in corpus:
                hard.append(f"{cid}: 引用未在来源中命中 -> {quote[:40]}…")
            elif len(norm(quote)) < 10:
                warn.append(f"{cid}: 引用过短（{len(norm(quote))} 字），判定力弱")
            elif norm(quote) not in norm(corpus_spaced):
                warn.append(f"{cid}: 引用疑似跨行/跨段拼接，请确认原文确为连续语句")
        for method in case.get("methods", []):
            if method not in METHODS:
                hard.append(f"{cid}: 设计方法不在词表内 -> {method!r}")
        tags = case.get("tags", [])
        if len(tags) > 2:
            warn.append(f"{cid}: 标签 {len(tags)} 个，建议不超过 2 个")
        for tag in tags:
            if tag not in TAGS:
                warn.append(f"{cid}: 标签不在词表内 -> {tag!r}")
        if len(case.get("steps", [])) < 2:
            warn.append(f"{cid}: 测试步骤仅 {len(case.get('steps', []))} 步，通常无法复现")
        if not case.get("expected"):
            hard.append(f"{cid}: 预期结果为空")

    # 覆盖度
    covering = {pid: [] for pid in points}
    for case in cases:
        for pid in case.get("covers", []):
            covering.setdefault(pid, []).append(case["id"])
    for pid, meta in points.items():
        text = meta.get("text", "") if isinstance(meta, dict) else str(meta)
        status = (meta.get("status", "") if isinstance(meta, dict) else "") or ""
        if not covering.get(pid) and not status:
            warn.append(f"{pid}: 无对应用例且未声明状态 -> {text[:30]}")
        if status.startswith("未覆盖") and not text:
            warn.append(f"{pid}: 未覆盖但缺少说明文本")

    # 状态判定：显式声明优先，未声明则按是否被用例覆盖推断
    resolved = {}
    for pid, meta in points.items():
        declared = (meta.get("status", "") if isinstance(meta, dict) else "") or ""
        has_cases = bool(covering.get(pid))
        if declared.startswith("覆盖") and not declared.startswith("部分覆盖") and not has_cases:
            warn.append(f"{pid}: 声明「{declared}」但没有任何用例 covers 它，不计入覆盖")
            resolved[pid] = "未覆盖（待确认）"
        else:
            resolved[pid] = declared or ("覆盖" if has_cases else "未覆盖（待确认）")

    total = len(points)
    full = sum(1 for s in resolved.values() if s.startswith("覆盖"))
    partial = sum(1 for s in resolved.values() if s == "部分覆盖")
    rate = ((full + 0.5 * partial) / total * 100) if total else 0.0

    # 待确认双写：用例里写了待确认，open_items 里也要有；反之亦然
    ids = {c.get("id") for c in cases}
    open_ids = set()
    for item in data.get("open_items", []):
        raw = str(item.get("case_id") or "")
        for cid in re.split(r"[、,，;；\s]+", raw):
            if not cid:
                continue
            open_ids.add(cid)
            if cid not in ids:
                warn.append(f"open_items: 关联用例 {cid} 不存在")
    for case in cases:
        expects = case.get("expected", [])
        expects = [expects] if isinstance(expects, str) else expects
        if any("待确认" in str(e) for e in expects) and case.get("id") not in open_ids:
            warn.append(f"{case.get('id')}: 预期结果标了待确认，但 open_items 未登记")

    # 等级分布自查：S1 通胀是最常见的定级失误
    sev = {}
    for case in cases:
        sev[case.get("severity")] = sev.get(case.get("severity"), 0) + 1
    if len(cases) >= 10:
        s1_ratio = sev.get("S1", 0) / len(cases)
        if s1_ratio > 0.30:
            warn.append(f"S1 占比 {s1_ratio:.0%}，超过 30%，逐条重问「失败后用户还能否完成核心任务」")
        elif s1_ratio == 0:
            warn.append("没有任何 S1 用例，确认是否回避了核心风险路径")

    misses = sum(1 for h in hard if "引用未在来源中命中" in h)
    print(f"cases: {len(cases)}  判定点: {total}  覆盖: {full}  部分覆盖: {partial}  覆盖率: {rate:.1f}%")
    print(f"引用回扫: {len(cases) - misses}/{len(cases)} 命中")

    if warn:
        print("\nWARN:")
        for w in warn:
            print(f"  - {w}")
    if hard:
        print("\nFAIL:")
        for h in hard:
            print(f"  - {h}")
        print(f"\n{len(hard)} 项硬失败，修复后再交付")
        return 1

    print("\nOK: 结构与引用校验通过")
    if rate < 80:
        print(f"注意: 判定点覆盖率 {rate:.1f}% 低于 80%，建议补齐判定点或用例", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
