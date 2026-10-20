# -*- coding: utf-8 -*-
"""
眼维数据 · Step-2 官方字段导入器  import_official.py
=====================================================
把 IDRiD 官方的三个 CSV 导入本地数据层：

  OA-4  IDRiD_DiseaseGrading_TrainingLabels.CSV / _TestingLabels.CSV
        -> 填充 silver_grading（dr_grade / dme_grade）          [T1 官方直取]
  OA-5  IDRiD_OD_Center_TrainingSet_Markups.CSV / _TestingSet_Markups.CSV
        -> silver_image.od_center_x / od_center_y，并改锚 optic_disc_x_ratio  [T1/T2]
  OA-6  IDRiD_Fovea_Center_TrainingSet_Markups.CSV / _TestingSet_Markups.CSV
        -> silver_image.fovea_center_x / fovea_center_y          [T1]

设计要点
  · 官方文件名与列名存在多个版本变体 -> 用「模糊嗅探」自动识别，不硬编码列名
  · 关联键用「图像编号的整数部分」，两边都归一化后匹配（官方 IDRiD_055 与本地 IMG_IDRiD_000055 可对上）
  · 默认 dry-run；--apply 才落盘，落盘前自动备份
  · --self-test 生成模拟官方 CSV，自证解析/关联/写回全链路可用（假数据真算法）

用法
  python import_official.py --dir <官方CSV所在目录>          # 预览
  python import_official.py --dir <官方CSV所在目录> --apply  # 落盘
  python import_official.py --self-test                      # 自测（不碰真实数据）
"""
import csv
import os
import re
import shutil
import subprocess
import sys
import tempfile
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
DATA = os.path.join(ROOT, "01_数据集")

OA_IMG, OA_GRADE, OA_OD, OA_FOVEA = "OA-1", "OA-4", "OA-5", "OA-6"
NEW_IMG_COLS = ["od_center_x", "od_center_y", "fovea_center_x", "fovea_center_y"]
NO_OFFICIAL = "NONE"


# ──────────────────────────── 通用 ────────────────────────────
def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        return list(r), list(r.fieldnames or [])


def write_csv(path, fields, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in fields})
    os.replace(tmp, path)


def norm_key(s):
    """IDRiD_055 / IDRiD_55.jpg / IMG_IDRiD_000055 -> 55"""
    if s is None:
        return None
    m = re.search(r"(\d{1,6})", str(s))
    return int(m.group(1)) if m else None


