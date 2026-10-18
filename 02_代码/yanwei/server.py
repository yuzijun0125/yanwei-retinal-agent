"""眼维 · 零依赖本地 HTTP 服务。

对外接口：
  GET  /                    单页主界面（web/index.html）
  GET  /api/health          健康检查 + 版本三元组
  GET  /api/samples         四个动作的演示样例
  GET  /api/trace           回放一条留痕（?trace_id=…）
  GET  /api/traces          最近留痕列表
  POST /api/run             跑一次完整决策（编排器）
  POST /api/tool            直接调某个工具（调试 / Nexent 之外的客户端）
  POST /api/feedback        写反馈（幂等）
  POST /mcp                 MCP JSON-RPC over HTTP（给支持 HTTP 的 MCP 客户端）

设计取舍：
  · 只用标准库 http.server，**不引任何框架**——演示机不装环境也能跑；
  · 默认只绑 127.0.0.1，不做对外监听（避免把本地数据服务暴露到网上）；
  · 所有异常都转成 JSON 错误返回，**服务端不因一次坏请求而中断**。
"""

from __future__ import annotations

import json
import os
import posixpath
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import mcp_stdio
from . import mcp_tools as MT
from . import pipeline as pipe
from . import evidence_store, assets, dialog, multimodal

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


class _BadJSON(Exception):
    """请求体不是合法 JSON。单独一个类型，好让各端点分头报错。"""

# 四个动作的一键演示样例（界面上直接点，不用手打）
SAMPLES = [
    {"id": "return_cases", "label": "① 完整描述 → 返回可比先例",
     "text": "眼底病灶区域：微血管瘤；眼别：右眼；位置：黄斑区；程度草案：散在。",
     "expect": "RETURN_CASES"},
    {"id": "ask_more", "label": "② 只给病灶 → 信息不足，追问",
     "text": "眼底出血", "expect": "ASK_MORE"},
    {"id": "abstain", "label": "③ 模态不可比 → 拒答",
     "text": "超广角眼底图像，右眼黄斑区微血管瘤", "expect": "ABSTAIN"},
    {"id": "referral", "label": "④ 高风险征象 → 建议转诊",
     "text": "右眼黄斑区较重硬性渗出", "expect": "SUGGEST_REFERRAL"},
]

_PL = None
_LOCK = None


def _pipeline() -> pipe.Pipeline:
    """懒加载：首次请求时才装载数据与索引，启动瞬间不卡。"""
    global _PL, _LOCK
    if _PL is None:
        import threading
        _LOCK = _LOCK or threading.Lock()
        with _LOCK:
            if _PL is None:
                _PL = pipe.Pipeline()
    return _PL


