"""五个 MCP 工具的实现（契约见 03_文档/接口契约.md）。

设计要点：
  1. **零依赖、纯标准库**；每个工具都做输入校验与异常兜底，绝不把异常抛给调用方；
  2. **动作不是"决定"出来的，是算出来的** —— decide_action() 是唯一的动作出口，
     界面 / 编排器 / Nexent 三方共用，避免三处实现漂移；
  3. **阈值一律读 config/thresholds.yaml**，本文件不硬编码任何决策阈值；
  4. 面向"未知"的态度：查不到就说查不到，不编造（对齐"未知 ≠ 阴性"）。

工具清单（数量锁死 5 个，任何理由都不加第 6 个）：
  · extract_medical_entities  五维解析（含否定 / 不确定）
  · search_cases              BM25 + 五维一致性线性融合检索
  · verify_medical_logic      逐维判定 + 冲突定位 + 充分性分数
  · build_evidence_chain      动作决策 + 证据卡（JSON + HTML 双形态）
  · record_feedback           反馈隔离落库（幂等）
"""

from __future__ import annotations

import html as _html
import json
import os
import re
import time
import uuid

from . import assets, bm25, dataio, imageqc, lesion_detect, texttoken, multimodal
from .config import default_config, default_data_dir, default_output_dir

# ---------------------------------------------------------------- 常量

DIMS = ("eye", "lesion", "location", "severity", "modality")

# 五维 → Region 上的属性名（区域画像的取值处）
_DIM_ATTR = {
    "eye": "eye",
    "lesion": "lesion_type",
    "location": "location",
    "severity": "severity",
    "modality": "modality",
}

# rules_medical.yaml 里 lexicon 的中文分组名 → 英文字段名
_DIM_KEY = {
    "眼别": "eye",
    "病灶": "lesion",
    "位置": "location",
    "程度": "severity",
    "模态": "modality",
}

# 否定词（长词优先匹配）：命中病灶/位置/眼别/模态后，把这些词判为"明确排除"
_NEG_WORDS = ("未见", "未", "无", "不", "没有", "阴性", "排除")
# 不确定词：实体保留但打 uncertain 标
_UNC_WORDS = ("疑似", "可能", "考虑", "倾向", "待排", "不排除")

# 动作枚举（前 4 个是"决策动作"，RETAKE_IMAGE 是质检门的直接出口）
ACTION_LABELS = {
    "RETURN_CASES": "返回可比先例",
    "ASK_MORE": "信息不足，需要追问",
    "ABSTAIN": "证据不足，拒答",
    "SUGGEST_REFERRAL": "建议转诊",
    "RETAKE_IMAGE": "建议重拍",
}

CAVEATS = [
    "眼别 / 位置 / 程度由规则推导，复核状态为 PENDING，不作为医学金标准。",
    "图文配对为合成/弱配对，不是真实临床报告，不得对外当指标（数据字典 MT-01/MT-02）。",
    "原始影像来自 IDRiD（CC-BY-4.0），使用时须引用官方论文并标注来源。",
    "本输出仅用于研究与教学参考，不构成临床诊断或治疗建议；涉及诊疗决策请咨询专业医师。",
]


class ToolError(Exception):
    """工具内部的受控错误，会被包装成 ok=false 返回。"""

    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


def _object_input(value, field):
    """Nexent's code-agent adapter may return upstream tool data as JSON text."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            raise ToolError("BAD_INPUT", field + "不是合法JSON", "传入前一步工具返回对象")
    if not isinstance(value, dict):
        raise ToolError("BAD_INPUT", field + "必须是对象", "传入前一步工具返回对象")
    return value


def _candidate_list(value):
    """Normalize legacy JSON strings and structured candidate envelopes."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            raise ToolError("BAD_INPUT", "candidates不是合法JSON", "传入search_cases返回对象")
    if isinstance(value, dict):
        value = value.get("candidates")
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ToolError("BAD_INPUT", "缺少候选对象列表", "传入search_cases返回对象或候选数组")
    return value


# ---------------------------------------------------------------- 运行时（单例）

class Runtime:
    """装载配置 + 数据 + 索引，进程内复用一次，避免每次调用都重读全表。"""

    _inst = None

    def __init__(self, cfg=None, data_dir: str | None = None):
        self.cfg = cfg or default_config()
        self.data_dir = data_dir or default_data_dir()
        self.ds = dataio.Dataset(self.data_dir, self.cfg)
        if not self.ds.cases or not self.ds.images or not self.ds.regions:
            raise ToolError("DATA_NOT_READY", "必要的病例、图像或区域数据未就绪", self.data_dir)
        self.tok = texttoken.Tokenizer(self.cfg.rules.get("lexicon", {}))
        self.index = bm25.BM25Index().build(self.ds.corpus(), self.tok.tokens)
        self.dim_lexicon = self._build_dim_lexicon()

    @classmethod
    def get(cls) -> "Runtime":
        if cls._inst is None:
            cls._inst = cls()
        return cls._inst

    def _build_dim_lexicon(self) -> dict:
        """{dim: [(surface, code), ...]}，按表面形式长度降序（长词优先）。"""
        lex = self.cfg.rules.get("lexicon", {}) or {}
        out = {d: [] for d in DIMS}
        for cn_key, pairs in lex.items():
            dim = _DIM_KEY.get(cn_key)
            if not dim or not isinstance(pairs, dict):
                continue
            for surface, code in pairs.items():
                out[dim].append((str(surface), str(code)))
        for d in out:
            out[d].sort(key=lambda kv: len(kv[0]), reverse=True)
        return out


# ---------------------------------------------------------------- 内部工具函数

def _now_ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 3)


def _ok(payload: dict) -> dict:
    payload.setdefault("ok", True)
    return payload


def _fail(code: str, message: str, hint: str = "") -> dict:
    return {"ok": False, "error": {"code": code, "message": message, "hint": hint}}


