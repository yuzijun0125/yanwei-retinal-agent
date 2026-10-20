"""Same-origin gateway: UI -> real Nexent SSE -> MCP retrieval.

Never logs/stores cookies or provider keys. Missing tool results stay missing.
"""
import argparse
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import select
import socket
import time
from urllib.parse import urlsplit
import uuid
import queue
import threading

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / '02_代码/web'
NEXENT = os.environ.get('NEXENT_UPSTREAM', 'http://127.0.0.1:3000').rstrip('/')
RETRIEVAL = os.environ.get('RETRIEVAL_UPSTREAM', 'http://127.0.0.1:8235').rstrip('/')
STATE = ROOT / '04_产物/integration'
CONFIG = STATE / 'agent.json'
HOP = {'connection', 'transfer-encoding', 'keep-alive', 'proxy-authenticate',
       'proxy-authorization', 'te', 'trailers', 'upgrade'}
LOCAL = {'/api/chat', '/api/upload', '/api/run', '/api/tool', '/api/feedback',
         '/api/asset', '/api/case-image', '/api/evidence', '/api/trace', '/api/traces',
         '/api/health', '/api/multimodal', '/api/samples', '/mcp'}


def connection(base, timeout=180):
    u = urlsplit(base)
    cls = http.client.HTTPSConnection if u.scheme == 'https' else http.client.HTTPConnection
    return cls(u.hostname, u.port, timeout=timeout)


def upstream_json(base, path, method='GET', data=None, headers=None, timeout=90):
    conn = connection(base, timeout)
    try:
        body = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
        conn.request(method, path, body, {'Content-Type': 'application/json', **(headers or {})})
        r = conn.getresponse()
        raw = r.read()
        if r.status >= 400:
            # Do not relay exception traces or authentication material into deployment logs.
            raise ValueError(f'上游接口 {path.split("?")[0]} 返回 HTTP {r.status}；请检查登录及平台配置')
        return json.loads(raw)
    finally:
        conn.close()


