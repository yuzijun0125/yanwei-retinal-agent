#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""坏图测试床生成器（对应搭建清单 2.2）。

为什么需要它：
  IDRiD 是干净的公开数据，真图跑质检会**全过**，"可判读 / 需降级 / 需重拍"三档
  演示不出来。本脚本**对图像注入退化**造出可复现的三档——这是给算法造测试床，
  不是造假数据；同一套质检代码对真图假图通吃。

两种来源：
  --source synth（默认，零依赖）  内置合成眼底图 + 注入缺陷
  --source real （需 Pillow）     读本地 IDRiD 真图，缩放后注入缺陷

输出：`02_代码/tests/fixtures/bad_images/`（PNG + manifest.json），并**自检**：
      生成后立刻跑一遍质检，逐张核对档位是否符合预期，不符即报错退出。

用法：
  python tools/make_bad_images.py                 # 合成测试床（零依赖）
  python tools/make_bad_images.py --source real   # 真图退化测试床（需 Pillow）
  python tools/make_bad_images.py --out 目录       # 指定输出目录
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # 02_代码
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from yanwei import imageqc, pnglite  # noqa: E402

DEFAULT_OUT = os.path.join(ROOT, "tests", "fixtures", "bad_images")
REAL_DIR = os.path.join(os.path.dirname(ROOT), "01_数据集", "图像", "IDRiD", "原始图像")

# (文件名, 缺陷, 期望档位, 说明)
SPECS = [
    ("ok_clean",        "none",        "GRADABLE",  "健康图基线（应全项通过）"),
    ("bad_blur",        "blur",        "DOWNGRADE", "清晰度不足（均值模糊）"),
    ("bad_overexposed", "overexposed", "DOWNGRADE", "曝光过度（三通道接近饱和）"),
    ("bad_dark",        "dark",        "RETAKE",    "曝光严重不足（致命项）"),
    ("bad_tiny",        "tiny",        "RETAKE",    "分辨率过低（致命项）"),
    ("bad_noframe",     "noframe",     "DOWNGRADE", "视野边界异常（无黑边）"),
    ("bad_masked",      "mask",        "DOWNGRADE", "遮挡黑边（眼睑/睫毛）"),
]


def _real_base(size: int = 640):
    """读一张本地 IDRiD 真图，缩放为 size×size 的 RGB 像素。需 Pillow。"""
    from PIL import Image
    if not os.path.isdir(REAL_DIR):
        raise SystemExit("找不到真图目录：%s" % REAL_DIR)
    cand = None
    for sub in ("Testing set", "Training set"):
        d = os.path.join(REAL_DIR, sub)
        if os.path.isdir(d):
            files = sorted(f for f in os.listdir(d) if f.lower().endswith((".jpg", ".jpeg", ".png")))
            if files:
                cand = os.path.join(d, files[0])
                break
    if not cand:
        raise SystemExit("真图目录里没有可用图像")
    im = Image.open(cand).convert("RGB").resize((size, size), Image.BILINEAR)
    return size, size, bytearray(im.tobytes()), cand


def main() -> int:
    ap = argparse.ArgumentParser(description="生成坏图测试床并自检")
    ap.add_argument("--source", choices=["synth", "real"], default="synth")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--size", type=int, default=512, help="合成图边长 / 真图缩放目标")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    base = None
    src_note = "内置合成眼底图"

    if args.source == "real":
        try:
            bw, bh, bpx, cand = _real_base(args.size)
            base = (bw, bh, bpx)
            src_note = "真图退化，源图：%s" % cand
        except SystemExit as e:
            print("[降级] %s → 回退为合成模式" % e)
        except Exception as e:  # noqa: BLE001
            print("[降级] 真图不可用（%s）→ 回退为合成模式（提示：pip install Pillow）" % e)

    manifest, failures = [], []
    for name, defect, expect, note in SPECS:
        path = os.path.join(args.out, name + ".png")
        if base is not None:
            bw, bh, bpx = base
            w = h = 180 if defect == "tiny" else bw
            px = bpx
            if w != bw:
                # 真图的低分辨率退化：按比例抽稀
                step = max(1, bw // w)
                small = bytearray(w * h * 3)
                for y in range(h):
                    for x in range(w):
                        si = ((y * step) * bw + x * step) * 3
                        di = (y * w + x) * 3
                        small[di:di + 3] = px[si:si + 3]
                px = small
            px = pnglite.apply_defect(bytearray(px), w, h, defect)
            pnglite.write_png_rgb(path, w, h, bytes(px))
        else:
            pnglite.synth_fundus(path, w=args.size, h=args.size, defect=defect)

        qr = imageqc.assess(path)
        ok = qr.label == expect
        if not ok:
            failures.append((name, expect, qr.label))
        manifest.append({
            "file": name + ".png",
            "defect": defect,
            "expected_label": expect,
            "actual_label": qr.label,
            "score": qr.score,
            "metrics": qr.metrics,
            "reasons": qr.reasons,
            "passed": ok,
            "note": note,
            "bytes": os.path.getsize(path),
        })
        print("  %-16s defect=%-12s 期望=%-10s 实测=%-10s score=%-6s %s"
              % (name, defect, expect, qr.label, qr.score, "✓" if ok else "✗ 不符"))

    total = sum(m["bytes"] for m in manifest)
    with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"source": src_note, "count": len(manifest), "items": manifest},
                  f, ensure_ascii=False, indent=2)

    print("\n生成 %d 张，共 %.2f MB → %s" % (len(manifest), total / 1024 / 1024, args.out))
    print("来源：%s" % src_note)
    if failures:
        print("\n自检未通过：")
        for name, exp, got in failures:
            print("  %s 期望 %s 实际 %s" % (name, exp, got))
        return 1
    print("自检全部通过 ✓（每张的实际档位与预期一致）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
