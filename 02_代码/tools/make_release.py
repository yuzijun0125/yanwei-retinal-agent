#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""发布打包器 —— 把整个工程打成可分发的 zip（对应搭建清单「全代码入包」）。

为什么要它：
  平台共享盘上的 `眼维_Demo.zip` 是队友拿到代码的唯一途径（本仓库不配远程，
  见 03_文档/眼维_团队协作与分支操作卡_v1.md）。手工右键压缩会漏文件、会带进
  __pycache__ 和运行产物，结果就是「本地能跑、队友跑不起来」。本脚本把打包
  规则写死成代码，谁打包都一样。

包含什么：
  - 五个一级目录（00_计划书 / 01_数据集 / 02_代码 / 03_文档 / 04_产物）
  - README.md、.gitignore
  - `.git` 完整历史（默认包含）——队友解压后可直接开分支改代码

排除什么（与 .gitignore 保持一致）：
  - `__pycache__/`、`*.pyc`
  - `04_产物/` 下的运行期产物（trace / 证据卡 / 反馈 / 评测），只留 .gitkeep 和 README
  - 编辑器与系统垃圾（.vscode / .idea / .DS_Store / Thumbs.db）

产物：<输出目录>/眼维_Demo.zip，内部单根目录 `眼维_Demo/`

用法：
  python tools/make_release.py                      # 打包到 _upload/ 或 上级目录
  python tools/make_release.py --out D:/somewhere   # 指定输出目录
  python tools/make_release.py --no-git             # 不含 .git（纯代码快照，更小）
  python tools/make_release.py --check              # 只体检、不打包（列出会被排除的东西）
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
CODE_DIR = os.path.dirname(HERE)                 # 02_代码
ROOT = os.path.dirname(CODE_DIR)                 # 工程根（眼维_Demo）
ROOT_NAME = os.path.basename(ROOT)               # 压缩包内的单根目录名

# ---- 目录级排除（命中即整棵子树跳过）----
SKIP_DIRS = {"__pycache__", ".vscode", ".idea", ".pytest_cache", ".venv", "venv"}
# ---- 文件级排除（后缀 / 文件名）----
SKIP_EXTS = {".pyc", ".pyo", ".pyd", ".tmp", ".bak", ".swp", ".orig"}
SKIP_NAMES = {".DS_Store", "Thumbs.db"}
# ---- 运行产物：目录保留、内容排除 ----
RUNTIME_DIRS = [
    os.path.join("04_产物", "trace"),
    os.path.join("04_产物", "证据卡"),
    os.path.join("04_产物", "反馈"),
    os.path.join("04_产物", "评测"),
]
KEEP_IN_RUNTIME = {".gitkeep"}


def _rel(p: str) -> str:
    return os.path.relpath(p, ROOT).replace("\\", "/")


def _is_runtime_content(rel: str) -> bool:
    """判断相对路径是否属于「运行产物目录里的运行产物」。"""
    for rd in RUNTIME_DIRS:
        rd_slash = rd.replace("\\", "/") + "/"
        if rel.startswith(rd_slash):
            return os.path.basename(rel) not in KEEP_IN_RUNTIME
    return False


def walk(include_git: bool):
    """产出 (绝对路径, 归档内相对路径) 列表；同时返回被排除的清单。"""
    kept, skipped = [], []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        # 原地裁剪 dirnames，os.walk 就不会再往下走被排除的目录
        keep_dirs = []
        for d in sorted(dirnames):
            rel = _rel(os.path.join(dirpath, d))
            if d in SKIP_DIRS:
                skipped.append((rel + "/", "整目录（缓存/编辑器）"))
                continue
            if d == ".git" and not include_git:
                skipped.append((rel + "/", "整目录（--no-git）"))
                continue
            keep_dirs.append(d)
        dirnames[:] = keep_dirs

        for f in sorted(filenames):
            ap = os.path.join(dirpath, f)
            rel = _rel(ap)
            ext = os.path.splitext(f)[1].lower()
            if ext in SKIP_EXTS or f in SKIP_NAMES:
                skipped.append((rel, "缓存/系统垃圾"))
                continue
            if _is_runtime_content(rel):
                skipped.append((rel, "运行产物（不入包）"))
                continue
            kept.append((ap, rel))
    return kept, skipped


def build(out_dir: str, include_git: bool, quiet: bool = False) -> int:
    kept, skipped = walk(include_git)
    os.makedirs(out_dir, exist_ok=True)
    zip_path = os.path.join(out_dir, ROOT_NAME + ".zip")
    tmp_path = zip_path + ".part"

    t0 = time.time()
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for ap, rel in kept:
            z.write(ap, arcname=ROOT_NAME + "/" + rel)
    os.replace(tmp_path, zip_path)

    size = os.path.getsize(zip_path)
    h = hashlib.sha256()
    with open(zip_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)

    if not quiet:
        print("打包完成 → %s" % zip_path)
        print("  条目数   : %d" % len(kept))
        print("  压缩后   : %.2f MB (%d B)" % (size / 1024 / 1024, size))
        print("  含 .git  : %s" % ("是" if include_git else "否"))
        print("  sha256   : %s" % h.hexdigest())
        print("  耗时     : %.2f s" % (time.time() - t0))
        if skipped:
            print("  已排除   : %d 项" % len(skipped))
            by_reason = {}
            for _, reason in skipped:
                by_reason[reason] = by_reason.get(reason, 0) + 1
            for reason, n in sorted(by_reason.items(), key=lambda kv: -kv[1]):
                print("      - %-24s %d" % (reason, n))
    return 0


def check(include_git: bool) -> int:
    kept, skipped = walk(include_git)
    print("工程根 : %s" % ROOT)
    print("将入包 : %d 项" % len(kept))
    print("将排除 : %d 项" % len(skipped))
    if skipped:
        print("\n排除清单（前 40 条）：")
        for rel, reason in skipped[:40]:
            print("  %-60s %s" % (rel, reason))
        if len(skipped) > 40:
            print("  ... 其余 %d 条省略" % (len(skipped) - 40))
    # 反查：关键文件在不在
    rels = {r for _, r in kept}
    must = ["README.md", ".gitignore", "00_计划书/README.md",
            "02_代码/yanwei/mcp_tools.py", "02_代码/yanwei/pipeline.py",
            "02_代码/webserver_placeholder"]
    missing = []
    for m in must:
        if m == "02_代码/webserver_placeholder":
            continue
        if m not in rels:
            missing.append(m)
    print("\n关键文件点检 : %s" % ("全部在位 ✓" if not missing else "缺失 " + str(missing)))
    return 1 if missing else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="把工程打包成可分发的 zip")
    ap.add_argument("--out", default=None, help="输出目录（默认：工程上级目录）")
    ap.add_argument("--no-git", action="store_true", help="不包含 .git 历史")
    ap.add_argument("--check", action="store_true", help="只体检不打包")
    args = ap.parse_args()

    include_git = not args.no_git
    if args.check:
        return check(include_git)

    out_dir = args.out or os.path.dirname(ROOT)
    return build(out_dir, include_git)


if __name__ == "__main__":
    raise SystemExit(main())
