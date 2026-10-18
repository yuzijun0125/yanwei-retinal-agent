#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
眼维 · 数据体检脚本  dataset_health.py
=====================================
零依赖（仅 Python 标准库）。对 01_数据集/ 下的 silver/gold 层做结构体检，
产出可复跑的基线报告（JSON + Markdown）。

设计目标：改数据之前先量清楚，改完再量一遍做对比。
每次任何数据改动，都应先跑本脚本确认没有引入新的断裂。

体检维度
  1. 表概况      行数 / 字段数 / 空值率 Top
  2. 主键        唯一性（有无重复 / 有无空）
  3. 外键        跨表引用完整性（含 gold 跨层多态引用）
  4. 枚举        关键字段取值分布
  5. 一致性      重复真源（lesion vs region）字段逐值比对
  6. 命名规范    ID 字段是否含非 ASCII（中文）
  7. 覆盖率      实体间覆盖关系（谁该有谁）
  8. 溯源完整性  官方锚定列是否齐备 / tier 合法 / T1-T2 是否真有官方出处 / 字段是否全在分级表登记

用法
  python dataset_health.py
  python dataset_health.py --root <数据集目录> --out <输出目录>
  python dataset_health.py --quiet          # 只写报告，不打印
"""

import argparse
import csv
import json
import os
import sys
from collections import Counter

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
DEFAULT_DATA = os.path.join(PROJ_ROOT, "01_数据集")

# ──────────────────────────── 配置区 ────────────────────────────
# 表 → (相对路径, 主键字段)
TABLES = {
    "silver_case":           ("silver/silver_case.csv",           "case_id"),
    "silver_grading":        ("silver/silver_grading.csv",        "grading_id"),
    "silver_image":          ("silver/silver_image.csv",          "image_id"),
    "silver_lesion":         ("silver/silver_lesion.csv",         "annotation_id"),
    "silver_pair_real":      ("silver/silver_pair_real.csv",      "pair_id"),
    "silver_pair_synthetic": ("silver/silver_pair_synthetic.csv", "pair_id"),
    "silver_region":         ("silver/silver_region.csv",         "region_id"),
    "silver_text":           ("silver/silver_text.csv",           "text_id"),
    "gold_relevance":        ("gold/gold_relevance.csv",          "relevance_id"),
    "gold_retrieval_query":  ("gold/gold_retrieval_query.csv",    "query_id"),
}

# 单值外键：(子表, 子字段, 母表, 母字段)
FKS = [
    ("silver_image",          "case_id",   "silver_case",          "case_id"),
    ("silver_region",         "image_id",  "silver_image",         "image_id"),
    ("silver_lesion",         "image_id",  "silver_image",         "image_id"),
    ("silver_lesion",         "region_id", "silver_region",        "region_id"),
    ("silver_lesion",         "text_id",   "silver_text",          "text_id"),
    ("silver_text",           "case_id",   "silver_case",          "case_id"),
    ("silver_pair_synthetic", "image_id",  "silver_image",         "image_id"),
    ("silver_pair_synthetic", "region_id", "silver_region",        "region_id"),
    ("silver_pair_synthetic", "text_id",   "silver_text",          "text_id"),
    ("silver_grading",        "image_id",  "silver_image",         "image_id"),
    ("gold_relevance",        "query_id",  "gold_retrieval_query", "query_id"),
]

# 多态引用：(子表, 子字段, [(值前缀, 母表, 母字段, 规范化后前缀), ...])
# gold_relevance.candidate_id 既可能指区域、也可能指文本，需按前缀分流
POLYMORPHIC = (
    "gold_relevance", "candidate_id",
    [
        ("区域_", "silver_region", "region_id", "REG_"),
        ("REG_",  "silver_region", "region_id", "REG_"),
        ("文本_", "silver_text",   "text_id",   "TXT_"),
        ("TXT_",  "silver_text",   "text_id",   "TXT_"),
    ],
)

# 枚举字段：子表 → [字段...]
ENUMS = {
    "silver_case":           ["eye", "split", "review_status", "data_version"],
    "silver_image":          ["eye", "split", "modality", "quality_label", "license",
                              "license_showable", "review_status"],
    "silver_region":         ["region_type", "lesion_type", "location", "is_lesion",
                              "region_source", "review_status"],
    "silver_lesion":         ["lesion_type", "location", "severity", "evidence_strength",
                              "review_status"],
    "silver_text":           ["text_type", "text_origin", "language", "license", "review_status"],
    "silver_pair_synthetic": ["pair_level", "metric_usable", "review_status"],
    "gold_retrieval_query":  ["query_type", "query_origin", "metric_validity", "split",
                              "review_status"],
    "gold_relevance":        ["relevance", "judgment_source", "review_status"],
}

# 一致性：两张表共享同一实体键，逐值比对重叠字段
# (表A, 键A, 表B, 键B, [(字段A, 字段B), ...])
CONSISTENCY = [
    ("silver_lesion", "region_id", "silver_region", "region_id",
     [("lesion_type", "lesion_type"),
      ("location", "location"),
      ("lesion_area_ratio", "area_ratio")]),
]

# 命名规范：这些 ID 字段不得含非 ASCII（中文）
NAMING = {
    "silver_case":           ["case_id"],
    "silver_image":          ["image_id", "case_id"],
    "silver_region":         ["region_id", "image_id"],
    "silver_lesion":         ["annotation_id", "region_id", "text_id"],
    "silver_pair_synthetic": ["pair_id", "region_id", "text_id"],
    "silver_text":           ["text_id", "case_id"],
    "gold_relevance":        ["relevance_id", "query_id", "candidate_id"],
    "gold_retrieval_query":  ["query_id", "query_image"],
}

# 覆盖率：(母表, 母键, 子表, 子字段, 说明)
# 注意：子表空值不计入覆盖（文献证据的 case_id 本就为空，属正常设计）
COVERAGE = [
    ("silver_case",   "case_id",   "silver_image",  "case_id",   "病例→图像"),
    ("silver_image",  "image_id",  "silver_region", "image_id",  "图像→区域"),
    ("silver_region", "region_id", "silver_lesion", "region_id", "区域→病灶"),
    ("silver_case",   "case_id",   "silver_text",   "case_id",   "病例→文本"),
    ("silver_region", "region_id", "silver_pair_synthetic", "region_id", "区域→区域级配对"),
]

NON_ASCII = "非ASCII(中文)"

# ── 溯源完整性（Step-1 溯源锚定重构后新增的校验维度）──
PROVENANCE_REQUIRED = ["provenance_tier", "official_source",
                       "official_locator", "transform", "intermediate_source"]
TIERS = {"T1", "T2", "T3", "T4"}
NO_OFFICIAL = "NONE"
# 这些是审计/治理列，不属于"业务字段"，不要求在字段来源分级表里登记
AUDIT_COLS = {"created_at", "updated_at", "deleted_at", "record_hash", "quality_score",
              "review_status", "data_version", "source_system", "provenance_tier",
              "official_source", "official_locator", "transform", "intermediate_source"}


# ──────────────────────────── 工具 ────────────────────────────
def load_table(data_root, rel_path):
    """读一张 CSV，返回 (fields, rows)。文件不存在返回 (None, None)。"""
    path = os.path.join(data_root, rel_path)
    if not os.path.exists(path):
        return None, None
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fields = list(reader.fieldnames or [])
        rows = [dict(r) for r in reader]
    return fields, rows


def has_non_ascii(s):
    return any(ord(ch) > 127 for ch in s)


def pct(n, d):
    return round(100.0 * n / d, 2) if d else 0.0


# ──────────────────────────── 各项检查 ────────────────────────────
def check_provenance(tables, data_root):
    """溯源完整性：官方锚定列是否齐备、tier 合法、T1/T2 是否真有官方出处、字段是否全部登记"""
    out = {"tables": {}, "registry": {}}
    for name in TABLES:
        fields, rows = tables.get(name, (None, None))
        rec = {"missing_columns": [], "bad_tier": 0, "t1t2_without_official": 0,
               "blank_transform": 0, "tier_dist": {}, "sample_missing": []}
        if fields is None:
            rec.update(status="表缺失", ok=None)
            out["tables"][name] = rec
            continue
        rec["missing_columns"] = [c for c in PROVENANCE_REQUIRED if c not in fields]
        if not rows:
            rec["status"] = "空表(仅校验列)"
            rec["ok"] = not rec["missing_columns"]
            out["tables"][name] = rec
            continue
        tc = Counter()
        for r in rows:
            t = str(r.get("provenance_tier", "")).strip()
            tc[t] += 1
            if t not in TIERS:
                rec["bad_tier"] += 1
            osrc = str(r.get("official_source", "")).strip()
            oloc = str(r.get("official_locator", "")).strip()
            if t in ("T1", "T2") and (osrc in ("", NO_OFFICIAL) or oloc in ("", NO_OFFICIAL)):
                rec["t1t2_without_official"] += 1
                if len(rec["sample_missing"]) < 3:
                    rec["sample_missing"].append(osrc + " | " + oloc)
            if str(r.get("transform", "")).strip() == "":
                rec["blank_transform"] += 1
        rec["tier_dist"] = dict(sorted(tc.items()))
        rec["ok"] = (not rec["missing_columns"] and rec["bad_tier"] == 0
                     and rec["t1t2_without_official"] == 0 and rec["blank_transform"] == 0)
        rec["status"] = "OK" if rec["ok"] else "异常"
        out["tables"][name] = rec

    # 字段来源分级表覆盖核对：每个业务字段都应在登记表中出现
    reg_path = os.path.join(data_root, "_quality", "字段来源分级表.csv")
    registered = set()
    if os.path.exists(reg_path):
        with open(reg_path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                registered.add("%s.%s" % (r.get("table", "").strip(), r.get("field", "").strip()))
    unregistered = []
    for name in TABLES:
        fields, _rows = tables.get(name, (None, None))
        if not fields:
            continue
        for c in fields:
            if c in AUDIT_COLS:
                continue
            if "%s.%s" % (name, c) not in registered:
                unregistered.append("%s.%s" % (name, c))
    out["registry"] = {
        "path": reg_path,
        "exists": os.path.exists(reg_path),
        "registered_fields": len(registered),
        "unregistered": unregistered,
        "ok": bool(registered) and not unregistered,
        "status": ("OK" if (registered and not unregistered)
                   else ("登记表缺失" if not os.path.exists(reg_path) else "有未登记字段")),
    }
    return out


def check_overview(tables):
    """表概况：行数 / 字段数 / 空值率 Top5"""
    out = {}
    for name, (fields, rows) in tables.items():
        if fields is None:
            out[name] = {"exists": False}
            continue
        nulls = Counter()
        for r in rows:
            for k, v in r.items():
                if v is None or str(v).strip() == "":
                    nulls[k] += 1
        top = [{"field": k, "null": c, "null_pct": pct(c, len(rows))}
               for k, c in nulls.most_common(5) if c > 0]
        out[name] = {
            "exists": True,
            "rows": len(rows),
            "fields": len(fields),
            "empty_table": len(rows) == 0,
            "null_fields": len([k for k, c in nulls.items() if c > 0]),
            "null_top5": top,
        }
    return out


def check_primary_key(tables):
    """主键唯一性"""
    out = {}
    for name, (rel, key) in TABLES.items():
        fields, rows = tables[name]
        if fields is None or not rows:
            out[name] = {"key": key, "status": "空表/缺失", "ok": None}
            continue
        if key not in fields:
            out[name] = {"key": key, "status": "主键字段不存在!", "ok": False}
            continue
        vals = [str(r.get(key, "")).strip() for r in rows]
        blanks = sum(1 for v in vals if v == "")
        dup = [v for v, c in Counter(vals).items() if c > 1]
        out[name] = {
            "key": key,
            "status": "OK" if (not dup and blanks == 0) else "异常",
            "ok": (not dup and blanks == 0),
            "blank": blanks,
            "dup_count": len(dup),
            "dup_sample": dup[:5],
        }
    return out


def check_foreign_keys(tables):
    """单值外键引用完整性"""
    out = []
    for child, cfield, parent, pfield in FKS:
        cf, cr = tables.get(child, (None, None))
        pf, pr = tables.get(parent, (None, None))
        rec = {"child": "%s.%s" % (child, cfield),
               "parent": "%s.%s" % (parent, pfield)}
        if cr is None or pr is None:
            rec.update(status="跳过(表缺失)", ok=None)
            out.append(rec)
            continue
        if not cr:
            rec.update(status="跳过(子表空)", ok=None, child_rows=0)
            out.append(rec)
            continue
        pids = set(str(r.get(pfield, "")).strip() for r in pr)
        vals = [str(r.get(cfield, "")).strip() for r in cr]
        nonblank = [v for v in vals if v != ""]
        blank = len(vals) - len(nonblank)
        missing = sorted(set(v for v in nonblank if v not in pids))
        rec.update(
            child_rows=len(cr),
            blank=blank,
            blank_pct=pct(blank, len(vals)),
            distinct=len(set(nonblank)),
            missing=len(missing),
            missing_sample=missing[:5],
            ok=(len(missing) == 0),
            status="OK" if not missing else ("全断裂" if len(missing) == len(set(nonblank)) else "部分断裂"),
        )
        out.append(rec)
    return out


def check_polymorphic(tables):
    """gold_relevance.candidate_id 多态引用"""
    child, cfield, routes = POLYMORPHIC
    cf, cr = tables.get(child, (None, None))
    rec = {"child": "%s.%s" % (child, cfield), "routes": [], "status": "", "ok": None}
    if not cr:
        rec["status"] = "跳过(子表空)"
        return rec

    # 母表 ID 池（同时建"规范化后 ID → 原始 ID"映射）
    pools = {}
    for prefix, ptable, pfield, _norm in routes:
        pf, pr = tables.get(ptable, (None, None))
        if pr is None:
            continue
        pools[(ptable, pfield)] = set(str(r.get(pfield, "")).strip() for r in pr)

    total = hit = miss = 0
    prefix_stat = Counter()
    missing_sample = []
    for r in cr:
        v = str(r.get(cfield, "")).strip()
        if v == "":
            continue
        total += 1
        matched = False
        for prefix, ptable, pfield, norm in routes:
            if not v.startswith(prefix):
                continue
            # 规范化：把前缀替换为母表现行前缀
            cand = norm + v[len(prefix):] if prefix != norm else v
            pool = pools.get((ptable, pfield))
            if pool and cand in pool:
                matched = True
                prefix_stat[prefix + "→" + norm] += 1
                break
        if matched:
            hit += 1
        else:
            miss += 1
            if len(missing_sample) < 5:
                missing_sample.append(v)
    rec.update(
        total=total, hit=hit, miss=miss,
        hit_pct=pct(hit, total),
        status="OK" if miss == 0 else ("全断裂" if hit == 0 else "部分断裂"),
        ok=(miss == 0),
        prefix_hit=dict(prefix_stat),
        missing_sample=missing_sample,
    )
    return rec


def check_enums(tables):
    """枚举取值分布"""
    out = {}
    for tname, fnames in ENUMS.items():
        fields, rows = tables.get(tname, (None, None))
        if not rows:
            continue
        out[tname] = {}
        for fn in fnames:
            if fn not in (fields or []):
                continue
            c = Counter(str(r.get(fn, "")).strip() for r in rows)
            out[tname][fn] = dict(c.most_common())
    return out


def check_consistency(tables):
    """重复真源逐值比对"""
    out = []
    for ta, ka, tb, kb, pairs in CONSISTENCY:
        fa, ra = tables.get(ta, (None, None))
        fb, rb = tables.get(tb, (None, None))
        if not ra or not rb:
            out.append({"pair": "%s[%s] vs %s[%s]" % (ta, ka, tb, kb),
                        "status": "跳过(表空/缺失)", "ok": None})
            continue
        mapb = {str(r.get(kb, "")).strip(): r for r in rb}
        rec = {"pair": "%s[%s] vs %s[%s]" % (ta, ka, tb, kb),
               "matched_keys": 0, "compared": len(ra), "fields": {}}
        for fa_name, fb_name in pairs:
            diff = 0
            n = 0
            sample = []
            for r in ra:
                k = str(r.get(ka, "")).strip()
                other = mapb.get(k)
                if other is None:
                    continue
                n += 1
                va, vb = str(r.get(fa_name, "")).strip(), str(other.get(fb_name, "")).strip()
                if va != vb:
                    diff += 1
                    if len(sample) < 3:
                        sample.append({"key": k, ta: va, tb: vb})
            rec["fields"]["%s↔%s" % (fa_name, fb_name)] = {
                "compared": n, "diff": diff, "diff_pct": pct(diff, n), "sample": sample}
        rec["matched_keys"] = sum(
            1 for r in ra if str(r.get(ka, "")).strip() in mapb)
        rec["ok"] = all(v["diff"] == 0 for v in rec["fields"].values())
        rec["status"] = "OK" if rec["ok"] else "存在不一致"
        out.append(rec)
    return out


def check_naming(tables):
    """ID 字段命名规范（不得含中文）"""
    out = []
    for tname, fields_list in NAMING.items():
        fields, rows = tables.get(tname, (None, None))
        if not rows:
            continue
        for fn in fields_list:
            if fn not in (fields or []):
                continue
            bad = Counter()
            blank = 0
            for r in rows:
                v = str(r.get(fn, "")).strip()
                if v == "":
                    blank += 1
                    continue
                if has_non_ascii(v):
                    bad[v] += 1
            if bad:
                out.append({
                    "table": tname, "field": fn,
                    "dirty_rows": sum(bad.values()),
                    "distinct_dirty": len(bad),
                    "sample": [k for k, _ in bad.most_common(3)],
                    "status": "含中文ID", "ok": False,
                })
    return out


def check_coverage(tables):
    """覆盖率：母表实体有多少在子表中有对应记录"""
    out = []
    for ptable, pkey, ctable, cfield, desc in COVERAGE:
        pf, pr = tables.get(ptable, (None, None))
        cf, cr = tables.get(ctable, (None, None))
        if not pr or cr is None:
            out.append({"desc": desc, "status": "跳过(表空/缺失)", "ok": None})
            continue
        pids = set(str(r.get(pkey, "")).strip() for r in pr)
        cvals = set(str(r.get(cfield, "")).strip() for r in cr)
        cvals.discard("")  # 子表空值不计入覆盖（如文献证据的 case_id 本就为空）
        covered = len(pids & cvals)
        miss = len(pids - cvals)
        out.append({
            "desc": desc,
            "parent_entities": len(pids),
            "covered": covered,
            "uncovered": miss,
            "coverage_pct": pct(covered, len(pids)),
            "ok": miss == 0,
            "status": "OK" if miss == 0 else "有未覆盖",
        })
    return out


# ──────────────────────────── 主流程 ────────────────────────────
def run(data_root, out_dir, quiet=False):
    tables = {}
    for name, (rel, _key) in TABLES.items():
        tables[name] = load_table(data_root, rel)

    report = {
        "root": data_root,
        "overview": check_overview(tables),
        "primary_key": check_primary_key(tables),
        "foreign_keys": check_foreign_keys(tables),
        "polymorphic_fk": check_polymorphic(tables),
        "enums": check_enums(tables),
        "consistency": check_consistency(tables),
        "naming": check_naming(tables),
        "coverage": check_coverage(tables),
        "provenance": check_provenance(tables, data_root),
    }

    # ── 汇总问题数 ──
    issues = []
    for name, pk in report["primary_key"].items():
        if pk.get("ok") is False:
            issues.append("主键异常: %s (%s)" % (name, pk.get("status")))
    for fk in report["foreign_keys"]:
        if fk.get("ok") is False:
            issues.append("外键断裂: %s → %s (%s, 缺失 %d)" %
                          (fk["child"], fk["parent"], fk["status"], fk.get("missing", 0)))
    pf = report["polymorphic_fk"]
    if pf.get("ok") is False:
        issues.append("多态引用断裂: %s (%s, 命中率 %.1f%%)" %
                      (pf["child"], pf["status"], pf.get("hit_pct", 0)))
    for c in report["consistency"]:
        if c.get("ok") is False:
            issues.append("一致性冲突: %s" % c["pair"])
    for n in report["naming"]:
        issues.append("命名不规范: %s.%s (%d 行含中文)" %
                      (n["table"], n["field"], n["dirty_rows"]))
    for cov in report["coverage"]:
        if cov.get("ok") is False:
            issues.append("覆盖不全: %s (缺 %d)" % (cov["desc"], cov.get("uncovered", 0)))
    prov = report["provenance"]
    for name, pr in prov["tables"].items():
        if pr.get("ok") is False:
            issues.append("溯源不完整: %s (缺列 %s / 非法tier %d / T1-T2无官方源 %d / 空transform %d)"
                          % (name, pr.get("missing_columns", []), pr.get("bad_tier", 0),
                             pr.get("t1t2_without_official", 0), pr.get("blank_transform", 0)))
    reg = prov["registry"]
    if reg.get("ok") is False:
        issues.append("字段来源分级表不全: %d 个业务字段未登记 %s"
                      % (len(reg.get("unregistered", [])), reg.get("unregistered", [])[:5]))

    report["issue_summary"] = issues
    report["issue_count"] = len(issues)

    # ── 写报告 ──
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "dataset_health_report.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    md_path = os.path.join(out_dir, "dataset_health_report.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_md(report))

    if not quiet:
        print(render_console(report))
        print()
        print("[报告] %s" % json_path)
        print("[报告] %s" % md_path)
    return report


def render_console(rep):
    L = []
    L.append("=" * 72)
    L.append("眼维 · 数据体检报告")
    L.append("数据集: %s" % rep["root"])
    L.append("=" * 72)
    L.append("")
    L.append("【表概况】")
    for name, ov in rep["overview"].items():
        if not ov.get("exists"):
            L.append("  %-24s 缺失" % name)
        elif ov.get("empty_table"):
            L.append("  %-24s 空表 (%d 字段, 结构保留)" % (name, ov["fields"]))
        else:
            L.append("  %-24s %4d 行 × %2d 字段, 空值字段 %d 个"
                     % (name, ov["rows"], ov["fields"], ov["null_fields"]))
    L.append("")
    L.append("【主键唯一性】")
    for name, pk in rep["primary_key"].items():
        if pk.get("ok") is None:
            continue
        mark = "OK " if pk["ok"] else "!! "
        extra = "" if pk["ok"] else "  空=%d 重复=%d %s" % (
            pk["blank"], pk["dup_count"], pk["dup_sample"])
        L.append("  %s%-24s [%s]%s" % (mark, name, pk["key"], extra))
    L.append("")
    L.append("【外键引用完整性】")
    for fk in rep["foreign_keys"]:
        if fk.get("ok") is None:
            continue
        mark = "OK " if fk["ok"] else "!! "
        L.append("  %s%-42s 行=%-4d 空=%-4d 缺失=%-4d  %s"
                 % (mark, fk["child"] + " → " + fk["parent"],
                    fk["child_rows"], fk["blank"], fk["missing"], fk["status"]))
    pf = rep["polymorphic_fk"]
    mark = "OK " if pf.get("ok") else "!! "
    L.append("  %s%-42s 总=%-4d 命中=%-4d 命中率=%.1f%%  %s"
             % (mark, pf["child"], pf.get("total", 0), pf.get("hit", 0),
                pf.get("hit_pct", 0), pf.get("status", "")))
    L.append("")
    L.append("【一致性（重复真源）】")
    for c in rep["consistency"]:
        mark = "OK " if c.get("ok") else ("!! " if c.get("ok") is False else "   ")
        L.append("  %s%s  %s" % (mark, c["pair"], c.get("status", "")))
        for fname, d in c.get("fields", {}).items():
            L.append("       %-34s 比对=%-4d 不一致=%-4d (%.2f%%)"
                     % (fname, d["compared"], d["diff"], d["diff_pct"]))
    L.append("")
    L.append("【命名规范（ID 不得含中文）】")
    if not rep["naming"]:
        L.append("  OK  未发现含中文的 ID")
    for n in rep["naming"]:
        L.append("  !!  %-24s.%-14s %d 行含中文, 样例 %s"
                 % (n["table"], n["field"], n["dirty_rows"], n["sample"]))
    L.append("")
    L.append("【覆盖率】")
    for cov in rep["coverage"]:
        if cov.get("ok") is None:
            L.append("  --  %-28s %s" % (cov["desc"], cov["status"]))
        else:
            mark = "OK " if cov["ok"] else "!! "
            L.append("  %s%-28s 母=%-4d 覆盖=%-4d 缺=%-4d (%.1f%%)"
                     % (mark, cov["desc"], cov["parent_entities"], cov["covered"],
                        cov["uncovered"], cov["coverage_pct"]))
    L.append("")
    L.append("【溯源完整性（官方锚定）】")
    dist_all = Counter()
    for name, pr in rep["provenance"]["tables"].items():
        if pr.get("ok") is None:
            L.append("  --  %-24s %s" % (name, pr.get("status", "")))
            continue
        mark = "OK " if pr["ok"] else "!! "
        for k, v in pr.get("tier_dist", {}).items():
            dist_all[k] += v
        extra = "" if pr["ok"] else "  缺列=%s 非法tier=%d T1T2无源=%d 空transform=%d" % (
            pr.get("missing_columns", []), pr.get("bad_tier", 0),
            pr.get("t1t2_without_official", 0), pr.get("blank_transform", 0))
        L.append("  %s%-24s %-14s%s" % (mark, name, str(pr.get("tier_dist", "(空表)")), extra))
    L.append("       行级实体等级合计: %s" % dict(sorted(dist_all.items())))
    reg = rep["provenance"]["registry"]
    L.append("  %s字段来源分级表登记 %d 个字段, 未登记 %d 个"
             % ("OK " if reg.get("ok") else "!! ", reg.get("registered_fields", 0),
                len(reg.get("unregistered", []))))
    if reg.get("unregistered"):
        L.append("       未登记样例: %s" % reg["unregistered"][:6])
    L.append("")
    L.append("=" * 72)
    L.append("问题汇总: 共 %d 项" % rep["issue_count"])
    for i, it in enumerate(rep["issue_summary"], 1):
        L.append("  %2d. %s" % (i, it))
    L.append("=" * 72)
    return "\n".join(L)


def render_md(rep):
    L = []
    L.append("# 眼维 · 数据体检报告")
    L.append("")
    L.append("> 由 `02_代码/tools/dataset_health.py` 自动生成，可复跑。")
    L.append("")
    L.append("数据集根目录：`%s`" % rep["root"])
    L.append("")
    L.append("**问题汇总：共 %d 项**" % rep["issue_count"])
    L.append("")
    L.append("## 表概况")
    L.append("")
    L.append("| 表 | 行数 | 字段数 | 空值字段数 | 状态 |")
    L.append("|---|---:|---:|---:|---|")
    for name, ov in rep["overview"].items():
        if not ov.get("exists"):
            L.append("| %s | — | — | — | ❌ 缺失 |" % name)
        elif ov.get("empty_table"):
            L.append("| %s | 0 | %d | — | ⬜ 空表(结构保留) |" % (name, ov["fields"]))
        else:
            L.append("| %s | %d | %d | %d | ✅ |"
                     % (name, ov["rows"], ov["fields"], ov["null_fields"]))
    L.append("")
    L.append("## 外键引用完整性")
    L.append("")
    L.append("| 子表.字段 → 母表.字段 | 行数 | 空 | 缺失 | 状态 |")
    L.append("|---|---:|---:|---:|---|")
    for fk in rep["foreign_keys"]:
        if fk.get("ok") is None:
            continue
        L.append("| %s → %s | %d | %d | %d | %s |"
                 % (fk["child"], fk["parent"], fk["child_rows"], fk["blank"],
                    fk["missing"], "✅" if fk["ok"] else "❌ " + fk["status"]))
    pf = rep["polymorphic_fk"]
    L.append("| %s (多态) | %d | — | %d | %s |"
             % (pf["child"], pf.get("total", 0), pf.get("miss", 0),
                "✅" if pf.get("ok") else "❌ %s (%.1f%%)" % (pf["status"], pf.get("hit_pct", 0))))
    L.append("")
    L.append("## 一致性（重复真源）")
    L.append("")
    for c in rep["consistency"]:
        L.append("- %s — %s" % (c["pair"], c.get("status", "")))
        for fname, d in c.get("fields", {}).items():
            L.append("  - `%s`：比对 %d，不一致 %d（%.2f%%）"
                     % (fname, d["compared"], d["diff"], d["diff_pct"]))
    L.append("")
    L.append("## 命名规范（ID 不得含中文）")
    L.append("")
    if not rep["naming"]:
        L.append("✅ 未发现含中文的 ID")
    else:
        for n in rep["naming"]:
            L.append("- ❌ `%s.%s`：%d 行含中文，样例 `%s`"
                     % (n["table"], n["field"], n["dirty_rows"], "`, `".join(n["sample"])))
    L.append("")
    L.append("## 覆盖率")
    L.append("")
    L.append("| 关系 | 母实体 | 覆盖 | 缺 | 覆盖率 |")
    L.append("|---|---:|---:|---:|---:|")
    for cov in rep["coverage"]:
        if cov.get("ok") is None:
            L.append("| %s | — | — | — | %s |" % (cov["desc"], cov["status"]))
        else:
            L.append("| %s | %d | %d | %d | %.1f%% |"
                     % (cov["desc"], cov["parent_entities"], cov["covered"],
                        cov["uncovered"], cov["coverage_pct"]))
    L.append("")
    L.append("## 溯源完整性（官方锚定）")
    L.append("")
    L.append("行级 `provenance_tier` = 本行**实体**的来源等级；字段级等级见 `_quality/字段来源分级表.csv`。")
    L.append("")
    L.append("| 表 | 行级实体等级分布 | 官方锚定列 | 状态 |")
    L.append("|---|---|---|---|")
    prov = rep.get("provenance", {})
    for name, pr in prov.get("tables", {}).items():
        if pr.get("ok") is None:
            L.append("| %s | — | — | %s |" % (name, pr.get("status", "")))
            continue
        cols = "缺 %s" % pr["missing_columns"] if pr.get("missing_columns") else "齐备"
        L.append("| %s | %s | %s | %s |"
                 % (name, str(pr.get("tier_dist", {})).replace("{", "").replace("}", "").replace("'", ""),
                    cols, "✅" if pr["ok"] else "❌"))
    reg = prov.get("registry", {})
    L.append("")
    L.append("- 字段来源分级表：登记 **%d** 个业务字段，未登记 **%d** 个 %s"
             % (reg.get("registered_fields", 0), len(reg.get("unregistered", [])),
                "✅" if reg.get("ok") else "❌ " + ", ".join(reg.get("unregistered", [])[:8])))
    L.append("")
    L.append("## 问题汇总")
    L.append("")
    if not rep["issue_summary"]:
        L.append("✅ 无问题")
    else:
        for i, it in enumerate(rep["issue_summary"], 1):
            L.append("%d. %s" % (i, it))
    L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="眼维数据体检脚本")
    ap.add_argument("--root", default=DEFAULT_DATA, help="数据集根目录")
    ap.add_argument("--out", default=None, help="报告输出目录（默认 <root>/_quality）")
    ap.add_argument("--quiet", action="store_true", help="不打印，只写报告")
    args = ap.parse_args()

    out_dir = args.out or os.path.join(args.root, "_quality")
    rep = run(args.root, out_dir, quiet=args.quiet)
    return 0 if rep["issue_count"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
