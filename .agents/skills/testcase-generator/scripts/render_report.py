#!/usr/bin/env python3
"""从 YAML 用例集生成覆盖度报告（Markdown）。

报告回答四个问题：覆盖到哪一级、缺口在哪、方法用得偏不偏、多少条只能待确认。
数字全部从 YAML 实算，不手填。

用法:
    python3 render_report.py --topic-dir testcases/attendance \
        --out testcases/attendance/coverage-report.md
"""
from __future__ import annotations

import argparse
import datetime as _dt
import pathlib
import re
import sys
from collections import Counter, defaultdict

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("需要 PyYAML: pip install pyyaml\n")
    raise SystemExit(2)

NEGATIVE_TAGS = {"异常", "边界", "安全", "并发"}


def _load(path: pathlib.Path, key: str):
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"{path.name} 解析失败: {exc}\n")
        return None
    if not isinstance(data, dict):
        return None
    items = data.get(key)
    return items if isinstance(items, list) else None


def _table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(out)


def _pct(part: int, total: int) -> str:
    return f"{part / total:.1%}" if total else "n/a"


def main() -> int:
    ap = argparse.ArgumentParser(description="生成覆盖度报告")
    ap.add_argument("--topic-dir", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    topic_dir = pathlib.Path(args.topic_dir)
    fps = _load(topic_dir / "01-functional-points.yaml", "functional_points") or []
    scs = _load(topic_dir / "02-scenarios.yaml", "scenarios") or []
    tps = _load(topic_dir / "03-test-points.yaml", "test_points") or []
    tc_doc = yaml.safe_load((topic_dir / "testcases.yaml").read_text(encoding="utf-8")) \
        if (topic_dir / "testcases.yaml").exists() else {}
    tc_doc = tc_doc if isinstance(tc_doc, dict) else {}
    cases = tc_doc.get("cases") or []
    open_items = tc_doc.get("open_items") or []
    meta = tc_doc.get("meta") or {}

    if not cases:
        sys.stderr.write("testcases.yaml 没有用例，无法生成报告\n")
        return 2

    cases_by_point: dict[str, list[dict]] = defaultdict(list)
    cases_by_fp: dict[str, list[dict]] = defaultdict(list)
    for case in cases:
        cases_by_point[str(case.get("point_id"))].append(case)
        cases_by_fp[str(case.get("fp_id"))].append(case)

    points_by_scenario: dict[str, list[dict]] = defaultdict(list)
    for tp in tps:
        points_by_scenario[str(tp.get("scenario_id"))].append(tp)

    scenarios_by_fp: dict[str, list[dict]] = defaultdict(list)
    for sc in scs:
        scenarios_by_fp[str(sc.get("fp_id"))].append(sc)

    covered_points = [tp for tp in tps if cases_by_point.get(str(tp.get("id")))]
    uncovered_points = [tp for tp in tps if not cases_by_point.get(str(tp.get("id")))]

    topics = meta.get("topic") or topic_dir.name
    title = meta.get("title") or topics
    generated = _dt.date.today().isoformat()

    lines: list[str] = []
    add = lines.append

    add(f"# 测试用例覆盖度报告 — {title}")
    add("")
    add(f"- 主题：`{topics}`")
    add(f"- 生成日期：{generated}")
    add(f"- 用例规模：{len(cases)} 条")
    if meta.get("knowledge_sources"):
        add("- 知识来源：")
        for src in meta["knowledge_sources"]:
            if isinstance(src, dict):
                add(f"  - `{src.get('path')}`（{src.get('title') or ''}）")
    add("")

    # ---- 总览
    add("## 1. 规模总览")
    add("")
    add(_table(
        ["层级", "数量", "说明"],
        [["功能点", len(fps), "独立可验证的业务能力"],
         ["测试场景", len(scs), "在什么条件下验什么"],
         ["测试点", len(tps), "可判定真伪的断言"],
         ["测试用例", len(cases), "一条一个目标"]],
    ))
    add("")
    add(f"测试点覆盖率 **{_pct(len(covered_points), len(tps))}**"
        f"（{len(covered_points)}/{len(tps)}）。"
        "分母是测试点，不是用例数——用例数是工作量指标。")
    add("")

    # ---- 逐功能点
    add("## 2. 逐功能点覆盖")
    add("")
    rows = []
    for fp in fps:
        fp_id = str(fp.get("id"))
        group = cases_by_fp.get(fp_id, [])
        tags = {t for c in group for t in (c.get("tags") or [])}
        fp_tps = [tp for sc in scenarios_by_fp.get(fp_id, [])
                  for tp in points_by_scenario.get(str(sc.get("id")), [])]
        fp_covered = [tp for tp in fp_tps if cases_by_point.get(str(tp.get("id")))]
        rows.append([
            fp_id,
            fp.get("name", ""),
            len(scenarios_by_fp.get(fp_id, [])),
            len(fp_tps),
            len(group),
            sum(1 for c in group if c.get("severity") == "S1"),
            "✓" if "正向" in tags else "—",
            "✓" if tags & NEGATIVE_TAGS else "—",
            _pct(len(fp_covered), len(fp_tps)) if fp_tps else "n/a",
        ])
    add(_table(
        ["功能点", "名称", "场景", "测试点", "用例", "S1", "正向", "负向", "TP覆盖"],
        rows,
    ))
    add("")

    # ---- 方法矩阵
    add("## 3. 设计方法分布")
    add("")
    method_counts = Counter(c.get("design_method") for c in cases)
    add(_table(
        ["设计方法", "用例数", "占比"],
        [[m, n, _pct(n, len(cases))]
         for m, n in method_counts.most_common()],
    ))
    add("")
    applied = Counter()
    for tp in tps:
        for m in ((tp.get("methods") or {}).get("applicable") or []):
            applied[m] += 1
    missing_methods = [m for m, n in applied.items() if method_counts.get(m, 0) == 0]
    if missing_methods:
        add(f"**标为适用却零用例的方法**：{'、'.join(sorted(missing_methods))}。"
            "要么补用例，要么改回不适用。")
        add("")
    if method_counts:
        top_method, top_n = method_counts.most_common(1)[0]
        if top_n / len(cases) > 0.5:
            add(f"**方法过度集中**：`{top_method}` 占 {top_n / len(cases):.0%}，"
                "通常说明方法分析退化成了形式主义。")
            add("")

    # ---- 等级
    add("## 4. 严重等级分布")
    add("")
    severity_counts = Counter(c.get("severity") for c in cases)
    add(_table(
        ["等级", "名称", "数量", "占比"],
        [[lv, name, severity_counts.get(lv, 0), _pct(severity_counts.get(lv, 0), len(cases))]
         for lv, name in [("S1", "致命"), ("S2", "严重"), ("S3", "一般"),
                          ("S4", "轻微"), ("S5", "建议")]],
    ))
    add("")
    s1 = severity_counts.get("S1", 0)
    if s1 == 0:
        add("⚠ 没有 S1 用例。可能没有在验真正会出事的路径。")
    elif s1 / len(cases) > 0.30:
        add(f"⚠ S1 占 {s1 / len(cases):.0%}，超过 30%。"
            "资金/权限类需求可豁免，其余需逐条复核是否通胀。")
    else:
        add(f"S1 占比 {s1 / len(cases):.0%}，处于常见区间（10%–25% 附近）。")
    add("")

    # ---- 缺口
    add("## 5. 缺口清单")
    add("")
    gaps: list[str] = []
    if uncovered_points:
        gaps.append(f"- **未覆盖测试点 {len(uncovered_points)} 个**：")
        for tp in uncovered_points[:20]:
            gaps.append(f"  - `{tp.get('id')}` {str(tp.get('statement'))[:60]}")
        if len(uncovered_points) > 20:
            gaps.append(f"  - …… 另有 {len(uncovered_points) - 20} 个")
    for sc in scs:
        if not points_by_scenario.get(str(sc.get("id"))):
            gaps.append(f"- 场景无测试点：`{sc.get('id')}` {sc.get('name', '')}")
    for fp in fps:
        fp_id = str(fp.get("id"))
        group = cases_by_fp.get(fp_id, [])
        tags = {t for c in group for t in (c.get("tags") or [])}
        if not group:
            gaps.append(f"- 功能点无用例：`{fp_id}` {fp.get('name', '')}")
        elif "正向" not in tags:
            gaps.append(f"- 功能点缺正向用例：`{fp_id}` {fp.get('name', '')}")
        elif not (tags & NEGATIVE_TAGS):
            gaps.append(f"- 功能点缺负向用例：`{fp_id}` {fp.get('name', '')}")
    add("\n".join(gaps) if gaps else "未发现结构性缺口。")
    add("")

    # ---- 待确认
    add("## 6. 待确认清单")
    add("")
    if not open_items:
        pending_cases = [c for c in cases if c.get("status") == "pending_confirmation"]
        add("无登记。" if not pending_cases
            else f"⚠ 有 {len(pending_cases)} 条用例标了待确认但未登记 open_items。")
    else:
        add(_table(
            ["编号", "待确认问题", "涉及用例"],
            [[oi.get("id", ""), oi.get("question", ""),
              "、".join(str(x) for x in (oi.get("related_case_ids") or []))]
             for oi in open_items],
        ))
        add("")
        add("这些是知识没写清的地方。确认前不作为缺陷判定依据。")
    add("")

    # ---- 标签
    add("## 7. 标签分布")
    add("")
    tag_counts = Counter(t for c in cases for t in (c.get("tags") or []))
    add(_table(["标签", "用例数"], [[t, n] for t, n in tag_counts.most_common()]))
    add("")

    # ---- 索引
    add("## 8. 用例索引")
    add("")
    index_rows = []
    for case in cases:
        index_rows.append([
            case.get("id", ""),
            str(case.get("title", ""))[:48],
            case.get("severity", ""),
            case.get("design_method", ""),
            "、".join(case.get("tags") or []),
        ])
    add(_table(["用例ID", "测试用例", "等级", "方法", "标签"], index_rows))
    add("")
    add("---")
    add("")
    add("本报告由 `scripts/render_report.py` 从 YAML 用例集实算生成。"
        "覆盖度分母是测试点；缺口清单不含语义重复问题，那部分见质量门禁的审查记录。")

    text = "\n".join(lines) + "\n"
    out = pathlib.Path(args.out) if args.out else topic_dir / "coverage-report.md"
    out.write_text(text, encoding="utf-8")
    print(f"报告已生成: {out}（{len(cases)} 条用例，{len(lines)} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