def config():
    try:
        return json.loads(CONFIG.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass  # paths may contain signed URLs; no request/body/credential logging

    def json(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def file(self, path):
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8' if path.suffix == '.html' else 'application/javascript; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def do_PUT(self):
        self.dispatch()

    def do_DELETE(self):
        self.dispatch()

    def do_PATCH(self):
        self.dispatch()

    def do_OPTIONS(self):
        self.send_error(405)

    def dispatch(self):
        path = urlsplit(self.path).path
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 <= size <= 20 * 1024 * 1024:
                return self.json({'error': {'message': '请求过大'}}, 413)
            origin = self.headers.get('Origin')
            if self.command != 'GET' and origin and urlsplit(origin).netloc != self.headers.get('Host'):
                return self.json({'error': {'message': '请通过同源工作台提交请求'}}, 403)
            body = self.rfile.read(size) if size else b''
            if path in ('/', '/console') and self.command == 'GET':
                return self.file(WEB / 'nexent-console.html')
            if path.startswith('/static/') and self.command == 'GET':
                relative = path[len('/static/'):]
                target = (WEB / relative).resolve()
                if not target.is_relative_to(WEB.resolve()) or not target.is_file():
                    return self.send_error(404)
                return self.file(target)
            if path == '/hosting-admin' and self.command == 'GET':
                return self.file(Path(__file__).with_name('hosting-admin.html'))
            if path == '/api/integration/hosting' and self.command == 'POST':
                from hosting import enable
                return self.json(enable(self.auth_headers()))
            if path == '/integration' and self.command == 'GET':
                return self.file(Path(__file__).with_name('setup.html'))
            if path == '/api/integration/status' and self.command == 'GET':
                statuses = {}
                for name, base, route in [('retrieval', RETRIEVAL, '/api/health'), ('nexent', NEXENT, '/zh')]:
                    conn = connection(base, 3)
                    try:
                        conn.request('GET', route)
                        r = conn.getresponse()
                        r.read(1024)
                        statuses[name] = r.status < 500
                    except OSError:
                        statuses[name] = False
                    finally:
                        conn.close()
                return self.json({'ok': True, 'architecture': 'frontend -> Nexent -> MCP -> retrieval',
                                  'services': statuses, 'agent': config(), 'login_url': '/zh',
                                  'local_mode': 'explicit-offline-demo-only'})
            if path == '/api/integration/models' and self.command == 'GET':
                data = upstream_json(NEXENT, '/api/model/llm_list', headers=self.auth_headers())
                rows = data if isinstance(data, list) else data.get('data', data.get('models', []))
                if isinstance(rows, dict):
                    rows = rows.get('models', rows.get('list', []))
                # Explicit allowlist: model listing may contain provider secrets.
                return self.json({'models': [{k: row.get(k) for k in ('model_id', 'id', 'name', 'display_name', 'model_name')}
                                            for row in rows if isinstance(row, dict)]})
            if path == '/api/integration/bootstrap' and self.command == 'POST':
                from bootstrap import provision
                request = json.loads(body or b'{}')
                if not isinstance(request, dict):
                    raise ValueError('初始化参数必须为 JSON 对象')
                model = request.get('model_id')
                if type(model) is not int or model < 1:
                    raise ValueError('请先在 Nexent 配置语言模型，再选择模型 ID')
                result = provision(model, self.auth_headers())
                return self.json({'ok': True, 'agent': result})
            if path == '/api/agent/run' and self.command == 'POST':
                return self.agent_run(body)
            target = RETRIEVAL if path in LOCAL else NEXENT
            return self.proxy(target, body)
        except (ValueError, OSError, http.client.HTTPException) as exc:
            self.json({'ok': False, 'error': {'message': str(exc) if isinstance(exc, ValueError)
                       else '依赖服务暂不可用；请在首次设置中检查 Nexent、检索服务及登录状态'}}, 502)

    def auth_headers(self):
        result = {}
        for name in ('Cookie', 'Authorization', 'Origin'):
            if self.headers.get(name):
                result[name] = self.headers[name]
        result['Host'] = self.headers.get('Host', 'localhost')
        result['X-Forwarded-Host'] = result['Host']
        result['X-Forwarded-Proto'] = 'http'
        return result

    def proxy(self, base, body, override=None, enrich=None):
        conn = connection(base)
        sent = False
        try:
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP | {'content-length', 'accept-encoding'}}
            headers.update(self.auth_headers())
            headers['Content-Length'] = str(len(body))
            headers['Accept-Encoding'] = 'identity'
            conn.request(self.command, override or self.path, body or None, headers)
            response = conn.getresponse()
            if self.handle_proxy_error(response.status):
                return
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() in HOP | {'content-length'}:
                    continue
                if key.lower() == 'location' and value.startswith(base):
                    value = value[len(base):] or '/'
                self.send_header(key, value)
            self.send_header('Connection', 'close')
            self.send_header('X-Accel-Buffering', 'no')
            self.end_headers()
            sent = True
            self.close_connection = True
            if enrich and response.status == 200 and 'text/event-stream' in response.getheader('Content-Type', ''):
                return self.stream_agent(conn, response, enrich)
            while True:
                block = response.read1(64 * 1024)
                if not block:
                    break
                self.wfile.write(block)
                self.wfile.flush()
            if enrich and response.status == 200 and 'text/event-stream' in response.getheader('Content-Type', ''):
                try:
                    result = upstream_json(RETRIEVAL, '/api/integration/result?id=' + enrich)
                    event = {'type': 'yanwei_result', 'request_id': enrich, 'result': result,
                             'provenance': 'actual-mcp-tool-result'}
                except (OSError, ValueError, http.client.HTTPException):
                    event = {'type': 'yanwei_status', 'request_id': enrich,
                             'message': '未收到本轮可核验的结构化工具结果；不据模型文字生成临床决策面板'}
                self.wfile.write(('\n\ndata: ' + json.dumps(event, ensure_ascii=False) + '\n\n').encode())
                self.wfile.flush()
        except (OSError, http.client.HTTPException):
            if not sent:
                raise
            # Client cancellation or broken stream: never append a second HTTP response.
            self.close_connection = True
        finally:
            conn.close()

    def stream_agent(self, conn, response, request_id):
        """Bound wall time even if upstream keeps sending heartbeat events."""
        chunks = queue.Queue(maxsize=64)
        stopped = threading.Event()
        def pump():
            try:
                while not stopped.is_set():
                    block = response.read1(65536)
                    while not stopped.is_set():
                        try:
                            chunks.put(block, timeout=0.5)
                            break
                        except queue.Full:
                            pass
                    if not block:
                        return
            except (OSError, http.client.HTTPException):
                if not stopped.is_set():
                    try: chunks.put_nowait(b'')
                    except queue.Full: pass
        threading.Thread(target=pump, daemon=True).start()
        start = last_poll = time.monotonic()
        tool_at = None
        pending = b''
        ended = False
        run_id = response.getheader('conversation_id', '')
        def event(value):
            self.wfile.write(('data: ' + json.dumps(value, ensure_ascii=False) + '\n\n').encode())
            self.wfile.flush()
        try:
            while True:
                now = time.monotonic()
                if now - start > 150 or (tool_at and now - tool_at > 35):
                    event({'type':'yanwei_status','message':'模型回答等待超时，已请求停止本轮；已返回的工具结果可继续查看。'})
                    break
                try:
                    block = chunks.get(timeout=0.5)
                    if block == b'':
                        ended = True
                    else:
                        pending += block.replace(b'\r\n', b'\n')
                        while b'\n\n' in pending:
                            packet, pending = pending.split(b'\n\n', 1)
                            self.wfile.write(self.transform_packet(packet))
                            self.wfile.flush()
                except queue.Empty:
                    pass
                if not tool_at and (ended or now - last_poll >= 1):
                    last_poll = now
                    try:
                        result = upstream_json(RETRIEVAL, '/api/integration/result?id='+request_id, timeout=3)
                    except (OSError, ValueError, http.client.HTTPException):
                        pass
                    else:
                        try:
                            self.accept_result(result)
                        except ValueError:
                            event({'type':'error','content':'工具结果未通过本轮访问校验，请新建对话重试'})
                            break
                        event({'type':'yanwei_document' if result.get('type')=='medical_record' else 'yanwei_result','request_id':request_id,'result':result,'provenance':'actual-mcp-tool-result'})
                        tool_at = time.monotonic()
                if ended:
                    self.finish_stream(bool(tool_at))
                    if not tool_at:
                        event({'type':'yanwei_status','message':'未收到本轮可核验的结构化工具结果；请检查模型工具调用配置。'})
                    break
                # Detect a disconnected browser even while model output is idle.
                self.wfile.write(b': gateway-alive\n\n')
                self.wfile.flush()
        finally:
            stopped.set()
            if not ended and str(run_id).isdigit():
                try:
                    self.stop_agent(run_id)
                except (OSError, ValueError, http.client.HTTPException):
                    pass
            if conn.sock:
                try: conn.sock.shutdown(socket.SHUT_RDWR)
                except OSError: pass

    def handle_proxy_error(self, status):
        return False

    def transform_packet(self, packet):
        return packet + b'\n\n'

    def accept_result(self, result):
        pass

    def finish_stream(self, result_ready):
        pass

    def stop_agent(self, run_id):
        upstream_json(NEXENT, '/api/agent/stop/'+str(run_id), headers=self.auth_headers(), timeout=5)

    def agent_run(self, raw):
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError('查询参数必须为 JSON 对象')
        cfg = config()
        if not cfg.get('agent_id'):
            return self.json({'error': {'message': '尚未初始化眼维智能体，请先打开 /integration 完成设置'}}, 409)
        request_id = uuid.uuid4().hex
        text = payload.get('query', '')
        if not isinstance(text, str) or len(text) > 10000:
            raise ValueError('查询文本无效或过长')
        asset = payload.pop('yanwei_asset_id', None)
        route = {'request_id': request_id, 'image_asset_id': None}
        if asset:
            if not isinstance(asset, str) or not re.fullmatch(r'[a-f0-9]{32}', asset):
                raise ValueError('图片资产 ID 无效')
            upstream_json(RETRIEVAL, '/api/integration/asset-check?id=' + asset)
            route['image_asset_id'] = asset
        payload.update({'agent_id': cfg['agent_id'], 'model_id': cfg['model_id'],
                        'version_no': cfg['version_no'], 'is_debug': False,
                        'enable_plan': False, 'enable_automation_tool': False})
        payload.pop('minio_files', None)
        payload['query'] = text + '\n\n[YANWEI_ROUTE] ' + json.dumps(route, ensure_ascii=False) + \
            '\n以上为本轮路由参数。调用 search_cases 时逐字传入 request_id 和非空 image_asset_id；query_text 保留用户描述及对话补充。'
        return self.proxy(NEXENT, json.dumps(payload, ensure_ascii=False).encode(), enrich=request_id)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=18880)
    args = ap.parse_args()
    STATE.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f'眼维一体化入口：http://localhost:{args.port}/console', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
