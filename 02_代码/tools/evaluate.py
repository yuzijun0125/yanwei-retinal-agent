#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""评测入口：双口径报告 + 信息论天花板。

    cd 02_代码
    python tools/evaluate.py                 # 全量 282 条 T2I
    python tools/evaluate.py --split TEST    # 只跑测试集
    python tools/evaluate.py --topk 1,5,10,20,30

报告同时打印到屏幕、写入 `04_产物/评测/`（JSON + Markdown）。
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from yanwei import evaluate as ev  # noqa: E402
from yanwei.config import default_output_dir  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="眼维 Demo 评测（双口径）")
    ap.add_argument("--split", default=None, help="TEST / TRAIN，默认全部")
    ap.add_argument("--topk", default="1,5,10,20", help="逗号分隔的 k 列表")
    ap.add_argument("--mode", default="standard", choices=["standard", "region", "negative"])
    ap.add_argument("--no-save", action="store_true", help="只打印不落盘")
    args = ap.parse_args()

    ks = tuple(int(x) for x in args.topk.split(",") if x.strip())
    report = ev.run_eval(split=args.split, ks=ks, mode=args.mode)
    print(ev.render(report))

    if not args.no_save:
        out = os.path.join(default_output_dir(), "评测")
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "evaluation_report.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        with open(os.path.join(out, "evaluation_report.md"), "w", encoding="utf-8") as f:
            f.write("# 眼维 · 第一版 Demo 评测报告（双口径）\n\n```\n")
            f.write(ev.render(report))
            f.write("\n```\n")
        print("[报告] %s" % os.path.join(out, "evaluation_report.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
