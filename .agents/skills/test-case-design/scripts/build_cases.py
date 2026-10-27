#!/usr/bin/env python3
"""cases.json -> 测试用例 Markdown + Excel。

用法:
    python3 build_cases.py --input cases.json --out-dir testcases

产物:
    <out-dir>/<slug>-test-cases.md
    <out-dir>/<slug>-test-cases.xlsx   （无 openpyxl 时降级为 .csv）

设计约束:
    渲染只负责排版，不补内容。cases.json 里缺的字段会原样暴露为空，不猜。
"""

import argparse
import csv
import json
import re
import sys
from collections import Counter, OrderedDict
from pathlib import Path

SEVERITY = {
    "S1": ("致命", "核心业务不可用、数据错误或权限越权，直接影响对外结果"),
    "S2": ("严重", "主要功能失败或结果与规则不一致，用户无法完成关键操作"),
    "S3": ("一般", "次要功能异常或有可用替代路径，影响范围有限"),
    "S4": ("轻微", "提示、展示、体验类问题，不影响业务结果"),
    "S5": ("建议", "优化建议或边界补充，非缺陷"),
}
SEV_ORDER = ["S1", "S2", "S3", "S4", "S5"]

METHOD_NOTE = {
    "场景法": "按业务主线串联配置与操作",
    "判定表": "多条件与开关组合下的判定规则",
    "边界值分析": "数值阈值、上下限、恰好命中",
    "等价类划分": "输入可按互斥类划分的场景",
    "错误推测": "越权、绕过入口、文档留白处",
    "状态迁移": "状态机流转前后的一致性",
    "流程分析": "多步链路：导入导出、权限分配、系统集成",
    "矩阵分析": "同一数据在多套配置下的横向对比",
    "因果图": "多条件因果关系",
}


def slugify(text):
    slug = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "-", text).strip("-").lower()
    return slug or "test-cases"


def cell(text):
    """表格单元格转义：竖线与换行会破坏 Markdown 表格。"""
    return str(text).replace("|", "／").replace("\n", "<br>")


