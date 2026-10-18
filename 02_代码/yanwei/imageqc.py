"""FundusPreprocess · 眼底图像质量评估与 ROI 判断。

定位（对齐计划书 3.5）：图像质量直接决定能否给出动作建议，是决策链的第一道门。
输出三档：GRADABLE（可判读）/ DOWNGRADE（需降级，仅文本线）/ RETAKE（需重拍）。

实现说明：
  - 纯 Python 算法，零依赖；PNG 走内置解码，JPG 若环境有 Pillow 则自动启用；
  - 图像缺失时不假装成功，返回 UNASSESSED 并说明原因（"未知 ≠ 阴性"原则）。

五项指标：
  1) 分辨率      —— 短边阈值
  2) 有效视野    —— 全图非近黑像素占比，判是否落在合理区间
                    （圆形视野外的黑边是眼底图的正常特征，不是缺陷）
  3) 曝光        —— 灰度均值落在合理带内 + 近饱和像素占比（捕获局部过曝）
  4) 清晰度      —— 拉普拉斯方差（用二阶差分近似）
  5) 对比度      —— 灰度标准差

v1.1 修复（依据：E 盘 81 张 IDRiD 真图实测，见 03_文档/眼维_第一步_执行报告_v1.md）
  - BUG-1 视野判据方向反：原判据"边缘近黑占比 > 0.62 → 异常"把眼底图的**正常黑边**
          当成缺陷。实测 81 张真图 border_black ∈ [0.640, 0.730] —— **张张越线**，
          每张被无端扣 0.20 分（score 恰好 0.80 才勉强留在 GRADABLE，离误判一线之隔），
          IDRiD_02/11 因叠加上曝光扣分直接掉到 DOWNGRADE。
          → 改为判"**有效视野占比** field_ratio"是否落在 [FIELD_MIN, FIELD_MAX]；
            border_black 降级为诊断指标（保留在 metrics，不参与判分）。
  - BUG-2 过曝判据失灵：原只看灰度均值（>205），局部饱和可整体绕过。
          → 新增"近饱和像素占比 sat_ratio（灰度 ≥ 250）"。实测真图 sat_ratio 全为 0.0000，
            可分性充足。
  - BUG-3（附带）曝光下限过严：原 EXPO_LOW=45 把偏暗的正常眼底图判为曝光不足
          （实测真图 mean_gray ∈ [40.5, 98.0]，眼底图天然偏暗）。
          → EXPO_LOW 调整为 30.0。
"""

from __future__ import annotations

import os

from . import pnglite


class QualityResult:
    def __init__(self, label, score, metrics, reasons, source):
        self.label = label          # GRADABLE / DOWNGRADE / RETAKE / UNASSESSED
        self.score = score          # 0—1 质量分
        self.metrics = metrics      # dict：原始指标
        self.reasons = reasons      # list[str]：人类可读原因
        self.source = source        # 实际评估来源文件

    def to_dict(self):
        return {
            "label": self.label,
            "score": round(self.score, 4),
            "metrics": self.metrics,
            "reasons": self.reasons,
            "source": self.source,
        }


# 判定阈值（与 thresholds.yaml 解耦：这里是**图像物理量**，不参与决策进化）
MIN_SIDE = 300             # 短边低于此 → 重拍
BLACK_LEVEL = 20           # 视为"黑"的灰度上限（视野外背景）
FIELD_MIN = 0.45           # 有效视野占比下限：低于此 → 视野过小 / 定位裁剪异常
FIELD_FATAL = 0.25         # 有效视野占比低于此 → 直接重拍
FIELD_MAX = 0.95           # 有效视野占比上限：高于此 → 几乎无视野边界，疑似非标准眼底图
EXPO_LOW, EXPO_HIGH = 30.0, 205.0   # 灰度均值合理带（眼底图天然偏暗，下限不宜高）
SAT_LEVEL = 250            # 近饱和灰度
SAT_WARN = 0.03            # 近饱和像素占比高于此 → 过曝（局部饱和也能捕获）
BLUR_MIN = 55.0            # 拉普拉斯方差下限
CONTRAST_MIN = 16.0        # 灰度标准差下限

