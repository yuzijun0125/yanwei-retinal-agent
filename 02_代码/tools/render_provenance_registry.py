# -*- coding: utf-8 -*-
"""
眼维数据 · 字段来源分级表渲染器  render_provenance_registry.py
================================================================
把 01_数据集/_quality/provenance_map.yaml（人工维护的字段级来源定义）
渲染成机器可读的 01_数据集/_quality/字段来源分级表.csv，
并**自动核对**：是否有业务字段漏登 / 登记了不存在的字段。

字段级 tier 是权威定义；CSV 里的行级 provenance_tier 只表示"本行实体"的等级。

用法：
  python render_provenance_registry.py            # 渲染 + 核对
  python render_provenance_registry.py --check    # 只核对，不写文件
"""
import csv
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
DATA = os.path.join(ROOT, "01_数据集")
YAML = os.path.join(DATA, "_quality", "provenance_map.yaml")
OUT = os.path.join(DATA, "_quality", "字段来源分级表.csv")

AUDIT_COLS = {"created_at", "updated_at", "deleted_at", "record_hash", "quality_score",
              "review_status", "data_version", "source_system", "provenance_tier",
              "official_source", "official_locator", "transform", "intermediate_source"}
TABLES = {
    "silver_case": "silver/silver_case.csv",
    "silver_grading": "silver/silver_grading.csv",
    "silver_image": "silver/silver_image.csv",
    "silver_lesion": "silver/silver_lesion.csv",
    "silver_pair_real": "silver/silver_pair_real.csv",
    "silver_pair_synthetic": "silver/silver_pair_synthetic.csv",
    "silver_region": "silver/silver_region.csv",
    "silver_text": "silver/silver_text.csv",
    "gold_relevance": "gold/gold_relevance.csv",
    "gold_retrieval_query": "gold/gold_retrieval_query.csv",
}


def split_inline(s):
    """按顶层逗号切分行内映射，尊重引号"""
    parts, buf, q = [], [], False
    for ch in s:
        if ch == '"':
            q = not q
            buf.append(ch)
        elif ch == "," and not q:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def parse_yaml(path):
    """解析 provenance_map.yaml 的 tables 段（行内映射格式）"""
    tables = {}
    cur = None
    in_fields = False
    with open(path, encoding="utf-8") as f:
        for ln in f:
            if re.match(r"^tables:\s*$", ln):
                continue
            m = re.match(r"^  (\w+):\s*$", ln)
            if m:
                cur = m.group(1)
                tables.setdefault(cur, {})
                in_fields = False
                continue
            if re.match(r"^\s+fields:\s*$", ln):
                in_fields = True
                continue
            m = re.match(r"^      (\w+):\s*\{(.*)\}\s*$", ln)
            if m and cur and in_fields:
                field, body = m.group(1), m.group(2)
                rec = {}
                for tok in split_inline(body):
                    if ":" not in tok:
                        continue
                    k, v = tok.split(":", 1)
                    v = v.strip()
                    if v.startswith('"') and v.endswith('"'):
                        v = v[1:-1]
                    rec[k.strip()] = v
                tables[cur][field] = rec
    return tables


def main():
    check_only = "--check" in sys.argv
    tables = parse_yaml(YAML)
    rows = []
    for t, fields in tables.items():
        for f, rec in fields.items():
            rows.append({
                "table": t,
                "field": f,
                "tier": rec.get("tier", ""),
                "official_source": rec.get("official_source", "") or ("NONE" if rec.get("official_source", "") == "" else ""),
                "official_locator": rec.get("official_locator", ""),
                "transform": rec.get("transform", ""),
                "pending": "1" if rec.get("pending") in ("true", "True") else "",
            })

    # ---- 与真实表字段核对 ----
    missing, extra = [], []
    for t, rel in TABLES.items():
        path = os.path.join(DATA, rel)
        if not os.path.exists(path):
            missing.append("<表缺失 %s>" % t)
            continue
        with open(path, encoding="utf-8-sig", newline="") as f:
            hdr = next(csv.reader(f), [])
        real = [c for c in hdr if c not in AUDIT_COLS]
        reg = set(tables.get(t, {}).keys())
        for c in real:
            if c not in reg:
                missing.append("%s.%s" % (t, c))
        for c in reg:
            if c not in hdr:
                extra.append("%s.%s" % (t, c))

    if not check_only:
        with open(OUT, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["table", "field", "tier", "official_source",
                                              "official_locator", "transform", "pending"])
            w.writeheader()
            for r in rows:
                w.writerow(r)

    from collections import Counter
    tc = Counter(r["tier"] for r in rows)
    print("=" * 74)
    print("字段来源分级表   %s" % ("【仅核对】" if check_only else "【已渲染】"))
    print("=" * 74)
    print("登记字段数: %d" % len(rows))
    print("等级分布  : %s" % dict(sorted(tc.items())))
    print()
    print("按表:")
    for t in TABLES:
        n = len(tables.get(t, {}))
        d = Counter(v.get("tier", "") for v in tables.get(t, {}).values())
        print("  %-24s %2d 字段  %s" % (t, n, dict(sorted(d.items()))))
    print()
    print("漏登的业务字段 (%d): %s" % (len(missing), missing or "无 ✅"))
    print("登记了不存在的字段 (%d): %s" % (len(extra), extra or "无 ✅"))
    if not check_only:
        print("\n已写出: %s" % OUT)
    print("=" * 74)
    return 1 if (missing or extra) else 0


if __name__ == "__main__":
    sys.exit(main())
