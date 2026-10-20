#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""HTTP 服务冒烟测试：REST 五个端点 / MCP-over-HTTP / 静态页 / 错误兜底 / 反馈幂等。

    cd 02_代码
    python tests/smoke_http.py            # 全部跑一遍，退出码 0 = 全过

它会在本机固定端口 8765 起一个**临时**服务（不动你手动开的那个），跑完自动关掉。
自建的临时图片写到系统临时目录，不污染 04_产物/。

⚠️ 唯一会写进 04_产物/ 的是**反馈幂等**那一条：它会往 `04_产物/反馈/feedback.jsonl`
   追加一条 `case_id = REG_SMOKE_<时间戳>` 的测试记录（反馈本来就是只追加的数据，
   且每次跑用新 id，不影响判重）。介意的话跑完手动删掉那几行即可。
"""

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                   # 02_代码
sys.path.insert(0, ROOT)

from yanwei import pnglite  # noqa: E402
from yanwei import server as srv  # noqa: E402

PORT = 8765
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print("  %s %-34s %s" % ("✓" if ok else "✗", name, detail))
    return bool(ok)


def main() -> int:
    httpd = srv.serve("127.0.0.1", PORT)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.4)
    base = "http://127.0.0.1:%d" % PORT

    def get(path):
        with urllib.request.urlopen(base + path, timeout=20) as r:
            return r.status, r.read().decode("utf-8")

    def post(path, obj):
        data = json.dumps(obj).encode("utf-8")
        req = urllib.request.Request(base + path, data=data,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    def post_raw(path, body, ctype="application/json"):
        req = urllib.request.Request(base + path, data=body.encode("utf-8"),
                                     headers={"Content-Type": ctype}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8")

    print("=== 1. GET /api/health ===")
    s, b = get("/api/health")
    h = json.loads(b)
    check("health 返回 200", s == 200)
    check("工具数为 6", len(h["tools"]) == 6, "实际 %d" % len(h["tools"]))
    check("阈值版本可读", bool(h.get("threshold_ver")), h.get("threshold_ver"))

    print("=== 2. GET /api/samples ===")
    s, b = get("/api/samples")
    samples = json.loads(b)["samples"]
    check("样例 ≥4 个（覆盖四动作）", len(samples) >= 4, "%d 个" % len(samples))

    print("=== 3. GET /（单页界面）===")
    s, b = get("/")
    panels = all(k in b for k in ("①", "②", "③", "④", "⑤"))
    check("首页 200 且含五面板", s == 200 and panels, "%d 字节" % len(b.encode("utf-8")))

    print("=== 4. POST /api/run（样例逐个跑）===")
    for x in samples:
        s, r = post("/api/run", {"query_text": x["text"]})
        ok = r.get("action") == x.get("expect")
        check("样例 %s" % x["id"], ok, "动作=%s 期望=%s" % (r.get("action"), x.get("expect")))

    print("=== 5. POST /api/run（带图走质检门）===")
    tmp = tempfile.mkdtemp(prefix="yanwei_smoke_")
    img = os.path.join(tmp, "tiny.png")
    pnglite.synth_fundus(img, defect="tiny")
    s, r = post("/api/run", {"query_text": "右眼黄斑区散在微血管瘤", "image_path": img})
    qc = (r.get("qc") or {}).get("label")
    check("低分辨率图被质检拦下", qc == "RETAKE" and r.get("action") == "RETAKE_IMAGE",
          "qc=%s 动作=%s" % (qc, r.get("action")))

    print("=== 6. POST /mcp（MCP over HTTP）===")
    s, r = post("/mcp", {"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}})
    check("tools/list 返回 6 个工具", len(r["result"]["tools"]) == 6)
    s, r = post("/mcp", {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                         "params": {"name": "search_cases",
                                    "arguments": {"query_text": "右眼黄斑区微血管瘤", "topk": 3}}})
    sc = r["result"]["structuredContent"]
    check("tools/call search_cases", sc["ok"] and sc["count"] > 0,
          "count=%s top=%s" % (sc["count"], sc["candidates"][0]["region_id"]))
    s, b = post_raw("/mcp", "{ 这不是合法 JSON ")
    ok = json.loads(b).get("error", {}).get("code") == -32700
    check("非法 JSON → -32700", ok)

    print("=== 7. POST /api/feedback（幂等）===")
    # 反馈是**只追加**的落盘数据，每次冒烟必须换一个 case_id，否则第二次跑就成了"重复提交"
    smoke_case = "REG_SMOKE_%d" % int(time.time())
    s, r1 = post("/api/feedback", {"case_id": smoke_case, "verdict": "ACCEPT", "note": "冒烟"})
    s, r2 = post("/api/feedback", {"case_id": smoke_case, "verdict": "ACCEPT", "note": "冒烟"})
    check("首次入库 / 再次判重", r1.get("stored") and r2.get("duplicate"),
          "stored=%s duplicate=%s" % (r1.get("stored"), r2.get("duplicate")))

    print("=== 8. 错误兜底（服务不能崩）===")
    try:
        get("/api/nope")
        check("未知 GET 路径返回 4xx", False, "居然 200")
    except urllib.error.HTTPError as e:
        check("未知 GET 路径返回 4xx", 400 <= e.code < 500, "HTTP %s" % e.code)
    s, r = post("/api/tool", {"name": "不存在的工具", "arguments": {}})
    check("未知工具 → 结构化错误", r["ok"] is False and bool(r["error"]["code"]), r["error"]["code"])
    s, r = post("/api/tool", {"name": "extract_medical_entities", "arguments": {}})
    check("缺必填参数 → 结构化错误", r["ok"] is False, r["error"]["code"])
    s, b = get("/api/health")
    check("全部异常后服务仍存活", json.loads(b)["ok"] is True)

    httpd.shutdown()

    bad = [n for n, ok in RESULTS if not ok]
    print("-" * 72)
    if bad:
        print("冒烟未通过：%s" % "、".join(bad))
        return 1
    print("HTTP 冒烟全部通过 ✓（%d/%d）" % (len(RESULTS), len(RESULTS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
