"""纯 Python PNG 读写（零依赖，仅用 zlib/struct）。

为什么需要它：
  - 项目原始图像（IDRiD jpg）当前不在本地，质检链路无法用真实图跑通；
  - 本模块可 **合成** 演示用眼底图（含典型缺陷：模糊、过曝、黑边、低分辨率），
    让 FundusPreprocess 的每一档判定都能被真实触发与演示；
  - 真实图片到位后，read_png 可直接读，或用 imageqc 的 Pillow 分支读 jpg。

支持：8bit 灰度(0)/RGB(2)/RGBA(6)，非隔行。写入仅 RGB。
"""

from __future__ import annotations

import math
import struct
import zlib

_SIG = b"\x89PNG\r\n\x1a\n"
_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}


def write_png_rgb(path: str, w: int, h: int, rgb: bytes) -> None:
    if len(rgb) != w * h * 3:
        raise ValueError("rgb 长度与 w*h*3 不符")
    raw = bytearray()
    row = w * 3
    for y in range(h):
        raw.append(0)
        raw += rgb[y * row:(y + 1) * row]

    def chunk(typ: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + typ
            + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
        )

    out = bytearray(_SIG)
    out += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    out += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    out += chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(bytes(out))


def read_png(path: str):
    """返回 (width, height, channels, pixels:bytes)。仅支持 8bit 非隔行。"""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != _SIG:
        raise ValueError("不是 PNG")
    pos = 8
    idat = bytearray()
    w = h = bitd = ctype = None
    while pos + 8 <= len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            w, h, bitd, ctype, _comp, _filt, inter = struct.unpack(">IIBBBBB", body)
            if bitd != 8:
                raise ValueError("仅支持 8bit")
            if inter:
                raise ValueError("不支持隔行扫描")
            if ctype not in _CHANNELS:
                raise ValueError("不支持的色彩类型 %s" % ctype)
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break
    if w is None:
        raise ValueError("缺 IHDR")
    ch = _CHANNELS[ctype]
    bpp = ch
    stride = w * bpp
    raw = zlib.decompress(bytes(idat))
    out = bytearray()
    prev = bytearray(stride)
    p = 0
    for _y in range(h):
        f = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if f == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pp = a + b - c
                pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        out += line
        prev = line
    return w, h, ch, bytes(out)


def to_gray(w: int, h: int, ch: int, px: bytes) -> bytes:
    """转灰度（RGB 加权 / 灰度直通 / 丢弃 alpha）。"""
    if ch == 1:
        return px
    out = bytearray(w * h)
    if ch == 2:
        for i in range(w * h):
            out[i] = px[i * 2]
    elif ch == 3:
        for i in range(w * h):
            r, g, b = px[i * 3], px[i * 3 + 1], px[i * 3 + 2]
            out[i] = (299 * r + 587 * g + 114 * b) // 1000
    elif ch == 4:
        for i in range(w * h):
            r, g, b = px[i * 4], px[i * 4 + 1], px[i * 4 + 2]
            out[i] = (299 * r + 587 * g + 114 * b) // 1000
    return bytes(out)


def downsample_gray(w: int, h: int, gray: bytes, max_side: int = 256):
    """等比缩放到长边 <= max_side（最近邻），统一不同输入的尺度。

    v1.2 修复：原实现用整除步长（step = max(w,h)//max_side），导致
    384→384、512→256、4288→225 缩到不同尺寸，laplacian_var 在不同尺度上
    计算产生档位漂移（尺度依赖）。改为真正等比缩放后，所有输入长边
    统一到 max_side，清晰度判据在同一尺度上才有可比性。
    """
    if max(w, h) <= max_side:
        return w, h, gray
    scale = max_side / max(w, h)
    nw = max(1, int(w * scale))
    nh = max(1, int(h * scale))
    out = bytearray(nw * nh)
    for y in range(nh):
        sy = int(y / scale)
        base = sy * w
        for x in range(nw):
            out[y * nw + x] = gray[base + int(x / scale)]
    return nw, nh, bytes(out)


# ----------------------------------------------------------------------
# 合成演示图：模拟眼底彩照的常见缺陷，用于质检链路演示
# ----------------------------------------------------------------------

