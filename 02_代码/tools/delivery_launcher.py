"""Portable Windows delivery entry: relative paths, preflight, browser and self-test."""
import argparse
import base64
import json
from logging.handlers import RotatingFileHandler
import logging
import msvcrt
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

CODE = Path(__file__).resolve().parents[1]
ROOT = CODE.parent
STATE = ROOT / '04_产物' / 'delivery'
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(base, path, data=None):
    req = urllib.request.Request(base + path,
        json.dumps(data, ensure_ascii=False).encode('utf-8') if data is not None else None,
        {'Content-Type': 'application/json'})
    with OPENER.open(req, timeout=20) as response:
        return json.load(response)


def stop(child):
    if child and child.poll() is None:
        subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, check=False)
        child.wait(timeout=15)


def self_test(base):
    checks = []
    def check(name, passed, detail=None):
        checks.append({'name': name, 'passed': bool(passed), 'detail': detail})
        print(('PASS ' if passed else 'FAIL ') + name, flush=True)
    model = request(base, '/api/multimodal')
    check('model_and_81_image_index', model.get('model_loaded') and model.get('image_count') == 81, model)
    with OPENER.open(base + '/console', timeout=20) as frontend_response:
        frontend_html = frontend_response.read().decode('utf-8', errors='replace')
    check('frontend_console_available', '眼维临床决策智能体' in frontend_html)
    normal = request(base, '/api/tool', {'name': 'search_cases', 'arguments': {
        'query_text': '右眼黄斑区散在微血管瘤', 'complete_flow': True}})
    check('text_returns_real_cases', normal.get('action') == 'RETURN_CASES' and len(normal.get('candidates', [])) > 0,
          {'action': normal.get('action'), 'count': len(normal.get('candidates', []))})
    first = request(base, '/api/chat', {'text': '左眼和右眼黄斑区散在出血'})
    r = first.get('result', {})
    check('P02_both_eyes_asks_without_candidates', r.get('action') == 'ASK_MORE' and not r.get('candidates')
          and r.get('query', {}).get('codes', {}).get('eye') is None, first.get('reply'))
    second = request(base, '/api/chat', {'text': '只查右眼', 'session_id': first['session_id']})
    r = second.get('result', {})
    check('P02_selection_retains_lesion_location', r.get('action') == 'RETURN_CASES' and
          r.get('query', {}).get('codes', {}).get('eye') == 'OD', r.get('query', {}).get('codes'))
    corrected = request(base, '/api/tool', {'name': 'search_cases', 'arguments': {
        'query_text': '不是左眼，是右眼黄斑区散在出血', 'complete_flow': True}})
    check('P02_explicit_correction', corrected.get('query', {}).get('codes', {}).get('eye') == 'OD'
          and not corrected.get('query', {}).get('input_conflicts'))
    image = ROOT / '01_数据集/图像/IDRiD/原始图像/Testing set/IDRiD_55.jpg'
    upload = request(base, '/api/upload', {'filename': image.name,
        'data': base64.b64encode(image.read_bytes()).decode('ascii')})
    asset = upload['asset']['asset_id']
    picture = request(base, '/api/chat', {'text': '右眼黄斑区散在微血管瘤', 'asset_id': asset})
    r = picture.get('result', {})
    check('real_image_vector_search', r.get('multimodal', {}).get('used') and
          r.get('multimodal', {}).get('query_kind') == 'image_text' and bool(r.get('candidates')),
          r.get('multimodal'))
    evidence = r.get('evidence') or {}
    evidence_ok = False
    if evidence.get('evidence_id'):
        card = request(base, '/api/evidence?id=' + evidence['evidence_id'] + '&format=json')
        evidence_ok = bool(card) and 'error' not in card
    check('evidence_card_readable', evidence_ok)
    report = {'at': time.strftime('%Y-%m-%d %H:%M:%S'), 'root': str(ROOT), 'checks': checks,
              'passed': all(c['passed'] for c in checks)}
    (STATE / 'self-test.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if report['passed'] else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--self-test', action='store_true')
    ap.add_argument('--no-browser', action='store_true')
    ap.add_argument('--port', type=int, default=8765)
    args = ap.parse_args()
    STATE.mkdir(parents=True, exist_ok=True)
    lock = open(STATE / 'launcher.lock', 'a+b')
    lock.seek(0)
    if not lock.read(1):
        lock.write(b'0')
        lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print('本目录已启动，请使用原窗口。', flush=True)
        if not args.no_browser and not args.self_test:
            try:
                old = json.loads((STATE / 'status.json').read_text(encoding='utf-8'))
                webbrowser.open(old['url'] + '/console')
            except (OSError, ValueError, KeyError):
                pass
        return 0
    child = None
    try:
        required = [CODE / 'run_demo.py', CODE / 'models/manifest.json',
                    ROOT / '04_产物/multimodal/index.npz', ROOT / '01_数据集/silver/silver_case.csv']
        for path in required:
            if not path.is_file():
                raise RuntimeError('交付文件缺失，请完整解压后运行：' + str(path))
        port = None
        for candidate in range(args.port, args.port + 20):
            try:
                with socket.socket() as probe:
                    probe.bind(('127.0.0.1', candidate))
                port = candidate
                break
            except OSError:
                continue
        if port is None:
            raise RuntimeError('没有可用的本地端口，请关闭旧演示或更换 --port')
        base = f'http://127.0.0.1:{port}'
        env = dict(os.environ, PYTHONUTF8='1', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                   PYTHONUNBUFFERED='1', HF_HOME=str(STATE / 'hf-cache'))
        for key in ('PYTHONHOME', 'PYTHONPATH'):
            env.pop(key, None)
        log = logging.getLogger('delivery')
        log.setLevel(logging.INFO)
        handler = RotatingFileHandler(STATE / 'service.log', maxBytes=5_000_000, backupCount=2, encoding='utf-8')
        log.addHandler(handler)
        def collect(stream):
            for line in stream:
                log.info(line.rstrip())
            stream.close()
        for attempt in range(1, 4):
            print('正在加载本地模型（无需联网），请保留此窗口…', flush=True)
            start = time.monotonic()
            child = subprocess.Popen([sys.executable, '-X', 'utf8', '-I', str(CODE / 'run_demo.py'), '--host',
                '127.0.0.1', '--port', str(port), '--no-browser'], cwd=CODE, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                creationflags=subprocess.CREATE_NO_WINDOW)
            threading.Thread(target=collect, args=(child.stdout,), daemon=True).start()
            while time.monotonic() - start < 180:
                if child.poll() is not None:
                    raise RuntimeError('服务启动失败，请查看 04_产物/delivery/service.log')
                try:
                    health = request(base, '/api/health')
                    if health.get('service') == 'yanwei-demo' and health.get('ok'):
                        break
                except (OSError, ValueError):
                    time.sleep(1)
            else:
                raise RuntimeError('模型加载超过 180 秒，请查看启动日志及可用内存')
            (STATE / 'status.json').write_text(json.dumps({'url': base, 'pid': child.pid,
                'root': str(ROOT), 'startup_seconds': round(time.monotonic() - start, 2)}, ensure_ascii=False), encoding='utf-8')
            print('已就绪：' + base + '/console\n停止演示请按 Ctrl+C。', flush=True)
            if args.self_test:
                return self_test(base)
            if not args.no_browser:
                webbrowser.open(base + '/console')
            child.wait()
            print(f'服务退出，正在恢复（{attempt}/3）…', flush=True)
            time.sleep(5)
        raise RuntimeError('服务连续退出，请查看日志，停止自动重试。')
    except KeyboardInterrupt:
        print('\n已停止演示。', flush=True)
        return 0
    except Exception as exc:
        print('启动或自检失败：' + str(exc), flush=True)
        return 1
    finally:
        stop(child)
        lock.close()


if __name__ == '__main__':
    raise SystemExit(main())
