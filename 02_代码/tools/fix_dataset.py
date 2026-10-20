#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
眼维 · 数据修复脚本  fix_dataset.py
==================================
修复结构硬伤 + ID 命名规范化。**默认 dry-run，加 --apply 才真正写入。**

修复项
  FIX-1  回填 silver_pair_synthetic.region_id
         · REGION 级 282 行 → 指向对应区域
         · IMAGE  级  81 行 → 清空（图像级配对本就不对应单个区域）
  FIX-2  回填 silver_lesion.text_id（282 行）
  FIX-3  ID 命名规范化（中文 → 英文），并同步所有引用方
         · 区域_/文本_/QRY_文本查图_/QRY_图查文_/PAIRSYN_IDRiD_图像_/TXT_IDRiD_区域_

依据（三重独立证据链，均 100% 吻合）
  ① 行序对齐    pair 序号 i 的 image_id == region 第 i 行 image_id
  ② 文本内容    pair 序号 i 文本描述的病灶类型 == region 第 i 行 lesion_type
  ③ lesion 同序 lesion[i].region_id == region[i].region_id
  详见 03_文档/眼维_数据优化报告_v1.md

安全设计
  · 默认只报告，不落盘；--apply 才写
  · --apply 时先把原始 CSV 备份到 <root>/_quality/backup_before_fix/
  · 输出 <root>/_quality/id_mapping.csv（旧ID → 新ID）供追溯与回滚

用法
  python fix_dataset.py                 # dry-run
  python fix_dataset.py --apply         # 真正写入（自动备份）
