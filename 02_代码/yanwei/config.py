"""配置装载 —— thresholds.yaml 是决策阈值的唯一真源。"""

from __future__ import annotations

import hashlib
import os

from . import miniyaml


class Config:
    """装载 config/ 下的三张表，并提供带默认值的读取方式。"""

    def __init__(self, config_dir: str):
        self.dir = os.path.abspath(config_dir)
        self.thresholds = miniyaml.load(os.path.join(self.dir, "thresholds.yaml"))
        self.rules = miniyaml.load(os.path.join(self.dir, "rules_medical.yaml"))
        self.dispatch = miniyaml.load(os.path.join(self.dir, "model_dispatch.yaml"))
        self.threshold_ver = str(self.thresholds.get("version", "v0"))
        self.rule_ver = str(self.rules.get("version", "v0"))
        self.model_ver = str(self.thresholds.get("model_ver", "none"))
        # 用于证据卡/缓存 key：三张表的联合指纹
        import json
        self.fingerprint = hashlib.sha256(json.dumps(
            {"thresholds": self.thresholds, "rules": self.rules, "dispatch": self.dispatch},
            ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()[:12]

    # --- 取阈值的统一入口（禁止在别处硬编码） ---
    def get(self, path: str, default=None):
        node = self.thresholds
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def sufficiency_return(self) -> float:
        return float(self.get("sufficiency.return_cases", 0.80))

    @property
    def sufficiency_ask(self) -> float:
        return float(self.get("sufficiency.ask_more", 0.50))

    @property
    def weights(self) -> dict:
        return dict(self.get("weights", {}))

    @property
    def rerank_w_retrieval(self) -> float:
        return float(self.get("rerank.w_retrieval", 0.55))

    @property
    def rerank_w_consistency(self) -> float:
        return float(self.get("rerank.w_consistency", 0.45))

    # --- 三道防跑飞保险（参数唯一来源：thresholds.yaml 的 guards 段）---
    @property
    def guards(self) -> dict:
        return dict(self.get("guards", {}))

    @property
    def max_steps(self) -> int:
        return int(self.get("guards.max_steps", 12))

    @property
    def token_budget(self) -> int:
        return int(self.get("guards.token_budget", 24000))

    @property
    def max_clarify_rounds(self) -> int:
        return int(self.get("guards.max_clarify_rounds", 2))

    @property
    def max_retrieval_expansions(self) -> int:
        return int(self.get("guards.max_retrieval_expansions", 2))

    @property
    def max_tool_retries(self) -> int:
        return int(self.get("guards.max_tool_retries", 1))

    def timeout_ms(self, tool: str) -> int:
        return int(self.get("guards.timeout_ms." + tool, 5000))

    @property
    def referral_severity_high(self) -> list:
        return list(self.get("referral.severity_high", ["MARKED", "SEVERE"]))

    @property
    def referral_dr_grade_min(self) -> int:
        return int(self.get("referral.dr_grade_min", 3))

    @property
    def completeness(self) -> dict:
        return dict(self.rules.get("completeness", {}))

    @property
    def conflict_policy(self) -> dict:
        return dict(self.rules.get("conflict_policy", {}))

    def display(self, dim: str, code: str) -> str:
        return str(self.rules.get("display", {}).get(dim, {}).get(code, code))


_DEFAULT_CONFIG_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config")
)


def default_config() -> Config:
    return Config(_DEFAULT_CONFIG_DIR)


def default_data_dir() -> str:
    return os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "01_数据集")
    )


def default_output_dir() -> str:
    return os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "04_产物")
    )