def _require(payload, key, types, default=None):
    if not isinstance(payload, dict):
        raise ToolError("BAD_INPUT", "入参必须是对象", "例如 {'text': '右眼黄斑区微血管瘤'}")
    v = payload.get(key, default)
    if v is None:
        raise ToolError("BAD_INPUT", "缺少必填参数 %s" % key, "必填字段：%s" % key)
    if not isinstance(v, types):
        raise ToolError("BAD_INPUT", "参数 %s 类型应为 %s" % (key, types), "收到 %s" % type(v).__name__)
    return v


def _profile(reg) -> dict:
    return {d: getattr(reg, _DIM_ATTR[d]) for d in DIMS}


def _profile_display(cfg, prof: dict) -> dict:
    return {d: cfg.display(d, c or "UNKNOWN") for d, c in prof.items()}


def _find_negation(text: str, start: int, window: int = 4) -> bool:
    """实体起点前 window 个字符内是否有否定词。"""
    lo = max(0, start - window)
    ctx = text[lo:start]
    ctx = re.split(r'[，,。；;！？!?\n]', ctx)[-1]
    ctx = re.sub(r'不对(?:是)?|说错了(?:是)?', '', ctx)
    for word in _UNC_WORDS:
        ctx = ctx.replace(word, '')
    return any(w in ctx for w in _NEG_WORDS)


def _uncertain_spans(text: str) -> list:
    """不确定词的作用区间：**句内、从不确定词到句末**。

    为什么按句而不是按"就近 4 字"：中文医学表述里"疑似右侧黄斑区中度出血"的
    不确定对象通常是**整个描述**，而不是紧邻的那一个词。按句覆盖更贴近真实语义。
    """
    if not text:
        return []
    seps = "。；;！？!?\n"
    bounds, start = [], 0
    for i, ch in enumerate(text):
        if ch in seps:
            bounds.append((start, i))
            start = i + 1
    bounds.append((start, len(text)))
    spans = []
    for lo, hi in bounds:
        seg = text[lo:hi]
        for w in _UNC_WORDS:
            k = seg.find(w)
            if k >= 0:
                spans.append((lo + k, hi))
                break
    return spans


def _find_uncertain(spans: list, start: int) -> bool:
    return any(lo <= start < hi for lo, hi in spans)


def parse_entities(rt: Runtime, text: str) -> dict:
    """分维度解析五维实体。**这是全案唯一的结构真源**，务必保持确定性。"""
    text = text or ""
    entities: dict = {}
    negated, uncertain, all_hits = [], [], {}
    unc_spans = _uncertain_spans(text)

    for dim in DIMS:
        hits = []
        for surface, code in rt.dim_lexicon[dim]:
            idx = text.find(surface)
            while idx >= 0:
                # "未见" 特例：若其后紧跟病灶词，它扮演的是**否定前缀**而不是程度值
                # （"未见出血" = 没看到出血，不是在说程度是"未见"）
                if dim == "severity" and surface == "未见":
                    tail = text[idx + len(surface): idx + len(surface) + 4]
                    if any(s in tail for s, _ in rt.dim_lexicon["lesion"]):
                        idx = text.find(surface, idx + 1)
                        continue
                hits.append({"code": code, "surface": surface, "span": [idx, idx + len(surface)]})
                idx = text.find(surface, idx + 1)

        for h in hits:
            h["negated"] = _find_negation(text, h["span"][0])
            h["uncertain"] = _find_uncertain(unc_spans, h["span"][0])
        all_hits[dim] = hits

        # 每维取一个"最佳命中"：**优先取未被否定的**（"散在微血管瘤，未见出血"该取微血管瘤）；
        # 全被否定时取最靠前的那个（仅用于展示，codes 会被置空）。
        if hits:
            positive = [h for h in hits if not h["negated"]] or hits
            positive.sort(key=lambda h: (-len(h["surface"]), h["span"][0]))
            best = dict(positive[0])
        else:
            best = None
        entities[dim] = best

        # negated / uncertain 收集**该维所有命中**，而不只是 best——
        # 否则"微血管瘤（阳性）+ 未见出血（阴性）"这种同维并存的情况会丢掉否定项。
        for h in hits:
            if h["negated"] and h["surface"] not in negated:
                negated.append(h["surface"])
            if h["uncertain"] and h["surface"] not in uncertain:
                uncertain.append(h["surface"])

    # Hard dimensions must never silently collapse contradictory positive values.
    input_conflicts, resolutions, active_codes = [], [], {}
    for dim in ('eye', 'modality'):
        positive = [h for h in all_hits[dim] if not h['negated'] and h['code'] != 'UNKNOWN']
        # Only explicit correction phrases resolve earlier values; ordinary conjunctions do not.
        for marker in re.finditer(r'不对[，,、\s]*(?:是)?|更正为|改为|改查|说错了[，,、\s]*(?:是)?', text):
            following = [h for h in positive if 0 <= h['span'][0] - marker.end() <= 8]
            if following:
                later = [h for h in positive if h['span'][0] >= marker.end()]
                if later:
                    resolutions.append({'dim': dim, 'marker': marker.group(), 'span': list(marker.span())})
                    positive = later
        values = sorted({h['code'] for h in positive})
        active_codes[dim] = values
        if len(values) > 1 or (dim == 'eye' and 'OU' in values):
            input_conflicts.append({'dim': dim, 'values': values,
                'hits': positive, 'message': '请确认本次只查左眼还是右眼；双眼请分别检索。' if dim == 'eye'
                else '同一查询包含多种成像模态，请确认本次使用眼底彩照还是超广角图像。'})
            entities[dim] = None
        elif positive:
            entities[dim] = dict(sorted(positive, key=lambda h: (-len(h['surface']), h['span'][0]))[0])
    codes = {d: (entities[d]["code"] if entities[d] else None) for d in DIMS}
    # 被否定的维度不参与检索意图
    for d in DIMS:
        if entities[d] and entities[d]["negated"]:
            codes[d] = None
    present = [d for d in DIMS if codes[d]]
    missing = [d for d in DIMS if not codes[d]]
    return {
        "entities": entities,
        "codes": codes,
        "present": present,
        "missing": missing,
        "negated": negated,
        "uncertain": uncertain,
        "all_hits": all_hits,
        "input_conflicts": input_conflicts,
        "resolutions": resolutions,
        "active_codes": active_codes,
    }


