"""眼维 · 轻编排器（L3）：9 段状态机 + 三道防跑飞保险。

设计对应构建方案 §4 与技术架构审计 v2 的修法：
  · **动作不是"决定"出来的，是算出来的** —— 编排器不判断，只执行；判断在
    mcp_tools.decide_action（确定性、可测试、可复现）；
  · **硬冲突在 GATE 段短路** —— 眼别/模态不可比时由代码强制改写为 ABSTAIN，
    这是把"敢拒答"从文档落成代码的那一步；
  · **三道保险**：①步数与轮次上限 ②会话字符预算 ③每工具超时 —— 参数全部读
    config/thresholds.yaml 的 guards 段，代码里不写死任何一个。

状态机：
  INGEST → QC → PARSE → RETRIEVE → GATE → DECIDE → EVIDENCE → TRACE → FEEDBACK
  （QC 判 RETAKE 时直接出"建议重拍"，不进检索；FEEDBACK 由调用方显式触发）
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time

from . import mcp_tools as MT
from . import imageqc
from . import trace as tracemod
from .config import default_config, default_output_dir

STAGES = ("INGEST", "QC", "PARSE", "RETRIEVE", "GATE", "DECIDE", "EVIDENCE", "TRACE", "FEEDBACK")
_ACTIVE_TOOLS = set()
_ACTIVE_LOCK = threading.Lock()


class BudgetError(Exception):
    """保险触发（步数/预算超限），由编排器捕获并强制收尾。"""


class Guard:
    """三道保险的统一记账处。每触发一次都留痕，供 trace 与界面展示。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.steps = 0
        self.chars = 0
        self.events = []
        self.truncated = False

    # ① 步数
    def spend_step(self, name: str):
        self.steps += 1
        if self.steps > self.cfg.max_steps:
            self.truncated = True
            self.note("guard", "max_steps_exceeded", {"limit": self.cfg.max_steps})
            raise BudgetError("超出全局最大步数 %d" % self.cfg.max_steps)

    # ② 字符预算（token 的近似口径）
    def spend_chars(self, text: str):
        self.chars += len(text or "")
        if self.chars > self.cfg.token_budget:
            self.truncated = True
            self.note("guard", "token_budget_exceeded", {"limit": self.cfg.token_budget, "used": self.chars})
            raise BudgetError("超出会话预算 %d 字符" % self.cfg.token_budget)

    def note(self, kind: str, name: str, detail: dict | None = None):
        self.events.append({"kind": kind, "name": name, "detail": detail or {}})

    def snapshot(self) -> dict:
        return {
            "steps_used": self.steps,
            "steps_limit": self.cfg.max_steps,
            "chars_used": self.chars,
            "chars_limit": self.cfg.token_budget,
            "max_clarify_rounds": self.cfg.max_clarify_rounds,
            "max_retrieval_expansions": self.cfg.max_retrieval_expansions,
            "max_tool_retries": self.cfg.max_tool_retries,
            "truncated": self.truncated,
            "events": self.events,
        }


