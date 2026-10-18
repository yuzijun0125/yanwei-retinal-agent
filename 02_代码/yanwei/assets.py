"""Validate uploaded images, preserve originals, and provide bounded inference previews."""
import base64
import hashlib
import io
import json
from pathlib import Path
import re
import uuid
from .config import default_output_dir

MAX_BYTES = 8 * 1024 * 1024


def folder():
    return Path(default_output_dir()) / "uploads"


def get(asset_id):
    if not isinstance(asset_id, str) or not re.fullmatch(r"[a-f0-9]{32}", asset_id):
        raise ValueError("图片资产ID无效")
    record = folder() / (asset_id + ".json")
    if not record.is_file():
        raise ValueError("图片资产不存在，请重新上传")
    return json.loads(record.read_text(encoding="utf-8"))


def upload(filename, encoded):
    from PIL import Image, UnidentifiedImageError
    if not isinstance(encoded, str) or len(encoded) > (MAX_BYTES * 4 // 3 + 8):
        raise ValueError("图片不得超过8MB")
    try:
        raw = base64.b64decode(encoded, validate=True)
        if not raw or len(raw) > MAX_BYTES:
            raise ValueError("图片为空或超过8MB")
        with Image.open(io.BytesIO(raw)) as source:
            width, height = source.size
            fmt = source.format
            if fmt not in ("JPEG", "PNG", "TIFF", "WEBP"):
                raise ValueError("仅支持JPEG、PNG、TIFF、WebP图片")
            if width * height > 30_000_000 or min(width, height) < 64:
                raise ValueError("图片尺寸应至少64像素且不超过3000万像素")
            source.verify()
    except (UnidentifiedImageError, OSError, base64.binascii.Error) as exc:
        raise ValueError("图片无法解码，请使用完整的原始图片") from exc
    asset_id = uuid.uuid4().hex
    target = folder()
    target.mkdir(parents=True, exist_ok=True)
    original = target / (asset_id + ".original." + {"JPEG": "jpg", "PNG": "png", "TIFF": "tif", "WEBP": "webp"}[fmt])
    preview = target / (asset_id + ".jpg")
    with Image.open(io.BytesIO(raw)) as source:
        image = source.convert("RGB")
        image.thumbnail((1024, 1024))
        image.save(preview, "JPEG", quality=95)
    original.write_bytes(raw)
    record = {"asset_id": asset_id, "filename": str(filename or "上传图片")[:200],
              "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
              "width": width, "height": height, "original_path": str(original),
              "analysis_path": str(preview), "preview_url": "/api/asset?id=" + asset_id,
              "notice": "已保留原图；处理预览用于整图相似检索，不能替代微小病灶定位或医学诊断"}
    (target / (asset_id + ".json")).write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    return record