class Handler(BaseHTTPRequestHandler):
    server_version = "YanweiDemo/1.0"
    protocol_version = "HTTP/1.1"

    # ---------------- 基础工具
    def log_message(self, fmt, *args):  # noqa: A003
        # 默认日志会刷屏；压成一行简短格式，且只走 stderr
        import sys
        sys.stderr.write("[http] %s\n" % (fmt % args))

    def _send(self, body: bytes, ctype: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200):
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", code)

    def _read_body(self) -> dict:
        """读请求体。空体 → {}；**不是合法 JSON → 抛 _BadJSON**（由调用方决定怎么报）。

        以前这里把解析失败也吞成 {}，结果 MCP 端点收到坏 JSON 会被当成"空请求"处理，
        报的错风马牛不相及。现在区分开：坏 JSON 归坏 JSON。
        """
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        if n > 12 * 1024 * 1024:
            self.close_connection = True
            raise _BadJSON("请求超过12MB限制")
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            raise _BadJSON(raw[:200])
        return data if isinstance(data, dict) else {"value": data}

    # ---------------- MCP Streamable HTTP（给 Nexent 这类用 fastmcp 客户端的调用方）
    def _handle_mcp_streamable(self):
        """MCP Streamable HTTP 传输（2025-03-26 spec），**无状态**实现。

        为什么单独写：fastmcp 的 StreamableHttpTransport 客户端握手要求——
          · POST 单条 JSON-RPC，响应一条 JSON（`Content-Type: application/json`）
          · POST 通知（无 id）→ HTTP 202，无 body
          · POST 批处理（数组）→ 响应数组（只含请求的响应，通知不回）
          · 非法 JSON → `-32700`
        我们**不**开 GET 长连接（不推服务端消息），GET /mcp 回 405。
        不下发 `Mcp-Session-Id` = 声明无状态，客户端不会在后续请求带它，也不开 GET 流。
        """
        host, port = self.server.server_address[:2]
        evidence_base_url = "http://%s:%s" % (host, port)
        try:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            raw = self.rfile.read(n) if n > 0 else b""

            if raw.strip() == b"":
                # 空体：按 JSON-RPC 当无效请求
                return self._mcp_respond_single(
                    {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32600, "message": "Invalid Request",
                               "data": "请求体为空"}})

            try:
                obj = json.loads(raw.decode("utf-8"))
            except Exception:  # noqa: BLE001
                return self._mcp_respond_single(
                    {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32700, "message": "Parse error"}})

            # 批处理
            if isinstance(obj, list):
                responses = []
                for item in obj:
                    if not isinstance(item, dict) or "id" not in item:
                        # 通知：执行但不回响应（此处仅记录，不真正触发副作用）
                        continue
                    resp = mcp_stdio.handle(item, evidence_base_url=evidence_base_url)
                    if resp is not None:
                        responses.append(resp)
                if responses:
                    return self._mcp_respond_single(responses)
                # 全是通知 → 202
                return self._send(b"", "application/json", 202)

            if not isinstance(obj, dict):
                return self._mcp_respond_single(
                    {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32600, "message": "Invalid Request"}})

            # 通知（无 id）：执行 + 202
            if "id" not in obj:
                mcp_stdio.handle(obj, evidence_base_url=evidence_base_url)
                return self._send(b"", "application/json", 202)

            resp = mcp_stdio.handle(obj, evidence_base_url=evidence_base_url)
            if resp is None:
                # 理论上请求必有响应；保险起见回一个 accepted
                return self._send(b"", "application/json", 202)
            return self._mcp_respond_single(resp)
        except Exception as exc:  # noqa: BLE001
            return self._mcp_respond_single(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32603, "message": "Internal error",
                           "data": str(exc)[:300]}})

    def _mcp_respond_single(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        return self._send(body, "application/json", 200)

    # ---------------- GET
    def do_GET(self):  # noqa: N802
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        path = u.path
        qs = parse_qs(u.query)
        try:
            if path in ("/", "/index.html"):
                return self._serve_file(os.path.join(WEB_DIR, "index.html"))
            if path == "/chat":
                return self._serve_file(os.path.join(WEB_DIR, "chat.html"))
            if path == "/api/asset":
                try:
                    asset = assets.get((qs.get("id") or [""])[0])
                except ValueError as exc:
                    return self._json({"ok": False, "error": {"code": "NOT_FOUND", "message": str(exc)}}, 404)
                preview = asset["analysis_path"]
                with open(preview, "rb") as f:
                    return self._send(f.read(), "image/jpeg")
            if path == "/mcp":
                # Streamable HTTP 的 GET（服务端推送流）我们不支持——无状态实现，
                # 客户端不会依赖它。回 405 告知能力外。
                return self._json({"jsonrpc": "2.0", "id": None,
                                   "error": {"code": -32000,
                                             "message": "GET /mcp not supported (stateless server)"}},
                                  405)
            if path.startswith("/static/"):
                rel = posixpath.normpath(path[len("/static/"):]).lstrip("/")
                return self._serve_file(os.path.join(WEB_DIR, rel))
            if path == "/api/health":
                cfg = MT.Runtime.get().cfg
                return self._json({"ok": True, "service": "yanwei-demo",
                                   "threshold_ver": cfg.threshold_ver, "rule_ver": cfg.rule_ver,
                                   "model_ver": cfg.model_ver, "fingerprint": cfg.fingerprint,
                                   "tools": list(MT.TOOLS)})
            if path == '/api/multimodal':
                return self._json(multimodal.status())
            if path == '/api/case-image':
                rt = MT.Runtime.get()
                record = rt.ds.images.get((qs.get('id') or [''])[0])
                if not record:
                    return self._json({'ok': False, 'error': {'code': 'NOT_FOUND'}}, 404)
                try:
                    region_id = (qs.get('region_id') or [''])[0]
                    region = rt.ds.region(region_id) if region_id else None
                    if region_id and (region is None or region.image_id != record['image_id']):
                        return self._json({'ok': False, 'error': {'code': 'REGION_NOT_FOUND'}}, 404)
                    return self._send(multimodal.thumbnail(rt.data_dir, record, region.bbox if region else None), 'image/jpeg')
                except (OSError, ValueError):
                    return self._json({'ok': False, 'error': {'code': 'IMAGE_UNAVAILABLE'}}, 404)
            if path == "/api/samples":
                return self._json({"ok": True, "samples": SAMPLES})
            if path == "/api/evidence":
                evidence_id = (qs.get("id") or [""])[0]
                card = evidence_store.read(evidence_id)
                if card is None:
                    return self._json({"ok": False, "error": {"code": "NOT_FOUND"}}, 404)
                if (qs.get("format") or ["html"])[0] == "json":
                    return self._json(card)
                return self._send(card.get("html", "").encode("utf8"), "text/html; charset=utf-8")
            if path == "/api/traces":
                return self._json({"ok": True, "traces": self._list_traces()})
            if path == "/api/trace":
                tid = (qs.get("trace_id") or [""])[0]
                return self._json(self._read_trace(tid))
            return self._json({"ok": False, "error": {"code": "NOT_FOUND",
                                                      "message": "无此路径：%s" % path}}, 404)
        except Exception as exc:  # noqa: BLE001
            return self._json({"ok": False, "error": {"code": "INTERNAL", "message": str(exc),
                                                      "traceback": traceback.format_exc()[-600:]}}, 500)

    # ---------------- POST
    def do_POST(self):  # noqa: N802
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        # /mcp 走 MCP Streamable HTTP 传输（见 _handle_mcp_streamable），自己读 body，
        # 不能走下面的 _read_body（那会把通知/批处理嚼碎）
        if path == "/mcp":
            return self._handle_mcp_streamable()
        try:
            try:
                body = self._read_body()
            except _BadJSON as bad:
                return self._json({"ok": False, "error": {"code": "BAD_JSON",
                                                          "message": "请求体不是合法 JSON：%s" % bad}}, 400)
            if path in ("/api/chat", "/api/upload"):
                try:
                    if path == "/api/chat":
                        return self._json(dialog.respond(body))
                    asset = assets.upload(body.get("filename", "image"), body.get("data", ""))
                    public = {k: v for k, v in asset.items() if k not in ("analysis_path", "original_path")}
                    return self._json({"ok": True, "asset": public})
                except ValueError as exc:
                    return self._json({"ok": False, "error": {"code": "BAD_INPUT", "message": str(exc)}}, 400)
            if path == "/api/run":
                r = _pipeline().run(
                    body.get("query_text") or "",
                    image_path=body.get("image_path") or None,
                    topk=body.get("topk"),
                    mode=body.get("mode") or "standard",
                    clarify_round=int(body.get("clarify_round") or 0),
                    primary_care=bool(body.get("primary_care", True)),
                )
                return self._json(r)
            if path == "/api/tool":
                name = body.get("name")
                args = body.get("arguments") or {}
                return self._json(MT.call_tool(name, args))
            if path == "/api/feedback":
                return self._json(MT.record_feedback(body))
            return self._json({"ok": False, "error": {"code": "NOT_FOUND",
                                                      "message": "无此路径：%s" % path}}, 404)
        except Exception as exc:  # noqa: BLE001
            return self._json({"ok": False, "error": {"code": "INTERNAL", "message": str(exc),
                                                      "traceback": traceback.format_exc()[-600:]}}, 500)

    # ---------------- 静态与留痕
    def _serve_file(self, abspath: str):
        abspath = os.path.abspath(abspath)
        if os.path.commonpath([abspath, os.path.abspath(WEB_DIR)]) != os.path.abspath(WEB_DIR) or not os.path.isfile(abspath):
            return self._json({"ok": False, "error": {"code": "NOT_FOUND",
                                                      "message": "文件不存在"}}, 404)
        ctype = "text/html; charset=utf-8" if abspath.endswith(".html") else \
            ("application/javascript" if abspath.endswith(".js") else
             ("text/css" if abspath.endswith(".css") else "application/octet-stream"))
        with open(abspath, "rb") as f:
            self._send(f.read(), ctype)

    def _trace_dir(self) -> str:
        return _pipeline().trace_dir

    def _list_traces(self) -> list:
        d = self._trace_dir()
        if not os.path.isdir(d):
            return []
        items = []
        for fn in sorted(os.listdir(d), reverse=True)[:30]:
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    meta = json.load(f).get("meta", {})
                items.append({k: meta.get(k) for k in
                              ("trace_id", "title", "outcome", "steps", "wall_ms", "started_at")})
            except Exception:  # noqa: BLE001
                continue
        return items

    def _read_trace(self, trace_id: str) -> dict:
        if not trace_id or "/" in trace_id or "\\" in trace_id:
            return {"ok": False, "error": {"code": "BAD_INPUT", "message": "非法 trace_id"}}
        p = os.path.join(self._trace_dir(), trace_id + ".json")
        if not os.path.isfile(p):
            return {"ok": False, "error": {"code": "NOT_FOUND", "message": "找不到该留痕"}}
        with open(p, encoding="utf-8") as f:
            return {"ok": True, "trace": json.load(f)}


def serve(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    if multimodal.status().get('ready'):
        try:
            multimodal.encoder()
        except Exception as exc:
            import sys
            sys.stderr.write('[multimodal] prewarm failed: %s\n' % type(exc).__name__)
    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd


def main(host: str = "127.0.0.1", port: int = 8000):
    httpd = serve(host, port)
    print("眼维 Demo 服务已启动： http://%s:%d" % (host, port))
    print("（Ctrl+C 停止）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