def input_conflict_decision(parsed, cfg, clarify_round=0):
    conflicts = parsed.get('input_conflicts') or []
    if not conflicts:
        return None
    capped = int(clarify_round) >= cfg.max_clarify_rounds
    return {'action': 'ABSTAIN' if capped else 'ASK_MORE',
            'reason': ('眼别或成像模态仍未确认，本轮暂不检索。' if capped else '') +
                      ' '.join(c['message'] for c in conflicts),
            'source': 'input_conflict_gate', 'decided_by': 'input_conflict_gate', 'overridden': True}


def consistency(cfg, query_codes: dict, reg, weights: dict | None = None) -> tuple:
    """五维一致性分 = Σ w·s / Σ w。

    s 取值（关键设计，决定"缺信息"如何体现）：
      1.0  查询有该维且与候选一致
      0.0  查询有该维但与候选不符
      0.5  查询未提及该维（中性：既不加分也不减分，缺口由完整度惩罚补）
    """
    w = dict(weights or cfg.weights)
    total = wsum = 0.0
    detail = {}
    for dim in DIMS:
        wd = float(w.get(dim, 0.0))
        if wd <= 0:
            continue
        wsum += wd
        q = query_codes.get(dim)
        c = getattr(reg, _DIM_ATTR[dim])
        if not q:
            s, verdict = 0.5, "UNKNOWN"
        elif q == c:
            s, verdict = 1.0, "MATCH"
        else:
            s, verdict = 0.0, "MISMATCH"
        detail[dim] = {"query": q, "candidate": c, "verdict": verdict}
        total += wd * s
    return (total / wsum if wsum else 0.0), detail


def _mode_weights(cfg, mode: str) -> dict:
    """region 模式：位置维权重上调，让"位置优先"可复现，而不是玄学。"""
    w = dict(cfg.weights)
    if mode == "region":
        w["location"] = float(w.get("location", 0.20)) * 1.6
    return w


def decide_action(cfg, logic: dict, query_codes: dict, primary_care: bool = True) -> dict:
    """**动作的唯一出口**（界面 / 编排器 / Nexent 都走这里，避免实现漂移）。

    优先级：硬冲突（代码强制改写）＞ 转诊规则（覆盖）＞ 阈值映射。
    对应构建方案 §4 状态机的 ⑤ GATE → ⑥ DECIDE。
    """
    if logic.get('input_conflicts'):
        return input_conflict_decision(logic, cfg, logic.get('clarify_round', 0))
    # ① 硬门禁：眼别 / 模态不可比 —— 由代码强制改写，不靠模型自觉
    hard = [c for c in logic.get("conflicts", []) if c.get("severity") == "hard"]
    if hard:
        dims = "、".join(sorted({cfg.display(c["dim"], c["dim"]) for c in hard}))
        return {
            "action": "ABSTAIN",
            "reason": "命中硬门禁：%s 存在不可比冲突（%s），由代码强制改写为拒答"
                      % (dims, "；".join(c["message"] for c in hard)),
            "source": "config/rules_medical.yaml#conflict_policy.hard",
            "decided_by": "hard_override",
            "overridden": True,
        }

    # ② 转诊规则：高风险覆盖常规阈值映射
    sev = query_codes.get("severity")
    ref_high = cfg.referral_severity_high
    if sev in ref_high:
        rule = _referral_rule_for(cfg, "severity", sev)
        return {
            "action": "SUGGEST_REFERRAL",
            "reason": "命中转诊规则 %s：程度为「%s」，%s"
                      % (rule.get("rule_id", "REF-?"), cfg.display("severity", sev),
                         "超出基层处理能力，建议转诊" if primary_care else "建议进一步检查"),
            "source": "config/rules_medical.yaml#referral_rules[%s]" % rule.get("rule_id", "REF-?"),
            "decided_by": "referral_override",
            "overridden": False,
            "hit_rules": [rule.get("rule_id")] if rule.get("rule_id") else [],
        }

    # ③ 阈值映射（唯一真源：thresholds.yaml）
    suf = float(logic.get("sufficiency", 0.0))
    hi, lo = cfg.sufficiency_return, cfg.sufficiency_ask
    if suf >= hi:
        return {
            "action": "RETURN_CASES",
            "reason": "证据充分性 %.2f ≥ %.2f，且无硬冲突" % (suf, hi),
            "source": "config/thresholds.yaml#sufficiency.return_cases",
            "decided_by": "deterministic",
            "overridden": False,
        }
    if suf >= lo:
        miss = "、".join(logic.get("missing_required", [])) or "、".join(logic.get("missing", []))
        return {
            "action": "ASK_MORE",
            "reason": "证据充分性 %.2f 落在 [%.2f, %.2f)，需补充关键信息（缺：%s）"
                      % (suf, lo, hi, miss or "未指定"),
            "source": "config/thresholds.yaml#sufficiency.ask_more",
            "decided_by": "deterministic",
            "overridden": False,
        }
    return {
        "action": "ABSTAIN",
        "reason": "证据充分性 %.2f < %.2f，不足以给出可比先例" % (suf, lo),
        "source": "config/thresholds.yaml#sufficiency.ask_more",
        "decided_by": "deterministic",
        "overridden": False,
    }


def _referral_rule_for(cfg, field: str, value) -> dict:
    for rule in (cfg.rules.get("referral_rules") or []):
        cond = str(rule.get("when", ""))
        if field in cond and str(value) in cond:
            return dict(rule)
    return {}


# ================================================================ 工具 1

