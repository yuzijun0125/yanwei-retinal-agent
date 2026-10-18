"""编排轨迹（trace）—— 记录与回放。

设计对应计划书 3.7 与 v2 审计 R5：
  - 每次决策"调了哪些工具、依据哪些证据、命中哪条规则"全程留痕；
  - 每条 trace 头部必须带 **版本三元组**（threshold_ver / rule_ver / model_ver），
    否则冻结 schema 后改一个字段名，历史录屏就再也放不出来。
"""

from __future__ import annotations

import json
import os
import time
import uuid


class Trace:
    def __init__(self, config, trace_dir: str, title: str = ""):
        self.cfg = config
        self.dir = trace_dir
        os.makedirs(self.dir, exist_ok=True)
        self.trace_id = "T" + time.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.title = title
        self.steps = []
        self._t0 = time.time()
        self.meta = {
            "trace_id": self.trace_id,
            "schema_version": "trace/1.0",
            "title": title,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "threshold_ver": config.threshold_ver,
            "rule_ver": config.rule_ver,
            "model_ver": config.model_ver,
            "config_fingerprint": config.fingerprint,
        }

    def add(self, kind: str, name: str, detail: dict = None, duration_ms: float = None):
        self.steps.append(
            {
                "seq": len(self.steps) + 1,
                "kind": kind,                       # tool / gate / decision / guard / note
                "name": name,
                "detail": detail or {},
                "duration_ms": round(duration_ms, 2) if duration_ms is not None else None,
                "elapsed_ms": round((time.time() - self._t0) * 1000, 2),
                "at": time.strftime("%H:%M:%S"),
            }
        )
        return self.steps[-1]

    def finalize(self, outcome: str):
        self.meta["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self.meta["wall_ms"] = round((time.time() - self._t0) * 1000, 2)
        self.meta["outcome"] = outcome
        self.meta["steps"] = len(self.steps)

    def to_dict(self):
        return {"meta": self.meta, "steps": self.steps}

    def save(self):
        path = os.path.join(self.dir, self.trace_id + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        with open(os.path.join(self.dir, "index.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(self.meta, ensure_ascii=False) + "\n")
        return path


def load_trace(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def render_timeline(trace: dict) -> str:
    m = trace["meta"]
    lines = [
        "编排轨迹回放 · %s" % m.get("title", ""),
        "trace_id=%s  outcome=%s  步数=%d  总耗时=%sms"
        % (m.get("trace_id"), m.get("outcome"), m.get("steps", 0), m.get("wall_ms")),
        "版本：threshold=%s rule=%s model=%s (指纹 %s)"
        % (m.get("threshold_ver"), m.get("rule_ver"), m.get("model_ver"), m.get("config_fingerprint")),
        "-" * 78,
    ]
    for s in trace["steps"]:
        dur = "" if s.get("duration_ms") is None else "  %sms" % s["duration_ms"]
        lines.append("[%d] %-8s %-28s%s" % (s["seq"], s["kind"], s["name"], dur))
        for k, v in (s.get("detail") or {}).items():
            lines.append("      %s: %s" % (k, v))
    return "\n".join(lines)
