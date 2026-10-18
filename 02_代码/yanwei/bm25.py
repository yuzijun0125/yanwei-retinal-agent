"""BM25 稀疏检索（Okapi BM25，零训练基线）。

这是计划书里 SparseRerank / text_encode 的**可替换插槽的第一版实现**：
接口固定，后续换成向量编码只改 config/model_dispatch.yaml 与这里的内部实现。
"""

from __future__ import annotations

import math
from collections import Counter


class BM25Index:
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.doc_ids: list = []
        self.doc_tokens: list = []
        self.doc_len: list = []
        self.tf: list = []
        self.df: dict = {}
        self.avgdl: float = 0.0
        self._tokens_fn = None

    def build(self, items, tokens_fn):
        """items: [(doc_id, raw_text)]；tokens_fn: raw_text -> [token]。"""
        self._tokens_fn = tokens_fn
        self.doc_ids = []
        self.doc_tokens = []
        self.doc_len = []
        self.tf = []
        self.df = {}
        for doc_id, text in items:
            toks = tokens_fn(text or "")
            self.doc_ids.append(doc_id)
            self.doc_tokens.append(toks)
            self.doc_len.append(len(toks))
            tf = Counter(toks)
            self.tf.append(tf)
            for term in tf:
                self.df[term] = self.df.get(term, 0) + 1
        n = len(self.doc_ids)
        self.avgdl = (sum(self.doc_len) / n) if n else 0.0
        return self

    def _idf(self, term: str) -> float:
        n = len(self.doc_ids)
        df = self.df.get(term, 0)
        # BM25+ 平滑，避免负 idf
        return math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    def search(self, query: str, topk: int = 30) -> list:
        """返回 [(doc_id, score)]，按分数降序。"""
        if not self.doc_ids or not query:
            return []
        terms = self._tokens_fn(query)
        if not terms:
            return []
        scores = [0.0] * len(self.doc_ids)
        for term in set(terms):
            idf = self._idf(term)
            if idf <= 0:
                continue
            for i, tf in enumerate(self.tf):
                f = tf.get(term)
                if not f:
                    continue
                dl = self.doc_len[i] or 1
                denom = f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
                scores[i] += idf * f * (self.k1 + 1) / denom
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        out = [(self.doc_ids[i], scores[i]) for i in ranked[:topk] if scores[i] > 0]
        return out

    def max_score(self, query: str) -> float:
        hits = self.search(query, topk=1)
        return hits[0][1] if hits else 0.0