def extract_medical_entities(payload: dict, rt: Runtime | None = None) -> dict:
    """五维解析：文本 → 结构化实体（含否定 / 不确定）。"""
    t0 = time.perf_counter()
    try:
        text = _require(payload, "text", str)
        rt = rt or Runtime.get()
        parsed = parse_entities(rt, text)
        return _ok({
            "text": text,
            "entities": parsed["entities"],
            "codes": parsed["codes"],
            "present": parsed["present"],
            "missing": parsed["missing"],
            "negated": parsed["negated"],
            "uncertain": parsed["uncertain"],
            "input_conflicts": parsed["input_conflicts"],
            "active_codes": parsed["active_codes"],
            "resolutions": parsed["resolutions"],
            "elapsed_ms": _now_ms(t0),
        })
    except ToolError as e:
        return _fail(e.code, e.message, e.hint)
    except Exception as e:  # noqa: BLE001
        return _fail("INTERNAL", "解析失败：%s" % e, "检查文本编码或术语表")


# ================================================================ 工具 2

def search_cases(payload: dict, rt: Runtime | None = None) -> dict:
    """跨模态检索：BM25 稀疏检索 + 五维一致性线性融合。

    mode=standard 常规 / region 位置优先 / negative 反例（文本像但画像不同）
    """
    t0 = time.perf_counter()
    try:
        if not isinstance(payload, dict):
            raise ToolError("BAD_INPUT", "入参必须是对象", "")
        if payload.get('image_asset_id'):
            try:
                asset = assets.get(payload['image_asset_id'])
            except ValueError as exc:
                raise ToolError('BAD_INPUT', str(exc)) from exc
            payload = {**payload, 'image_path': asset['analysis_path']}
        if payload.get("complete_flow") is True:
            return complete_flow(payload, rt)
        mode = payload.get("mode") or "standard"
        if mode not in ("standard", "region", "negative"):
            raise ToolError("BAD_INPUT", "未知 mode：%s" % mode, "可选 standard / region / negative")
        query_text = payload.get("query_text", "")
        if not isinstance(query_text, str) or len(query_text) > 8000:
            raise ToolError("BAD_INPUT", "查询必须是8000字符以内的文本")
        image_path = payload.get("image_path")
        filters = payload.get("filters") or {}
        rt = rt or Runtime.get()
        cfg = rt.cfg

        # --- 前置：图像质检（有图才做，且 RETAKE 时如实返回，由编排器决定不进检索）
        qc = None
        image_detect = None
        if image_path:
            qr = imageqc.assess_cached(image_path)
            qc = qr.to_dict()
            # 图像可判读才做病灶检测（支柱二：图 → 病灶 → 检索意图）
            if qr.label == "GRADABLE" and not multimodal.status().get('ready'):
                image_detect = lesion_detect.detect_lesions(image_path)

        # --- 五维解析（解析结果即检索意图）
        parsed = parse_entities(rt, query_text)
        conflict = input_conflict_decision(parsed, cfg)
        if conflict:
            return _ok({'action': conflict['action'], 'action_reason': conflict['reason'],
                        'query': parsed, 'query_codes': parsed['codes'], 'count': 0,
                        'candidates': [], 'input_conflicts': parsed['input_conflicts'],
                        'elapsed_ms': _now_ms(t0)})
        semantic_hits, semantic_info = multimodal.recall(
            image_path if qc and qc.get('label') == 'GRADABLE' else None,
            parsed['codes'], parsed.get('negated', []))
        codes = dict(parsed["codes"])
        if payload.get("modality"):
            codes["modality"] = str(payload["modality"])
        # 图像病灶检测结果补充检索意图（文字优先：用户明说的维度不被图像推测覆盖）
        if image_detect and image_detect.get("available"):
            # Unvalidated image estimates may suggest a lesion, never severity or laterality.
            proposed = {k: v for k, v in image_detect.get("codes", {}).items()
                        if k in ("lesion", "modality")}
            codes = lesion_detect.merge_image_codes(codes, proposed)

        # 病灶检测出的病灶词并入 BM25 召回 query——否则纯图检索时 query_text 为空，
        # 召回阶段拿不到候选（库里 doc_text 是"微动脉瘤 MA"中英双写，病灶词可直接命中）
        search_query = query_text
        if image_detect and image_detect.get("available"):
            terms = []
            for d in image_detect.get("detections", []):
                if d.get("label"):
                    terms.append(d["label"])
                if d.get("code"):
                    terms.append(d["code"])
            if terms:
                search_query = (query_text + " " + " ".join(terms)).strip()

        topk_final = payload.get("topk", cfg.get("retrieval.topk_final", 5))
        if type(topk_final) is not int or not 1 <= topk_final <= 30:
            raise ToolError("BAD_INPUT", "topk必须是1到30的整数", "例如 topk=3")
        if not isinstance(query_text, str) or len(query_text) > 8000:
            raise ToolError("BAD_INPUT", "查询必须是8000字符以内的文本")
        if not isinstance(filters, dict):
            raise ToolError("BAD_INPUT", "filters必须是对象")
        topk_coarse = int(cfg.get("retrieval.topk_coarse", 30))
        min_cand = int(cfg.get("retrieval.min_candidates", 3))

        weights = _mode_weights(cfg, mode)
        w_r, w_c = cfg.rerank_w_retrieval, cfg.rerank_w_consistency

        def _collect(topk_n: int, rerank: bool):
            hits = rt.index.search(search_query, topk=topk_n)
            sparse_ranks = {rid: rank for rank, (rid, _) in enumerate(hits, 1)}
            if semantic_info.get('used'):
                existing = {rid for rid, _ in hits}
                hits += [(reg.region_id, 0.0) for reg in rt.ds.regions.values()
                         if reg.image_id in semantic_hits and reg.region_id not in existing]
            if not hits:
                return []
            max_score = hits[0][1] or 1.0
            rows = []
            for rid, raw in hits:
                reg = rt.ds.region(rid)
                if reg is None:
                    continue
                prof = _profile(reg)
                if any(str(filters.get(k)) not in ("", "None") and prof.get(k) != filters.get(k) for k in filters):
                    continue
                cons, detail = consistency(cfg, codes, reg, weights)
                rn = raw / max_score
                semantic = semantic_hits.get(reg.image_id)
                if semantic_info.get('used'):
                    opts = multimodal.settings()
                    k = opts.get('rrf_k', 20)
                    sw = opts.get('semantic_weight', .65) if query_text.strip() else 1.0
                    sr = (k + 1) / (k + semantic['rank']) if semantic else 0.0
                    br = (k + 1) / (k + sparse_ranks[rid]) if rid in sparse_ranks else 0.0
                    rn = sw * sr + (1 - sw) * br
                if not rerank:
                    final = rn
                elif mode == "negative":
                    final = w_r * rn - w_c * cons
                else:
                    final = w_r * rn + w_c * cons
                rows.append({
                    "region_id": rid,
                    "score": round(final, 6),
                    "retrieval_score": round(raw, 4),
                    "retrieval_norm": round(rn, 6),
                    "consistency_score": round(cons, 6),
                    "profile": prof,
                    "profile_display": _profile_display(cfg, prof),
                    "dim_verdicts": {d: v["verdict"] for d, v in detail.items()},
                    "semantic_scores": semantic,
                    "retrieval_basis": 'FLAIR_RRF_BM25' if semantic_info.get('used') else 'BM25_TEXT',
                    "image_url": '/api/case-image?id=' + reg.image_id,
                    "region_image_url": '/api/case-image?id=' + reg.image_id + '&region_id=' + rid,
                    "region_note": '参考病例既有bbox区域，元数据待复核；不是对本次上传图的自动病灶定位',
                    "source": {
                        "image_id": reg.image_id,
                        "case_id": reg.case_id,
                        "raw_path": reg.raw_path,
                        "text_ids": list(reg.text_ids),
                        "provenance_tier": "",
                        "official_source": "",
                        "official_locator": "",
                        "mask_path": "",
                        "area_ratio": "",
                    },
                })
            rows.sort(key=lambda r: r["score"], reverse=True)
            return rows

        rows = _collect(topk_coarse, rerank=True)
        degradation = None
        if len(rows) < min_cand:
            # 候选不足 → 降级为纯检索排序（coarse），并如实标注"未重排"
            rows = _collect(topk_coarse, rerank=False)
            degradation = {
                "reason": "候选不足（%d < %d）" % (len(rows), min_cand),
                "from": mode, "to": "coarse",
                "note": "已降级为纯检索排序，未做五维重排",
            }

        # 补齐可溯源字段（区域 → 官方锚点）
        raw_by_region = {r["region_id"]: r for r in rt.ds.regions_raw}
        out = []
        if semantic_info.get('used'):
            seen_images = set()
            diversified = []
            for row in rows:
                image_id = row['source']['image_id']
                if image_id not in seen_images:
                    diversified.append(row)
                    seen_images.add(image_id)
            rows = diversified
        for i, r in enumerate(rows[:topk_final], start=1):
            r["rank"] = i
            src_raw = raw_by_region.get(r["region_id"], {})
            r["source"]["mask_path"] = src_raw.get("mask_path", "")
            r["source"]["provenance_tier"] = src_raw.get("provenance_tier", "")
            r["source"]["official_source"] = src_raw.get("official_source", "")
            r["source"]["official_locator"] = src_raw.get("official_locator", "")
            r["source"]["area_ratio"] = src_raw.get("area_ratio", "")
            out.append(r)

        return _ok({
            "mode": mode,
            "query_codes": codes,
            "qc": qc,
            "image_detect": image_detect,
            "multimodal": semantic_info,
            "candidates": out,
            "count": len(out),
            "elapsed_ms": _now_ms(t0),
            "degradation": degradation,
        })
    except ToolError as e:
        return _fail(e.code, e.message, e.hint)
    except Exception as e:  # noqa: BLE001
        return _fail("INTERNAL", "检索失败：%s" % e, "检查数据表与索引是否完整")


