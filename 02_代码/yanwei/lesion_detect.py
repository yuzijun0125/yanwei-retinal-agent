"""病灶检测引擎（支柱二 · 图像理解的落地）。

补的是计划书 3.3 的支柱②「轻量跨模态对齐」里缺失的那条腿：**图 → 病灶 → 检索**。
之前 search_cases 的 image_path 只做质检（imageqc），病灶信息被丢弃，导致
"给一张眼底影像报未入库"。本模块把病灶检测接到 image_path 入口上，让图像真正参与检索。

三层引擎（按优先级自动选择，可插拔、可降级）：
  1. UnetDetector    —— PyTorch U-Net 四分类（MA/HE/EX/SE），精度最高，阶段 B 接入；
                       懒加载：未装 torch / 无权重时自动降级，不阻塞主链路。
  2. ZeroDepDetector —— 零依赖启发式（当前默认），纯标准库，毫秒级；
                       只承诺"有无病灶 + 亮斑(渗出类)/暗斑(出血类)大类 + 粗程度"，
                       不承诺精确四分类（诚实边界，见下）。
  3. SAM-Med2D       —— 挂起（预留位，阶段 C 可选，做"看不清时第二意见"）。

诚实的能力边界（务必同步到证据卡/汇报）：
  - 零依赖启发式：只提供未经医学验证的亮/暗区域提示，可能把血管当出血。
  - U-Net：接口可用性不代表识别有效，必须以独立验证集逐类评测结果为准。
  - 病灶检测结果只做"检索意图补充"，不直接下临床结论；程度(severity)是面积粗推测。

数据口径（2026-10-17 实测 IDRiD 原图 + 掩膜）：
  - 掩膜：P 模式调色板，病灶像素值 = 1（不是 255），尺寸 4288×2848。
  - 原图灰度：视野内背景中位 ~106，亮斑(渗出/视盘) > 180 占比 ~1.25%，
    视野外黑边 < 20 占比 ~31%。
  - 病灶规模（占图面积）：MA ~0.08%、HE ~0.3-0.8%、EX ~0.4-4%、SE ~0.02-0.6%、
    OD(视盘) ~1.5-2.2%。
"""

from __future__ import annotations

import os
import time

from . import pnglite

# ---------------------------------------------------------------------------
# 常量（基于 IDRiD 真实图灰度分布标定，见模块 docstring）
# ---------------------------------------------------------------------------

DETECT_SIDE = 256          # 检测用统一尺度（长边），复用 imageqc 的降采样思路

BLACK_LEVEL = 20           # 视野外黑边：灰度 < 此值视为背景（不参与病灶检测）
BG_LOW, BG_HIGH = 30, 170  # 视野内背景灰度带
BRIGHT = 180               # 亮斑阈值：灰度 > 此值视为渗出类/视盘
DARK = 60                  # 暗斑阈值：视野内灰度 < 此值视为出血类

OD_AREA_MIN = 800          # 视盘（256 尺度下）最小面积，用于和渗出区分
MA_AREA_MAX = 6            # 微动脉瘤（256 尺度下）暗斑面积上限（像素）
HE_AREA_MIN = MA_AREA_MAX  # 出血：暗斑面积 > MA 上限
EX_AREA_MAX = 80           # 硬性渗出（256 尺度下）亮斑面积上限
SE_AREA_MIN = EX_AREA_MAX  # 软性渗出：亮斑面积 > EX 上限

# 程度映射（基于病灶面积占视野比，粗推测，非临床标准）
SEV_FEW = 0.005
SEV_SCATTERED = 0.02
SEV_MODERATE = 0.05
SEV_MARKED = 0.10

LESION_LABELS = {
    "MA": "微动脉瘤",
    "HE": "出血",
    "EX": "硬性渗出",
    "SE": "软性渗出",
    "OD": "视盘",
}
# 病灶大类：用于启发式的"亮/暗"判断与主病灶选择
_LESION_KIND = {"MA": "dark", "HE": "dark", "EX": "bright", "SE": "bright", "OD": "od"}


# ---------------------------------------------------------------------------
# 图像读取（Pillow 懒加载；PNG 走内置零依赖解码）
# ---------------------------------------------------------------------------

