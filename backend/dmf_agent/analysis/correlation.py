"""相关性分析模型（analysis_correlation，2026-11-18 P1）。

指标两两 Pearson（线性）/ Spearman（秩）相关，输出相关矩阵 + 强相关对提示。
只依赖 `{columns, rows}`；时间列自动排除。

**职责边界（方案 §9.2）**：本模型输出「数值相关强度」（序列口径）；归因节点的
`metric_relations` 输出「变化方向关系」（期间口径）。答复 prompt 已写死：相关 ≠ 因果，
本模型的结论不得改写为"X 导致 Y"。

n < 10 时给出 warning（相关性估计不稳）；n < 3 返回 ok=false。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple

from dmf_agent.analysis.base import (
    AnalysisResult,
    AnalysisModel,
    column_values,
    numeric_columns,
    register_model,
)

_MIN_PAIRS = 3
_STABLE_N = 10
# 强相关阈值（|r| 与 |ρ| 同用）
_STRONG = 0.7


def _pearson(x: List[float], y: List[float]) -> float:
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = math.sqrt(sum((a - mx) ** 2 for a in x))
    syy = math.sqrt(sum((b - my) ** 2 for b in y))
    if not sxx or not syy:
        return 0.0
    return max(-1.0, min(1.0, sxy / (sxx * syy)))


def _ranks(values: List[float]) -> List[float]:
    """平均秩（并列值取平均，Spearman 标准做法）。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def _spearman(x: List[float], y: List[float]) -> float:
    return _pearson(_ranks(x), _ranks(y))


class CorrelationModel(AnalysisModel):
    name = "analysis_correlation"
    description = (
        "相关性分析：计算多个指标两两之间的 Pearson（线性）与 Spearman（秩）相关系数，"
        "输出相关矩阵与强相关对。适用于「指标之间有没有关系/哪个指标和它最相关」类问题；"
        "相关 ≠ 因果，需要 ≥3 行数据。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "metrics": {
                "type": "array",
                "description": "目标数值列名列表；缺省取全部数值列（≤6 个，防矩阵爆炸）",
            },
            "method": {
                "type": "string",
                "enum": ["pearson", "spearman"],
                "description": "主指标（默认 pearson，两种都会计算）",
            },
        },
    }

    def analyze(self, query_result: Dict[str, Any], params: Dict[str, Any]) -> AnalysisResult:
        columns = numeric_columns(query_result)
        requested = [str(m) for m in (params.get("metrics") or []) if str(m) in columns]
        metrics = requested or columns[:6]
        if len(metrics) < 2:
            return AnalysisResult(
                ok=False,
                model=self.name,
                summary="",
                error=f"可分析数值列不足（{len(metrics)} 个，相关性分析需要 ≥2 个指标）",
            )
        series: Dict[str, List[float]] = {m: column_values(query_result, m) for m in metrics}
        n = min(len(v) for v in series.values())
        if n < _MIN_PAIRS:
            return AnalysisResult(
                ok=False,
                model=self.name,
                summary="",
                error=f"样本不足（最短序列 {n} 行，相关性分析需要 ≥{_MIN_PAIRS}）",
            )
        trimmed = {m: v[:n] for m, v in series.items()}

        method = str(params.get("method") or "pearson")
        fn = _spearman if method == "spearman" else _pearson
        matrix: Dict[str, Dict[str, float]] = {}
        pairs: List[Tuple[str, str, float]] = []
        for a in metrics:
            matrix[a] = {}
            for b in metrics:
                r = round(fn(trimmed[a], trimmed[b]), 4)
                matrix[a][b] = r
                if a < b and abs(r) >= _STRONG:
                    pairs.append((a, b, r))
        pairs.sort(key=lambda p: abs(p[2]), reverse=True)

        summary = (
            f"{len(metrics)} 个指标两两相关（{method}，n={n}）："
            + ("；".join(f"{a} 与 {b} r={r}" for a, b, r in pairs[:3]) if pairs else "未发现强相关对")
            + "。"
        )
        insight_lines = [summary]
        for a, b, r in pairs[:3]:
            direction = "正相关" if r > 0 else "负相关"
            insight_lines.append(
                f"{a} 与 {b} 呈{direction}（r={r}）——仅表示数值同涨同跌的关联，不是因果结论。"
            )
        warnings: List[str] = []
        if n < _STABLE_N:
            warnings.append(f"样本仅 {n} 行，相关系数估计不稳定，结论仅供参考。")
        for m in metrics:
            vals = trimmed[m]
            if max(vals) == min(vals):
                warnings.append(f"指标[{m}]取值恒定，其相关系数无意义。")
        return AnalysisResult(
            ok=True,
            model=self.name,
            summary=summary,
            data={
                "type": "correlation",
                "method": method,
                "n": n,
                "metrics": metrics,
                "matrix": matrix,
                "strong_pairs": [
                    {"a": a, "b": b, "r": r} for a, b, r in pairs
                ],
            },
            insight_lines=insight_lines,
            warnings=warnings,
        )


correlation_model = CorrelationModel()
register_model(correlation_model)