# ================================================================ 工具 3

def complete_flow(payload, rt=None):
    """Execute the deterministic workflow once, without model-generated intermediate state."""
    from .pipeline import Pipeline
    from . import evidence_store
    if not isinstance(payload, dict):
        return _fail("BAD_INPUT", "入参必须是对象")
    text = payload.get("query_text", "")
    if not isinstance(text, str) or len(text) > 8000:
        return _fail("BAD_INPUT", "查询必须是8000字符以内的文本")
    clarify = payload.get("clarify_round", 0)
    topk = payload.get("topk", 3)
    if type(clarify) is not int or not 0 <= clarify <= 100:
        return _fail("BAD_INPUT", "clarify_round必须是0到100的整数")
    if type(topk) is not int or not 1 <= topk <= 30:
        return _fail("BAD_INPUT", "topk必须是1到30的整数")
    rt = rt or Runtime.get()
    asset_id = payload.get('image_asset_id')
    if asset_id:
        try:
            payload = {**payload, 'image_path': assets.get(asset_id)['analysis_path']}
        except ValueError as exc:
            return _fail('BAD_INPUT', str(exc))
    result = Pipeline(rt=rt).run(text, image_path=payload.get("image_path"),
                                topk=payload.get("topk", 3), mode=payload.get("mode", "standard"),
                                clarify_round=clarify, save_artifacts=False)
    evidence = result.get("evidence")
    if evidence:
        if (result.get('multimodal') or {}).get('used'):
            evidence['multimodal'] = result['multimodal']
            evidence['query_image_asset_id'] = asset_id
            pictures = []
            if asset_id:
                pictures.append(('本次上传图片', '/api/asset?id=' + asset_id))
            pictures += [(candidate['source']['case_id'], candidate['image_url']) for candidate in result['candidates'][:3]]
            gallery = '<section><h2>整图相似候选对照</h2><p>仅表示整图相似，不是病灶定位或疾病概率。</p><div style="display:flex;gap:12px;flex-wrap:wrap">'
            for title, url in pictures:
                gallery += '<figure style="margin:0;width:220px"><img style="width:100%%" alt="%s" src="%s"><figcaption>%s</figcaption></figure>' % (_html.escape(title), _html.escape(url, quote=True), _html.escape(title))
            gallery += '</div></section>'
            evidence['html'] = evidence['html'].replace('</body>', gallery + '</body>')
        evidence = evidence_store.save(evidence)
        evidence["view_url"] = evidence["evidence_url"]
    return {"ok": result["ok"], "action": result["action"], "action_label": result["action_label"],
            "action_reason": result["action_reason"], "query": result["parsed"], "qc": result["qc"],
            "image_detect": result.get("image_detect"), "candidates": result["candidates"][:3],
            "multimodal": result.get('multimodal'), "image_asset_id": asset_id,
            "evidence": evidence, "trace_id": result["trace_id"], "elapsed_ms": result["elapsed_ms"]}

