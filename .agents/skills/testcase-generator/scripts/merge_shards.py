#!/usr/bin/env python3
"""合并某一级的分片 YAML 成一个阶段文件。

多智能体流水线里每个 agent 只写自己那一片，这个脚本负责机械合并：
按 ID 排序、检出重复 ID、汇总 meta。语义去重交给合并 agent，脚本不猜。

用法:
    python3 merge_shards.py --stage cases \
        --shard-dir testcases/attendance/.shards/cases \
        --out testcases/attendance/testcases.yaml \
        --topic attendance --title 打卡管理

退出码: 0 成功; 1 有重复 ID 或结构错误(文件仍会写出,便于查看); 2 输入不可用。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import pathlib
import re
import sys

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("需要 PyYAML: pip install pyyaml\n")
    raise SystemExit(2)

STAGES = {
    "functional-points": "functional_points",
    "scenarios": "scenarios",
    "test-points": "test_points",
    "cases": "cases",
}


def _natural_key(value: str):
    """让 FP-2 排在 FP-10 前面，而不是字典序。"""
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", str(value))]


def _load_shard(path: pathlib.Path, top_key: str):
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return None, f"{path.name}: YAML 解析失败 ({exc})"

    if data is None:
        return None, f"{path.name}: 空文件"
    if isinstance(data, list):
        items, meta = data, {}
    elif isinstance(data, dict):
        items = data.get(top_key)
        if items is None:
            # agent 有时会用单数键名
            for alt in (top_key.rstrip("s"), top_key.replace("_", "")):
                if alt in data:
                    items = data[alt]
                    break
        meta = data.get("meta") or {}
    else:
        return None, f"{path.name}: 顶层既不是 list 也不是 dict"

    if not isinstance(items, list):
        return None, f"{path.name}: 找不到列表键 {top_key}"
    for i, item in enumerate(items):
        if not isinstance(item, dict) or not item.get("id"):
            return None, f"{path.name}: 第 {i + 1} 项缺少 id"
    return (items, meta), None


def main() -> int:
    ap = argparse.ArgumentParser(description="合并分片 YAML")
    ap.add_argument("--stage", required=True, choices=sorted(STAGES))
    ap.add_argument("--shard-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--topic", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--generated-at", default=None)
    args = ap.parse_args()

    top_key = STAGES[args.stage]
    shard_dir = pathlib.Path(args.shard_dir)
    if not shard_dir.is_dir():
        sys.stderr.write(f"分片目录不存在: {shard_dir}\n")
        return 2

    shard_files = sorted(
        [p for p in shard_dir.iterdir() if p.suffix in (".yaml", ".yml")],
        key=lambda p: _natural_key(p.name),
    )
    if not shard_files:
        sys.stderr.write(f"分片目录里没有 YAML: {shard_dir}\n")
        return 2

    items, sources, problems = [], {}, []
    for path in shard_files:
        loaded, err = _load_shard(path, top_key)
        if err:
            problems.append(err)
            continue
        shard_items, meta = loaded
        items.extend(shard_items)
        for src in (meta.get("knowledge_sources") or []):
            if isinstance(src, dict) and src.get("path"):
                sources[src["path"]] = src.get("title") or pathlib.Path(src["path"]).stem

    by_id: dict[str, list[str]] = {}
    for item in items:
        by_id.setdefault(str(item["id"]), []).append(item.get("title") or item.get("name") or "")

    duplicates = {k: v for k, v in by_id.items() if len(v) > 1}
    if duplicates:
        for dup_id, labels in sorted(duplicates.items(), key=lambda kv: _natural_key(kv[0])):
            problems.append(f"重复 ID {dup_id}: {labels}")

    items.sort(key=lambda it: _natural_key(it["id"]))

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    doc = {
        "meta": {
            "topic": args.topic,
            "title": args.title or args.topic,
            "stage": args.stage,
            "generated_at": args.generated_at or _dt.date.today().isoformat(),
            "generator": "testcase-generator",
            "shard_count": len(shard_files),
            "knowledge_sources": [
                {"path": p, "title": t} for p, t in sorted(sources.items())
            ],
        },
        top_key: items,
    }

    if args.stage == "cases":
        open_items: dict[str, dict] = {}
        for path in shard_files:
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except Exception:  # noqa: BLE001
                continue
            for oi in (data.get("open_items") or []) if isinstance(data, dict) else []:
                if isinstance(oi, dict) and oi.get("question"):
                    open_items[str(oi.get("id") or oi["question"])] = oi
        doc["open_items"] = [
            open_items[k] for k in sorted(open_items, key=_natural_key)
        ]
        doc["meta"]["stats"] = {"cases": len(items), "open_items": len(doc["open_items"])}

    out.write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )

    print(f"合并 {len(shard_files)} 个分片 → {len(items)} 条 {top_key} → {out}")
    if problems:
        print("\n问题:")
        for p in problems:
            print(f"  - {p}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
