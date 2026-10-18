#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""MCP 服务端入口（stdio）—— 给 Nexent / 任何 MCP 客户端接入用。

在 Nexent 里配置 MCP 时，命令就指向本文件：
    python <工程目录>/02_代码/mcp_server.py

⚠️ 本进程走 stdout 说话（MCP 协议），所以**不要**在这里加 print 调试；
   要看日志请看 stderr。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from yanwei import mcp_stdio  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(mcp_stdio.main())