def verify_medical_logic(payload: dict, rt: Runtime | None = None) -> dict:
    """五维硬门禁：逐维判定 + 冲突定位 + 充分性分数。"""
    t0 = time.perf_counter()
    try:
        if not isinstance(payload, dict):
            raise ToolError("BAD_INPUT", "入参必须是对象", "")
        rt = rt or Runtime.get()
        cfg = rt.cfg

        query = _object_input(payload.get("query") or {}, "query")
        codes = query.get("codes") if isinstance(query, dict) else None
        if not codes:
            raise ToolError("BAD_INPUT", "缺少 query.codes", "先调 extract_medical_entities")
        if query.get('input_conflicts'):
            return _ok({'verdict': 'INPUT_CONFLICT', 'input_conflicts': query['input_conflicts'],
                        'clarify_round': query.get('clarify_round', 0), 'conflicts': [],
                        'sufficiency': 0.0, 'per_dimension': {}, 'missing': query.get('missing', []),
                        'missing_required': [], 'elapsed_ms': _now_ms(t0)})

        comp = cfg.completeness or {}
        required = list(comp.get("required", ["lesion", "location"]))

        cands = _candidate_list(payload.get("candidates"))

        # 空候选不是调用方的错——它是检索的一个合法结果。这里给一个明确的**终结口径**，
        # 让直连 MCP 的调用方（含 LLM）能干净地收敛到 ABSTAIN，而不是收到 error 反复重试。
        # 编排器另有更早的短路（pipeline ⑤ GATE，decided_by=retrieval_empty），不受影响。
        if not cands:
            missing_all = [d for d in DIMS if not codes.get(d)]
            missing_required = [d for d in required if not codes.get(d)]
            return _ok({
                "per_dimension": {d: {"query": codes.get(d), "candidate": None, "verdict": "UNKNOWN"}
                                  for d in DIMS},
                "conflicts": [],
                "missing": missing_all,
                "missing_required": missing_required,
                "sufficiency": 0.0,
                "components": {"consistency": 0.0, "penalty": 0.0,
                               "missing_required_count": len(missing_required)},
                "verdict": "NO_CANDIDATES",
                "note": "检索返回 0 条候选：库中无可比区域，不编造结果",
                "elapsed_ms": _now_ms(t0),
            })

        # 以最高分候选作为"当前最佳证据"
        top = cands[0]
        prof = top.get("profile") or {}
        hard_dims = set((cfg.conflict_policy or {}).get("hard", ["eye", "modality"]))

        per_dim, conflicts = {}, []
        for dim in DIMS:
            qv = codes.get(dim)
            cv = prof.get(dim)
            if not qv:
                verdict = "UNKNOWN"
            elif qv == cv:
                verdict = "MATCH"
            else:
                verdict = "MISMATCH"
                is_hard = dim in hard_dims
                conflicts.append({
                    "dim": dim,
                    "query": qv,
                    "candidate": cv,
                    "severity": "hard" if is_hard else "soft",
                    "message": "%s不符：查询为%s，候选为%s%s"
                               % (cfg.display(dim, dim), cfg.display(dim, qv), cfg.display(dim, cv),
                                  "，不可比" if is_hard else "，仅扣分"),
                })
            per_dim[dim] = {"query": qv, "candidate": cv, "verdict": verdict}

        # 充分性 = 一致性 − 缺失必需维惩罚
        cons = float(top.get("consistency_score", 0.0))
        penalty_per = float(comp.get("penalty_per_missing", 0.12))
        missing_all = [d for d in DIMS if not codes.get(d)]
        missing_required = [d for d in required if not codes.get(d)]
        penalty = round(penalty_per * len(missing_required), 6)
        sufficiency = max(0.0, min(1.0, cons - penalty))

        if any(c["severity"] == "hard" for c in conflicts):
            verdict = "HARD_CONFLICT"
        elif conflicts:
            verdict = "SOFT_CONFLICT"
        else:
            verdict = "PASS"

        return _ok({
            "per_dimension": per_dim,
            "conflicts": conflicts,
            "missing": missing_all,
            "missing_required": missing_required,
            "sufficiency": round(sufficiency, 6),
            "components": {"consistency": round(cons, 6), "penalty": penalty,
                           "missing_required_count": len(missing_required)},
            "verdict": verdict,
            "uncertain": list(query.get("uncertain") or []),
            "negated": list(query.get("negated") or []),
            "elapsed_ms": _now_ms(t0),
        })
    except ToolError as e:
        return _fail(e.code, e.message, e.hint)
    except Exception as e:  # noqa: BLE001
        return _fail("INTERNAL", "门禁校验失败：%s" % e, "检查候选结构与查询解析结果")


# ================================================================ 工具 4

