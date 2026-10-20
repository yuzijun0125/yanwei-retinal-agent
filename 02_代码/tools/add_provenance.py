# -*- coding: utf-8 -*-
"""
眼维数据 · Step-1 溯源锚定重构（provenance anchoring）
=====================================================
作用：把每张表的 `source_file` / `source_locator`（原本指向团队中间表，溯源链第一环即断）
      替换为「官方锚定四列」+「行级实体等级一列」。

改后列（替换原 source_file / source_locator 两列）：
  provenance_tier   本行「实体」的来源等级（T1官方直取/T2官方派生/T3规则派生/T4合成外部）
                    注意：字段级等级不在此列，见 01_数据集/_quality/字段来源分级表.csv
  official_source   官方文件名（OAn:文件名）；官方不提供时填 NONE
  official_locator  官方文件内定位键（业务键，非行号）；官方不提供时填 NONE
  transform         官方值 -> 本地值的变换说明
  intermediate_source  团队中间表留痕（二级审计，旧值降级保留）

设计要点：
  · official_locator 用业务键（ImageNo=...）而非行号 —— 行号随官方文件版本失效，业务键不会
  · 本脚本只改元数据列，不动任何数据值
  · 默认 dry-run，加 --apply 才落盘；落盘前自动备份

用法：
  python add_provenance.py            # 预览
  python add_provenance.py --apply    # 落盘
"""
import csv
import os
import re
import sys
import shutil
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))          # 眼维_Demo/
DATA = os.path.join(ROOT, "01_数据集")

DROP_COLS = ["source_file", "source_locator"]
NEW_COLS = ["provenance_tier", "official_source", "official_locator",
            "transform", "intermediate_source"]
NO_OFFICIAL = "NONE"

# 官方资产编号（与 01_数据集/_quality/provenance_map.yaml 的 official_assets 对齐）
OA_IMG, OA_MASK, OA_DISK, OA_GRADE, OA_OD, OA_FOVEA = "OA-1", "OA-2", "OA-3", "OA-4", "OA-5", "OA-6"


def read_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        return list(r), list(r.fieldnames or [])


def imageno(*cands):
    """从形如 IMG_IDRiD_000055 / REG_IDRiD_055_MA / IDRiD_55 中抽出官方业务号 IDRiD_NNN"""
    for c in cands:
        if not c:
            continue
        m = re.search(r"IDRiD[_\-](\d{2,6})", c)
        if m:
            return "IDRiD_%03d" % int(m.group(1))
        m = re.search(r"(\d{2,6})\s*$", c)
        if m:
            return "IDRiD_%03d" % int(m.group(1))
    return None


def setname(split):
    s = (split or "").upper()
    if s.startswith("TEST"):
        return "Testing"
    if s.startswith("TRAIN"):
        return "Training"
    return None


def les_type(rid, lt):
    if lt:
        return lt
    m = re.search(r"_([A-Z]{2})$", rid or "")
    return m.group(1) if m else None