def load(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    data.setdefault("slug", slugify(data.get("title", "test-cases")))
    data.setdefault("version", "v1.0")
    data.setdefault("sources", [])
    data.setdefault("functional_points", {})
    data.setdefault("points", {})
    data.setdefault("cases", [])
    data.setdefault("open_items", [])
    return data


def point_status(data):
    """判定点状态：显式声明优先，否则按被引用情况推断。"""
    covering = {pid: [] for pid in data["points"]}
    for case in data["cases"]:
        for pid in case.get("covers", []):
            covering.setdefault(pid, []).append(case["id"])
    out = {}
    for pid, meta in data["points"].items():
        declared = (meta.get("status") if isinstance(meta, dict) else None) or ""
        if not declared:
            declared = "覆盖" if covering.get(pid) else "未覆盖（待确认）"
        out[pid] = {"text": meta.get("text", "") if isinstance(meta, dict) else str(meta),
                    "status": declared, "cases": covering.get(pid, [])}
    return out


def coverage_stats(data):
    status = point_status(data)
    total = len(status)
    full = sum(1 for v in status.values() if v["status"].startswith("覆盖"))
    partial = sum(1 for v in status.values() if v["status"] == "部分覆盖")
    rate = ((full + 0.5 * partial) / total * 100) if total else 0.0
    return status, total, full, partial, rate


def fmt_expected(items):
    if isinstance(items, str):
        return [items]
    return list(items)


def build_markdown(data):
    cases = data["cases"]
    fps = data["functional_points"]
    by_fp = OrderedDict()
    for case in cases:
        by_fp.setdefault(case.get("fp", "未分组"), []).append(case)

    sev_count = Counter(c.get("severity") for c in cases)
    method_count = Counter()
    tag_count = Counter()
    for case in cases:
        for m in case.get("methods", []):
            method_count[m] += 1
        for t in case.get("tags", []):
            tag_count[t] += 1

    status, total_points, full, partial, rate = coverage_stats(data)

    L = []
    w = L.append

    w(f"# {data['title']} — 测试用例文档")
    w("")
    w(f"> 版本：{data['version']} ｜ 编写日期：{data.get('date', '')} ｜ 用例总数：{len(cases)}")
    for src in data["sources"]:
        w(f"> 依据：`{src['path']}`（{src.get('label', '')}）")
    w("")
    w("## 1. 文档说明")
    w("")
    w("### 1.1 范围")
    w("")
    w(f"本套用例覆盖 {len(fps)} 个功能点：" + "、".join(v.get("name", k) for k, v in fps.items()) + "。")
    w("")
    w("### 1.2 依据与边界")
    w("")
    w("| 项 | 说明 |")
    w("| --- | --- |")
    w(f"| 需求来源 | {data.get('source_note', '见文件头依据')} |")
    w("| 事实约束 | 用例中的入口、开关名、数值、示例数据均取自需求原文，未引入外部经验值 |")
    w("| 文档未定义处 | 预期结果标注「待确认」，不臆造行为，统一登记在 4.5 节 |")
    w("")
    w("### 1.3 严重等级定义（5 档）")
    w("")
    w("| 等级 | 名称 | 判定标准 | 用例数 |")
    w("| --- | --- | --- | --- |")
    for key in SEV_ORDER:
        name, desc = SEVERITY[key]
        w(f"| {key} | {name} | {desc} | {sev_count.get(key, 0)} |")
    w("")
    w("### 1.4 设计方法与标签")
    w("")
    w("| 设计方法 | 使用次数 | 适用说明 |")
    w("| --- | --- | --- |")
    for method, count in method_count.most_common():
        w(f"| {method} | {count} | {METHOD_NOTE.get(method, '')} |")
    w("")
    w("**关键标签**：" + "、".join(f"{t}({n})" for t, n in tag_count.most_common()))
    w("")

    w("## 2. 用例总览")
    w("")
    w("| 用例ID | 功能点 | 测试点 | 测试用例 | 等级 | 设计方法 | 标签 |")
    w("| --- | --- | --- | --- | --- | --- | --- |")
    for fp, group in by_fp.items():
        fp_name = fps.get(fp, {}).get("name", fp)
        for case in group:
            w(f"| {case['id']} | {cell(fp_name)} | {cell(case.get('point', ''))} | {cell(case.get('title', ''))} "
              f"| {case.get('severity', '')} | {cell(', '.join(case.get('methods', [])))} "
              f"| {cell(', '.join(case.get('tags', [])))} |")
    w("")

    w("## 3. 用例明细")
    w("")
    for fp, group in by_fp.items():
        meta = fps.get(fp, {})
        w(f"### {fp} {meta.get('name', '')}")
        w("")
        if meta.get("doc"):
            w(f"对应文档：`{meta['doc']}` — {meta.get('section', '')}")
            w("")
        for case in group:
            sev = case.get("severity", "")
            w(f"#### {case['id']} {case.get('title', '')}")
            w("")
            w("| 项 | 内容 |")
            w("| --- | --- |")
            w(f"| 功能点 | {cell(meta.get('name', fp))} |")
            w(f"| 测试场景 | {cell(case.get('scene', ''))} |")
            w(f"| 测试点 | {cell(case.get('point', ''))} |")
            w(f"| 测试用例 | {cell(case.get('title', ''))} |")
            w(f"| 设计方法 | {cell(', '.join(case.get('methods', [])))} |")
            w(f"| 关键标签 | {cell(', '.join(case.get('tags', [])))} |")
            w(f"| 严重等级 | {sev} {SEVERITY.get(sev, ('', ''))[0]} |")
            w(f"| 前置条件 | {cell(case.get('preconditions', ''))} |")
            w("")
            w("**测试步骤**")
            w("")
            for i, step in enumerate(case.get("steps", []), 1):
                w(f"{i}. {step}")
            w("")
            w("**预期结果（测试结果）**")
            w("")
            for exp in fmt_expected(case.get("expected", [])):
                w(f"- {exp}")
            w("")
            w("**原始文档上下文**")
            w("")
            ctx = case.get("context", {})
            w(f"> {ctx.get('quote', '')}")
            w(">")
            w(f"> —— `{ctx.get('source', '')}`")
            w("")

    w("## 4. 覆盖度统计报告")
    w("")
    w("### 4.1 功能点覆盖")
    w("")
    w("| 功能点 | 名称 | 用例数 | " + " | ".join(SEV_ORDER) + " | 判定点数 |")
    w("| --- | --- | --- | " + " | ".join("---" for _ in SEV_ORDER) + " | --- |")
    per_fp_points = 0
    for fp, group in by_fp.items():
        sc = Counter(c.get("severity") for c in group)
        pts = {p for c in group for p in c.get("covers", [])}
        per_fp_points += len(pts)
        row = [fp, fps.get(fp, {}).get("name", fp), str(len(group))]
        row += [str(sc.get(k, 0)) for k in SEV_ORDER]
        row.append(str(len(pts)))
        w("| " + " | ".join(row) + " |")
    total_row = ["**合计**", "—", f"**{len(cases)}**"] + [f"**{sev_count.get(k, 0)}**" for k in SEV_ORDER]
    total_row.append(f"**{per_fp_points}**")
    w("| " + " | ".join(total_row) + " |")
    if per_fp_points != total_points:
        w("")
        w(f"> 判定点跨功能点时按功能点分别计数，故合计 {per_fp_points} 条 > 全局去重 {total_points} 条；覆盖率以全局去重为准。")
    w("")

    w("### 4.2 判定点覆盖矩阵")
    w("")
    w("| 判定点 | 内容 | 覆盖用例 | 状态 |")
    w("| --- | --- | --- | --- |")
    for pid, meta in status.items():
        ids = "、".join(meta["cases"]) or "—"
        w(f"| {pid} | {cell(meta['text'])} | {ids} | {meta['status']} |")
    w("")
    w(f"- 判定点总数：{total_points}；完全覆盖 {full} 条，部分覆盖 {partial} 条")
    w(f"- **判定点覆盖率 = ({full} + 0.5×{partial}) / {total_points} = {rate:.1f}%**")
    w("")

    w("### 4.3 章节覆盖")
    w("")
    w("| 文档 | 章节 | 对应用例 |")
    w("| --- | --- | --- |")
    for fp, group in by_fp.items():
        meta = fps.get(fp, {})
        ids = "、".join(c["id"] for c in group)
        w(f"| {meta.get('doc', '—')} | {meta.get('section', '—')} | {ids} |")
    w("")

    w("### 4.4 分布统计")
    w("")
    w("| 维度 | 分布 |")
    w("| --- | --- |")
    w("| 严重等级 | " + "、".join(f"{k} {SEVERITY[k][0]} {sev_count.get(k, 0)} 条" for k in SEV_ORDER) + " |")
    w("| 设计方法 | " + "、".join(f"{m} {n}" for m, n in method_count.most_common()) + " |")
    w("| 关键标签 | " + "、".join(f"{t} {n}" for t, n in tag_count.most_common()) + " |")
    w("| 覆盖方式 | 正向 " + str(tag_count.get("正向", 0)) + " 条、异常 " + str(tag_count.get("异常", 0))
      + " 条、边界 " + str(tag_count.get("边界", 0)) + " 条、权限 " + str(tag_count.get("权限", 0)) + " 条 |")
    w("")

    w("### 4.5 未覆盖与待确认清单")
    w("")
    if data["open_items"]:
        w("| # | 待确认项 | 关联用例 | 文档缺口说明 |")
        w("| --- | --- | --- | --- |")
        for i, item in enumerate(data["open_items"], 1):
            w(f"| {i} | {cell(item.get('name', ''))} | {item.get('case_id', '')} | {cell(item.get('note', ''))} |")
    else:
        w("(none)")
    w("")

    s1 = [c["id"] for c in cases if c.get("severity") == "S1"]
    empty_fps = [fp for fp in fps if fp not in by_fp]
    pos, neg = tag_count.get("正向", 0), tag_count.get("异常", 0)
    w("### 4.6 结论")
    w("")
    if empty_fps:
        w(f"1. {len(fps)} 个功能点中有 {len(empty_fps)} 个尚未建立用例（{('、'.join(empty_fps))}）；"
          f"{total_points} 条判定点中 {full} 条完全覆盖、{partial} 条部分覆盖，覆盖率 {rate:.1f}%。")
    else:
        w(f"1. {len(fps)} 个功能点均已建立用例；{total_points} 条判定点中 {full} 条完全覆盖、{partial} 条部分覆盖，覆盖率 {rate:.1f}%。")
    if pos and neg:
        w(f"2. 正向 {pos} 条、异常 {neg} 条、边界 {tag_count.get('边界', 0)} 条，正反成对，不只覆盖主链路。")
    else:
        w(f"2. 正向 {pos} 条、异常 {neg} 条：分布不完整，"
          f"{'缺少异常用例，缺陷会从反方向漏出' if not neg else '缺少正向用例，主链路未被验证'}。")
    if s1:
        w(f"3. S1 必跑集（{len(s1)} 条）：{'、'.join(s1)}，建议进冒烟与回归。")
    else:
        w("3. 没有 S1 用例：确认是否回避了会直接造成数据错误、资金损失或越权的路径。")
    if data["open_items"]:
        w(f"4. 共 {len(data['open_items'])} 项文档未定义行为，已登记待确认；确认结论前，相关用例的预期结果不作为缺陷判定依据。")
    else:
        w("4. 未登记待确认项：确认需求确实没有留白，而不是把留白写成了确定的预期。")
    w("")
    return "\n".join(L)


def build_excel(data, path):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        csv_path = path.with_suffix(".csv")
        with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["用例ID", "功能点", "测试场景", "测试点", "测试用例", "设计方法",
                             "关键标签", "严重等级", "前置条件", "测试步骤", "预期结果", "原始文档上下文"])
            for case in data["cases"]:
                writer.writerow([
                    case["id"], case.get("fp", ""), case.get("scene", ""), case.get("point", ""),
                    case.get("title", ""), ", ".join(case.get("methods", [])), ", ".join(case.get("tags", [])),
                    case.get("severity", ""), case.get("preconditions", ""),
                    "\n".join(f"{i}. {s}" for i, s in enumerate(case.get("steps", []), 1)),
                    "\n".join(f"• {e}" for e in fmt_expected(case.get("expected", []))),
                    case.get("context", {}).get("quote", ""),
                ])
        return csv_path

    wb = Workbook()
    ws = wb.active
    ws.title = "测试用例"
    headers = ["用例ID", "功能点", "功能点名称", "测试场景", "测试点", "测试用例", "设计方法", "关键标签",
               "严重等级", "严重等级名称", "前置条件", "测试步骤", "预期结果（测试结果）", "原始文档上下文", "依据文档"]
    ws.append(headers)

    head_fill = PatternFill("solid", fgColor="1F4E79")
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for c in ws[1]:
        c.fill = head_fill
        c.font = Font(color="FFFFFF", bold=True, size=10)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = border

    sev_fill = {"S1": "FFC7CE", "S2": "FFEB9C", "S3": "DDEBF7", "S4": "E2EFDA", "S5": "F2F2F2"}
    fps = data["functional_points"]
    for case in data["cases"]:
        sev = case.get("severity", "")
        ws.append([
            case["id"], case.get("fp", ""), fps.get(case.get("fp"), {}).get("name", ""),
            case.get("scene", ""), case.get("point", ""), case.get("title", ""),
            ", ".join(case.get("methods", [])), ", ".join(case.get("tags", [])),
            sev, SEVERITY.get(sev, ("", ""))[0], case.get("preconditions", ""),
            "\n".join(f"{i}. {s}" for i, s in enumerate(case.get("steps", []), 1)),
            "\n".join(f"• {e}" for e in fmt_expected(case.get("expected", []))),
            case.get("context", {}).get("quote", ""),
            (case.get("context", {}).get("path") or case.get("context", {}).get("source", "")),
        ])
        row = ws.max_row
        for cl in ws[row]:
            cl.alignment = Alignment(vertical="top", wrap_text=True)
            cl.border = border
            cl.font = Font(size=10)
        ws.cell(row=row, column=9).fill = PatternFill("solid", fgColor=sev_fill.get(sev, "FFFFFF"))
        ws.cell(row=row, column=1).font = Font(size=10, bold=True)

    widths = [12, 8, 18, 14, 30, 34, 16, 14, 9, 9, 26, 46, 40, 46, 36]
    for i, wd in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = wd
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:O{ws.max_row}"

    status, total_points, full, partial, rate = coverage_stats(data)
    ws2 = wb.create_sheet("覆盖度统计")
    ws2.append(["一、功能点覆盖"])
    ws2.append(["功能点", "名称", "用例数"] + SEV_ORDER + ["判定点数", "对应用例"])
    by_fp = OrderedDict()
    for case in data["cases"]:
        by_fp.setdefault(case.get("fp", "未分组"), []).append(case)
    for fp, group in by_fp.items():
        sc = Counter(c.get("severity") for c in group)
        ws2.append([fp, fps.get(fp, {}).get("name", fp), len(group)]
                   + [sc.get(k, 0) for k in SEV_ORDER]
                   + [len({p for c in group for p in c.get("covers", [])}),
                      "、".join(c["id"] for c in group)])
    ws2.append([])
    ws2.append(["二、判定点覆盖矩阵"])
    ws2.append(["判定点", "内容", "覆盖用例", "状态"])
    for pid, meta in status.items():
        ws2.append([pid, meta["text"], "、".join(meta["cases"]) or "—", meta["status"]])
    ws2.append([])
    ws2.append(["判定点覆盖率", f"({full} + 0.5×{partial}) / {total_points} = {rate:.1f}%"])
    ws2.append([])
    ws2.append(["三、待确认项"])
    ws2.append(["#", "待确认项", "关联用例", "文档缺口说明"])
    for i, item in enumerate(data["open_items"], 1):
        ws2.append([i, item.get("name", ""), item.get("case_id", ""), item.get("note", "")])

    head_rows = {"一、功能点覆盖", "功能点", "二、判定点覆盖矩阵", "判定点", "三、待确认项", "#"}
    for row in ws2.iter_rows():
        for cl in row:
            cl.alignment = Alignment(vertical="top", wrap_text=True)
        if row[0].value in head_rows:
            for cl in row:
                if cl.value:
                    cl.font = Font(bold=True, color="FFFFFF")
                    cl.fill = head_fill
    for col, wd in zip("ABCDEFGHIJ", [14, 40, 10, 6, 6, 6, 6, 6, 10, 40]):
        ws2.column_dimensions[col].width = wd

    wb.save(path)
    return path


def main():
    ap = argparse.ArgumentParser(description="cases.json -> md + xlsx")
    ap.add_argument("--input", default="cases.json")
    ap.add_argument("--out-dir", default="testcases")
    args = ap.parse_args()

    data = load(args.input)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    md = build_markdown(data)
    md_path = out / f"{data['slug']}-test-cases.md"
    md_path.write_text(md + "\n", encoding="utf-8")

    xlsx_path = build_excel(data, out / f"{data['slug']}-test-cases.xlsx")

    status, total, full, partial, rate = coverage_stats(data)
    print(f"cases={len(data['cases'])} points={total} coverage={rate:.1f}%")
    print(f"md={md_path}")
    print(f"table={xlsx_path}")
    if total and not full:
        print("warning: 没有任何判定点被用例覆盖，检查 cases[].covers", file=sys.stderr)


if __name__ == "__main__":
    main()
