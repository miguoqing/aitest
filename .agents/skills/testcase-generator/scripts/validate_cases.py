#!/usr/bin/env python3
"""校验 YAML 测试用例集：结构、四级追溯链、证据回扫、覆盖与分布。

硬失败(非零退出) = 结构坏了或引用编造了，必须修。
告警(不阻断)     = 需要人判断的地方，交付前应逐条读。

用法:
    python3 validate_cases.py --topic-dir testcases/attendance \
        --corpus wiki/attendance-management --abbr ATT
    python3 validate_cases.py --topic-dir testcases/attendance --json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import unicodedata
from collections import Counter, defaultdict

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("需要 PyYAML: pip install pyyaml\n")
    raise SystemExit(2)

METHODS = {
    "边界值法", "等价类划分", "错误推测法", "穷举法", "逆向场景法", "因果图法",
    "场景法", "异常法", "并发法", "安全性法", "判定表法", "状态迁移法",
}
# 用户要求的"至少包含"这十种：每个测试点都必须对它们逐一表态(适用/不适用+原因)
CORE_METHODS = {
    "边界值法", "等价类划分", "错误推测法", "穷举法", "逆向场景法",
    "因果图法", "场景法", "异常法", "并发法", "安全性法",
}
TAGS = {
    "正向", "异常", "边界", "权限", "数据准确性", "数据一致性", "冒烟",
    "回归", "安全", "并发", "集成", "导出", "可用性", "待确认",
}
NEGATIVE_TAGS = {"异常", "边界", "安全", "并发"}
SEVERITIES = {"S1", "S2", "S3", "S4", "S5"}
STATUSES = {"active", "pending_confirmation"}

FP_RE = re.compile(r"^FP-(\d{2,})$")
SC_RE = re.compile(r"^SC-FP-(\d{2,})-(\d{2,})$")
TP_RE = re.compile(r"^TP-FP-(\d{2,})-(\d{2,})-(\d{2,})$")
TC_RE = re.compile(r"^TC-([A-Z]{2,8})-(\d{2})(\d{2})$")

REQUIRED_CASE = [
    "id", "fp_id", "scenario_id", "point_id", "functional_point", "scenario",
    "test_point", "title", "design_method", "tags", "severity", "severity_reason",
    "preconditions", "steps", "expected", "source_context",
]
REQUIRED_FP = ["id", "name", "module", "summary", "source_ref"]
REQUIRED_SC = ["id", "fp_id", "name", "description", "basis"]
REQUIRED_TP = ["id", "scenario_id", "statement", "evidence", "methods"]

VAGUE_PHRASES = (
    "功能正常", "显示正确", "一切正常", "正常工作", "符合预期",
    "体验良好", "友好", "合理", "显示正常", "工作正常", "使用正常",
)
TEXT_SUFFIXES = {".md", ".markdown", ".mdx", ".txt", ".rst", ".csv"}


# ---------------------------------------------------------------- 归一化

def _norm(text: str) -> str:
    """NFKC + 去掉所有空白和反斜杠。中文语料不需要空格，去掉反而更稳。"""
    t = unicodedata.normalize("NFKC", str(text))
    t = t.replace("\\", "")
    t = re.sub(r"[\s\u3000\u200b]+", "", t)
    return t.lower()


def _compact(text: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", _norm(text))


def _bigrams(text: str) -> set[str]:
    s = _norm(text)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ---------------------------------------------------------------- 语料

class Corpus:
    def __init__(self, paths: list[str]):
        self.files: dict[str, str] = {}
        for entry in paths:
            p = pathlib.Path(entry)
            targets = sorted(p.rglob("*")) if p.is_dir() else [p]
            for f in targets:
                if f.is_file() and f.suffix.lower() in TEXT_SUFFIXES:
                    try:
                        self.files[str(f.resolve())] = f.read_text(encoding="utf-8")
                    except Exception:  # noqa: BLE001
                        continue
        self.all_norm = _norm("\n".join(self.files.values()))
        self.all_compact = _compact(self.all_norm)

    def lookup(self, quote: str) -> str:
        """返回 'exact' | 'loose' | 'miss'。"""
        nq = _norm(quote)
        if len(nq) < 6:
            return "miss"
        if nq in self.all_norm:
            return "exact"
        cq = _compact(quote)
        if cq and cq in self.all_compact:
            return "loose"
        return "miss"

    def file_for(self, quote: str, declared: str | None) -> str | None:
        """声明路径命中返回 None，命中其他文件返回那个文件名。"""
        if not declared:
            return None
        candidates = {str(pathlib.Path(declared).resolve())}
        for resolved in self.files:
            if resolved.endswith(_norm_path_tail(declared)):
                candidates.add(resolved)
        for resolved in candidates:
            text = self.files.get(resolved)
            if text and _norm(quote) in _norm(text):
                return None
        hit_other = [p for p, t in self.files.items() if _norm(quote) in _norm(t)]
        if hit_other:
            return pathlib.Path(hit_other[0]).name
        return None


def _norm_path_tail(path: str) -> str:
    parts = [p for p in pathlib.Path(path).parts if p not in ("/", "\\")]
    return "/".join(parts[-3:]) if parts else path


# ---------------------------------------------------------------- 报告

class Report:
    def __init__(self) -> None:
        self.errors: list[dict] = []
        self.warnings: list[dict] = []

    def err(self, code: str, where: str, msg: str) -> None:
        self.errors.append({"code": code, "where": where, "message": msg})

    def warn(self, code: str, where: str, msg: str) -> None:
        self.warnings.append({"code": code, "where": where, "message": msg})


# ---------------------------------------------------------------- 加载

def _load(path: pathlib.Path, key: str, report: Report):
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        report.err("bad_yaml", path.name, f"YAML 解析失败: {exc}")
        return None
    if not isinstance(data, dict):
        report.err("bad_yaml", path.name, "顶层不是 mapping")
        return None
    items = data.get(key)
    if items is not None and not isinstance(items, list):
        report.err("bad_yaml", path.name, f"{key} 不是列表")
        return None
    return data


def _is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


# ---------------------------------------------------------------- 各级校验

def check_fps(fps: list[dict], report: Report) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for i, fp in enumerate(fps):
        where = str(fp.get("id") or f"functional_points[{i}]")
        if not isinstance(fp, dict):
            report.err("bad_item", where, "功能点不是 mapping")
            continue
        for field in REQUIRED_FP:
            if _is_blank(fp.get(field)):
                report.err("missing_field", where, f"功能点缺字段 {field}")
        match = FP_RE.match(str(fp.get("id", "")))
        if not match:
            report.err("bad_id_pattern", where, "功能点 ID 应为 FP-<两位数字>")
        src = fp.get("source_ref") or {}
        if isinstance(src, dict):
            for field in ("path", "section"):
                if _is_blank(src.get(field)):
                    report.err("missing_field", where, f"source_ref 缺 {field}")
        if str(fp.get("id")) in index:
            report.err("duplicate_id", where, "功能点 ID 重复")
        index[str(fp.get("id"))] = fp
    return index


def check_scs(scs: list[dict], fps: dict[str, dict], report: Report) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for i, sc in enumerate(scs):
        where = str(sc.get("id") or f"scenarios[{i}]")
        if not isinstance(sc, dict):
            report.err("bad_item", where, "场景不是 mapping")
            continue
        for field in REQUIRED_SC:
            if _is_blank(sc.get(field)):
                report.err("missing_field", where, f"场景缺字段 {field}")
        match = SC_RE.match(str(sc.get("id", "")))
        if not match:
            report.err("bad_id_pattern", where, "场景 ID 应为 SC-FP-<两位>-<两位>")
        fp_id = str(sc.get("fp_id", ""))
        if fps and fp_id not in fps:
            report.err("broken_chain", where, f"场景指向不存在的功能点 {fp_id}")
        if match and fp_id:
            fp_num = re.match(r"^FP-(\d+)", fp_id)
            if fp_num and int(fp_num.group(1)) != int(match.group(1)):
                report.err("broken_chain", where, "场景 ID 里的功能点序号与 fp_id 不一致")
        if str(sc.get("id")) in index:
            report.err("duplicate_id", where, "场景 ID 重复")
        index[str(sc.get("id"))] = sc
    return index


def check_tps(tps: list[dict], scs: dict[str, dict], report: Report) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for i, tp in enumerate(tps):
        where = str(tp.get("id") or f"test_points[{i}]")
        if not isinstance(tp, dict):
            report.err("bad_item", where, "测试点不是 mapping")
            continue
        for field in REQUIRED_TP:
            if _is_blank(tp.get(field)):
                report.err("missing_field", where, f"测试点缺字段 {field}")
        match = TP_RE.match(str(tp.get("id", "")))
        if not match:
            report.err("bad_id_pattern", where, "测试点 ID 应为 TP-FP-<两位>-<两位>-<两位>")
        sc_id = str(tp.get("scenario_id", ""))
        if scs and sc_id not in scs:
            report.err("broken_chain", where, f"测试点指向不存在的场景 {sc_id}")
        elif match and sc_id:
            sc_match = SC_RE.match(sc_id)
            if sc_match:
                if int(sc_match.group(1)) != int(match.group(1)) or \
                        int(sc_match.group(2)) != int(match.group(2)):
                    report.err("broken_chain", where, "测试点 ID 序号与 scenario_id 不一致")

        evidence = tp.get("evidence")
        if isinstance(evidence, dict):
            for field in ("quote", "path"):
                if _is_blank(evidence.get(field)):
                    report.err("missing_field", where, f"evidence 缺 {field}")

        methods = tp.get("methods")
        if isinstance(methods, dict):
            applicable = methods.get("applicable")
            if not isinstance(applicable, list) or not applicable:
                report.err("missing_field", where, "methods.applicable 不能为空")
            else:
                for m in applicable:
                    if m not in METHODS:
                        report.err("bad_enum", where, f"设计方法不在词表: {m}")
            for skipped in methods.get("skipped") or []:
                if not isinstance(skipped, dict):
                    report.err("bad_item", where, "methods.skipped 项不是 mapping")
                    continue
                if skipped.get("method") not in METHODS:
                    report.err("bad_enum", where, f"skipped 方法不在词表: {skipped.get('method')}")
                if _is_blank(skipped.get("reason")):
                    report.warn("method_skip_no_reason", where,
                                f"适用性判定缺原因: {skipped.get('method')}")
            accounted = set(applicable or [])
            accounted |= {s.get("method") for s in (methods.get("skipped") or [])
                          if isinstance(s, dict)}
            missing = sorted(CORE_METHODS - accounted)
            if missing:
                report.warn("methods_incomplete", where,
                            f"未逐点过筛的方法: {'、'.join(missing)}")
        if str(tp.get("id")) in index:
            report.err("duplicate_id", where, "测试点 ID 重复")
        index[str(tp.get("id"))] = tp
    return index


def check_cases(cases, fps, scs, tps, open_items, corpus, abbr, report):
    seen_ids: set[str] = set()
    for i, case in enumerate(cases):
        where = str(case.get("id") or f"cases[{i}]")
        if not isinstance(case, dict):
            report.err("bad_item", where, "用例不是 mapping")
            continue

        for field in REQUIRED_CASE:
            if _is_blank(case.get(field)):
                report.err("missing_field", where, f"缺字段 {field}")

        case_id = str(case.get("id"))
        if case_id in seen_ids:
            report.err("duplicate_id", where, "用例 ID 重复")
        seen_ids.add(case_id)

        match = TC_RE.match(str(case.get("id", "")))
        if not match:
            report.err("bad_id_pattern", where, "用例 ID 应为 TC-<缩写>-<四位数字>")
        elif abbr and match.group(1) != abbr.upper():
            report.warn("abbr_mismatch", where, f"用例 ID 缩写 {match.group(1)} ≠ {abbr}")

        severity = case.get("severity")
        if severity not in SEVERITIES:
            report.err("bad_enum", where, f"严重等级非法: {severity}")

        method = case.get("design_method")
        if method not in METHODS:
            report.err("bad_enum", where, f"设计方法不在词表: {method}")

        tags = case.get("tags")
        if not isinstance(tags, list) or not tags:
            report.err("missing_field", where, "tags 不能为空")
            tags = []
        else:
            for tag in tags:
                if tag not in TAGS:
                    report.err("bad_enum", where, f"标签不在词表: {tag}")
            if len(tags) > 3:
                report.warn("too_many_tags", where, f"标签 {len(tags)} 个，建议 ≤3")
            if "正向" in tags and "异常" in tags:
                report.warn("mixed_direction_tags", where, "同时挂正向与异常，通常该拆")

        status = case.get("status", "active")
        if status not in STATUSES:
            report.err("bad_enum", where, f"status 非法: {status}")

        steps = case.get("steps")
        if not isinstance(steps, list) or len(steps) < 2:
            report.err("bad_steps", where, "steps 至少 2 步")
        else:
            if len(steps) < 3:
                report.warn("few_steps", where, f"steps 只有 {len(steps)} 步")
            for j, step in enumerate(steps):
                if _is_blank(step):
                    report.err("bad_steps", where, f"第 {j + 1} 步为空")
                elif isinstance(step, str) and re.match(r"^\s*(预期|期望|应|结果)[:：]", step):
                    report.warn("expected_in_steps", where, f"第 {j + 1} 步像预期，不该写在步骤里")

        expected = case.get("expected")
        if not isinstance(expected, list) or not expected:
            report.err("bad_expected", where, "expected 不能为空")
        else:
            for item in expected:
                if _is_blank(item):
                    report.err("bad_expected", where, "expected 含空项")
                elif isinstance(item, str) and any(p in item for p in VAGUE_PHRASES):
                    report.warn("vague_expected", where, f"预期不可判定: {item[:40]}")

        # 追溯链
        point_id = str(case.get("point_id", ""))
        scenario_id = str(case.get("scenario_id", ""))
        fp_id = str(case.get("fp_id", ""))
        if tps and point_id not in tps:
            report.err("broken_chain", where, f"指向不存在的测试点 {point_id}")
        elif tps:
            tp = tps[point_id]
            if str(tp.get("scenario_id")) != scenario_id:
                report.err("broken_chain", where,
                           f"测试点属于 {tp.get('scenario_id')}，用例却写 {scenario_id}")
        if scs and scenario_id not in scs:
            report.err("broken_chain", where, f"指向不存在的场景 {scenario_id}")
        elif scs:
            sc = scs[scenario_id]
            if str(sc.get("fp_id")) != fp_id:
                report.err("broken_chain", where,
                           f"场景属于 {sc.get('fp_id')}，用例却写 {fp_id}")
        if fps and fp_id not in fps:
            report.err("broken_chain", where, f"指向不存在的功能点 {fp_id}")
        elif fps and match:
            fp_num = re.match(r"^FP-(\d+)", fp_id)
            if fp_num and int(fp_num.group(1)) != int(match.group(2)):
                report.warn("case_id_fp_mismatch", where,
                            f"ID 序号 {match.group(2)} 与 {fp_id} 不一致")
            declared = str(case.get("functional_point", "")).strip()
            name = str(fps[fp_id].get("name", "")).strip()
            if declared and name and _norm(declared) != _norm(name):
                report.warn("functional_point_mismatch", where,
                            f"functional_point 与 {fp_id} 的 name 不一致")

        # 证据回扫
        ctx = case.get("source_context") or {}
        if isinstance(ctx, dict):
            quote = str(ctx.get("quote") or "")
            if quote and corpus.files:
                verdict = corpus.lookup(quote)
                if verdict == "miss":
                    report.err("ungrounded_quote", where,
                               f"引用在知识原文中找不到: {quote[:50]}")
                elif verdict == "loose":
                    report.warn("loose_quote", where,
                                "引用仅在忽略标点后才命中，检查是否拼接或改写")
                other = corpus.file_for(quote, ctx.get("path"))
                if other and verdict != "miss":
                    report.warn("quote_wrong_file", where,
                                f"引用实际出现在 {other}，与声明的 path 不符")
                if len(_norm(quote)) > 240:
                    report.warn("long_quote", where, "引用过长，提取最相关的一句即可")
                if "..." in quote or "…" in quote:
                    report.warn("quote_ellipsis", where, "引用含省略号，不是逐字原文")

        # 待确认登记
        registered = set()
        for oi in open_items:
            for ref in (oi.get("related_case_ids") or []):
                registered.add(str(ref))
        if status == "pending_confirmation" and where not in registered:
            report.warn("pending_not_registered", where, "待确认用例未登记 open_items")
        if status == "active" and where in registered:
            report.warn("registered_but_active", where, "已登记待确认却标 active")

        # 三个一句话字段
        if _norm(str(case.get("scenario", ""))) == _norm(str(case.get("test_point", ""))):
            report.warn("scenario_equals_point", where, "scenario 与 test_point 完全相同")

    # 语义近似(同测试点内)
    by_point: dict[str, list[dict]] = defaultdict(list)
    for case in cases:
        if isinstance(case, dict) and case.get("point_id"):
            by_point[str(case["point_id"])].append(case)

    for point_id, group in by_point.items():
        seen: dict[tuple, str] = {}
        for case in group:
            key = (
                str(case.get("design_method")),
                _norm("|".join(str(s) for s in (case.get("steps") or []))),
            )
            if key in seen:
                report.warn("duplicate_case", str(case.get("id")),
                            f"与 {seen[key]} 步骤与方法完全相同")
            else:
                seen[key] = str(case.get("id"))
        for a in range(len(group)):
            for b in range(a + 1, len(group)):
                ca, cb = group[a], group[b]
                if ca.get("design_method") == cb.get("design_method"):
                    continue
                score = _jaccard(_bigrams(str(ca.get("title", ""))),
                                 _bigrams(str(cb.get("title", ""))))
                if score >= 0.85:
                    report.warn("near_duplicate", str(cb.get("id")),
                                f"标题与 {ca.get('id')} 高度相似({score:.2f})，确认是否重复")


def check_coverage(cases, fps, scs, tps, report):
    cases_by_point: dict[str, list[dict]] = defaultdict(list)
    for case in cases:
        if isinstance(case, dict) and case.get("point_id"):
            cases_by_point[str(case["point_id"])].append(case)

    points_by_scenario: dict[str, list[dict]] = defaultdict(list)
    for tp in tps.values():
        points_by_scenario[str(tp.get("scenario_id"))].append(tp)

    scenarios_by_fp: dict[str, list[dict]] = defaultdict(list)
    for sc in scs.values():
        scenarios_by_fp[str(sc.get("fp_id"))].append(sc)

    for point_id, tp in tps.items():
        group = cases_by_point.get(point_id, [])
        if not group:
            report.warn("tp_without_cases", point_id, f"测试点未被任何用例覆盖: {tp.get('statement', '')[:40]}")
            continue
        applicable = (tp.get("methods") or {}).get("applicable") or []
        used = {c.get("design_method") for c in group}
        for method in applicable:
            if method not in used:
                report.warn("method_uncovered", point_id,
                            f"标了适用但无用例: {method}")

    for sc_id in scs:
        if not points_by_scenario.get(sc_id):
            report.warn("scenario_without_points", sc_id, "场景下没有测试点")

    for fp_id, fp in fps.items():
        group = cases_by_point_of_fp(cases, fp_id)
        if not scenarios_by_fp.get(fp_id):
            report.warn("fp_without_scenarios", fp_id, "功能点下没有场景")
        if not group:
            report.warn("fp_without_cases", fp_id, "功能点下没有用例")
            continue
        tags = {t for c in group for t in (c.get("tags") or [])}
        if "正向" not in tags:
            report.warn("fp_missing_positive", fp_id, "功能点缺正向用例")
        if not (tags & NEGATIVE_TAGS):
            report.warn("fp_missing_negative", fp_id, "功能点缺异常/边界/安全类用例")


def cases_by_point_of_fp(cases, fp_id):
    return [c for c in cases
            if isinstance(c, dict) and str(c.get("fp_id")) == str(fp_id)]


def check_distribution(cases, report):
    severities = Counter(c.get("severity") for c in cases if isinstance(c, dict))
    total = sum(severities.values())
    if not total:
        return severities
    s1 = severities.get("S1", 0)
    if s1 == 0:
        report.warn("no_s1", "全部用例", "没有 S1 用例，可能没在验真正会出事的路径")
    elif s1 / total > 0.30:
        report.warn("s1_ratio_high", "全部用例",
                    f"S1 占 {s1}/{total}={s1 / total:.0%}，逐条复核是否通胀")
    return severities


def check_open_items(open_items, case_ids, report):
    for i, oi in enumerate(open_items):
        where = str(oi.get("id") or f"open_items[{i}]")
        if not isinstance(oi, dict):
            report.err("bad_item", where, "open_items 项不是 mapping")
            continue
        if _is_blank(oi.get("question")):
            report.err("missing_field", where, "open_items 缺 question")
        refs = oi.get("related_case_ids")
        if not isinstance(refs, list) or not refs:
            report.warn("open_item_no_refs", where, "open_items 没关联任何用例")
            continue
        for ref in refs:
            if str(ref) not in case_ids:
                report.warn("open_item_bad_ref", where, f"关联了不存在的用例 {ref}")


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="校验 YAML 测试用例集")
    ap.add_argument("--topic-dir", required=True)
    ap.add_argument("--corpus", action="append", default=[],
                    help="知识原文文件或目录，可重复")
    ap.add_argument("--abbr", default=None, help="用例 ID 里的项目缩写，如 ATT")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--no-corpus", action="store_true", help="跳过引用回扫")
    args = ap.parse_args()

    topic_dir = pathlib.Path(args.topic_dir)
    if not topic_dir.is_dir():
        sys.stderr.write(f"目录不存在: {topic_dir}\n")
        return 2

    report = Report()
    corpus = Corpus([] if args.no_corpus else args.corpus)
    if not args.no_corpus and not corpus.files:
        report.warn("no_corpus", "参数", "没有找到可用于回扫的语料，引用检查被跳过")

    fp_doc = _load(topic_dir / "01-functional-points.yaml", "functional_points", report)
    sc_doc = _load(topic_dir / "02-scenarios.yaml", "scenarios", report)
    tp_doc = _load(topic_dir / "03-test-points.yaml", "test_points", report)
    tc_doc = _load(topic_dir / "testcases.yaml", "cases", report)

    if tc_doc is None:
        report.err("no_cases", "testcases.yaml", "缺少 testcases.yaml 或无法解析")
        return _emit(report, {}, args, None)

    for name, doc in (("01-functional-points.yaml", fp_doc),
                      ("02-scenarios.yaml", sc_doc),
                      ("03-test-points.yaml", tp_doc)):
        if doc is None:
            report.warn("missing_stage_file", name, "缺少该级文件，追溯链与覆盖度检查被削弱")

    fps = check_fps((fp_doc or {}).get("functional_points") or [], report)
    scs = check_scs((sc_doc or {}).get("scenarios") or [], fps, report)
    tps = check_tps((tp_doc or {}).get("test_points") or [], scs, report)

    cases = tc_doc.get("cases") or []
    if not cases:
        report.err("no_cases", "testcases.yaml", "用例列表为空")

    open_items = tc_doc.get("open_items") or []
    case_ids = {str(c.get("id")) for c in cases if isinstance(c, dict)}

    check_cases(cases, fps, scs, tps, open_items, corpus, args.abbr, report)
    check_open_items(open_items, case_ids, report)
    check_coverage(cases, fps, scs, tps, report)
    severities = check_distribution(cases, report)

    covered = {str(c.get("point_id")) for c in cases if isinstance(c, dict)}
    tp_total = len(tps) or len([t for t in covered if t])
    tp_covered = sum(1 for tp_id in tps if tp_id in covered) if tps else 0
    stats = {
        "functional_points": len(fps),
        "scenarios": len(scs),
        "test_points": len(tps),
        "cases": len(cases),
        "open_items": len(open_items),
        "tp_coverage": (tp_covered / tp_total) if tp_total else None,
        "tp_covered": tp_covered,
        "severity": dict(sorted(severities.items())),
        "corpus_files": len(corpus.files),
    }
    return _emit(report, stats, args, tp_total)


def _emit(report: Report, stats: dict, args, tp_total) -> int:
    if args.json:
        print(json.dumps(
            {"errors": report.errors, "warnings": report.warnings, "stats": stats},
            ensure_ascii=False, indent=2))
        return 1 if report.errors else 0

    print("=== 用例集校验 ===")
    if stats:
        coverage = stats.get("tp_coverage")
        coverage_text = f"{coverage:.1%}" if coverage is not None else "n/a(缺测试点清单)"
        print(f"功能点 {stats['functional_points']} | 场景 {stats['scenarios']} | "
              f"测试点 {stats['test_points']} | 用例 {stats['cases']} | "
              f"待确认 {stats['open_items']}")
        print(f"测试点覆盖率 {coverage_text}  ({stats['tp_covered']}/{tp_total or 0})")
        if stats.get("severity"):
            dist = " ".join(f"{k}:{v}" for k, v in stats["severity"].items())
            print(f"等级分布 {dist}")
        print(f"语料文件 {stats['corpus_files']} 个")

    print(f"\n硬失败 {len(report.errors)} | 告警 {len(report.warnings)}")

    if report.errors:
        print("\n[硬失败] 必须修复")
        for item in report.errors:
            print(f"  {item['code']:<20} {item['where']:<24} {item['message']}")

    if report.warnings:
        grouped: dict[str, list[dict]] = defaultdict(list)
        for item in report.warnings:
            grouped[item["code"]].append(item)
        print("\n[告警] 需要判断")
        for code in sorted(grouped, key=lambda c: (-len(grouped[c]), c)):
            items = grouped[code]
            print(f"  {code} ({len(items)})")
            for item in items[:6]:
                print(f"      {item['where']:<24} {item['message']}")
            if len(items) > 6:
                print(f"      ... 另有 {len(items) - 6} 条")

    if not report.errors:
        print("\n结论: 结构通过。" +
              ("告警需逐条判断后再交付。" if report.warnings else " 无告警。"))
    else:
        print("\n结论: 不合格，先修硬失败。")

    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
