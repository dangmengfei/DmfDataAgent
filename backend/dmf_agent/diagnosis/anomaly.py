"""异动前置判定（2026-11-12 归因优化 P1，纯 Python 确定性计算）。

在维度归因**之前**先回答「这次波动是否值得归因」：
- significant：变化幅度显著超过日常波动，进入完整归因；
- mild：变化幅度偏小，归因结论附带「仅供参考」提示；
- noise：处于正常波动范围，只输出趋势描述、跳过维度归因（避免把随机波动解释成业务原因）；
- insufficient：时间点不足 / 上期无数据等无法判定的情形。

方法（零第三方依赖，与《数据分析能力提升方案》的 analysis_anomaly 工具共用实现）：
- 波动基准 = 时间点环比变化率的**中位数**（抗单点异常；比均值稳健）；
- 判定阈值：显著 = max(2×基准, 2%)，轻度 = max(0.5×基准, 0.5%)——
  基准为 0（极稳定序列）时回落到固定阈值，避免「任何微小变化都被判为显著」；
- 序列 ≥ 8 个点时叠加 3σ 离群点检测（|z| ≥ 2.5），供答复标注异常时间点。
"""

from __future__ import annotations

from statistics import median
from typing import Any, Dict, List, Optional

# 3σ 离群点判定的 z 阈值与最小样本量
_OUTLIER_Z = 2.5
_OUTLIER_MIN_N = 8

# 变化率判定阈值的下限（防止极稳定序列上任何微小变化都判显著）
_SIG_FLOOR = 0.02
_MILD_FLOOR = 0.005


def _daily_fluctuation(values: List[float]) -> float:
    """时间点环比变化率绝对值的中位数（波动基准）；无法计算时为 0。"""
    rates: List[float] = []
    for i in range(1, len(values)):
        prev, curr = values[i - 1], values[i]
        if prev:
            rates.append(abs((curr - prev) / prev))
        elif curr:
            # 从 0 突增至非 0：记为 100% 波动（prev=0 时比率无定义，取保守上限）
            rates.append(1.0)
    return median(rates) if rates else 0.0


def _outliers_3sigma(values: List[float]) -> List[Dict[str, Any]]:
    """3σ 离群点（样本 ≥ 8 且标准差 > 0 时才有意义）。"""
    n = len(values)
    if n < _OUTLIER_MIN_N:
        return []
    mean = sum(values) / n
    std = (sum((v - mean) ** 2 for v in values) / n) ** 0.5
    if std <= 0:
        return []
    out: List[Dict[str, Any]] = []
    for idx, v in enumerate(values):
        z = (v - mean) / std
        if abs(z) >= _OUTLIER_Z:
            out.append({"index": idx, "value": round(v, 4), "z": round(z, 4)})
    return out


def _fmt_pct(rate: float) -> str:
    return f"{round(abs(rate) * 100, 2)}%"


def detect_anomaly(
    values: List[float], change_rate: Optional[float] = None
) -> Dict[str, Any]:
    """对时间点序列做确定性异动判定。

    - values：按时间排序的各时间点指标总量（归因窗口内）；
    - change_rate：本期 vs 上期的整体变化率（上期为 0 时传 None，单独判定）。

    返回 {status, method, score, outliers, message}：
    - status：significant | mild | noise | insufficient
    - method：period-rate（仅期间对比阈值）/ 3sigma+period-rate（含离群点检测）
    - score：|整体变化率|（insufficient 时为 0）
    """
    n = len(values)
    if n < 2:
        return {
            "status": "insufficient",
            "method": "none",
            "score": 0.0,
            "outliers": [],
            "message": "取数范围内不足两个有数据的时间点，无法进行异动判定与归因。",
        }

    # 上期无数据（无法计算变化率）：本期有量即视为显著结构变化
    if change_rate is None:
        has_value = any(v != 0 for v in values)
        return {
            "status": "significant" if has_value else "insufficient",
            "method": "period-rate",
            "score": 0.0,
            "outliers": [],
            "message": (
                "上期无数据，本期为新增量，属显著的结构性变化。"
                if has_value
                else "本期与上期均无有效数据，无法进行异动判定。"
            ),
        }

    base = _daily_fluctuation(values)
    rate = abs(change_rate)
    sig_threshold = max(2 * base, _SIG_FLOOR)
    mild_threshold = max(0.5 * base, _MILD_FLOOR)
    if rate >= sig_threshold:
        status = "significant"
    elif rate >= mild_threshold:
        status = "mild"
    else:
        status = "noise"
    method = "3sigma+period-rate" if n >= _OUTLIER_MIN_N else "period-rate"
    outliers = _outliers_3sigma(values)

    if status == "significant":
        message = (
            f"本期较上期变化 {_fmt_pct(change_rate)}，显著超过日常波动水平"
            f"（约 {_fmt_pct(base)}），适合进行归因分析。"
        )
    elif status == "mild":
        message = (
            f"本期较上期变化 {_fmt_pct(change_rate)}，幅度较小"
            f"（日常波动约 {_fmt_pct(base)}），以下拆解仅供参考。"
        )
    else:
        message = (
            f"本期较上期变化 {_fmt_pct(change_rate)}，处于正常波动范围内"
            f"（日常波动约 {_fmt_pct(base)}），未发现显著异动。"
        )
    return {
        "status": status,
        "method": method,
        "score": round(rate, 4),
        "outliers": outliers,
        "message": message,
    }
