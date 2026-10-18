"""评测：**双口径**报告 + 信息论天花板自动核算。

为什么必须双口径（这一步是防误读的关键）：
  282 个区域只落在 **53 种五维组合** 上，最大一簇 **35 个区域**的五维画像完全相同。
  而 T2I 是纯文本检索，查询文本里不含"是哪张图的哪个掩膜"这层信息——所以那 35 个
  区域在文本上**不可区分**，正确答案排第几本质上靠掷骰子。

  ⇒ 只报 Recall@k 会被误读成"系统很烂"；必须同时报**相对排序质量**
     （EXACT 是否压过 SIMILAR / DISSIMILAR），才能说明"排序是对的，缺的是判据信息"。

口径：
  · 绝对定位能力：Recall@k（正确答案是否进前 k）
  · 相对排序质量：所有 EXACT 是否都排在所有 SIMILAR（及 DISSIMILAR）之前
  · 对照基线：纯 BM25（不融合）——用来证明"五维一致性融合"到底加了多少
  · 天花板：自动打印同五维组合的最大簇大小

⚠️ gold 全部 metric_validity=DEMO_ONLY（数据字典 MT-01/MT-02）：
   这里的分数**只证明链路可跑通**，不可对外当指标。
"""

from __future__ import annotations

import collections
import time

from . import mcp_tools as MT


def _ranks(ranked: list, ids: set) -> list:
    return [ranked.index(x) for x in ids if x in ranked]


def run_eval(rt: "MT.Runtime | None" = None, split: str | None = None,
             ks=(1, 5, 10, 20), mode: str = "standard") -> dict:
    rt = rt or MT.Runtime.get()
    ds = rt.ds
    queries = ds.t2i_queries(split)
    ks = sorted(set(int(k) for k in ks))
    kmax = max(ks)

    recall = {k: 0 for k in ks}
    bm25_recall = {k: 0 for k in ks}
    all_regions = set(ds.regions.keys())
    n = 0
    cond_n = 0          # 条件口径的分母：正确答案**进入了候选**的查询数
    e_over_s = e_over_d = oracle = 0
    t0 = time.perf_counter()

    for q in queries:
        gold = ds.gold_of(q["query_id"])
        if not gold["exact"]:
            continue
        n += 1
        exact, sim, dis = set(gold["exact"]), set(gold["similar"]), set(gold["dissimilar"])

        # --- 融合检索（与线上同一入口，保证评测与演示口径一致）
        res = MT.search_cases({"query_text": q["query_text"], "topk": kmax, "mode": mode}, rt)
        ranked = [c["region_id"] for c in (res.get("candidates") or [])]

        for k in ks:
            if exact & set(ranked[:k]):
                recall[k] += 1

        re_, rs, rd = _ranks(ranked, exact), _ranks(ranked, sim), _ranks(ranked, dis)
        if re_:
            cond_n += 1
            if not rs or max(re_) < min(rs):
                e_over_s += 1
            if not rd or max(re_) < min(rd):
                e_over_d += 1
        # 全库命中率：正确答案**一定在语料里**（查询由掩膜生成），与是否排进前 k 无关
        if exact & all_regions:
            oracle += 1

        # --- 对照：纯 BM25（不融合）
        brow = [rid for rid, _ in rt.index.search(q["query_text"], topk=kmax)]
        for k in ks:
            if exact & set(brow[:k]):
                bm25_recall[k] += 1

    elapsed = round((time.perf_counter() - t0) * 1000, 2)

    # --- 信息论天花板
    combos = collections.Counter(
        (r.eye, r.lesion_type, r.location, r.severity, r.modality) for r in ds.regions.values()
    )
    top_combo, top_n = (combos.most_common(1)[0] if combos else ((), 0))

    d = float(n) or 1.0
    report = {
        "n_queries": n,
        "split": split or "ALL",
        "mode": mode,
        "recall": {("@%d" % k): round(recall[k] / d, 4) for k in ks},
        "baseline_bm25_recall": {("@%d" % k): round(bm25_recall[k] / d, 4) for k in ks},
        "rank_quality": {
            # 全体口径：把"正确答案根本没进候选"也算排序失败（严格）
            "exact_over_similar": round(e_over_s / d, 4),
            "exact_over_dissimilar": round(e_over_d / d, 4),
            # 条件口径：只看"正确答案已进入候选"的那些查询，答"进了以后排得对不对"
            "exact_over_similar_conditional": round(e_over_s / cond_n, 4) if cond_n else 0.0,
            "exact_over_dissimilar_conditional": round(e_over_d / cond_n, 4) if cond_n else 0.0,
            "conditional_base": cond_n,
            "note": "条件口径接近 1 说明「排序是对的，缺的是判据信息」；全体口径偏低是召回问题，不是排序问题",
        },
        "oracle_hit_rate": round(oracle / d, 4),
        "ceiling": {
            "regions": len(ds.regions),
            "distinct_combos": len(combos),
            "max_cluster_size": top_n,
            "max_cluster_combo": list(top_combo),
            "note": "同五维组合的区域在纯文本上不可区分；该簇大小即为 Recall 上限的成因",
        },
        "elapsed_ms": elapsed,
        "caveat": "gold 全部 DEMO_ONLY：分数只证明链路可跑通，不可对外当指标（数据字典 MT-01/MT-02）。",
    }
    return report


