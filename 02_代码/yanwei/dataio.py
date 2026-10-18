"""数据装载：读 silver/gold，构建内存索引。

检索单元的选择（重要设计决定）：
    以 **病灶区域（region）** 为检索单元，而不是整图。
    理由：gold 层的 T2I 任务（282 条）判定的候选正是区域，
          且区域天然携带"病灶/位置/程度"三维，正好是五维一致性的作用面。
    整图级向量召回由 multimodal.py 实现，再映射回区域进行规则复核。

已知数据边界（诚实标注）：
    - 原始图像是否就位以 stats() 的实际文件检查为准，不固定为零；
    - gold 全部 metric_validity=DEMO_ONLY → 评测分数只用于**证明链路可跑通**，
      不作为项目对外指标（数据字典 MT-01/MT-02 规则的直接后果）。
"""

from __future__ import annotations

import csv
import json
import os


def read_csv(path: str):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _loads(v, default):
    if not v:
        return default
    try:
        return json.loads(v)
    except Exception:  # noqa: BLE001
        return default


class Region:
    __slots__ = (
        "region_id", "legacy_id", "image_id", "case_id", "lesion_type", "location",
        "severity", "area_ratio", "bbox", "eye", "disease_tags", "modality",
        "quality_label", "raw_path", "split", "text_ids", "text_span", "doc_text",
    )


class Dataset:
    """把 silver/gold 一次性装进内存，并建好区域语料。"""

    def __init__(self, data_dir: str, config):
        self.dir = os.path.abspath(data_dir)
        self.cfg = config
        s = lambda n: os.path.join(self.dir, "silver", n)  # noqa: E731
        g = lambda n: os.path.join(self.dir, "gold", n)    # noqa: E731

        self.cases = {r["case_id"]: r for r in read_csv(s("silver_case.csv"))}
        self.images = {r["image_id"]: r for r in read_csv(s("silver_image.csv"))}
        self.gradings = read_csv(s("silver_grading.csv"))
        self.lesions = read_csv(s("silver_lesion.csv"))
        self.regions_raw = read_csv(s("silver_region.csv"))
        self.texts = read_csv(s("silver_text.csv"))
        self.pairs_syn = read_csv(s("silver_pair_synthetic.csv"))
        self.pairs_real = read_csv(s("silver_pair_real.csv"))
        self.queries = read_csv(g("gold_retrieval_query.csv"))
        self.relevance = read_csv(g("gold_relevance.csv"))

        # 区域 → 病灶程度（severity 在 lesion 表上）
        les_by_region = {}
        for row in self.lesions:
            rid = row.get("region_id")
            if rid:
                les_by_region[rid] = row

        # 病例 → 合成描述文本
        texts_by_case = {}
        for row in self.texts:
            if row.get("text_type") == "SYNTHETIC_DESC":
                texts_by_case.setdefault(row.get("case_id"), []).append(row)

        self.regions = {}
        for row in self.regions_raw:
            rid = row["region_id"]
            reg = Region()
            reg.region_id = rid
            reg.legacy_id = rid.replace("REG_", "区域_", 1)
            reg.image_id = row.get("image_id", "")
            img = self.images.get(reg.image_id, {})
            reg.case_id = img.get("case_id", "")
            case = self.cases.get(reg.case_id, {})
            les = les_by_region.get(rid, {})
            reg.lesion_type = row.get("lesion_type") or les.get("lesion_type", "UNKNOWN")
            reg.location = row.get("location") or les.get("location", "UNKNOWN")
            reg.severity = les.get("severity", "UNKNOWN")
            reg.area_ratio = row.get("area_ratio", "")
            reg.bbox = row.get("bbox", "")
            reg.eye = case.get("eye", "UNKNOWN")
            reg.disease_tags = _loads(case.get("disease_tags"), [])
            reg.modality = img.get("modality", "UNKNOWN")
            reg.quality_label = img.get("quality_label", "UNASSESSED")
            reg.raw_path = img.get("raw_path", "")
            reg.split = img.get("split", "UNASSIGNED")
            t = texts_by_case.get(reg.case_id, [])
            reg.text_ids = [x["text_id"] for x in t]
            reg.text_span = t[0]["text_span"] if t else ""
            reg.doc_text = self._doc_text(reg)
            self.regions[rid] = reg

        self.by_legacy = {r.legacy_id: r for r in self.regions.values()}

        # 相关度真值：query_id → {区域id: relevance}
        self.rel_by_query = {}
        for row in self.relevance:
            qid = row["query_id"]
            cand = row["candidate_id"].replace("区域_", "REG_", 1)
            self.rel_by_query.setdefault(qid, {})[cand] = row["relevance"]

    def _doc_text(self, reg: Region) -> str:
        """生成区域的可检索文本 —— 中英码双写，让稀疏检索天然可命中。"""
        cfg = self.cfg
        d = lambda dim, code: cfg.display(dim, code)  # noqa: E731
        parts = [
            "眼底病灶区域",
            "%s %s" % (d("lesion", reg.lesion_type), reg.lesion_type),
            "%s %s" % (d("location", reg.location), reg.location),
            "%s %s" % (d("severity", reg.severity), reg.severity),
            "%s %s" % (d("eye", reg.eye), reg.eye),
            "%s %s" % (d("modality", reg.modality), reg.modality),
            " ".join(reg.disease_tags),
        ]
        if reg.text_span:
            parts.append(reg.text_span)
        return " ".join(parts)

    # ---------------- 语料与任务 ----------------
    def corpus(self):
        """检索语料：[(region_id, doc_text)]"""
        return [(r.region_id, r.doc_text) for r in self.regions.values()]

    def t2i_queries(self, split: str | None = None):
        """文本查区域任务（282 条）。"""
        out = []
        for q in self.queries:
            if q["query_type"] != "T2I":
                continue
            if split and q["split"] != split:
                continue
            if q["query_id"] not in self.rel_by_query:
                continue
            out.append(q)
        return out

    def gold_of(self, query_id: str):
        rel = self.rel_by_query.get(query_id, {})
        exact = [k for k, v in rel.items() if v == "EXACT"]
        return {
            "exact": exact,
            "similar": [k for k, v in rel.items() if v == "SIMILAR"],
            "dissimilar": [k for k, v in rel.items() if v == "DISSIMILAR"],
            "conflict": [k for k, v in rel.items() if v == "CONFLICT"],
        }

    def region(self, region_id_or_legacy: str):
        if region_id_or_legacy in self.regions:
            return self.regions[region_id_or_legacy]
        return self.by_legacy.get(region_id_or_legacy)

    def stats(self):
        from .multimodal import resolve_image
        present = 0
        for record in self.images.values():
            try:
                present += int(resolve_image(self.dir, record).is_file())
            except (OSError, ValueError):
                pass
        return {
            "cases": len(self.cases),
            "images": len(self.images),
            "regions": len(self.regions),
            "lesions": len(self.lesions),
            "texts": len(self.texts),
            "pairs_synthetic": len(self.pairs_syn),
            "pairs_real": len(self.pairs_real),
            "gradings": len(self.gradings),
            "queries_all": len(self.queries),
            "queries_t2i": len(self.t2i_queries()),
            "relevance_rows": len(self.relevance),
            "images_present_on_disk": present,
        }
