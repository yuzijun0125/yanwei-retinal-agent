"""眼维 · MCP 服务端（stdio 传输）—— 供 Nexent / 任何 MCP 客户端接入。

协议：JSON-RPC 2.0，消息以**换行分隔的 JSON** 传输（MCP stdio transport）。

⚠️ 两条铁律（否则接入必失败）：
  1. **stdout 只能输出协议消息**。任何日志、警告、调试信息一律走 stderr——
     往 stdout 多打一行，客户端就解析崩了。
  2. **不要在协议消息外夹带 BOM / 多余空行**。每行一条完整 JSON。

暴露的工具 = `mcp_tools.TOOLS` 里的 5 个，一个不多一个不少（数量锁死）。
"""

from __future__ import annotations

import json
import sys
import traceback

from . import mcp_tools as MT
from . import evidence_store

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "yanwei-fundus-retrieval", "version": "1.0.0"}

# 工具描述写细一点：MCP 客户端（含 Nexent）靠这段文字决定"该调哪个工具"，
# 描述含糊 = 选错工具 = 交互不准。
_AUTO = "（自动带上版本三元组与耗时；阈值全部读 config/thresholds.yaml，不硬编码）"

TOOL_SCHEMAS = [
    {
        "name": "extract_medical_entities",
        "description": "把自由文本解析成眼科五维结构化实体（眼别/病灶/位置/程度/模态），"
                       "并识别否定项（未见、无）与不确定项（疑似、考虑）。"
                       "**任何检索之前都先调它**，它是全案唯一的结构真源。" + _AUTO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "病灶描述文本，例如「右眼黄斑区散在微血管瘤」"}
            },
            "required": ["text"],
        },
    },
    {
        "name": "search_cases",
        "description": "按文字与结构化元数据检索可比先例：BM25 稀疏检索 + 五维一致性线性融合，返回候选病灶区域"
                       "（含总分拆解、五维画像、可溯源字段）。可选先做图像质检。"
                       "启用FLAIR索引时支持整图语义召回，仍不构成病灶诊断。"
                       "mode=standard 常规 / region 位置优先 / negative 找反例。" + _AUTO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["standard", "region", "negative"],
                         "description": "检索模式，默认 standard"},
                "query_text": {"type": "string", "description": "病灶描述文本"},
                "modality": {"type": "string", "description": "可选，覆盖文本解析出的模态（如 CFP/UWF）"},
                "topk": {"type": "integer", "minimum": 1, "maximum": 30, "description": "返回候选数，默认读配置"},
                "complete_flow": {"type": "boolean", "default": False, "description": "设为true时一次完成解析、检索、门禁、决策和证据；短输入自动追问。推荐智能体使用。"},
                "clarify_round": {"type": "integer", "minimum": 0, "description": "当前已经追问的轮次，达到上限转人工"},
                "image_path": {"type": "string", "description": "可选，眼底图像路径（带图则先质检）"},
                "image_asset_id": {"type": "string", "description": "Nexent附件桥接提供的真实图片资产ID，不能填写文件名或猜测ID。"},
                "filters": {"type": "object", "description": "可选硬过滤，如 {\"eye\":\"OD\"}"},
            },
            "required": ["query_text"],
        },
    },
    {
        "name": "verify_medical_logic",
        "description": "五维硬门禁：逐维判定一致/不符，把冲突**定位到具体维度**，算出证据充分性分数。"
                       "眼别或模态不符属硬冲突（不可比）。" + _AUTO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "object", "description": "extract_medical_entities 的输出"},
                "candidates": {"type": "object", "description": "search_cases完整返回对象，直接传结构化对象，不要转成字符串",
                               "properties": {"candidates": {"type": "array", "items": {"type": "object"}}},
                               "required": ["candidates"]},
            },
            "required": ["query", "candidates"],
        },
    },
    {
        "name": "build_evidence_chain",
        "description": "生成证据卡：算出**唯一一个**决策动作（RETURN_CASES / ASK_MORE / ABSTAIN / "
                       "SUGGEST_REFERRAL），给出依据候选、命中规则、版本溯源与免责声明，"
                       "默认返回精简JSON与证据查看地址，HTML另存，不要把HTML复制进回答。" + _AUTO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "candidates": {"type": "object", "description": "search_cases完整返回对象，直接传结构化对象，不要转成字符串",
                               "properties": {"candidates": {"type": "array", "items": {"type": "object"}}},
                               "required": ["candidates"]},
                "logic_result": {"type": "object", "description": "verify_medical_logic 的输出"},
                "query": {"type": "object", "description": "extract_medical_entities 的输出（用于判定动作）"},
                "trace_id": {"type": "string", "description": "可选，编排轨迹 ID"},
                "primary_care": {"type": "boolean", "description": "是否基层场景，默认 true"},
                "include_html": {"type": "boolean", "default": False,
                                 "description": "默认false：HTML保存在证据查看器，不传入模型上下文"},
            },
            "required": ["candidates", "logic_result"],
        },
    },
    {
        "name": "record_feedback",
        "description": "把医生对某条结果的反馈写入隔离区（只追加不覆盖），幂等键防重复提交。" + _AUTO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "case_id": {"type": "string", "description": "病例/区域 ID"},
                "verdict": {"type": "string", "enum": ["ACCEPT", "REJECT", "CORRECT", "UNKNOWN"]},
                "note": {"type": "string", "description": "可选备注"},
                "idempotency_key": {"type": "string", "description": "可选幂等键；缺省由内容派生"},
            },
            "required": ["case_id", "verdict"],
        },
    },
]