def read_gray(path: str):
    """读图转灰度，返回 (gray: bytes, w, h)。jpg/tif 走 Pillow，png 走内置。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".png":
        w, h, ch, px = pnglite.read_png(path)
        return pnglite.to_gray(w, h, ch, px), w, h
    try:
        from PIL import Image  # 可选依赖（venv 已装；未装则此处抛错，由上层降级）
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("读取 jpg/tif 需要 Pillow（可选依赖未安装）") from exc
    with Image.open(path) as im:
        im = im.convert("L")
        return im.tobytes(), im.size[0], im.size[1]


# ---------------------------------------------------------------------------
# 连通域（4 邻域 BFS，零依赖）
# ---------------------------------------------------------------------------

def _components(mask, w, h):
    """mask: list[bool]，返回 [(area, pixels)] 按面积降序。"""
    seen = [False] * (w * h)
    comps = []
    for start in range(w * h):
        if not mask[start] or seen[start]:
            continue
        stack = [start]
        seen[start] = True
        pixels = []
        area = 0
        while stack:
            i = stack.pop()
            pixels.append(i)
            area += 1
            x, y = i % w, i // w
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < w and 0 <= ny < h:
                    j = ny * w + nx
                    if mask[j] and not seen[j]:
                        seen[j] = True
                        stack.append(j)
        comps.append((area, pixels))
    comps.sort(key=lambda c: c[0], reverse=True)
    return comps


def _centroid(pixels, w):
    xs = [p % w for p in pixels]
    ys = [p // w for p in pixels]
    return sum(xs) / len(pixels), sum(ys) / len(pixels)


# ---------------------------------------------------------------------------
# 零依赖启发式引擎
# ---------------------------------------------------------------------------

class ZeroDepDetector:
    """零依赖启发式：亮斑(渗出)/暗斑(出血)大类 + 视盘 + 粗程度。

    只做"有无 + 大类 + 面积"，不做可靠的 MA/HE/EX/SE 细分。细分交给 U-Net。
    """

    name = "zero_dep"

    def detect(self, gray, w, h):
        # 1. 视野内掩膜：排除视野外黑边（圆形视野外的近黑背景）
        inner = [v > BLACK_LEVEL for v in gray]
        inner_count = sum(inner)
        if inner_count < (w * h) * 0.1:
            return {"error": "视野内有效像素过少", "detections": [], "codes": {}}

        # 2. 亮斑（渗出类 + 视盘）
        bright_mask = [v > BRIGHT for v in gray]
        bright_comps = _components(bright_mask, w, h)
        od_pixels = []
        bright_lesions = []  # 非视盘的亮斑
        if bright_comps:
            # 视盘 = 最亮最大的连通域（OD 面积 1.5-2.2%，远大于任何渗出）
            od_area, od_pixels = bright_comps[0]
            for area, pixels in bright_comps[1:]:
                if area < 2:
                    continue
                bright_lesions.append((area, pixels))

        # 3. 暗斑（出血类）：视野内、暗于背景、但排除纯黑边
        dark_mask = [(BLACK_LEVEL < v < DARK) for v in gray]
        dark_comps = _components(dark_mask, w, h)
        dark_lesions = [(a, p) for a, p in dark_comps if a >= 2]

        # 4. 归类 + 同类汇总（按病灶类合并，避免输出海量连通域）
        field_area = inner_count or 1
        summary = {}
        for area, pixels in bright_lesions:
            code = "SE" if area > EX_AREA_MAX else "EX"
            summary.setdefault(code, {"count": 0, "area": 0, "cx": 0.0, "cy": 0.0})
            cx, cy = _centroid(pixels, w)
            s = summary[code]
            s["count"] += 1; s["area"] += area; s["cx"] += cx; s["cy"] += cy
        for area, pixels in dark_lesions:
            code = "MA" if area <= MA_AREA_MAX else "HE"
            summary.setdefault(code, {"count": 0, "area": 0, "cx": 0.0, "cy": 0.0})
            cx, cy = _centroid(pixels, w)
            s = summary[code]
            s["count"] += 1; s["area"] += area; s["cx"] += cx; s["cy"] += cy

        detections = []
        for code, s in summary.items():
            n = s["count"]
            detections.append({
                "code": code,
                "label": LESION_LABELS.get(code, code),
                "kind": _LESION_KIND.get(code, "other"),
                "count": n,
                "area": s["area"],
                "area_ratio": round(s["area"] / field_area, 6),
                "centroid": [round(s["cx"] / n / w, 3), round(s["cy"] / n / h, 3)],
                "confidence": "low",
            })
        detections.sort(key=lambda d: d["area_ratio"], reverse=True)

        # 视盘：位置锚点（不是病灶，不参与 lesion 选择，但提供位置线索）
        od = None
        if od_pixels:
            od = _mk("OD", len(od_pixels), od_pixels, field_area, "od", w, h)

        # 5. 主病灶 + 程度
        if not detections:
            return {
                "engine": self.name,
                "detections": [],
                "od": od,
                "dominant_lesion": None,
                "severity": None,
                "codes": {"modality": "CFP"},
                "note": "未检出明显病灶（零依赖启发式，仅大类粗判，可能漏检轻微病灶）",
            }

        # 主病灶 = 面积最大者（面积更稳，数量受连通域噪声影响大）
        dominant = max(detections, key=lambda d: d["area_ratio"])
        severity = self._severity(dominant["area_ratio"], detections)

        codes = {
            "lesion": dominant["code"],
            "severity": severity,
            "modality": "CFP",
        }
        # 视盘提供位置锚点：病灶大概率在后极部/黄斑区附近，保守标 POSTERIOR_POLE
        if od is not None:
            codes["location"] = "POSTERIOR_POLE"

        return {
            "engine": self.name,
            "detections": detections,
            "od": od,
            "dominant_lesion": dominant["code"],
            "severity": severity,
            "codes": codes,
            "confidence": "low",
            "note": "零依赖启发式：亮/暗大类可靠，MA/HE/EX/SE 细分与程度为粗推测",
        }

    @staticmethod
    def _severity(dominant_ratio, detections):
        total = sum(d["area_ratio"] for d in detections)
        if total >= SEV_MARKED:
            return "MARKED"
        if total >= SEV_MODERATE:
            return "MODERATE"
        if total >= SEV_SCATTERED:
            return "SCATTERED"
        if total >= SEV_FEW:
            return "FEW"
        return "FEW"


def _mk(code, area, pixels, field_area, kind, w, h):
    cx, cy = _centroid(pixels, w)
    return {
        "code": code,
        "label": LESION_LABELS.get(code, code),
        "kind": kind,
        "count": 1,           # 零依赖下以连通域计，count 意义有限
        "area": area,
        "area_ratio": round(area / field_area, 6),
        "centroid": [round(cx / w, 3), round(cy / h, 3)],
        "confidence": "low",
    }


# ---------------------------------------------------------------------------
# U-Net 引擎（阶段 B：PyTorch 懒加载，先留清晰骨架）
# ---------------------------------------------------------------------------

class UnetDetector:
    """U-Net 五类病灶分割（MA/HE/EX/SE/OD）。阶段 B：现成 unet.pth 探路，
    跑通后自训替换（绕开候选仓库无 LICENSE 的合规风险）。

    懒加载：构造时不 import torch；首次 detect 才加载权重。未装 torch / 无权重
    时 available() 为 False，上层自动降级到 ZeroDepDetector。

    输入口径（与候选仓库 correct_dataset.ipynb 训练时对齐）：
      - RGB → resize 256×256 → /255（**无 ImageNet 归一化**，cell 0 实测如此）
      - 输出 5 通道 sigmoid，顺序 = [MA, HE, EX, SE, OD]
      - 阈值沿用作者标定的 Otsu 阈值（待本仓库 GT 复标后再校准）
    """

    name = "unet"

    # U-Net 输出 5 通道顺序（候选仓库 lesion_names 顺序，index 即通道）
    LESION_ORDER = ["MA", "HE", "EX", "SE", "OD"]
    # 作者标定的 Otsu 阈值（对应上述顺序）；presence 判定 = 面积 > PRESENCE_MIN
    THRESHOLDS = [0.05, 0.06, 0.19, 0.07, 0.35]
    SIDE = 256
    PRESENCE_MIN = 100  # 256×256 尺度下的最小病灶像素数（作者口径）

    def __init__(self, weight_path: str | None = None):
        self.weight_path = weight_path
        self._model = None
        self._available = None  # None=未探测 / True / False
        self._reason = ""

    def available(self) -> bool:
        if self._available is None:
            self._probe()
        return self._available

    def _probe(self):
        if not self.weight_path or not os.path.exists(self.weight_path):
            self._available = False
            self._reason = "无权重文件（unet.pth 未就位）"
            return
        try:
            import torch  # noqa: F401
        except ImportError:
            self._available = False
            self._reason = "PyTorch 未安装"
            return
        self._available = True
        self._reason = ""

    def _load_model(self):
        """构建 U-Net 架构（yanwei/unet_model.py）并加载权重。懒加载：未装 torch
        时本方法不会被调用（available() 已拦截）。"""
        import torch
        from .unet_model import build_unet

        model = build_unet(n_classes=5)
        state = torch.load(self.weight_path, map_location="cpu", weights_only=True)
        if isinstance(state, dict) and "state_dict" in state:
            state = state["state_dict"]
        model.load_state_dict(state)
        model.eval()
        self._model = model

    def detect(self, image_path):
        """U-Net 推理。注意：需要 RGB 输入（训练口径），因此接收原始路径自读，
        与 ZeroDepDetector.detect(gray, w, h) 签名不同，由 detect_lesions 分流。"""
        import numpy as np
        import torch
        from PIL import Image

        if self._model is None:
            self._load_model()

        with Image.open(image_path) as im:
            rgb = im.convert("RGB").resize((self.SIDE, self.SIDE))
        arr = np.asarray(rgb, dtype=np.float32).transpose(2, 0, 1) / 255.0
        x = torch.from_numpy(arr).unsqueeze(0)

        with torch.no_grad():
            probs = torch.sigmoid(self._model(x))[0].numpy()  # (5, 256, 256)

        field_area = self.SIDE * self.SIDE
        detections = []
        od = None
        for i, code in enumerate(self.LESION_ORDER):
            mask_flat = (probs[i] > self.THRESHOLDS[i]).ravel().tolist()
            area = sum(mask_flat)
            if area < self.PRESENCE_MIN:
                continue
            comps = _components(mask_flat, self.SIDE, self.SIDE)
            all_px = [p for _, px in comps for p in px]
            cx, cy = _centroid(all_px, self.SIDE) if all_px else (0.0, 0.0)
            item = {
                "code": code,
                "label": LESION_LABELS.get(code, code),
                "kind": _LESION_KIND.get(code, "other"),
                "count": len(comps),
                "area": area,
                "area_ratio": round(area / field_area, 6),
                "centroid": [round(cx / self.SIDE, 3), round(cy / self.SIDE, 3)],
                "confidence": "medium",
            }
            if code == "OD":
                od = item
            else:
                detections.append(item)

        detections.sort(key=lambda d: d["area_ratio"], reverse=True)

        if not detections:
            return {
                "engine": self.name,
                "detections": [],
                "od": od,
                "dominant_lesion": None,
                "severity": None,
                "codes": {"modality": "CFP"},
                "confidence": "medium",
                "note": "U-Net 未检出明显病灶",
            }

        dominant = max(detections, key=lambda d: d["area_ratio"])
        severity = ZeroDepDetector._severity(dominant["area_ratio"], detections)

        codes = {
            "lesion": dominant["code"],
            "severity": severity,
            "modality": "CFP",
        }
        if od is not None:
            codes["location"] = "POSTERIOR_POLE"

        return {
            "engine": self.name,
            "detections": detections,
            "od": od,
            "dominant_lesion": dominant["code"],
            "severity": severity,
            "codes": codes,
            "confidence": "medium",
            "note": "现成 unet.pth 探路推理（仅链路验证，不对外当指标）",
        }


# ---------------------------------------------------------------------------
# 统一入口：自动选引擎 + 病灶 → 五维码
# ---------------------------------------------------------------------------

_ENGINE_CACHE = {}


def _pick_engine():
    """选择引擎：U-Net（若可用）→ 零依赖。结果缓存，避免每次调用重复探测。"""
    if "unet" not in _ENGINE_CACHE:
        unet = UnetDetector(weight_path=_default_unet_path())
        _ENGINE_CACHE["unet"] = unet
    unet = _ENGINE_CACHE["unet"]
    if unet.available():
        return unet
    return ZeroDepDetector()


def _default_unet_path():
    """阶段 B 的权重位置：02_代码/models/unet.pth（预留，随包分发或自训产出）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "..", "models", "unet.pth")


