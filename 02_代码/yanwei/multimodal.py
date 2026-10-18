"""Pinned FLAIR embeddings and an offline image index; optional dependencies are lazy."""
from functools import lru_cache
import hashlib
import io
import importlib.util
import json
from pathlib import Path
import threading
import time
from .config import default_output_dir

CODE_ROOT = Path(__file__).resolve().parents[1]
_LOCK = threading.RLock()
_ENCODER = None
_WARMING = False
_STATE_LOCK = threading.Lock()


def settings():
    return json.loads((CODE_ROOT / 'config' / 'multimodal.json').read_text(encoding='utf-8'))


def index_folder():
    return Path(default_output_dir()) / 'multimodal'


def resolve_image(data_dir, record):
    """Map the legacy dataset-root prefix, never accept client-supplied file paths."""
    root = Path(data_dir).resolve()
    raw = str(record.get('raw_path', '')).replace('\\', '/')
    parts = raw.split('/')
    if parts and parts[0] in ('01_数据集本体', '01_数据集'):
        parts = parts[1:]
    target = root.joinpath(*parts).resolve()
    if not target.is_relative_to(root):
        raise ValueError('病例图片路径超出数据目录')
    return target


class FlairEncoder:
    def __init__(self):
        import torch
        from flair import FLAIRModel
        cfg = settings()
        torch.set_num_threads(int(cfg.get('cpu_threads', 4)))
        model_root = CODE_ROOT / 'models'
        # All downloads happen in the explicit preparation command, never a user request.
        self.model = FLAIRModel.from_pretrained(str(model_root / 'flair'), local_files_only=True,
                                               vision_pretrained=False,
                                               bert_type=str(model_root / 'clinicalbert'))
        self.model.eval()
        self.torch = torch

    def image(self, path):
        from PIL import Image
        import numpy as np
        with Image.open(path) as source:
            pixels = np.array(source.convert('RGB'))
        with _LOCK, self.torch.inference_mode():
            batch = self.model.preprocess_image(pixels)
            vector = self.model.vision_model(batch)[0].cpu().numpy().copy()
        return normalize(vector)

    @lru_cache(maxsize=128)
    def text(self, phrase):
        with _LOCK, self.torch.inference_mode():
            ids, mask = self.model.preprocess_text([phrase])
            vector = self.model.text_model(ids, mask)[0].cpu().numpy().copy()
        return normalize(vector)


def normalize(vector):
    import numpy as np
    result = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(result))
    if result.ndim != 1 or not np.isfinite(result).all() or norm <= 0:
        raise ValueError('模型向量无效')
    return result / norm


def encoder():
    global _ENCODER
    with _LOCK:
        if _ENCODER is None:
            _ENCODER = FlairEncoder()
        return _ENCODER


def start_warming():
    global _WARMING
    with _STATE_LOCK:
        if _WARMING or _ENCODER is not None:
            return
        _WARMING = True
    def load():
        global _WARMING
        try:
            encoder()
        except Exception:
            pass
        finally:
            with _STATE_LOCK:
                _WARMING = False
    threading.Thread(target=load, daemon=True, name='yanwei-flair-warmup').start()


def english_query(codes, negated=()):
    # FLAIR uses English ClinicalBERT. Do not pretend arbitrary Chinese is translated.
    terms = {
        'lesion': {'MA': 'microaneurysms', 'HE': 'retinal hemorrhages', 'EX': 'hard exudates',
                   'SE': 'soft exudates', 'NV': 'neovascularization'},
        'location': {'MACULA': 'in the macula', 'POSTERIOR_POLE': 'at the posterior pole',
                     'PERIPHERY': 'in the peripheral retina', 'OPTIC_DISC': 'near the optic disc'},
    }
    if not codes.get('lesion') or 'lesion' in negated:
        return None
    return ' '.join(terms[d].get(codes.get(d), '') for d in terms).strip() or None


@lru_cache(maxsize=2)
def _load_index(path, modified):
    import numpy as np
    with np.load(path, allow_pickle=False) as packed:
        ids = packed['image_ids'].tolist()
        vectors = packed['vectors'].copy()
    if vectors.ndim != 2 or len(ids) != len(vectors) or not np.isfinite(vectors).all():
        raise ValueError('图文索引损坏')
    return ids, vectors