# 局部遮挡（v1.1 新增）：把图切成网格，找"视野内"的连续无信号区块。
# 为什么需要：圆形视野外的黑边是**正常**的（见 BUG-1），但视野**内部**出现大块
# 连续近黑（眼睑/睫毛遮挡、镜下遮光）是**缺陷**——这条判据专门抓后者，
# 且只看内层区块，天然排除视野外黑边，不会与 field_ratio 判据打架。
BLOCK_GRID = 8             # 8×8 网格
BLOCK_DARK = 0.70          # 单块内近黑像素占比高于此 → 该块"无信号"
OCCLUSION_MIN_BLOCKS = 2   # 内层连续无信号区块达到此数 → 判局部遮挡


def _laplacian_var(nw, nh, g):
    """拉普拉斯响应的方差：越小越模糊。"""
    if nw < 3 or nh < 3:
        return 0.0
    vals = []
    for y in range(1, nh - 1):
        base = y * nw
        for x in range(1, nw - 1):
            i = base + x
            lap = 4 * g[i] - g[i - 1] - g[i + 1] - g[i - nw] - g[i + nw]
            vals.append(lap)
    if not vals:
        return 0.0
    m = sum(vals) / len(vals)
    return sum((v - m) ** 2 for v in vals) / len(vals)


def _occlusion_blocks(nw, nh, g):
    """返回内层网格中**最大连通无信号区块**的大小（块数）。

    只看内层（去掉最外一圈）：视野外的黑边全在外圈，这样排除掉它，
    剩下的"黑块"才真的是视野内遮挡。返回 0 表示没有连续遮挡。
    """
    grid = BLOCK_GRID
    bw, bh = nw // grid, nh // grid
    if bw < 2 or bh < 2:
        return 0
    dark = set()
    for by in range(grid):
        for bx in range(grid):
            cnt = tot = 0
            for y in range(by * bh, (by + 1) * bh):
                base = y * nw
                for x in range(bx * bw, (bx + 1) * bw):
                    tot += 1
                    if g[base + x] <= BLACK_LEVEL:
                        cnt += 1
            if tot and (cnt / tot) > BLOCK_DARK:
                dark.add((bx, by))
    inner = {b for b in dark if 0 < b[0] < grid - 1 and 0 < b[1] < grid - 1}
    if not inner:
        return 0
    seen, best = set(), 0
    for b in inner:
        if b in seen:
            continue
        stack, n = [b], 0
        seen.add(b)
        while stack:
            x, y = stack.pop()
            n += 1
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nb = (x + dx, y + dy)
                if nb in inner and nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        best = max(best, n)
    return best


def assess_cached(image_path: str | None) -> QualityResult:
    """Bounded metadata cache; stat changes invalidate results and callers receive copies."""
    import copy
    try:
        path = os.path.abspath(os.fspath(image_path))
        st = os.stat(path)
    except (TypeError, OSError):
        return assess(image_path)
    return copy.deepcopy(_cached_quality(path, st.st_mtime_ns, st.st_size))


from functools import lru_cache


@lru_cache(maxsize=16)
def _cached_quality(path, modified_ns, size):
    return assess(path)


