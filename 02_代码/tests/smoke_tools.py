#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""工具层边界冒烟：五个工具串起来跑一批**刁钻输入**，看动作是不是该怎么样就怎么样。

    cd 02_代码
    python tests/smoke_tools.py           # 退出码 0 = 全过
    python tests/smoke_tools.py -v        # 打印每条的候选与理由

覆盖：完整查询 / 缺必备维 / 模态冲突（代码强制改写）/ 高风险转诊 /
      自然口语 / 否定表述 / 不确定表述 / 空输入 / 无关输入。
这批是**边界**用例，和 `run_golden.py`（核心回归）互补。
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                   # 02_代码
sys.path.insert(0, ROOT)

from yanwei import mcp_tools as T  # noqa: E402

# (标签, 输入, 期望动作或 None=只看不判, 说明)
CASES = [
    ("完整查询", "眼底病灶区域：微血管瘤；眼别：右眼；位置：黄斑区；程度草案：散在。",
     "RETURN_CASES", "五维齐全且无冲突"),
    ("缺位置", "眼底出血", "ASK_MORE", "缺必备维 → 追问"),
    ("模态冲突", "超广角眼底图像，右眼黄斑区微血管瘤", "ABSTAIN", "库中全为 CFP，不可比 → 代码强制拒答"),
    ("高风险", "右眼黄斑区较重硬性渗出", "SUGGEST_REFERRAL", "命中转诊规则，覆盖阈值映射"),
    ("自然口语", "右眼黄斑区有散在的微血管瘤", None, "口语化，不该崩"),
    ("否定表述", "右眼黄斑区未见出血", None, "否定词应进 negated，不该当成阳性"),
    ("不确定表述", "左眼疑似黄斑区中度出血", None, "不确定词应进 uncertain"),
    ("空输入", "", None, "空串要稳住，不能抛异常"),
    ("无关输入", "今天天气怎么样", None, "与眼底无关，应走缺维/拒答，不能乱返回病例"),
]


def run(text, mode="standard"):
    p = T.extract_medical_entities({"text": text}, T.Runtime.get())
    s = T.search_cases({"mode": mode, "query_text": text}, T.Runtime.get())
    if not s.get("ok"):
        return p, s, None, None
    v = T.verify_medical_logic({"query": p, "candidates": s}, T.Runtime.get())
    e = T.build_evidence_chain({"candidates": s, "logic_result": v, "query": p, "trace_id": "SMOKE"},
                               T.Runtime.get())
    return p, s, v, e


def main() -> int:
    ap = argparse.ArgumentParser(description="工具层边界冒烟")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    rt = T.Runtime.get()
    print("工具层冒烟 ｜ 索引 %d 篇 ｜ 平均文档长 %.1f" % (len(rt.index.doc_ids), rt.index.avgdl))
    print("-" * 72)

    failed = []
    for label, text, expect, note in CASES:
        p, s, v, e = run(text)
        # 不崩 = 解析与检索都给了结构化结果
        stable = isinstance(p, dict) and isinstance(s, dict) and p.get("ok") is not None
        action = (e or {}).get("action")
        ok = stable and (expect is None or action == expect)
        if not ok:
            failed.append(label)

        print("%s %-10s %s" % ("✓" if ok else "✗", label, note))
        print("     输入: %r" % text)
        if not stable:
            print("     ✗ 解析/检索未返回结构化结果"); continue
        print("     五维: %s" % {k: v2 for k, v2 in p.get("codes", {}).items() if v2})
        if p.get("negated"):
            print("     否定: %s" % p["negated"])
        if p.get("uncertain"):
            print("     不确定: %s" % p["uncertain"])
        if v:
            print("     充分性=%.4f verdict=%s" % (v.get("sufficiency", 0), v.get("verdict")))
        print("     动作=%s 判定方式=%s %s" % (
            action, (e or {}).get("decided_by"),
            "（期望 %s）" % expect if expect else ""))
        if args.verbose and s.get("ok"):
            for c in s["candidates"][:3]:
                print("        #%d %s score=%.4f" % (c["rank"], c["region_id"], c["score"]))

    print("-" * 72)
    if failed:
        print("冒烟未通过：%s" % "、".join(failed))
        return 1
    print("工具层冒烟全部通过 ✓（%d/%d）" % (len(CASES), len(CASES)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