def _log(msg: str):
    """日志一律走 stderr —— 绝不能让 stdout 沾上非协议内容。"""
    sys.stderr.write("[yanwei-mcp] %s\n" % msg)
    sys.stderr.flush()


def _write(obj: dict):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _ok(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code, message, data=None):
    e = {"code": code, "message": message}
    if data:
        e["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": e}


def _tool_result(res: dict) -> dict:
    """把工具返回包成 MCP 的 content 结构（同时给 text 与 structuredContent）。"""
    is_error = not res.get("ok", True)
    return {
        "content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}],
        "structuredContent": res,
        "isError": is_error,
    }


def handle(req: dict, evidence_base_url=None):
    """处理一条 JSON-RPC 请求。返回 None 表示"通知，无需响应"。"""
    if not isinstance(req, dict):
        return _err(None, -32600, "Invalid Request")
    method = req.get("method")
    rid = req.get("id")
    params = req.get("params") or {}

    # --- 握手
    if method == "initialize":
        return _ok(rid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": "眼底跨模态检索与证据复核工具组。先 extract_medical_entities 解析五维，"
                            "再 search_cases 检索，再 verify_medical_logic 门禁，"
                            "最后 build_evidence_chain 出动作与证据卡。动作由代码确定性给出，不要自行改写。",
        })
    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return _ok(rid, {})

    # --- 工具
    if method == "tools/list":
        return _ok(rid, {"tools": TOOL_SCHEMAS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _err(rid, -32602, "Tool arguments must be an object")
        args = dict(args)
        include_html = args.pop("include_html", False) is True
        try:
            res = MT.call_tool(name, args)
        except Exception as exc:  # noqa: BLE001  协议层兜底：工具炸了也不能断连接
            _log("tool %s crashed: %s" % (name, exc))
            return _err(rid, -32603, "工具执行异常：%s" % exc,
                        {"traceback": traceback.format_exc()[-800:]})
        if name == "build_evidence_chain" and res.get("ok", True) and not include_html:
            try:
                res = evidence_store.save(res)
                if evidence_base_url:
                    res["evidence_url"] = evidence_base_url.rstrip("/") + res["evidence_url"]
            except OSError:
                res = {"ok": False, "error": {"code": "ARTIFACT_WRITE_FAILED",
                       "message": "证据卡保存失败，请检查产物目录权限；未返回不完整证据"}}
        if name == "search_cases" and evidence_base_url and isinstance(res.get("evidence"), dict):
            url = res["evidence"].get("view_url", "")
            if url.startswith("/"):
                res["evidence"]["view_url"] = evidence_base_url.rstrip("/") + url
        if name == 'search_cases' and evidence_base_url:
            for candidate in res.get('candidates', []):
                for key in ('image_url', 'region_image_url'):
                    if candidate.get(key, '').startswith('/'):
                        candidate[key] = evidence_base_url.rstrip('/') + candidate[key]
        _log("tools/call %s -> ok=%s action=%s" % (name, res.get("ok"), res.get("action")))
        return _ok(rid, _tool_result(res))

    # --- 空资源/提示（避免客户端因"方法不存在"报错）
    if method == "resources/list":
        return _ok(rid, {"resources": []})
    if method == "prompts/list":
        return _ok(rid, {"prompts": []})

    return _err(rid, -32601, "Method not found: %s" % method)


def main() -> int:
    _log("started (protocol %s, %d tools)" % (PROTOCOL_VERSION, len(TOOL_SCHEMAS)))
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:  # noqa: BLE001
            _write(_err(None, -32700, "Parse error"))
            continue
        try:
            resp = handle(req)
        except Exception as exc:  # noqa: BLE001
            _log("handler crashed: %s" % exc)
            resp = _err(req.get("id") if isinstance(req, dict) else None, -32603, str(exc))
        if resp is not None:
            _write(resp)
    _log("stdin closed, exit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
