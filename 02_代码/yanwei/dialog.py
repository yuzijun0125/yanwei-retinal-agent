"""Persistent, deterministic retrieval dialogue; no LLM required for basic interaction."""
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import threading
import uuid
from . import assets, mcp_tools as tools
from .config import default_output_dir

_LOCK = threading.RLock()


def respond(payload):
    text = payload.get("text", "")
    if not isinstance(text, str) or len(text) > 8000:
        raise ValueError("请输入8000字符以内的文字")
    text = text.strip()
    sid = payload.get("session_id") or uuid.uuid4().hex
    if not isinstance(sid, str) or not re.fullmatch(r"[a-f0-9]{32}", sid):
        raise ValueError("会话ID无效，请新建会话")
    root = Path(default_output_dir())
    root.mkdir(parents=True, exist_ok=True)
    with _LOCK, closing(sqlite3.connect(str(root / "sessions.sqlite3"), timeout=10)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, state TEXT NOT NULL)")
        row = db.execute("SELECT state FROM sessions WHERE id=?", (sid,)).fetchone()
        state = json.loads(row[0]) if row else {}
        result = _turn(text, payload.get("asset_id"), state)
        db.execute("INSERT INTO sessions VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET state=excluded.state",
                   (sid, json.dumps(state, ensure_ascii=False)))
    return {"ok": True, "session_id": sid, **result}


def _turn(text, asset_id, state):
    if asset_id:
        assets.get(asset_id)
        state.clear()
        state["asset_id"] = asset_id
        state.pop("pending_query", None)
    if text in ("你好", "您好", "你能做什么", "你可以帮我做什么", "怎么用", "我提供什么指令"):
        return {"reply": "不用固定指令。可以直接描述眼别、病灶和位置，或上传眼底图片。信息不足时我会追问；图片结果只是未经医学验证的检索提示。"}
    if text in ("左眼", "右眼") and state.get("last_action") == "RETURN_CASES":
        previous = state.get("query", "")
        state["pending_query"] = re.sub(r"左眼|右眼|\bOD\b|\bOS\b", text, previous, flags=re.I)
        return {"reply": f"是把上一条改为{text}，其他描述保持不变吗？请回复“是”或补充新的描述。"}
    if text in ("是", "对", "确认", "是的", "其他不变") and state.get("pending_query"):
        query = state.pop("pending_query")
        state["round"] = 0
    elif text == "继续":
        if state.get("last_action") == "RETURN_CASES":
            return {"reply": "上一轮检索已完成。可以修改眼别、补充新的病灶描述，或新建会话上传另一张图片。"}
        query = state.get("query", "")
    else:
        previous = state.get("query", "")
        rt = tools.Runtime.get()
        new_codes = tools.parse_entities(rt, text)["codes"]
        complete_new = bool(new_codes.get("lesion") and new_codes.get("location"))
        old_parsed = tools.parse_entities(rt, previous)
        selecting_eye = (not complete_new and new_codes.get('eye') in ('OS', 'OD') and
                         any(c['dim'] == 'eye' for c in old_parsed.get('input_conflicts', [])))
        if selecting_eye:
            # The answer selects an eye for the pending request, keeping its lesion/location.
            previous = re.sub(r'左眼|右眼|双眼|\b(?:OD|OS|OU)\b', '', previous, flags=re.I)
            query = text + '；' + previous
            state['round'] = 0
        else:
            query = text if complete_new or state.get("last_action") == "RETURN_CASES" else (previous + "；" + text).strip("；")
        if complete_new and state.get("last_action") == "ASK_MORE" and not new_codes.get("eye"):
            eye = tools.parse_entities(rt, previous)["codes"].get("eye")
            if eye in ("OS", "OD"):
                query = {"OS": "左眼", "OD": "右眼"}[eye] + "；" + query
    image = assets.get(state["asset_id"])["analysis_path"] if state.get("asset_id") else None
    flow = tools.complete_flow({"query_text": query, "image_path": image,
                                "image_asset_id": state.get('asset_id'),
                                "clarify_round": state.get("round", 0), "topk": 3})
    if not flow.get("ok") and not flow.get("action"):
        return {"reply": "本轮未完成：" + flow.get("error", {}).get("message", "服务暂不可用"), "result": flow}
    state["query"] = query
    state["last_action"] = flow["action"]
    state["round"] = state.get("round", 0) + 1 if flow["action"] == "ASK_MORE" else 0
    reply = flow["action_reason"] or "请补充想查询的内容。"
    if (flow.get('multimodal') or {}).get('used'):
        reply = '已完成图文向量检索。以下展示整图相似候选，相似度不是疾病概率。\n' + reply
    elif state.get('asset_id') and flow.get('multimodal'):
        reply = '图文编码未启用，本轮已降级：' + flow['multimodal'].get('reason', '模型不可用') + '。\n' + reply
    if flow.get("image_detect"):
        detected = flow["image_detect"]
        if detected.get("available"):
            labels = "、".join(x["label"] for x in detected.get("detections", [])[:4]) or "无明确提示"
            reply = f"已读取图片，粗粒度图像提示：{labels}。这些提示未经医学验证，请结合文字确认。\n" + reply
        else:
            reply = "图片检测暂不可用：" + detected.get("error", "未知错误") + "。\n" + reply
    if flow["action"] == "RETURN_CASES":
        reply = "找到以下研究用候选病例，需人工复核。\n" + reply
    return {"reply": reply, "result": flow, "asset_id": state.get("asset_id")}