"""

import argparse
import csv
import os
import re
import shutil
import sys
from collections import Counter

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
DEFAULT_DATA = os.path.join(PROJ_ROOT, "01_数据集")

FILES = {
    "image":    "silver/silver_image.csv",
    "region":   "silver/silver_region.csv",
    "lesion":   "silver/silver_lesion.csv",
    "pair_syn": "silver/silver_pair_synthetic.csv",
    "pair_real":"silver/silver_pair_real.csv",
    "text":     "silver/silver_text.csv",
    "grading":  "silver/silver_grading.csv",
    "query":    "gold/gold_retrieval_query.csv",
    "rel":      "gold/gold_relevance.csv",
}

# ── ID 规范化规则（必须按旧前缀长度降序应用，否则短前缀会误吃长前缀）──
ID_RULES = sorted([
    ("文本_IDRiD_区域_",     "TXT_IDRiD_REGION_"),
    ("区域_IDRiD_",          "REG_IDRiD_"),
    ("文本_IDRiD_",          "TXT_IDRiD_"),
    ("QRY_文本查图_IDRiD_",  "QRY_T2I_IDRiD_"),
    ("QRY_图查文_IDRiD_",    "QRY_I2T_IDRiD_"),
    ("PAIRSYN_IDRiD_图像_",  "PAIRSYN_IDRiD_IMAGE_"),
    ("PAIRSYN_IDRiD_区域_",  "PAIRSYN_IDRiD_REGION_"),
    ("TXT_IDRiD_区域_",      "TXT_IDRiD_REGION_"),
], key=lambda p: -len(p[0]))

# 需要过规范化的 (表, 字段)
NORM_TARGETS = [
    ("region",   "region_id"),
    ("lesion",   "annotation_id"), ("lesion", "region_id"), ("lesion", "text_id"),
    ("pair_syn", "pair_id"), ("pair_syn", "region_id"), ("pair_syn", "text_id"),
    ("pair_real","pair_id"), ("pair_real", "text_id"),
    ("text",     "text_id"),
    ("query",    "query_id"), ("query", "query_image"),
    ("rel",      "query_id"), ("rel", "candidate_id"),
]

# 关联字段名（用于 FIX-2 / FIX-1 查找）
REGION_TEXT_ID_FIELD = "text_id"


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        fields = list(rd.fieldnames or [])
        rows = [dict(r) for r in rd]
    return fields, rows


def write_csv(path, fields, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def seq_of(s):
    m = re.search(r"(\d+)$", s or "")
    return int(m.group(1)) if m else -1


def has_non_ascii(s):
    return any(ord(c) > 127 for c in s or "")


def normalize_id(v):
    for old, new in ID_RULES:
        if v and v.startswith(old):
            return new + v[len(old):]
    return v


def main():
    ap = argparse.ArgumentParser(description="眼维数据修复脚本")
    ap.add_argument("--root", default=DEFAULT_DATA)
    ap.add_argument("--apply", action="store_true", help="真正写入（默认 dry-run）")
    args = ap.parse_args()
    data = args.root

    tables = {}
    for key, rel in FILES.items():
        p = os.path.join(data, rel)
        if not os.path.exists(p):
            tables[key] = (None, None)
            continue
        tables[key] = read_csv(p)

    reg_f, reg = tables["region"]
    les_f, les = tables["lesion"]
    pys_f, pys = tables["pair_syn"]
    txt_f, txt = tables["text"]

    changes = Counter()
    mapping = []          # (表, 字段, 旧值, 新值)
    notes = []

    # ─────────── FIX-1: 回填 pair_synthetic.region_id ───────────
    region_pairs = sorted([r for r in pys if r["pair_level"] == "REGION"],
                          key=lambda r: seq_of(r["pair_id"]))
    image_pairs = [r for r in pys if r["pair_level"] == "IMAGE"]
    if len(region_pairs) != len(reg):
        notes.append("!! 区域级配对数(%d) != 区域数(%d)，回填中止" % (len(region_pairs), len(reg)))
    else:
        # ① 行序对齐验证
        ok_img = sum(1 for i, pr in enumerate(region_pairs) if pr["image_id"] == reg[i]["image_id"])
        # ② 文本内容验证（用区域级文本的病灶类型描述比对 region.lesion_type）
        reg_txt = sorted([r for r in txt if "区域" in r["text_id"]],
                         key=lambda r: seq_of(r["text_id"]))
        CN2CODE = {"微血管瘤": "MA", "出血": "HE", "硬性渗出": "EX", "棉絮斑": "SE", "软性渗出": "SE"}
        ok_txt = 0
        for i in range(len(reg)):
            span = reg_txt[i]["text_span"] if i < len(reg_txt) else ""
            codes = {v for k, v in CN2CODE.items() if k in span}
            if reg[i]["lesion_type"] in codes:
                ok_txt += 1
        notes.append("FIX-1 依据校验：行序对齐 %d/%d，文本内容吻合 %d/%d"
                     % (ok_img, len(reg), ok_txt, len(reg)))
        if ok_img == len(reg) and ok_txt == len(reg):
            for i, pr in enumerate(region_pairs):
                if pr["region_id"] != reg[i]["region_id"]:
                    mapping.append(("silver_pair_synthetic", "region_id",
                                    pr["region_id"], reg[i]["region_id"]))
                    pr["region_id"] = reg[i]["region_id"]
                    changes["FIX-1 pair_synthetic.region_id 回填"] += 1
            for pr in image_pairs:
                if pr["region_id"] != "":
                    mapping.append(("silver_pair_synthetic", "region_id",
                                    pr["region_id"], ""))
                    pr["region_id"] = ""
                    changes["FIX-1 pair_synthetic.region_id 清空(IMAGE级)"] += 1
        else:
            notes.append("!! FIX-1 校验未全通过，跳过回填（请人工复核）")

    # ─────────── FIX-2: 回填 lesion.text_id ───────────
    # 建立 region_id → 区域级 text_id
    rid2txt = {}
    reg_txt_sorted = sorted([r for r in txt if "区域" in r["text_id"]],
                            key=lambda r: seq_of(r["text_id"]))
    if len(reg_txt_sorted) == len(reg):
        for i, r in enumerate(reg):
            rid2txt[r["region_id"]] = reg_txt_sorted[i]["text_id"]
        filled = 0
        unmapped = 0
        for lr in les:
            rid = lr["region_id"]
            want = rid2txt.get(rid)
            if want is None:
                unmapped += 1
                continue
            if lr["text_id"] != want:
                if not lr["text_id"]:
                    changes["FIX-2 lesion.text_id 回填"] += 1
                else:
                    changes["FIX-2 lesion.text_id 修正"] += 1
                lr["text_id"] = want
                filled += 1
        notes.append("FIX-2 回填 %d 行，无法映射 %d 行" % (filled, unmapped))
    else:
        notes.append("!! 区域级文本数(%d) != 区域数(%d)，FIX-2 跳过" % (len(reg_txt_sorted), len(reg)))

    # ─────────── FIX-3: ID 命名规范化 ───────────
    for key, field in NORM_TARGETS:
        f, rows = tables.get(key, (None, None))
        if not rows:
            continue
        cnt = 0
        for r in rows:
            old = r.get(field, "")
            new = normalize_id(old)
            if new != old:
                mapping.append((FILES[key], field, old, new))
                r[field] = new
                cnt += 1
        if cnt:
            changes["FIX-3 命名规范化 %s.%s" % (key, field)] += cnt

    # ─────────── 报告 ───────────
    print("=" * 72)
    print("眼维 · 数据修复%s" % ("（APPLY 写入）" if args.apply else "（DRY-RUN 预览）"))
    print("=" * 72)
    for n in notes:
        print("  · %s" % n)
    print()
    print("【变更摘要】")
    if not changes:
        print("  无变更")
    for k, v in changes.items():
        print("  %-46s %d" % (k, v))
    print("  合计改动手数：%d" % sum(changes.values()))
    print()

    # 残留中文校验
    residual = []
    for key, field in NORM_TARGETS:
        f, rows = tables.get(key, (None, None))
        if not rows:
            continue
        bad = sum(1 for r in rows if has_non_ascii(r.get(field, "")))
        if bad:
            residual.append((key, field, bad))
    if residual:
        print("【残留中文 ID】")
        for k, fld, n in residual:
            print("  !! %s.%s : %d 行仍含中文" % (k, fld, n))
    else:
        print("【残留中文 ID】OK  全部规范")
    print()

    if args.apply:
        # 备份
        bak = os.path.join(data, "_quality", "backup_before_fix")
        os.makedirs(bak, exist_ok=True)
        for key, rel in FILES.items():
            src = os.path.join(data, rel)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(bak, os.path.basename(rel)))
        # 写回
        for key, rel in FILES.items():
            f, rows = tables.get(key, (None, None))
            if f is None:
                continue
            write_csv(os.path.join(data, rel), f, rows)
        # 映射表
        mp = os.path.join(data, "_quality", "id_mapping.csv")
        with open(mp, "w", encoding="utf-8-sig", newline="") as fp:
            w = csv.writer(fp)
            w.writerow(["table", "field", "old_id", "new_id"])
            w.writerows(mapping)
        print("已写入。备份目录：%s" % bak)
        print("ID 映射表：%s（%d 条）" % (mp, len(mapping)))
    else:
        print("（dry-run 未写入。加 --apply 执行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