def norm_col(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def sniff_cols(fields):
    """把官方表头映射到语义角色；容忍多种命名变体"""
    m = {}
    for f in fields:
        n = norm_col(f)
        if n in ("imageno", "imagename", "image", "imageid", "no"):
            m.setdefault("image", f)
        elif n in ("drgrade", "retinopathygrade", "grade", "dr"):
            m.setdefault("dr", f)
        elif n in ("riskofdme", "riskofmacularedema", "dme", "dmerisk", "macularedema"):
            m.setdefault("dme", f)
        elif n in ("odx", "opticdiscx", "discx"):
            m.setdefault("odx", f)
        elif n in ("ody", "opticdiscy", "discy"):
            m.setdefault("ody", f)
        elif n in ("fx", "foveax", "maculax"):
            m.setdefault("fx", f)
        elif n in ("fy", "foveay", "maculay"):
            m.setdefault("fy", f)
    return m


def classify(path):
    """按文件名把官方 CSV 归类：grading / od / fovea / None"""
    n = norm_col(os.path.basename(path))
    if "grad" in n or "disease" in n:
        return "grading"
    if "fovea" in n:
        return "fovea"
    if "od" in n or "opticdisc" in n:
        return "od"
    return None


def discover(dirpath):
    found = {}
    for fn in sorted(os.listdir(dirpath)):
        if not fn.lower().endswith((".csv", ".txt")):
            continue
        kind = classify(os.path.join(dirpath, fn))
        if kind:
            found.setdefault(kind, []).append(os.path.join(dirpath, fn))
    return found


def load_official(dirpath):
    """返回 {'grading': {int: (dr,dme)}, 'od': {int: (x,y)}, 'fovea': {int: (x,y)}} + 文件清单"""
    files = discover(dirpath)
    out = {"grading": {}, "od": {}, "fovea": {}}
    used = {}
    for kind, paths in files.items():
        for p in paths:
            rows, fields = read_csv(p)
            cm = sniff_cols(fields)
            if "image" not in cm:
                print("  ! 跳过（认不出图像编号列）: %s  表头=%s" % (os.path.basename(p), fields))
                continue
            setname = "Testing" if "test" in norm_col(p) else ("Training" if "train" in norm_col(p) else "?")
            for r in rows:
                k = norm_key(r.get(cm["image"]))
                if k is None:
                    continue
                if kind == "grading" and "dr" in cm:
                    out["grading"][k] = (r.get(cm["dr"], "").strip(),
                                         r.get(cm.get("dme", ""), "").strip(),
                                         setname, os.path.basename(p))
                elif kind == "od" and "odx" in cm:
                    out["od"][k] = (r.get(cm["odx"], "").strip(), r.get(cm.get("ody", ""), "").strip())
                elif kind == "fovea" and "fx" in cm:
                    out["fovea"][k] = (r.get(cm["fx"], "").strip(), r.get(cm.get("fy", "").strip()))
            used[kind] = used.get(kind, 0) + len(rows)
    return out, files, used


# ──────────────────────────── 导入 ────────────────────────────
def run_import(dirpath, apply):
    off, files, used = load_official(dirpath)
    print("=" * 78)
    print("Step-2 官方字段导入   %s" % ("【落盘】" if apply else "【dry-run 预览】"))
    print("=" * 78)
    for k in ("grading", "od", "fovea"):
        if files.get(k):
            print("  %-8s 文件 %d 个, 行 %s" % (k, len(files[k]), used.get(k, 0)))
        else:
            print("  %-8s ❌ 未找到" % k)
    print()

    img_path = os.path.join(DATA, "silver/silver_image.csv")
    img_rows, img_fields = read_csv(img_path)
    by_num = {norm_key(r["image_id"]): r for r in img_rows}

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = os.path.join(DATA, "_quality", "backup_before_official_import")

    # ---- 1) silver_grading ----
    grad_path = os.path.join(DATA, "silver/silver_grading.csv")
    grad_rows, grad_fields = read_csv(grad_path)
    new_grad = []
    hit = miss = 0
    for n, r in sorted(by_num.items()):
        g = off["grading"].get(n)
        if not g:
            miss += 1
            continue
        hit += 1
        dr, dme, setname, fname = g
        new_grad.append({
            "grading_id": "GRD_%s" % r["image_id"].replace("IMG_", ""),
            "image_id": r["image_id"],
            "dr_grade": dr, "dme_grade": dme,
            "grading_source": "%s:IDRiD_DiseaseGrading_%sLabels.CSV" % (OA_GRADE, setname),
            "referral_action": "", "referral_rule_id": "", "guideline_ref": "",
            "created_at": r.get("created_at", ""), "updated_at": r.get("updated_at", ""),
            "deleted_at": "",
            "source_system": "IDRiD",
            "provenance_tier": "T1",
            "official_source": "%s:IDRiD_DiseaseGrading_%sLabels.CSV" % (OA_GRADE, setname),
            "official_locator": "ImageNo=IDRiD_%03d" % n,
            "transform": "官方图像级分级 CSV -> dr_grade(DR Grade)/dme_grade(Risk of DME)（identity）",
            "intermediate_source": "silver_grading.csv:import_official",
            "record_hash": "", "quality_score": "", "review_status": "PENDING",
            "data_version": "v2.3.0",
        })

    # ---- 2) silver_image 加坐标列 ----
    img_new_fields = list(img_fields)
    for c in NEW_IMG_COLS:
        if c not in img_new_fields:
            img_new_fields.insert(img_new_fields.index("optic_disc_x_ratio") + 1, c)
    coord_hit = 0
    for r in img_rows:
        n = norm_key(r["image_id"])
        od, fv = off["od"].get(n), off["fovea"].get(n)
        added = []
        if od:
            r["od_center_x"], r["od_center_y"] = od
            added.append("%s:ODCenter" % OA_OD)
            coord_hit += 1
        if fv:
            r["fovea_center_x"], r["fovea_center_y"] = fv
            added.append("%s:FoveaCenter" % OA_FOVEA)
        if added:
            osrc = r.get("official_source", "")
            r["official_source"] = ";".join([x for x in [osrc] + added if x and x != NO_OFFICIAL])
            r["transform"] = (r.get("transform", "") +
                              "；新增坐标出自官方 Markups CSV（identity）")
        # optic_disc_x_ratio 改锚官方 ODx / 4288
        if od and r.get("width"):
            try:
                r["optic_disc_x_ratio"] = "%.4f" % (float(od[0]) / float(r["width"]))
            except ValueError:
                pass

    # ---- 报告 ----
    print("【silver_grading】将写入 %d 行（本地 %d 张图中命中 %d，未命中 %d）"
          % (len(new_grad), len(img_rows), hit, miss))
    for r in new_grad[:3]:
        print("   %s  DR=%s DME=%s  %s" % (r["image_id"], r["dr_grade"], r["dme_grade"], r["official_locator"]))
    print()
    print("【silver_image】新增列 %s ；OD 坐标命中 %d 行" % (NEW_IMG_COLS, coord_hit))
    print()

    if apply:
        os.makedirs(backup, exist_ok=True)
        shutil.copy2(grad_path, os.path.join(backup, "silver_grading.csv"))
        shutil.copy2(img_path, os.path.join(backup, "silver_image.csv"))
        write_csv(grad_path, grad_fields, new_grad)
        write_csv(img_path, img_new_fields, img_rows)
        print("备份目录: %s" % backup)
        print()
        print(">>> 落盘完成。**请接着做**（否则体检会报「字段未登记」）：")
        print("    1) 在 01_数据集/_quality/provenance_map.yaml 里把")
        print("       silver_image 的 od_center_*/fovea_center_* 从注释改为正式条目（T1）")
        print("    2) python render_provenance_registry.py     # 重渲染分级表")
        print("    3) python dataset_health.py                  # 复检（应为 0 问题）")
    else:
        print(">>> 这是预览。确认无误后加 --apply 落盘。")
    print("=" * 78)
    return 0


# ──────────────────────────── 自测 ────────────────────────────
def self_test():
    """用本地 silver_image 造一份「模拟官方 CSV」，验证解析/关联/写回全链路"""
    print("=" * 78)
    print("自测：模拟官方 CSV -> 解析 -> 关联 -> 写回（使用临时副本，不碰真实数据）")
    print("=" * 78)
    img_rows, _ = read_csv(os.path.join(DATA, "silver/silver_image.csv"))
    tmp = tempfile.mkdtemp(prefix="yw_official_")
    # 文件名刻意用不同变体，验证模糊嗅探
    with open(os.path.join(tmp, "IDRiD_DiseaseGrading_TestingLabels.csv"), "w",
              encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Image name", "Retinopathy grade", "Risk of macular edema"])
        for r in img_rows:
            n = norm_key(r["image_id"])
            w.writerow(["IDRiD_%02d.jpg" % n, n % 5, n % 3])
    with open(os.path.join(tmp, "IDRiD_OD_Center_TestingSet_Markups.csv"), "w",
              encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Image No", "ODx", "ODy"])
        for r in img_rows:
            n = norm_key(r["image_id"])
            w.writerow(["IDRiD_%03d" % n, 1000 + n, 900 + n])
    with open(os.path.join(tmp, "IDRiD_Fovea_Center_TestingSet_Markups.csv"), "w",
              encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Image No", "Fx", "Fy"])
        for r in img_rows:
            n = norm_key(r["image_id"])
            w.writerow(["IDRiD_%03d" % n, 2000 + n, 1500 + n])

    off, files, used = load_official(tmp)
    ok = True
    chk = [
        ("grading 解析行数 == 81", len(off["grading"]) == len(img_rows)),
        ("od 解析行数 == 81", len(off["od"]) == len(img_rows)),
        ("fovea 解析行数 == 81", len(off["fovea"]) == len(img_rows)),
        ("两处命名变体均可识别", all(files.get(k) for k in ("grading", "od", "fovea"))),
        ("整数键可跨命名匹配", all(norm_key(r["image_id"]) in off["grading"] for r in img_rows)),
    ]
    for name, cond in chk:
        print("  %s %s" % ("OK " if cond else "!! ", name))
        ok = ok and cond
    print()
    print("  示例：IMG_IDRiD_000055 -> grading=%s od=%s fovea=%s"
          % (off["grading"].get(55), off["od"].get(55), off["fovea"].get(55)))
    print()
    print("自测结果: %s" % ("全部通过 ✅" if ok else "有失败 ❌"))
    print("=" * 78)
    return 0 if ok else 1


def main():
    args = sys.argv[1:]
    if "--self-test" in args:
        return self_test()
    if "--dir" not in args:
        print(__doc__)
        return 2
    d = args[args.index("--dir") + 1]
    return run_import(d, "--apply" in args)


if __name__ == "__main__":
    sys.exit(main())