def _fundus_base(w: int, h: int, seed: int = 7):
    """造一张"能看"的视网膜底图：暗红背景 + 中央视盘 + 若干病灶亮点。"""
    cx, cy = w / 2.0, h / 2.0
    rad = min(w, h) * 0.48
    buf = bytearray(w * h * 3)
    s = seed
    for y in range(h):
        dy = y - cy
        for x in range(w):
            dx = x - cx
            d2 = dx * dx + dy * dy
            i = (y * w + x) * 3
            if d2 > rad * rad:
                buf[i] = buf[i + 1] = buf[i + 2] = 8  # 视野外：近黑（黑边来源）
                continue
            t = (d2 / (rad * rad)) ** 0.5
            r = int(120 + 60 * (1 - t))
            g = int(38 + 22 * (1 - t))
            b = int(30 + 16 * (1 - t))
            # 视盘
            if (dx - rad * 0.32) ** 2 + (dy + rad * 0.05) ** 2 < (rad * 0.11) ** 2:
                r, g, b = 205, 175, 120
            buf[i], buf[i + 1], buf[i + 2] = r & 255, g & 255, b & 255
    # 撒一些病灶亮点（确定性伪随机）
    for k in range(26):
        s = (1103515245 * s + 12345) & 0x7FFFFFFF
        ang = (s % 6283) / 1000.0
        s = (1103515245 * s + 12345) & 0x7FFFFFFF
        rr = (s % 1000) / 1000.0 * rad * 0.82
        px, py = int(cx + rr * math.cos(ang)), int(cy + rr * math.sin(ang))
        s = (1103515245 * s + 12345) & 0x7FFFFFFF
        size = 2 + s % 5
        for yy in range(py - size, py + size + 1):
            for xx in range(px - size, px + size + 1):
                if 0 <= xx < w and 0 <= yy < h:
                    i = (yy * w + xx) * 3
                    buf[i] = min(255, buf[i] + 90)
                    buf[i + 1] = min(255, buf[i + 1] + 60)
    return bytes(buf)


def apply_defect(px: bytearray, w: int, h: int, defect: str, seed: int = 7) -> bytes:
    """对 RGB 像素施加退化，返回新字节串。

    **单独抽出来的理由**：合成图与"真图注入退化"要能共用同一套退化算子，
    否则测试床与演示图会各退化各的，口径就散了。

    defect: none / blur / overexposed / dark / noframe / mask
    """
    if not defect or defect == "none":
        return bytes(px)
    px = bytearray(px)

    if defect == "blur":
        # 3x3 均值模糊两遍
        for _ in range(2):
            src = bytes(px)
            for y in range(1, h - 1):
                for x in range(1, w - 1):
                    acc = [0, 0, 0]
                    for dy in (-1, 0, 1):
                        for dx in (-1, 0, 1):
                            j = ((y + dy) * w + (x + dx)) * 3
                            acc[0] += src[j]; acc[1] += src[j + 1]; acc[2] += src[j + 2]
                    i = (y * w + x) * 3
                    px[i], px[i + 1], px[i + 2] = acc[0] // 9, acc[1] // 9, acc[2] // 9
    elif defect == "overexposed":
        # v1.1 修复（第一步执行报告「发现二」）：原实现逐通道 ×2.1 + 60 只让 R 通道饱和，
        # 转灰度后最高仅 ~203，**低于过曝判据的 250**，导致"过曝档"演示不出来。
        # 改为整体增益 + 偏移，使视野内三通道同时接近饱和（贴近真实曝光过度）。
        for i in range(len(px)):
            px[i] = min(255, int(px[i] * 2.8) + 120)
    elif defect == "dark":
        for i in range(len(px)):
            px[i] = int(px[i] * 0.18)
    elif defect == "noframe":
        cx, cy = w / 2.0, h / 2.0
        rad = min(w, h) * 0.48
        for y in range(h):
            for x in range(w):
                if (x - cx) ** 2 + (y - cy) ** 2 > rad * rad:
                    i = (y * w + x) * 3
                    px[i], px[i + 1], px[i + 2] = 90, 60, 55
    elif defect == "mask":
        # 遮挡黑边：视野内贴一块近黑矩形，模拟眼睑/睫毛遮挡
        x0, y0 = int(w * 0.05), int(h * 0.38)
        x1, y1 = int(w * 0.45), int(h * 0.60)
        for y in range(y0, y1):
            for x in range(x0, x1):
                i = (y * w + x) * 3
                px[i] = px[i + 1] = px[i + 2] = 6

    return bytes(px)


def synth_fundus(path: str, w: int = 512, h: int = 512, defect: str = "none", seed: int = 7) -> str:
    """生成一张演示用眼底图（合成底图 + 可选退化）。

    defect: none | blur（模糊）| overexposed（过曝）| dark（过暗）
            | noframe（无黑边/边界异常）| mask（遮挡黑边）| tiny（低分辨率）
    """
    if defect == "tiny":
        w = h = 180
    px = bytearray(_fundus_base(w, h, seed))
    px = bytearray(apply_defect(px, w, h, defect, seed))
    write_png_rgb(path, w, h, bytes(px))
    return path
