"""趋势分析模型（analysis_trend，2026-11-18 P1）。

算法（纯 Python）：一元线性回归斜率 + R² + 移动平均(默认 7 期) + 连涨/连跌/平稳分段。
只依赖 `{columns, rows}`；时间列自动识别，取第一个数值列（或 params.metric 指定列）。

样本 < 3 个时间点返回 ok=false（无法拟合趋势）；summary 只描述真实计算结果。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from dmf_agent.analysis.base import (
    AnalysisResult,
    AnalysisModel,
    column_values,
    numeric_columns,
    register_model,
)

_MIN_POINTS = 3


def _linreg(values: List[float]) -> Tuple[float, float]:
    """一元线性回归：返回 (slope, intercept)。"""
    n = len(values)
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(values) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, values))
    slope = sxy / sxx if sxx else 0.0
    return slope, my - slope * mx


def _r_squared(values: List[float], slope: float, intercept: float) -> float:
    n = len(values)
    mean = sum(values) / n
    ss_tot = sum((y - mean) ** 2 for y in values)
    if ss_tot == 0:
        return 0.0
    ss_res = sum((y - (slope * i + intercept)) ** 2 for i, y in enumerate(values))
    return max(0.0, 1 - ss_res / ss_tot)


def _moving_average(values: List[float], window: int) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    for i in range(len(values)):
        if i + 1 < window:
            out.append(None)
            continue
        out.append(round(sum(values[i + 1 - window : i + 1]) / window, 4))
    return out


def _segments(values: List[float], tol_ratio: float = 0.02) -> List[Dict[str, Any]]:
    """按相邻变化切分段：连续同向（涨/跌）或平稳（变化 < tol×量级）合并为一段。"""
    if not values:
        return []
    scale = max(abs(v) for v in values) or 1.0
    tol = tol_ratio * scale
    segs: List[Dict[str, Any]] = []
    # 段类型：up / down / flat，由该段内相邻变化的多数方向决定
    def _seg_kind(a: float, b: float) -> str:
        if b - a > tol:
            return "up"
        if a - b > tol:
            return "down"
        return "flat"

    start = 0
    kind = None
    for i in range(1, len(values)):
        k = _seg_kind(values[i - 1], values[i])
        if kind is None:
            kind = k
            start = i - 1
        elif k != kind:
            segs.append(
                {
                    "start": start,
                    "end": i - 1,
                    "kind": kind,
                    "change": round(values[i - 1] - values[start], 4),
                }
            )
            kind = k
            start = i - 1
    segs.append(
        {
            "start": start,
            "end": len(values) - 1,
            "kind": kind or "flat",
            "change": round(values[-1] - values[start], 4),
        }
    )
    return segs


class TrendModel(AnalysisModel):
    name = "analysis_trend"
    description = (
        "趋势分析：对时间序列做线性回归（方向/斜率/R²）、移动平均与涨跌分段。"
        "适用于「走势如何/是涨是跌/趋势稳不稳」类问题；需要 ≥3 个时间点。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "metric": {"type": "string", "description": "目标数值列名；缺省取首个数值列"},
            "window": {"type": "integer", "description": "移动平均窗口（默认 7）"},
        },
    }

    def analyze(self, query_result: Dict[str, Any], params: Dict[str, Any]) -> AnalysisResult:
        warnings: List[str] = []
        metric = str(params.get("metric") or "")
        if not metric:
            cols = numeric_columns(query_result)
            if not cols:
                return AnalysisResult(
                    ok=False, model=self.name, summary="", error="结果集中无可分析的数值列"
                )
            metric = cols[0]
        values = column_values(query_result, metric)
        if len(values) < _MIN_POINTS:
            return AnalysisResult(
                ok=False,
                model=self.name,
                summary="",
                error=f"样本不足（{len(values)} 个时间点，趋势分析需要 ≥{_MIN_POINTS}）",
            )
        slope, intercept = _linreg(values)
        r2 = _r_squared(values, slope, intercept)
        window = int(params.get("window") or 7)
        ma = _moving_average(values, max(2, window))
        segs = _segments(values)
        direction = "上升" if slope > 0 else ("下降" if slope < 0 else "平稳")
        strength = "强" if r2 >= 0.6 else ("中" if r2 >= 0.3 else "弱")
        summary = (
            f"{metric} 趋势{direction}（线性斜率 {round(slope, 4)}/期，拟合优度 R²={round(r2, 4)}，"
            f"{strength}拟合），共 {len(values)} 期，分 {len(segs)} 段。"
        )
        insight_lines = [summary]
        major = [s for s in segs if s["kind"] != "flat"]
        for s in major[:3]:
            insight_lines.append(
                f"第 {s['start'] + 1}~{s['end'] + 1} 期持续{'上升' if s['kind'] == 'up' else '下降'}"
                f"（累计 {s['change']}）"
            )
        if r2 < 0.3:
            warnings.append("线性拟合优度较低，序列波动大，趋势结论仅供参考。")
        return AnalysisResult(
            ok=True,
            model=self.name,
            summary=summary,
            data={
                "type": "line",
                "metric": metric,
                "values": [round(v, 4) for v in values],
                "moving_average": ma,
                "slope": round(slope, 4),
                "r2": round(r2, 4),
                "segments": segs,
            },
            insight_lines=insight_lines,
            warnings=warnings,
        )


trend_model = TrendModel()
register_model(trend_model)