# ---------------------------------------------------------------- 每表规则
def build(table, rows, idx):
    """返回 list[dict]：每行 -> {provenance_tier, official_source, official_locator, transform, intermediate_source}"""
    out = []
    for i, r in enumerate(rows):
        old = "%s:%s" % (r.get("source_file", ""), r.get("source_locator", ""))
        d = {"intermediate_source": old}
        if table == "silver_image":
            nnn = imageno(r.get("image_id"), r.get("source_ref"))        # IDRiD_055
            st = setname(r.get("split"))
            d.update(provenance_tier="T1",
                     official_source="%s:IDRiD_%s.jpg" % (OA_IMG, nnn.split("_")[-1]),
                     official_locator="ImageNo=%s" % nnn,
                     transform="官方原图 -> 本地路径/元数据；sha256 为本地文件哈希(非官方)；split 映射官方 %s 集" % (st or "?"))
        elif table == "silver_case":
            nnn = imageno(r.get("case_id"))
            d.update(provenance_tier="T3",
                     official_source="%s:IDRiD_%s.jpg" % (OA_IMG, nnn.split("_")[-1]),
                     official_locator="ImageNo=%s" % nnn,
                     transform="官方原图 -> 按眼别 1:1 聚合为病例（项目规则；官方无「病例」概念）")
        elif table == "silver_region":
            nnn = imageno(r.get("image_id"), r.get("region_id"))
            t = les_type(r.get("region_id"), r.get("lesion_type"))
            d.update(provenance_tier="T2",
                     official_source="%s:IDRiD_%s_%s.tif" % (OA_MASK, nnn.split("_")[-1], t),
                     official_locator="ImageNo=%s;LesionType=%s" % (nnn, t),
                     transform="官方病灶掩膜 -> 连通域外接矩形/pixel_count/area_ratio（确定性像素运算）")
        elif table == "silver_lesion":
            nnn = imageno(r.get("image_id"), r.get("region_id"))
            t = les_type(r.get("region_id"), r.get("lesion_type"))
            d.update(provenance_tier="T2",
                     official_source="%s:IDRiD_%s_%s.tif" % (OA_MASK, nnn.split("_")[-1], t),
                     official_locator="ImageNo=%s;LesionType=%s" % (nnn, t),
                     transform="官方病灶掩膜（lesion_type/area 与 region 同源；severity/location 为项目规则派生）")
        elif table == "silver_grading":
            nnn = imageno(r.get("image_id"))
            d.update(provenance_tier="T1",
                     official_source="%s:IDRiD_DiseaseGrading_%sLabels.CSV" % (OA_GRADE, setname(r.get("split")) or "Training/Testing"),
                     official_locator="ImageNo=%s" % nnn,
                     transform="官方图像级分级 CSV -> dr_grade(DR Grade)/dme_grade(Risk of DME)（identity）")
        elif table == "silver_text":
            o = (r.get("text_origin") or "").upper()
            if o == "LITERATURE":
                d.update(provenance_tier="T4", official_source=NO_OFFICIAL,
                         official_locator="Title=%s;Page=%s" % (r.get("source_title", ""), r.get("source_page", "") or "-"),
                         transform="公开论文 PDF 文本抽取（非 IDRiD；DOI 待补，当前为 PDF+页码级锚定）")
            else:
                nnn = imageno(r.get("text_id")) or idx["txt2img"].get(r.get("text_id"))
                loc = "ImageNo=%s" % nnn if nnn else "ImageNo=-"
                d.update(provenance_tier="T4", official_source=NO_OFFICIAL, official_locator=loc,
                         transform="文本由该图像/区域自身掩膜规则生成（官方不提供文本，含循环论证风险）")
        elif table == "silver_pair_synthetic":
            nnn = imageno(r.get("image_id"), r.get("region_id"))
            lvl = (r.get("pair_level") or "").upper()
            if lvl == "REGION":
                t = les_type(r.get("region_id"), None)
                osrc = "%s:IDRiD_%s_%s.tif" % (OA_MASK, nnn.split("_")[-1], t)
                loc = "ImageNo=%s;LesionType=%s" % (nnn, t)
            else:
                osrc = "%s:IDRiD_%s.jpg" % (OA_IMG, nnn.split("_")[-1])
                loc = "ImageNo=%s" % nnn
            d.update(provenance_tier="T4", official_source=osrc, official_locator=loc,
                     transform="掩膜 -> 生成文本 -> 与掩膜配对（配对本身为项目合成，非官方配对）")
        elif table == "silver_pair_real":
            d.update(provenance_tier="T4", official_source=NO_OFFICIAL, official_locator="NONE",
                     transform="待真实图文对引入（官方不提供）")
        elif table == "gold_retrieval_query":
            nnn = imageno(r.get("query_image"))
            d.update(provenance_tier="T4",
                     official_source="%s:IDRiD_%s.jpg" % (OA_IMG, nnn.split("_")[-1]) if nnn else NO_OFFICIAL,
                     official_locator="ImageNo=%s" % nnn if nnn else "NONE",
                     transform="查询图片来源=官方原图；查询文本由掩膜生成（query_origin 全 SYNTHETIC）")
        elif table == "gold_relevance":
            cand = r.get("candidate_id", "")
            nnn = imageno(cand) or imageno(idx["qry2img"].get(r.get("query_id")))
            if cand.startswith("REG_"):
                t = les_type(cand, None)
                osrc = "%s:IDRiD_%s_%s.tif" % (OA_MASK, nnn.split("_")[-1], t)
            elif cand.startswith("IMG_"):
                osrc = "%s:IDRiD_%s.jpg" % (OA_IMG, nnn.split("_")[-1])
            else:
                osrc = NO_OFFICIAL
            d.update(provenance_tier="T4", official_source=osrc,
                     official_locator=("ImageNo=%s" % nnn) if nnn else "NONE",
                     transform="候选锚定官方掩膜/原图；相关性判定为 AUTO_RULE 规则判定（无人工确认）")
        else:
            d.update(provenance_tier="?", official_source=NO_OFFICIAL, official_locator="NONE", transform="?")
        out.append(d)
    return out


