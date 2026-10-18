"""中文分词与术语归一（零依赖）。

策略：
  1) 拉丁字/数字按词切分；
  2) 汉字同时产出 unigram 与 bigram（中文无空格，bigram 足以支撑稀疏检索）；
  3) 术语表把中文表面形式映射为标准枚举码，让"微血管瘤"与"MA"能互相命中。
"""

from __future__ import annotations

import re

_WORD = re.compile(r"[a-zA-Z0-9]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_BAD = re.compile(r"[^\w\u4e00-\u9fff]+")


def _flatten_lexicon(lexicon: dict) -> dict:
    """把 {类别: {中文: 码}} 压平成 {中文: 码}。"""
    flat = {}
    for group in (lexicon or {}).values():
        if isinstance(group, dict):
            for k, v in group.items():
                flat[str(k)] = str(v)
    return flat


class Tokenizer:
    def __init__(self, lexicon: dict | None = None):
        self.lex = _flatten_lexicon(lexicon or {})
        # 长词优先，避免"黄斑中心"被"黄斑区"抢先匹配
        self._terms = sorted(self.lex.keys(), key=len, reverse=True)

    def normalize(self, text: str) -> str:
        """把中文表面形式替换为标准码，返回归一后的文本。"""
        if not text:
            return ""
        out = text
        for term in self._terms:
            if term in out:
                out = out.replace(term, " " + self.lex[term] + " ")
        return _BAD.sub(" ", out)

    def tokens(self, text: str) -> list:
        if not text:
            return []
        t = self.normalize(text).lower()
        toks = _WORD.findall(t)
        cjk = _CJK.findall(t)
        toks.extend(cjk)
        toks.extend(cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1))
        return toks

    def extract_codes(self, text: str) -> set:
        """从文本里抽出命中的标准枚举码（用于五维解析）。"""
        hits = set()
        if not text:
            return hits
        for term in self._terms:
            if term in text:
                hits.add(self.lex[term])
        return hits