class Pipeline:
    """轻编排器。一次 run() = 一次完整决策（含留痕）。"""

    def __init__(self, rt=None, trace_dir: str | None = None, artifact_dir: str | None = None):
        self.rt = rt or MT.Runtime.get()
        self.cfg = self.rt.cfg
        out = default_output_dir()
        self.trace_dir = trace_dir or os.path.join(out, "trace")
        self.artifact_dir = artifact_dir or os.path.join(out, "证据卡")

    # ---------------------------------------------------------- ③ 每工具超时
    def _call_tool(self, name: str, payload: dict, guard: Guard) -> tuple:
        """带超时与重试的工具调用。返回 (result, duration_ms)。

        零依赖实现：把调用放进工作线程，主线程 join(timeout)。超时后该线程不再
        阻塞主流程（标记为 daemon），主流程按降级路径继续——**演示时宁可真超时并
        如实标注，也不要卡死**。
        """
        timeout = self.cfg.timeout_ms(name) / 1000.0
        attempts = int(self.cfg.max_tool_retries) + 1
        last = None
        for att in range(1, attempts + 1):
            box: dict = {}
            key = (id(self.rt), name)
            with _ACTIVE_LOCK:
                if key in _ACTIVE_TOOLS:
                    return {"ok": False, "error": {"code": "TOOL_BUSY", "message": "上次任务仍在执行，请稍后再试"}}, None
                _ACTIVE_TOOLS.add(key)

            def target(box=box, key=key):
                try:
                    box["r"] = MT.call_tool(name, payload, self.rt)
                except Exception as exc:  # noqa: BLE001
                    box["e"] = exc
                finally:
                    with _ACTIVE_LOCK:
                        _ACTIVE_TOOLS.discard(key)

            th = threading.Thread(target=target, daemon=True)
            t0 = time.perf_counter()
            th.start()
            th.join(timeout)
            dur = round((time.perf_counter() - t0) * 1000, 2)

            if th.is_alive():
                last = {"ok": False, "error": {"code": "TIMEOUT",
                                               "message": "%s 超时（>%.0fms）" % (name, timeout * 1000),
                                               "hint": "可在 thresholds.yaml#guards.timeout_ms 调整"}}
                guard.note("guard", "tool_timeout", {"tool": name, "attempt": att, "limit_ms": timeout * 1000})
                last["error"]["running"] = True
                last["error"]["hint"] = "任务尚未结束，已禁止重叠重试；此超时不是强制取消"
                return last, dur
            if "e" in box:
                last = {"ok": False, "error": {"code": "INTERNAL", "message": str(box["e"])}}
                guard.note("guard", "tool_error", {"tool": name, "attempt": att, "message": str(box["e"])})
                continue
            res = box.get("r") or {}
            if not res.get("ok") and res.get("error", {}).get("code") in ("BAD_INPUT", "NOT_FOUND"):
                return res, dur          # 入参问题重试无意义，直接返回
            if not res.get("ok"):
                last = res
                guard.note("guard", "tool_degraded", {"tool": name, "attempt": att,
                                                      "error": res.get("error", {})})
                continue
            return res, dur
        return last, None

    # ---------------------------------------------------------- 主流程
    def run(self, query_text: str, image_path: str | None = None, topk: int | None = None,
            mode: str = "standard", clarify_round: int = 0, primary_care: bool = True,
            save_artifacts: bool = True) -> dict:
        t_all = time.perf_counter()
        guard = Guard(self.cfg)
        tr = tracemod.Trace(self.cfg, self.trace_dir, title=(query_text or "")[:40])
        result = {
            "ok": True,
            "trace_id": tr.trace_id,
            "action": None,
            "action_label": None,
            "action_reason": "",
            "decided_by": None,
            "overridden": False,
            "qc": None,
            "parsed": None,
            "candidates": [],
            "logic": None,
            "evidence": None,
            "degradation": None,
            "guards": None,
            "steps": [],
            "artifacts": {},
            "elapsed_ms": None,
        }

        try:
            # ---- ① INGEST
            guard.spend_step("INGEST")
            text = (query_text or "").strip()
            tr.add("note", "INGEST", {"query": text[:120], "image": image_path or "(无)"})
            if not text and not image_path:
                result.update({
                    "action": "ASK_MORE", "action_label": MT.ACTION_LABELS["ASK_MORE"],
                    "action_reason": "输入为空：请描述病灶（例如「右眼黄斑区散在微血管瘤」）",
                    "decided_by": "input_guard",
                })
                self._finish(tr, guard, result, t_all, save_artifacts)
                return result
            guard.spend_chars(text)

            # ---- ② QC（有图才做；RETAKE 直接出建议重拍，不进检索）
            guard.spend_step("QC")
            if image_path:
                qr = imageqc.assess_cached(image_path)
                result["qc"] = qr.to_dict()
                tr.add("tool", "imageqc.assess", {"label": qr.label, "score": qr.score,
                                                  "reasons": qr.reasons})
                if qr.label in ("RETAKE", "UNASSESSED"):
                    result.update({
                        "action": "RETAKE_IMAGE",
                        "action_label": MT.ACTION_LABELS["RETAKE_IMAGE"],
                        "action_reason": "图像质检为 %s：%s（%s）——不进检索，请重拍或改用文本描述"
                                         % (qr.label, "；".join(qr.reasons) or "无说明", qr.source or "-"),
                        "decided_by": "qc_gate",
                    })
                    self._finish(tr, guard, result, t_all, save_artifacts)
                    return result
            else:
                tr.add("note", "QC", {"note": "未提供图像，跳过质检（走纯文本线）"})

            # ---- ③ PARSE
            guard.spend_step("PARSE")
            parsed, dur = self._call_tool("extract_medical_entities", {"text": text}, guard)
            if not parsed or not parsed.get("ok"):
                return self._degrade(result, tr, guard, "PARSE", parsed, t_all, save_artifacts)
            result["parsed"] = parsed
            parsed["clarify_round"] = int(clarify_round)
            conflict = MT.input_conflict_decision(parsed, self.cfg, clarify_round)
            if conflict:
                result.update({'action': conflict['action'],
                               'action_label': MT.ACTION_LABELS[conflict['action']],
                               'action_reason': conflict['reason'], 'decided_by': conflict['decided_by'],
                               'overridden': True})
                tr.add('gate', 'INPUT_CONFLICT', {'conflicts': parsed['input_conflicts']})
                self._finish(tr, guard, result, t_all, save_artifacts)
                return result
            guard.spend_chars(text)
            tr.add("tool", "extract_medical_entities",
                   {"codes": {k: v for k, v in parsed["codes"].items() if v},
                    "missing": parsed["missing"], "negated": parsed["negated"],
                    "uncertain": parsed["uncertain"]}, dur)

            # ---- ④ RETRIEVE（含扩展上限）
            guard.spend_step("RETRIEVE")
            payload = {"mode": mode, "query_text": text, "image_path": image_path}
            if topk is not None:
                payload["topk"] = topk
            searched, dur = self._call_tool("search_cases", payload, guard)
            if not searched or not searched.get("ok"):
                return self._degrade(result, tr, guard, "RETRIEVE", searched, t_all, save_artifacts)
            result["qc"] = result["qc"] or searched.get("qc")
            result["candidates"] = searched.get("candidates", [])
            result["degradation"] = searched.get("degradation")
            result["image_detect"] = searched.get("image_detect")
            result['multimodal'] = searched.get('multimodal')
            parsed['retrieval_context'] = result['multimodal']
            if image_path:
                previous_codes = dict(parsed["codes"])
                parsed["codes"] = searched.get("query_codes", previous_codes)
                inferred = [k for k, v in parsed["codes"].items() if v and not previous_codes.get(k)]
                parsed["image_inferred"] = inferred
                parsed["uncertain"] = list(dict.fromkeys(parsed.get("uncertain", []) + inferred))
            tr.add("tool", "search_cases",
                   {"mode": searched.get("mode"), "count": searched.get("count"),
                    "degradation": result["degradation"],
                    "top": [c["region_id"] for c in result["candidates"][:3]]}, dur)

            # 扩展次数上限：降级重排也算一次扩展，超上限即停（不再循环重试）
            if result["degradation"] and int(self.cfg.max_retrieval_expansions) <= 0:
                guard.note("guard", "expansion_blocked",
                           {"limit": self.cfg.max_retrieval_expansions})

            # ---- ⑤ GATE
            guard.spend_step("GATE")
            required = list(self.cfg.completeness.get("required", ["lesion", "location"]))
            missing = [d for d in required if not parsed["codes"].get(d)]
            if "lesion" in parsed.get("image_inferred", []) and "lesion" not in missing:
                missing.append("lesion_confirmation")
            if missing:
                capped = int(clarify_round) >= self.cfg.max_clarify_rounds
                result.update({"action": "ABSTAIN" if capped else "ASK_MORE",
                               "action_label": MT.ACTION_LABELS["ABSTAIN" if capped else "ASK_MORE"],
                               "action_reason": "已达追问上限，请转人工复核" if capped else
                                   "请补充" + "、".join({"lesion": "病灶或症状", "location": "病灶位置", "lesion_confirmation": "人工确认的病灶描述（不能仅凭图像提示）"}.get(d, d) for d in missing),
                               "decided_by": "clarify_cap" if capped else "deterministic"})
                tr.add("gate", "GATE", {"missing_required": missing, "clarify_round": clarify_round})
                self._finish(tr, guard, result, t_all, save_artifacts)
                return result
            if not result["candidates"]:
                result.update({
                    "action": "ABSTAIN", "action_label": MT.ACTION_LABELS["ABSTAIN"],
                    "action_reason": "检索无候选：库中无可比区域（不编造结果）",
                    "decided_by": "retrieval_empty",
                })
                tr.add("gate", "GATE", {"verdict": "NO_CANDIDATE"})
                self._finish(tr, guard, result, t_all, save_artifacts)
                return result
            logic, dur = self._call_tool(
                "verify_medical_logic",
                {"query": parsed, "candidates": result["candidates"]}, guard)
            if not logic or not logic.get("ok"):
                return self._degrade(result, tr, guard, "GATE", logic, t_all, save_artifacts)
            result["logic"] = logic
            tr.add("gate", "GATE", {"verdict": logic["verdict"], "sufficiency": logic["sufficiency"],
                                    "conflicts": [c["dim"] for c in logic["conflicts"]]}, dur)

            # ---- ⑥ DECIDE（动作由 decide_action 确定性算出，不在此处判断）
            guard.spend_step("DECIDE")
            decision = MT.decide_action(self.cfg, logic, parsed["codes"], primary_care=primary_care)

            # 追问轮次上限：已达上限仍要 ASK_MORE → 降级为 ABSTAIN（不再无限追问）
            if decision["action"] == "ASK_MORE" and int(clarify_round) >= int(self.cfg.max_clarify_rounds):
                guard.note("guard", "clarify_capped",
                           {"round": clarify_round, "limit": self.cfg.max_clarify_rounds})
                decision = {
                    "action": "ABSTAIN",
                    "reason": "已达追问上限（%d 轮）仍不足以给出可比先例，转为拒答"
                              % self.cfg.max_clarify_rounds,
                    "source": "config/thresholds.yaml#guards.max_clarify_rounds",
                    "decided_by": "clarify_cap", "overridden": True,
                }
            tr.add("decision", "DECIDE", {"action": decision["action"],
                                          "decided_by": decision["decided_by"],
                                          "source": decision["source"]})

            # ---- ⑦ EVIDENCE
            guard.spend_step("EVIDENCE")
            ev, dur = self._call_tool(
                "build_evidence_chain",
                {"candidates": result["candidates"], "logic_result": logic,
                 "query": parsed, "trace_id": tr.trace_id, "primary_care": primary_care},
                guard)
            if not ev or not ev.get("ok"):
                return self._degrade(result, tr, guard, "EVIDENCE", ev, t_all, save_artifacts)
            result["evidence"] = ev
            result.update({
                "action": ev["action"], "action_label": ev["action_label"],
                "action_reason": ev["action_reason"], "decided_by": ev["decided_by"],
                "overridden": ev["overridden"],
            })
            guard.spend_chars(json.dumps(result["candidates"][:3], ensure_ascii=False))
            tr.add("tool", "build_evidence_chain",
                   {"action": ev["action"], "basis": len(ev["basis"])}, dur)

        except BudgetError as be:
            result.update({
                "action": result["action"] or "ABSTAIN",
                "action_label": result["action_label"] or MT.ACTION_LABELS["ABSTAIN"],
                "action_reason": (result["action_reason"] or "") + " ｜ 保险触发：%s" % be,
                "decided_by": result["decided_by"] or "guard",
                "overridden": True,
            })
            tr.add("guard", "BUDGET_STOP", {"message": str(be)})
        except Exception as exc:  # noqa: BLE001  编排器是最后一道防线：绝不让异常穿透
            result["ok"] = False
            result["action"] = result["action"] or "ABSTAIN"
            result["action_label"] = result["action_label"] or MT.ACTION_LABELS["ABSTAIN"]
            result["action_reason"] = "编排异常已兜底：%s" % exc
            tr.add("guard", "EXCEPTION", {"message": str(exc)})

        self._finish(tr, guard, result, t_all, save_artifacts)
        return result

    # ---------------------------------------------------------- 收尾与降级
    def _degrade(self, result, tr, guard, stage, tool_res, t_all, save_artifacts) -> dict:
        """工具降级：不假装成功，如实标注是哪一段掉链子。"""
        err = (tool_res or {}).get("error", {}) or {}
        result["ok"] = False
        result["action"] = result["action"] or "ABSTAIN"
        result["action_label"] = result["action_label"] or MT.ACTION_LABELS["ABSTAIN"]
        result["action_reason"] = "工具降级于 %s：%s（%s）" % (stage, err.get("message", "未知"), err.get("code", "-"))
        result["decided_by"] = "degraded"
        tr.add("guard", "DEGRADE", {"stage": stage, "error": err})
        self._finish(tr, guard, result, t_all, save_artifacts)
        return result

    def _finish(self, tr, guard, result, t_all, save_artifacts):
        # ---- ⑧ TRACE
        tr.finalize(result.get("action") or "UNKNOWN")
        try:
            path = tr.save()
            result["artifacts"]["trace"] = path
        except Exception as exc:  # noqa: BLE001
            guard.note("guard", "trace_save_failed", {"message": str(exc)})
        result["steps"] = tr.to_dict()["steps"]
        result["trace_meta"] = tr.to_dict()["meta"]
        result["guards"] = guard.snapshot()
        result["elapsed_ms"] = round((time.perf_counter() - t_all) * 1000, 2)

        # ---- 证据卡落盘（JSON + HTML）
        if save_artifacts and result.get("evidence"):
            try:
                os.makedirs(self.artifact_dir, exist_ok=True)
                base = tr.trace_id
                with open(os.path.join(self.artifact_dir, base + ".json"), "w", encoding="utf-8") as f:
                    json.dump(result["evidence"], f, ensure_ascii=False, indent=2)
                with open(os.path.join(self.artifact_dir, base + ".html"), "w", encoding="utf-8") as f:
                    f.write(result["evidence"]["html"])
                result["artifacts"]["evidence_json"] = os.path.join(self.artifact_dir, base + ".json")
                result["artifacts"]["evidence_html"] = os.path.join(self.artifact_dir, base + ".html")
            except Exception as exc:  # noqa: BLE001
                guard.note("guard", "artifact_save_failed", {"message": str(exc)})


# ---------------------------------------------------------------- 便捷函数

def run_once(query_text: str, **kw) -> dict:
    return Pipeline().run(query_text, **kw)