def detect_lesions(image_path: str | None) -> dict:
    """图 → 病灶检测结果（含可直接合并进 search_cases 的 codes）。

    返回结构见模块 docstring。任何失败都返回 available=False + 原因，不抛异常，
    让 search_cases 能干净地降级为纯文本检索。
    """
    t0 = time.perf_counter()
    if not image_path:
        return _unavailable("未提供图像")
    if not os.path.exists(image_path):
        return _unavailable("图像文件不存在：%s" % image_path)

    try:
        gray, w, h = read_gray(image_path)
    except Exception as exc:  # noqa: BLE001
        return _unavailable("图像读取失败：%s" % exc)

    # 统一尺度（复用等比缩放，避免尺度依赖）
    nw, nh, g = pnglite.downsample_gray(w, h, gray, max_side=DETECT_SIDE)

    engine = _pick_engine()
    try:
        if engine.name == "unet":
            result = engine.detect(image_path)  # U-Net 需 RGB 原图，自读
        else:
            result = engine.detect(g, nw, nh)
    except Exception as exc:  # noqa: BLE001
        return _unavailable("检测引擎失败：%s" % exc)

    result.setdefault("engine", engine.name)
    result["available"] = True
    result["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 3)
    return result


def _unavailable(reason: str) -> dict:
    return {"available": False, "error": reason, "detections": [], "codes": {}}


def merge_image_codes(existing_codes: dict, image_codes: dict) -> dict:
    """把图像检测出的五维码合并进现有检索意图。

    原则：**文字优先**——用户明说的维度（existing_codes 已有）不被图像推测覆盖；
    图像只补充文字没覆盖的维度（尤其是 lesion，这正是"给图也能检索"的关键）。
    """
    merged = dict(existing_codes or {})
    for dim, val in (image_codes or {}).items():
        if val and not merged.get(dim):
            merged[dim] = val
    return merged