def render(report: dict) -> str:
    """把报告渲染成给人看的文本（CLI 与文档共用）。"""
    L = []
    L.append("=" * 72)
    L.append("眼维 · 第一版 Demo 评测报告（双口径）")
    L.append("=" * 72)
    L.append("查询数 %d ｜ 切分 %s ｜ 模式 %s ｜ 耗时 %.1f ms"
             % (report["n_queries"], report["split"], report["mode"], report["elapsed_ms"]))
    L.append("")
    L.append("【口径一】绝对定位能力 Recall@k（融合检索 vs 纯 BM25 对照）")
    for k in report["recall"]:
        L.append("  %-5s 融合 %-7s ｜ 纯 BM25 %-7s ｜ 提升 %+.4f"
                 % (k, report["recall"][k], report["baseline_bm25_recall"][k],
                    report["recall"][k] - report["baseline_bm25_recall"][k]))
    L.append("")
    L.append("【口径二】相对排序质量（这才是「检索能不能用」的证据）")
    rq = report["rank_quality"]
    L.append("  条件口径（答案已进入候选的 %d 条里）：" % rq["conditional_base"])
    L.append("    EXACT 全部排在 SIMILAR 之前：   %.4f" % rq["exact_over_similar_conditional"])
    L.append("    EXACT 全部排在 DISSIMILAR 之前：%.4f" % rq["exact_over_dissimilar_conditional"])
    L.append("  全体口径（把「没进候选」也算失败）：")
    L.append("    EXACT 全部排在 SIMILAR 之前：   %.4f" % rq["exact_over_similar"])
    L.append("    EXACT 全部排在 DISSIMILAR 之前：%.4f" % rq["exact_over_dissimilar"])
    L.append("  全库命中率（答案必定在语料里，与排名无关）：%.4f" % report["oracle_hit_rate"])
    L.append("")
    c = report["ceiling"]
    L.append("【信息论天花板】（自动核算，防止数字被误读）")
    L.append("  区域总数 %d ｜ 不同五维组合 %d ｜ 最大簇 %d 个区域"
             % (c["regions"], c["distinct_combos"], c["max_cluster_size"]))
    L.append("  最大簇画像：%s" % "/".join(str(x) for x in c["max_cluster_combo"]))
    L.append("  该簇内 %d 个区域在纯文本上完全不可区分 → Recall 上限被锁死，"
             "病根在评测集的查询表示，不在图像。" % c["max_cluster_size"])
    L.append("")
    L.append("⚠️ %s" % report["caveat"])
    L.append("=" * 72)
    return "\n".join(L)
