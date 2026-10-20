#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
provenance_stats.py —— 字段来源分级统计（零依赖）

读取 01_数据集/_quality/provenance_map.yaml，统计每个表的字段来源等级分布，
输出：T1/T2/T3/T4 汇总、按表分布、pending（等官方 CSV 即可升级）清单。

用法：
    python provenance_stats.py                # 打印报告
    python provenance_stats.py --json         # 输出 JSON
"""
import os
import re
import sys
import json
import collections

HERE = os.path.dirname(os.path.abspath(__file__))
MAP_PATH = os.path.abspath(os.path.join(HERE, "..", "..", "01_数据集", "_quality", "provenance_map.yaml"))

TIER_NAME = {"T1": "官方直取", "T2": "官方派生", "T3": "规则派生", "T4": "合成/外部"}

RE_TABLE = re.compile(r"^([a-z_]+):\s*$")
RE_FIELD_TIER = re.compile(r"^([a-z_][A-Za-z0-9_]*):[ \t]*\{[^}]*\btier:[ \t]*(T[1-4])\b")
RE_TIER_ANY = re.compile(r"\btier:[ \t]*(T[1-4])\b")


def parse(path):
    tables = collections.OrderedDict()
    cur = None
    for raw in open(path, encoding="utf-8"):
        line = raw.rstrip("\n")
        if line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        if not stripped:
            continue
        # 表名：仅二级缩进且无 '{'
        m = RE_TABLE.match(stripped) if (line.startswith("  ") and not line.startswith("   ")) else None
        if m:
            cur = m.group(1)
            tables.setdefault(cur, {})
            continue
        # 字段行：形如  field_name: { tier: T2, ... }
        m = RE_FIELD_TIER.match(stripped)
        if m and cur:
            fname, tier = m.group(1), m.group(2)
            pending = "pending: true" in stripped
            tables[cur][fname] = {"tier": tier, "pending": pending}
    return tables


def main():
    tables = parse(MAP_PATH)
    tier_total = collections.Counter()
    pending = []
    for tname, fields in tables.items():
        for fname, info in fields.items():
            tier_total[info["tier"]] += 1
            if info["pending"]:
                pending.append("%s.%s" % (tname, fname))

    total = sum(tier_total.values()) or 1
    if "--json" in sys.argv:
        print(json.dumps({
            "map_path": MAP_PATH,
            "total_fields": sum(tier_total.values()),
            "by_tier": dict(tier_total),
            "by_table": {t: dict(collections.Counter(f["tier"] for f in fs.values())) for t, fs in tables.items()},
            "pending_upgradeable_to_T1": pending,
        }, ensure_ascii=False, indent=2))
        return

    print("=" * 66)
    print("眼维数据集 · 字段来源分级统计")
    print("来源定义: %s" % MAP_PATH)
    print("=" * 66)
    print()
    print("【总体分布】共 %d 个已标注字段" % sum(tier_total.values()))
    for t in ("T1", "T2", "T3", "T4"):
        n = tier_total.get(t, 0)
        bar = "#" * int(round(36.0 * n / total))
        print("  %s %-10s %3d 个  %5.1f%%  %s" % (t, TIER_NAME[t], n, 100.0 * n / total, bar))
    print()
    print("【按表分布】")
    print("  %-26s %4s %4s %4s %4s  %5s" % ("表", "T1", "T2", "T3", "T4", "合计"))
    for tname, fields in tables.items():
        c = collections.Counter(f["tier"] for f in fields.values())
        print("  %-26s %4d %4d %4d %4d  %5d" % (
            tname, c.get("T1", 0), c.get("T2", 0), c.get("T3", 0), c.get("T4", 0), sum(c.values())))
    print()
    print("【等官方 CSV 即可升级为 T1 的字段】(%d 个)" % len(pending))
    for p in pending:
        print("  - %s" % p)
    print()
    print("【核心读数】")
    print("  当前真正官方直取(T1): %d 个" % tier_total.get("T1", 0))
    print("  待官方 CSV 补齐可升 T1: %d 个" % len(pending))


if __name__ == "__main__":
    main()
