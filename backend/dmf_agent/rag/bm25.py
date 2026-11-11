"""Okapi BM25 关键词检索（纯 Python，无外部依赖）。

迁移自老系统：plugin/utils/bm25_recall.py（知识库 + 候选召回共用 BM25）。
v2 用于知识检索的混合召回——与向量检索做 RRF 融合；Embedding 未配置时可纯 BM25 检索
（知识问答离线可用，对齐老系统 BM25 为主的知识检索）。
"""

from __future__ import annotations

import math
import re
from typing import Dict, List, Tuple

# 中文按字符 bigram 分词（无分词器依赖；英数按整词）
_HANZI = re.compile(r"[一-鿿]+")
_WORD = re.compile(r"[a-zA-Z0-9_]+")


def _tokenize(text: str) -> List[str]:
    text = text.lower()
    tokens: List[str] = []
    for run in _HANZI.findall(text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    tokens.extend(_WORD.findall(text))
    return tokens


class BM25Index:
    """BM25 索引；docs 为 [(content, source), ...]，fit 后按 query 打分排序。"""

    def __init__(self, docs: List[Tuple[str, str]], k1: float = 1.5, b: float = 0.75) -> None:
        self._docs = docs
        self._k1 = k1
        self._b = b
        self._n = len(docs)
        self._avgdl = 0.0
        self._doc_len: List[int] = []
        self._tf: List[Dict[str, int]] = []
        self._df: Dict[str, int] = {}
        self._fitted = False
        if docs:
            self._fit()

    def _fit(self) -> None:
        for content, _ in self._docs:
            tokens = _tokenize(content)
            self._doc_len.append(len(tokens))
            tf: Dict[str, int] = {}
            for t in tokens:
                tf[t] = tf.get(t, 0) + 1
            self._tf.append(tf)
            for t in set(tf):
                self._df[t] = self._df.get(t, 0) + 1
        self._avgdl = sum(self._doc_len) / self._n if self._n else 0.0
        self._fitted = True

    @property
    def fitted(self) -> bool:
        return self._fitted

    def _score(self, query_tokens: List[str], doc_idx: int) -> float:
        tf = self._tf[doc_idx]
        dl = self._doc_len[doc_idx]
        score = 0.0
        for t in set(query_tokens):
            f = tf.get(t, 0)
            if f == 0:
                continue
            nq = self._df.get(t, 0)
            idf = math.log(1 + (self._n - nq + 0.5) / (nq + 0.5))
            score += idf * (f * (self._k1 + 1)) / (
                f + self._k1 * (1 - self._b + self._b * dl / self._avgdl)
            )
        return score

    def search(self, query: str, k: int) -> List[dict]:
        """返回 [{content, source, score, bm25: True}]，仅保留有命中的文档，按分数降序。"""
        results = []
        for idx, score in self.search_indices(query, k):
            content, source = self._docs[idx]
            results.append(
                {"content": content, "source": source, "score": round(score, 4), "bm25": True}
            )
        return results

    def search_indices(self, query: str, k: int) -> List[Tuple[int, float]]:
        """返回 [(doc_idx, score)]，按分数降序；供上层精确映射回原语料（如经验库）。"""
        if not self._fitted or not query.strip():
            return []
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []
        scores = [self._score(query_tokens, i) for i in range(self._n)]
        ranked = sorted(range(self._n), key=lambda i: -scores[i])
        results = []
        for i in ranked[:k]:
            if scores[i] <= 0:
                break  # 已按分数降序，后续均为 0
            results.append((i, round(scores[i], 4)))
        return results
