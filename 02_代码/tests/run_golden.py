#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""回归夹具跑分器：3+1 条固定输入 → 期望动作。

    cd 02_代码
    python tests/run_golden.py            # 跑全部夹具
    python tests/run_golden.py -v         # 带上候选详情

**任何改动先跑它**：跑不过就是改坏了。退出码 0 = 全过。
夹具定义在 `tests/golden_traces/golden.json`。
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                   # 02_代码
sys.path.insert(0, ROOT)

from yanwei import pipeline as pipe  # noqa: E402

SPEC = os.path.join(HERE, "golden_traces", "golden.json")


def main() -> int:
    ap = argparse.ArgumentParser(description="golden trace 回归")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    with open(SPEC, encoding="utf-8") as f:
        spec = json.load(f)
    cases = spec.get("cases", [])
    print("回归夹具 v%s ｜ %d 条 ｜ 定义：%s" % (spec.get("version"), len(cases), SPEC))
    print("-" * 72)

    pl = pipe.Pipeline()
    failed = []
    for c in cases:
        r = pl.run(c["text"], image_path=c.get("expect_qc") or None)
        checks = []

        def chk(name, ok, got):
            checks.append((name, ok, got))

        chk("动作", r.get("action") == c["expect_action"], r.get("action"))
        if c.get("expect_decided_by"):
            chk("判定方式", r.get("decided_by") == c["expect_decided_by"], r.get("decided_by"))
        if "expect_overridden" in c:
            chk("强制改写标", bool(r.get("overridden")) == c["expect_overridden"], r.get("overridden"))
        if c.get("minimum_candidates"):
            got = len(r.get("candidates") or [])
            chk("候选数", got >= c["minimum_candidates"], got)
        if c.get("expect_hit_rule"):
            got = (r.get("evidence") or {}).get("hit_rules") or []
            chk("命中规则", c["expect_hit_rule"] in got, got)

        ok = all(x[1] for x in checks)
        if not ok:
            failed.append(c["id"])
        print("%s %-29s %s" % ("✓" if ok else "✗", c["id"], c.get("note", "")))
        for name, good, got in checks:
            print("     %s %-8s 实际=%s" % ("·" if good else "✗", name, got))
        if args.verbose:
            for cand in (r.get("candidates") or [])[:3]:
                print("        #%d %s score=%s" % (cand["rank"], cand["region_id"], cand["score"]))

    print("-" * 72)
    if failed:
        print("回归未通过：%s" % "、".join(failed))
        return 1
    print("全部通过 ✓（%d/%d）" % (len(cases), len(cases)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
