"""字符级分词与清洗。

迁移自老系统 plugin/utils/get_common_str.py 的 split_str_by_cn / remove_punctuation。
设计要点：中文**逐字成词**（不做词级分词），英文/数字连续成词——这样在
错别字、缩写、口语化（"近7天GMV"）场景下字符级相似度才有效。
"""

from __future__ import annotations

import re
import string
from typing import List


def remove_punctuation(s: str) -> str:
    """去除中英文标点。"""
    res = re.sub(f"[{re.escape(string.punctuation)}]", "", s)  # 英文标点
    res = re.sub(r"[　-〿＀-￯]+", "", res)  # 中文标点
    return res


def _is_cn_char(ch: str) -> bool:
    """是否中文字符（含全角字母数字）。"""
    return not bool(re.match(r"[^一-龥]", ch)) or ch in "０-９ａ-ｚＡ-Ｚ"  # noqa: RUF001


def split_str_by_cn(s: str) -> List[str]:
    """把字符串切分为 token 列表：中文逐字成词，英文/数字连续成词。

    例："近7天GMV趋势" -> ["近", "7", "天", "GMV", "趋", "势"]（"7"为数字连续，"GMV"为英文连续）
    """
    res: List[str] = []
    n = len(s)
    i = 0  # 前指针
    j = 0  # 后指针
    while j < n:
        j_cn = _is_cn_char(s[j])
        i_cn = _is_cn_char(s[i])
        if not j_cn:  # 英文/数字：连续成词
            if j == n - 1:  # 结束
                res.append(s[i : j + 1])
            elif j_cn != i_cn:  # i、j 类型不同，截断
                res.append(s[i:j])
                i = j
            j += 1
        else:  # 中文字符
            if i_cn:  # i 也是中文，逐字成词
                res.append(s[i : j + 1])
            else:  # i 非中文，先收掉 i 的连续词，再收当前中文字
                res.append(s[i:j])
                res.append(s[j : j + 1])
            j += 1
            i = j
    res = [t for t in res if t != " "]
    return res
