"""容错公共子序列（维度/过滤候选召回用）。

迁移自老系统 plugin/utils/get_common_str.py 的
get_common_str_by_interval（带最大间隔的最长公共子序列），
用于 candidates_dim / candidates_filter 的 mapping 打分。

老实现分两步：get_common_str（字符级"长度>=2 子串"匹配，结果再按
字符逐字与 query_list 对齐） + get_max_length_str（间隔容忍回溯）。
两步叠加对**英文/数字连续成词**（如 "GMV"、"2024"）存在字符级与
token 级错位，导致含拉丁/数字的维度名匹配失效。

v2 改为**单次 token 级 DP**（与 segment.split_str_by_cn 一致），
语义等价：最长公共子序列，匹配 token 在 query 中相邻间隔 <= interval，
且匹配字符长度 < 2 丢弃。对纯中文维度名结果与老实现一致；对含英文/数字
的维度名更正确（老实现会漏配）。
"""

from __future__ import annotations

from dmf_agent.recall.segment import remove_punctuation, split_str_by_cn


def get_common_str_by_interval(query: str, short: str, interval: int = 3) -> str:
    """返回 query 与 short 的最长公共子序列（间隔容忍，长度<2 返回空串）。

    规则（对齐老 get_common_str_by_interval）：
    - 先去除中英文标点，中文逐字、英文/数字连续成词
    - 匹配不区分大小写
    - 相邻匹配 token 在 query 中的间隔 <= interval（老系统 interval=3）
    - 匹配字符长度 < 2 丢弃（单字不构成"子序列"信号；多字符英文/数字 token 有效）
    """
    _query = remove_punctuation(query)
    _short = remove_punctuation(short)
    if len(_query) < len(_short):
        _query, _short = _short, _query
    q = split_str_by_cn(_query)
    s = split_str_by_cn(_short)
    if not q or not s:
        return ""
    n, m = len(q), len(s)
    # dp[i][j]：以 q[i-1] 与 s[j-1] 匹配结尾的最长匹配 token 数；
    # 仅匹配位置非零，便于回溯时在窗口内找前驱。
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    best_len, best_i, best_j = 0, 0, 0
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if q[i - 1].lower() != s[j - 1].lower():
                continue
            prev = 0
            if j > 1:
                lo = max(1, i - interval)
                # i==1 且 j>1 时窗口 range(lo, i) 为空（query 首 token 匹配
                # short 非首 token），视为无前驱
                prev = max(dp[k][j - 1] for k in range(lo, i)) if lo < i else 0
            dp[i][j] = prev + 1
            if dp[i][j] > best_len:
                best_len, best_i, best_j = dp[i][j], i, j
    if best_len == 0:
        return ""
    res = [""] * best_len
    i, j = best_i, best_j
    for length in range(best_len, 0, -1):
        res[length - 1] = s[j - 1]
        if length > 1:
            lo = max(1, i - interval)
            i = max(range(lo, i), key=lambda x: dp[x][j - 1])
            j -= 1
    result = "".join(res)
    # 匹配字符长度 < 2 丢弃（单个中文字符不构成信号；但"GMV"这类
    # 单个多字符 token 属于有效命中，老实现因字符/token 错位会漏配）
    return result if len(result) >= 2 else ""