def assess(image_path: str | None) -> QualityResult:
    if not image_path:
        return QualityResult("UNASSESSED", 0.0, {}, ["未提供图像，无法质检"], None)
    if not os.path.exists(image_path):
        return QualityResult(
            "UNASSESSED", 0.0, {}, ["图像文件不存在：%s（原始图像未入库）" % image_path], image_path
        )

    ext = os.path.splitext(image_path)[1].lower()
    try:
        if ext == ".png":
            w, h, ch, px = pnglite.read_png(image_path)
            gray = pnglite.to_gray(w, h, ch, px)
        else:
            gray, (w, h) = _read_with_pillow(image_path)
    except Exception as exc:  # noqa: BLE001
        return QualityResult("UNASSESSED", 0.0, {}, ["图像解码失败：%s" % exc], image_path)

    nw, nh, g = pnglite.downsample_gray(w, h, gray, max_side=224)

    # --- 指标计算 ---
    n = len(g)
    mean = sum(g) / n
    var = sum((v - mean) ** 2 for v in g) / n
    std = var ** 0.5
    lap = _laplacian_var(nw, nh, g)

    # 有效视野占比：全图非近黑像素占比（圆形视野外的黑边是正常特征）
    dark_n = sum(1 for v in g if v <= BLACK_LEVEL)
    field_ratio = 1.0 - dark_n / n

    # 边缘环近黑占比：**保留为诊断指标**（不参与判分）
    ring = []
    for x in range(nw):
        ring.append(g[x]); ring.append(g[(nh - 1) * nw + x])
    for y in range(nh):
        ring.append(g[y * nw]); ring.append(g[y * nw + nw - 1])
    border_black = sum(1 for v in ring if v <= BLACK_LEVEL) / max(1, len(ring))

    # 近饱和像素占比：捕获局部过曝（只看均值会漏）
    sat_ratio = sum(1 for v in g if v >= SAT_LEVEL) / n

    # 视野内局部遮挡：连续无信号区块（与 field_ratio 互补，不重复扣分）
    occ_blocks = _occlusion_blocks(nw, nh, g)

    metrics = {
        "width": w, "height": h, "min_side": min(w, h),
        "mean_gray": round(mean, 2), "contrast_std": round(std, 2),
        "laplacian_var": round(lap, 2),
        "field_ratio": round(field_ratio, 4),          # 有效视野占比（判据）
        "sat_ratio": round(sat_ratio, 4),              # 近饱和像素占比（判据）
        "occlusion_blocks": occ_blocks,                # 视野内连续无信号区块数（判据）
        "border_black_ratio": round(border_black, 4),  # 诊断用（不判分）
    }

    reasons, score = [], 1.0

    if min(w, h) < MIN_SIDE:
        reasons.append("分辨率过低：短边 %d < %d" % (min(w, h), MIN_SIDE))
        score -= 0.45

    # 曝光：均值带 + 近饱和占比（双判据）
    if mean < EXPO_LOW:
        reasons.append("曝光不足：灰度均值 %.1f < %.0f" % (mean, EXPO_LOW))
        score -= 0.25
    elif mean > EXPO_HIGH:
        reasons.append("曝光过度：灰度均值 %.1f > %.0f" % (mean, EXPO_HIGH))
        score -= 0.25
    if sat_ratio >= SAT_WARN:
        reasons.append("过曝：近饱和像素占比 %.1f%% ≥ %.0f%%" % (sat_ratio * 100, SAT_WARN * 100))
        score -= 0.25

    if lap < BLUR_MIN:
        reasons.append("清晰度不足：拉普拉斯方差 %.1f < %.1f" % (lap, BLUR_MIN))
        score -= 0.30

    if std < CONTRAST_MIN:
        reasons.append("对比度过低：灰度标准差 %.1f < %.1f" % (std, CONTRAST_MIN))
        score -= 0.20

    if occ_blocks >= OCCLUSION_MIN_BLOCKS:
        reasons.append("局部遮挡：视野内检测到 %d 个连续无信号区块（疑似眼睑/睫毛遮挡）" % occ_blocks)
        score -= 0.25

    # 有效视野：判是否落在合理区间（替代原"边缘黑占比 > 0.62"的错误判据）
    if field_ratio < FIELD_MIN:
        reasons.append("有效视野过小：占比 %.2f < %.2f（定位/裁剪异常）" % (field_ratio, FIELD_MIN))
        score -= 0.25
    elif field_ratio > FIELD_MAX:
        reasons.append("视野边界异常：有效视野占比 %.2f > %.2f（疑似非标准眼底图）" % (field_ratio, FIELD_MAX))
        score -= 0.15

    score = max(0.0, min(1.0, score))

    # 三档判定：任一致命项直接重拍；轻度问题降级为仅文本线
    fatal = (
        (min(w, h) < MIN_SIDE)
        or (mean < EXPO_LOW * 0.6)
        or (lap < BLUR_MIN * 0.35)
        or (field_ratio < FIELD_FATAL)
    )
    if fatal:
        label = "RETAKE"
    elif score >= 0.80:
        label = "GRADABLE"
        if not reasons:
            reasons.append("五项指标均在合理区间")
    else:
        label = "DOWNGRADE"

    return QualityResult(label, score, metrics, reasons, image_path)


def _read_with_pillow(path):
    from PIL import Image  # 可选依赖

    with Image.open(path) as im:
        im = im.convert("L")
        w, h = im.size
        return im.tobytes(), (w, h)
