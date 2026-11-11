"""分布分析（FR2）：指标取值的等宽分桶直方统计。

「各门店 GMV 的分布」= 把某指标在维度成员上的取值分桶计数。分桶是纯展示层
统计（明细行数受 MAX_ROWS 限制），在取数后的 Python 侧确定性完成（先算后说），
不做 SQL 下推。只依赖 `{columns, rows}` 契约，不感知数据来源。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# 少于该行数不做分布（样本过少直方图无意义）
_MIN_ROWS = 3


def _fmt_value(value: float) -> str:
    """量纲友好的数值标签：≥1亿用亿，≥1万用万，≥1千用k，否则原值（至多 2 位小数）。"""
    abs_v = abs(value)
    if abs_v >= 1e8:
        text = f"{value / 1e8:.2f}".rstrip("0").rstrip(".")
        return f"{text}亿"
    if abs_v >= 1e4:
        text = f"{value / 1e4:.2f}".rstrip("0").rstrip(".")
        return f"{text}万"
    if abs_v >= 1e3:
        text = f"{value / 1e3:.1f}".rstrip("0").rstrip(".")
        return f"{text}k"
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}"


def compute_distribution(
    query_result: Dict[str, Any],
    metric_name: Optional[str] = None,
    bins: int = 8,
) -> Optional[Dict[str, Any]]:
    """对结果集中的指标列做等宽分桶计数。

    - metric_name 指定列名时取该列；缺省时取**首个数值列**（列名与指标名对齐，
      编译器输出的指标列名即业务指标名）；
    - 等宽分桶 min→max 共 bins 桶，末桶闭区间（含 max）；min==max 时单桶；
    - 空结果 / 无数值列 / 行数不足 _MIN_ROWS 时返回 None（调用方跳过分布段）。
    """
    columns: List[str] = query_result.get("columns", [])
    rows: List[List[Any]] = query_result.get("rows", [])
    if not columns or len(rows) < _MIN_ROWS:
        return None

    # 定位目标列：优先指定指标名，否则首个存在数值的列
    col_idx: Optional[int] = None
    col_name: str = ""
    if metric_name:
        for idx, col in enumerate(columns):
            if str(col) == metric_name:
                col_idx, col_name = idx, str(col)
                break
    if col_idx is None:
        for idx, col in enumerate(columns):
            sample = [
                r[idx] for r in rows if idx < len(r) and isinstance(r[idx], (int, float))
            ]
            if sample:
                col_idx, col_name = idx, str(col)
                break
    if col_idx is None:
        return None

    values = [
        float(r[col_idx])
        for r in rows
        if col_idx < len(r) and isinstance(r[col_idx], (int, float))
    ]
    if len(values) < _MIN_ROWS:
        return None

    lo, hi = min(values), max(values)
    n_bins = max(1, int(bins))
    if lo == hi:
        # 全部同值：单桶
        bucket_edges = [lo, hi]
        counts = [len(values)]
    else:
        width = (hi - lo) / n_bins
        bucket_edges = [lo + i * width for i in range(n_bins + 1)]
        bucket_edges[-1] = hi  # 修正浮点累计误差
        counts = [0] * n_bins
        for v in values:
            idx = int((v - lo) / width)
            if idx >= n_bins:  # v == hi 落末桶
                idx = n_bins - 1
            counts[idx] += 1

    bin_items: List[Dict[str, Any]] = []
    for i, count in enumerate(counts):
        b_lo, b_hi = bucket_edges[i], bucket_edges[i + 1]
        label = (
            f"{_fmt_value(b_lo)}~{_fmt_value(b_hi)}"
            if b_lo != b_hi
            else f"={_fmt_value(b_lo)}"
        )
        bin_items.append(
            {"label": label, "count": count, "lo": round(b_lo, 4), "hi": round(b_hi, 4)}
        )

    return {
        "metric": col_name,
        "bins": bin_items,
        "bin_count": len(bin_items),
        "total_count": len(values),
        "min": round(lo, 4),
        "max": round(hi, 4),
    }


def format_distribution(dist: Optional[Dict[str, Any]]) -> str:
    """分布结果的确定性文本（LLM 不可用时模板答复用；仅描述真实统计）。"""
    if not dist or not dist.get("bins"):
        return ""
    lines = [f"{dist['metric']} 分布（共 {dist['total_count']} 个取值，范围 "
             f"{_fmt_value(dist['min'])} ~ {_fmt_value(dist['max'])}）："]
    for b in dist["bins"]:
        lines.append(f"- {b['label']}：{b['count']} 个")
    return "\n".join(lines)
