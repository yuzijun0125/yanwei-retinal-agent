#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""眼维 Demo · 唯一入口：一条命令启动服务并打开浏览器。

    python run_demo.py                 # 起服务 + 自动开浏览器
    python run_demo.py --port 8080     # 换端口
    python run_demo.py --no-browser    # 只起服务（服务器上跑时用）

零第三方依赖。默认只绑 127.0.0.1（不会暴露到局域网）。
"""

import argparse
import os
import sys
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from yanwei import server as srv  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="启动眼维 Demo")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    httpd = srv.serve(args.host, args.port)
    url = "http://%s:%d" % (args.host, args.port)
    print("=" * 62)
    print("  眼维 · 眼底跨模态检索与证据复核  Demo-1")
    print("  服务地址：%s" % url)
    print("  停止：Ctrl+C")
    print("=" * 62)

    if not args.no_browser:
        def _open():
            time.sleep(0.6)
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=_open, daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
