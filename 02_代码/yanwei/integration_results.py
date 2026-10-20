"""Correlate actual MCP results with one gateway request; never infer from LLM prose."""
import json
from pathlib import Path
import re
import time
from .config import default_output_dir


def valid(value):
    return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{32}', value) is not None


def save(request_id, result):
    if not valid(request_id):
        return
    root = Path(default_output_dir()) / 'integration_results'
    root.mkdir(parents=True, exist_ok=True)
    target = root / (request_id + '.json')
    temp = root / (request_id + '.tmp')
    temp.write_text(json.dumps({'at': time.time(), 'result': result}, ensure_ascii=False), encoding='utf-8')
    temp.replace(target)
    for item in root.glob('*.json'):
        if time.time() - item.stat().st_mtime > 24 * 3600:
            item.unlink(missing_ok=True)


def read(request_id):
    if not valid(request_id):
        raise ValueError('本轮请求 ID 无效')
    path = Path(default_output_dir()) / 'integration_results' / (request_id + '.json')
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
        if time.time() - record['at'] > 3600:
            raise ValueError('本轮工具结果已过期')
        return record['result']
    except (OSError, KeyError) as exc:
        raise ValueError('本轮尚无实际 MCP 工具结果') from exc