def status():
    folder = index_folder()
    manifest_path = folder / 'manifest.json'
    try:
        cfg = settings()
        if not cfg.get('enabled'):
            return {'ready': False, 'reason': '图文模型已关闭'}
        if any(importlib.util.find_spec(name) is None for name in ('torch', 'flair', 'numpy')):
            return {'ready': False, 'reason': '当前Python缺少图文模型依赖，请使用独立图文环境启动'}
        if not (CODE_ROOT / 'models' / 'manifest.json').is_file():
            return {'ready': False, 'reason': '尚未完成官方模型权重准备'}
        if not manifest_path.is_file() or not (folder / 'index.npz').is_file():
            return {'ready': False, 'reason': '尚未完成离线图像向量索引'}
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest.get('model_revision') != cfg['model_revision']:
            return {'ready': False, 'reason': '模型与索引版本不一致，请重建索引'}
        return {'ready': True, 'encoder': 'FLAIR', 'model_revision': cfg['model_revision'],
                'image_count': manifest['image_count'], 'model_loaded': _ENCODER is not None,
                'embedding_scope': 'whole_image', 'clinical_validity': 'UNVALIDATED'}
    except (OSError, ValueError, KeyError) as exc:
        return {'ready': False, 'reason': '索引状态读取失败：' + type(exc).__name__}


@lru_cache(maxsize=16)
def _image_vector(path, modified, size):
    return encoder().image(path)


def recall(image_path, codes, negated=(), limit=30):
    info = status()
    phrase = english_query(codes, negated)
    if not info['ready'] or (not image_path and not phrase):
        return {}, {**info, 'used': False, 'reason': info.get('reason', '没有可映射的语义查询术语')}
    if _ENCODER is None:
        start_warming()
        return {}, {**info, 'used': False, 'reason': '图文模型正在初始化，本轮先走文字召回', 'fallback': 'BM25_TEXT_ONLY'}
    t0 = time.perf_counter()
    try:
        import numpy as np
        image_vector = text_vector = None
        if image_path:
            p = Path(image_path).resolve()
            st = p.stat()
            image_vector = _image_vector(str(p), st.st_mtime_ns, st.st_size)
        if phrase:
            text_vector = encoder().text(phrase)
        index = index_folder() / 'index.npz'
        ids, vectors = _load_index(str(index), index.stat().st_mtime_ns)
        weight = settings().get('image_weight', .7)
        query = (normalize(weight * image_vector + (1 - weight) * text_vector)
                 if image_vector is not None and text_vector is not None
                 else image_vector if image_vector is not None else text_vector)
        scores = vectors @ query
        order = np.argsort(-scores, kind='stable')[:limit]
        hits = {ids[int(i)]: {'rank': rank, 'cosine': round(float(scores[i]), 6),
                              'image_cosine': round(float(vectors[i] @ image_vector), 6) if image_vector is not None else None,
                              'text_cosine': round(float(vectors[i] @ text_vector), 6) if text_vector is not None else None}
                for rank, i in enumerate(order, 1)}
        return hits, {**info, 'used': True, 'query_kind': 'image_text' if image_path and phrase else 'image' if image_path else 'text',
                      'english_terms': phrase, 'elapsed_ms': round((time.perf_counter() - t0) * 1000, 2),
                      'note': '整图向量相似度用于召回，不是病灶定位或疾病概率'}
    except Exception as exc:
        # Do not expose request paths or URLs in an error response.
        return {}, {**info, 'used': False, 'reason': '图文编码不可用：' + type(exc).__name__,
                    'fallback': 'BM25_TEXT_ONLY'}


def thumbnail(data_dir, image_record, bbox=None):
    from PIL import Image
    target = resolve_image(data_dir, image_record)
    with Image.open(target) as source:
        image = source.convert('RGB')
        if bbox is not None:
            values = json.loads(bbox) if isinstance(bbox, str) else bbox
            if not isinstance(values, list) or len(values) != 4 or any(type(v) not in (int, float) for v in values):
                raise ValueError('参考区域坐标无效')
            x, y, width, height = [int(v) for v in values]
            if x < 0 or y < 0 or width <= 0 or height <= 0 or x >= image.width or y >= image.height:
                raise ValueError('参考区域坐标越界')
            image = image.crop((x, y, min(x + width, image.width), min(y + height, image.height)))
        image.thumbnail((640, 640))
        output = io.BytesIO()
        image.save(output, 'JPEG', quality=88)
    return output.getvalue()