def build_evidence_chain(payload: dict, rt: Runtime | None = None) -> dict:
    """证据卡：动作决策 + 依据 + 溯源 + 免责声明（JSON + HTML 双形态）。"""
    t0 = time.perf_counter()
    try:
        if not isinstance(payload, dict):
            raise ToolError("BAD_INPUT", "入参必须是对象", "")
        rt = rt or Runtime.get()
        cfg = rt.cfg

        logic = _object_input(payload.get("logic_result") or payload.get("logic") or {}, "logic_result")
        if not isinstance(logic, dict) or "sufficiency" not in logic:
            raise ToolError("BAD_INPUT", "缺少 logic_result", "先调 verify_medical_logic")

        cands = _candidate_list(payload.get("candidates", []))

        query = _object_input(payload.get("query") or {}, "query")
        codes = query.get("codes") if isinstance(query, dict) else (query or {})

        trace_id = payload.get("trace_id") or ""
        primary_care = bool(payload.get("primary_care", True))

        decision = decide_action(cfg, logic, codes or {}, primary_care=primary_care)
        if decision["action"] == "ASK_MORE" and int(query.get("clarify_round", 0)) >= cfg.max_clarify_rounds:
            decision = {"action": "ABSTAIN", "reason": "已达追问轮次上限，信息仍不足，请转人工复核",
                        "source": "guards.max_clarify_rounds", "decided_by": "clarify_cap", "overridden": True}

        # 依据候选：把"为什么选它"写成人话，而不是只给一个分数
        basis = []
        for c in cands[:3]:
            dv = c.get("dim_verdicts") or {}
            matched = [d for d, v in dv.items() if v == "MATCH"]
            if len(matched) == len(DIMS):
                why = "五维完全一致"
            elif matched:
                why = "一致的维度：%s" % "、".join(matched)
            else:
                why = "文本相近但五维画像不一致（作为对比项）"
            basis.append({
                "region_id": c.get("region_id"),
                "rank": c.get("rank"),
                "score": c.get("score"),
                "consistency_score": c.get("consistency_score"),
                "why": why,
                "profile_display": c.get("profile_display"),
                "source": c.get("source"),
            })

        provenance = {
            "threshold_ver": cfg.threshold_ver,
            "rule_ver": cfg.rule_ver,
            "model_ver": cfg.model_ver,
            "config_fingerprint": cfg.fingerprint,
            "data_version": "v2.2.0",
            "trace_id": trace_id,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        retrieval_context = query.get('retrieval_context') or {}
        if retrieval_context.get('used'):
            provenance['model_ver'] = 'FLAIR@' + retrieval_context['model_revision'][:12]
            provenance['embedding_scope'] = 'whole_image'
            import hashlib
            provenance['retrieval_config_fingerprint'] = hashlib.sha256(json.dumps(multimodal.settings(), sort_keys=True).encode()).hexdigest()[:12]

        card = {
            "action": decision["action"],
            "action_label": ACTION_LABELS.get(decision["action"], decision["action"]),
            "action_reason": decision["reason"],
            "action_source": decision["source"],
            "decided_by": decision["decided_by"],
            "overridden": decision["overridden"],
            "hit_rules": decision.get("hit_rules", []),
            "basis": basis,
            "query_codes": codes,
            "query_uncertain": list(query.get("uncertain") or []),
            "query_negated": list(query.get("negated") or []),
            "logic": {
                "verdict": logic.get("verdict"),
                "sufficiency": logic.get("sufficiency"),
                "conflicts": logic.get("conflicts", []),
                "missing": logic.get("missing", []),
                "per_dimension": logic.get("per_dimension", {}),
                "components": logic.get("components", {}),
            },
            "provenance": provenance,
            "caveats": list(CAVEATS) + (["输入包含不确定描述或图像推测：" + "、".join(query.get("uncertain") or [])]
                                       if query.get("uncertain") else []),
        }
        card["html"] = _render_evidence_html(cfg, card)
        card["elapsed_ms"] = _now_ms(t0)
        return _ok(card)
    except ToolError as e:
        return _fail(e.code, e.message, e.hint)
    except Exception as e:  # noqa: BLE001
        return _fail("INTERNAL", "证据卡生成失败：%s" % e, "检查 logic_result 结构")


def _render_evidence_html(cfg, card: dict) -> str:
    """自包含证据卡（零 CDN、零外链，能离线打开）。"""
    e = _html.escape

    def rows(d: dict):
        return "".join(
            "<tr><td class=dim>%s</td><td>%s</td></tr>"
            % (e(str(k)), e(json.dumps(v, ensure_ascii=False)))
            for k, v in d.items()
        )

    basis_rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
        % (e(str(b.get("rank"))), e(str(b.get("region_id"))), e(str(b.get("score"))), e(str(b.get("why"))))
        for b in card["basis"]
    ) or "<tr><td colspan=4 class=muted>无候选</td></tr>"

    conflict_rows = "".join(
        "<li class=%s>%s</li>" % (e(c.get("severity", "soft")), e(c.get("message", "")))
        for c in card["logic"]["conflicts"]
    ) or "<li class=muted>无冲突</li>"

    caveat_rows = "".join("<li>%s</li>" % e(c) for c in card["caveats"])
    p = card["provenance"]

    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>眼维证据卡 · %(action_label)s</title>
