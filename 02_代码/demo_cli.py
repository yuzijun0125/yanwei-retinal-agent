#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""眼维 Demo · 命令行版：没有浏览器也能跑完一次完整决策。

    python demo_cli.py                       # 跑内置四个动作的演示样例
    python demo_cli.py "右眼黄斑区散在微血管瘤"  # 跑你自己的描述
    python demo_cli.py --image 图.png "右眼黄斑区散在微血管瘤"
    python demo_cli.py --replay T2026...      # 回放一条留痕

零第三方依赖。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from yanwei import pipeline as pipe  # noqa: E402

C = {"r": "\033[0m", "b": "\033[1m", "dim": "\033[2m",
     "g": "\033[32m", "y": "\033[33m", "R": "\033[31m", "p": "\033[35m", "c": "\033[36m"}


def _c(color: str, text: str) -> str:
    return "%s%s%s" % (C.get(color, ""), text, C["r"])


ACTION_COLOR = {"RETURN_CASES": "g", "ASK_MORE": "y", "ABSTAIN": "R",
                "SUGGEST_REFERRAL": "p", "RETAKE_IMAGE": "c"}

SAMPLES = [
    "眼底病灶区域：微血管瘤；眼别：右眼；位置：黄斑区；程度草案：散在。",
    "眼底出血",
    "超广角眼底图像，右眼黄斑区微血管瘤",
    "右眼黄斑区较重硬性渗出",
]


def show(r: dict, verbose: bool = True):
    action = r.get("action") or "?"
    line = _c(ACTION_COLOR.get(action, "b"), "【%s】%s" % (action, r.get("action_label") or ""))
    print("\n" + "─" * 70)
    print("%s  输入：%s" % (_c("dim", "决策"), (r.get("parsed") or {}).get("text", "")))
    print("  " + line)
    print("  理由：%s" % r.get("action_reason"))
    print("  判定方式：%s%s ｜ 总耗时 %.1fms"
          % (r.get("decided_by"), "（已强制改写）" if r.get("overridden") else "", r.get("elapsed_ms") or 0))

    qc = r.get("qc")
    if qc:
        print("  质检：%s（%s）%s" % (_c("c", qc.get("label")), qc.get("score"),
                                     ("｜" + "；".join(qc.get("reasons") or [])) if qc.get("reasons") else ""))

    logic = r.get("logic") or {}
    if logic:
        print("  充分性 %.4f ｜ 门禁 %s ｜ 缺失 %s"
              % (logic.get("sufficiency", 0), logic.get("verdict", "-"),
                 "、".join(logic.get("missing") or []) or "无"))
        for c in logic.get("conflicts") or []:
            print("    %s %s" % (_c("R", "冲突[%s]" % c["severity"]), c["message"]))

    cands = r.get("candidates") or []
    if cands:
        print("  候选（前 %d）：" % min(3, len(cands)))
        for c in cands[:3]:
            p = c.get("profile_display") or {}
            print("    #%d %-22s 总分 %.4f (检索 %.3f / 一致 %.3f)  %s"
                  % (c["rank"], c["region_id"], c["score"], c["retrieval_norm"], c["consistency_score"],
                     " · ".join(str(p.get(d, "—")) for d in ("eye", "lesion", "location", "severity", "modality"))))

    deg = r.get("degradation")
    if deg:
        print("  %s %s — %s" % (_c("y", "降级"), deg.get("reason"), deg.get("note")))

    g = r.get("guards") or {}
    if g:
        print("  保险：步数 %s/%s ｜ 字符 %s/%s ｜ 触发 %s"
              % (g.get("steps_used"), g.get("steps_limit"), g.get("chars_used"),
                 g.get("chars_limit"), "是" if g.get("truncated") else "否"))

    if verbose:
        print("  轨迹：%s" % " → ".join(s["name"] for s in (r.get("steps") or [])))
        art = r.get("artifacts") or {}
        if art:
            print("  产物：%s" % "、".join(os.path.basename(v) for v in art.values()))


def replay(trace_path_or_id: str):
    from yanwei import trace as tm
    p = trace_path_or_id
    if not os.path.isfile(p):
        d = pipe.Pipeline().trace_dir
        p = os.path.join(d, trace_path_or_id + ".json")
    if not os.path.isfile(p):
        print("找不到留痕：%s" % trace_path_or_id)
        return 1
    print(tm.render_timeline(tm.load_trace(p)))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="眼维 Demo 命令行版")
    ap.add_argument("query", nargs="?", help="病灶描述文本；留空则跑四个演示样例")
    ap.add_argument("--image", help="可选，眼底图像路径")
    ap.add_argument("--topk", type=int, help="候选数")
    ap.add_argument("--mode", default="standard", choices=["standard", "region", "negative"])
    ap.add_argument("--replay", help="回放一条留痕（trace_id 或 json 路径）")
    ap.add_argument("--quiet", action="store_true", help="精简输出")
    args = ap.parse_args()

    if args.replay:
        return replay(args.replay)

    pl = pipe.Pipeline()
    if args.query:
        show(pl.run(args.query, image_path=args.image, topk=args.topk, mode=args.mode),
             verbose=not args.quiet)
        return 0

    print(_c("b", "眼维 Demo · 四个决策动作演示"))
    expect = ["RETURN_CASES", "ASK_MORE", "ABSTAIN", "SUGGEST_REFERRAL"]
    results = []
    for text, exp in zip(SAMPLES, expect):
        r = pl.run(text)
        results.append((r, exp, text))
        show(r, verbose=False)
    print("\n" + "=" * 70)
    print(_c("b", "汇总"))
    ok = True
    for r, exp, text in results:
        hit = r["action"] == exp
        ok &= hit
        print("  %-18s 期望 %-18s %s" % (r["action"], exp, _c("g", "✓") if hit else _c("R", "✗")))
    print("四个动作验收：%s" % (_c("g", "通过 ✓") if ok else _c("R", "未通过 ✗")))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