def main():
    apply = "--apply" in sys.argv
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = os.path.join(DATA, "_quality", "backup_before_provenance")
    if apply:
        os.makedirs(backup, exist_ok=True)

    tables = ["silver/silver_case.csv", "silver/silver_image.csv", "silver/silver_region.csv",
              "silver/silver_lesion.csv", "silver/silver_grading.csv", "silver/silver_text.csv",
              "silver/silver_pair_synthetic.csv", "silver/silver_pair_real.csv",
              "gold/gold_retrieval_query.csv", "gold/gold_relevance.csv"]

    # ---- 先建两套外键索引，供「数字段不是图像号」的两处陷阱换算
    pair, _ = read_rows(os.path.join(DATA, "silver/silver_pair_synthetic.csv"))
    txt2img = {x["text_id"]: imageno(x.get("image_id")) for x in pair if x.get("text_id")}
    q, _ = read_rows(os.path.join(DATA, "gold/gold_retrieval_query.csv"))
    qry2img = {x["query_id"]: x.get("query_image") for x in q}
    idx = {"txt2img": txt2img, "qry2img": qry2img}

    print("=" * 78)
    print("Step-1 溯源锚定重构   %s" % ("【落盘】" if apply else "【dry-run 预览】"))
    print("=" * 78)
    total_rows = 0
    tier_cnt = {}
    unresolved = []
    for t in tables:
        path = os.path.join(DATA, t)
        rows, cols = read_rows(path)
        prov = build(os.path.splitext(os.path.basename(t))[0], rows, idx)
        newcols = [c for c in cols if c not in DROP_COLS]
        # 把新列插到 source_system 之后
        if "source_system" in newcols:
            k = newcols.index("source_system") + 1
            newcols = newcols[:k] + NEW_COLS + newcols[k:]
        else:
            newcols = newcols + NEW_COLS

        total_rows += len(rows)
        for d in prov:
            tier_cnt[d["provenance_tier"]] = tier_cnt.get(d["provenance_tier"], 0) + 1
            if d["official_locator"] == "NONE" and d["provenance_tier"] in ("T1", "T2"):
                unresolved.append((t, d["intermediate_source"]))

        print("\n[%s]  %d 行" % (t, len(rows)))
        shown = 0
        for r, d in zip(rows, prov):
            if shown < 2:
                key = r.get("region_id") or r.get("text_id") or r.get("query_id") or r.get("case_id") or r.get("relevance_id") or r.get("image_id") or r.get("pair_id") or "-"
                print("   %s" % key)
                for c in NEW_COLS:
                    print("      %-20s %s" % (c, d[c]))
                shown += 1

        if apply:
            shutil.copy2(path, os.path.join(backup, os.path.basename(t)))
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=newcols)
                w.writeheader()
                for r, d in zip(rows, prov):
                    nr = {c: r.get(c, "") for c in newcols if c not in NEW_COLS}
                    nr.update(d)
                    w.writerow({c: nr.get(c, "") for c in newcols})
            os.replace(tmp, path)

    print("\n" + "=" * 78)
    print("汇总：%d 张表 / %d 行" % (len(tables), total_rows))
    print("行级实体等级分布：%s" % dict(sorted(tier_cnt.items())))
    print("T1/T2 行中仍有 NONE 定位键的：%d %s" % (len(unresolved), unresolved[:5]))
    if apply:
        print("备份目录：%s" % backup)
    else:
        print(">>> 这是预览。确认无误后加 --apply 落盘。")
    print("=" * 78)


if __name__ == "__main__":
    main()