<style>
 body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;margin:0;padding:28px;
      background:#f6f7fb;color:#1b1d29;line-height:1.6}
 .card{max-width:960px;margin:0 auto;background:#fff;border:1px solid #e3e6f0;border-radius:14px;
       padding:26px 30px;box-shadow:0 6px 24px rgba(20,24,60,.06)}
 h1{font-size:20px;margin:0 0 4px}
 .sub{color:#6a7086;font-size:13px;margin-bottom:18px}
 .action{display:inline-block;padding:6px 14px;border-radius:999px;font-weight:700;font-size:15px}
 .a-RETURN_CASES{background:#e6f7ee;color:#0d7a45}
 .a-ASK_MORE{background:#fff5e0;color:#9a6700}
 .a-ABSTAIN{background:#fdecec;color:#b4232b}
 .a-SUGGEST_REFERRAL{background:#f0e9ff;color:#5b34c9}
 .a-RETAKE_IMAGE{background:#eaf1ff;color:#1c4fb8}
 section{margin-top:20px}
 h2{font-size:14px;color:#4a5170;border-left:3px solid #5b6cff;padding-left:8px;margin:0 0 10px}
 table{width:100%%;border-collapse:collapse;font-size:13px}
 th,td{border-bottom:1px solid #eceff6;padding:7px 8px;text-align:left;vertical-align:top}
 th{background:#fafbff;color:#5b6180;font-weight:600}
 td.dim{color:#5b6180;width:120px}
 ul{margin:6px 0;padding-left:20px;font-size:13px}
 li.hard{color:#b4232b} li.soft{color:#9a6700} .muted{color:#9aa0b4}
 .prov{font-size:12px;color:#6a7086;background:#fafbff;border:1px dashed #dfe3ef;border-radius:8px;padding:10px 12px}
 .caveat{font-size:12px;color:#6a7086}
</style></head><body><div class="card">
 <h1>眼底跨模态检索 · 证据卡</h1>
 <div class="sub">trace_id: %(trace)s ｜ 生成于 %(time)s</div>
 <div class="action a-%(action)s">%(action_label)s</div>
 <p style="font-size:14px;margin:12px 0 0">%(reason)s</p>
 <div class="sub" style="margin-top:6px">判定来源：%(source)s ｜ 判定方式：%(decided_by)s%(override)s</div>

 <section><h2>依据候选（为什么是它们）</h2>
  <table><tr><th>名次</th><th>区域</th><th>总分</th><th>依据</th></tr>%(basis)s</table></section>

 <section><h2>五维门禁结论</h2>
  <table><tr><th>冲突</th></tr>%(conflicts)s</table>
  <ul>%(conflict_list)s</ul>
  <table><tr><th>维度</th><th>判定</th></tr>%(perdim)s</table>
  <div class="sub">证据充分性：<b>%(suf)s</b> ｜ 一致性 %(cons)s ｜ 惩罚 %(pen)s ｜ 缺失必备维 %(miss)s</div>
 </section>

 <section><h2>溯源</h2>
  <div class="prov">版本三元组：threshold=<b>%(tv)s</b> ／ rule=<b>%(rv)s</b> ／ model=<b>%(mv)s</b>
   ｜ 配置指纹 %(fp)s ｜ 数据版本 %(dv)s</div>
 </section>

 <section><h2>免责与限制</h2><ul class="caveat">%(caveats)s</ul></section>
</div></body></html>""" % {
        "action": e(card["action"]),
        "action_label": e(card["action_label"]),
        "reason": e(card["action_reason"]),
        "source": e(card["action_source"]),
        "decided_by": e(card["decided_by"]),
        "override": "（已强制改写）" if card["overridden"] else "",
        "basis": basis_rows,
        "conflicts": conflict_rows,
        "conflict_list": conflict_rows,
        "perdim": rows(card["logic"]["per_dimension"]),
        "suf": e(str(card["logic"]["sufficiency"])),
        "cons": e(str(card["logic"]["components"].get("consistency"))),
        "pen": e(str(card["logic"]["components"].get("penalty"))),
        "miss": e(str(card["logic"]["components"].get("missing_required_count"))),
        "tv": e(p["threshold_ver"]), "rv": e(p["rule_ver"]), "mv": e(p["model_ver"]),
        "fp": e(p["config_fingerprint"]), "dv": e(p["data_version"]),
        "trace": e(p["trace_id"] or "-"),
        "time": e(p["generated_at"]),
        "caveats": caveat_rows,
    }


# ================================================================ 工具 5

def record_feedback(payload: dict, rt: Runtime | None = None) -> dict:
    """反馈落隔离区：只追加不覆盖，幂等键防重复点"采纳"污染数据。"""
    t0 = time.perf_counter()
    try:
        if not isinstance(payload, dict):
            raise ToolError("BAD_INPUT", "入参必须是对象", "")
        case_id = _require(payload, "case_id", str)
        verdict = _require(payload, "verdict", str)
        if verdict not in ("ACCEPT", "REJECT", "CORRECT", "UNKNOWN"):
            raise ToolError("BAD_INPUT", "未知 verdict：%s" % verdict,
                            "可选 ACCEPT / REJECT / CORRECT / UNKNOWN")
        note = str(payload.get("note") or "")
        rt = rt or Runtime.get()

        key = payload.get("idempotency_key") or ("%s|%s|%s" % (case_id, verdict, note))
        out_dir = os.path.join(default_output_dir(), "反馈")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, "feedback.jsonl")

        rec = {
            "record_id": "FB-" + time.strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:6],
            "idempotency_key": key,
            "case_id": case_id,
            "verdict": verdict,
            "note": note,
            "trace_id": payload.get("trace_id") or "",
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "provenance": {
                "threshold_ver": rt.cfg.threshold_ver,
                "rule_ver": rt.cfg.rule_ver,
                "model_ver": rt.cfg.model_ver,
            },
        }
        from .feedback_store import append
        try:
            rec, duplicate = append(out_dir, rec)
        except ValueError as exc:
            raise ToolError("BAD_INPUT", str(exc)) from exc
        return _ok({"stored": not duplicate, "duplicate": duplicate, "record_id": rec["record_id"],
                    "path": os.path.relpath(path, os.path.dirname(default_output_dir())),
                    "elapsed_ms": _now_ms(t0)})
    except ToolError as e:
        return _fail(e.code, e.message, e.hint)
    except Exception as e:  # noqa: BLE001
        return _fail("INTERNAL", "反馈落库失败：%s" % e, "检查 04_产物/反馈 目录写权限")


# ================================================================ 统一入口

TOOLS = {
    "extract_medical_entities": extract_medical_entities,
    "search_cases": search_cases,
    "verify_medical_logic": verify_medical_logic,
    "build_evidence_chain": build_evidence_chain,
    "record_feedback": record_feedback,
}


def call_tool(name: str, payload: dict, rt: Runtime | None = None) -> dict:
    """统一调用口：界面 / 编排器 / MCP server 都从这里进。"""
    fn = TOOLS.get(name)
    if fn is None:
        return _fail("BAD_INPUT", "未知工具：%s" % name, "可用工具：%s" % "、".join(TOOLS))
    return fn(payload or {}, rt)
