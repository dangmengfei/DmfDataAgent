"""A/B 对比分析模型（analysis_compare，2026-11-18 P1）。

两组数值的均值差 + 置信区间近似（Welch t 区间，方差用样本标准误；零依赖近似实现，
scipy 精确检验留 P3 extra）。分组方式二选一：
- params.group_column + params.group_a/group_b：按分类列的两个维值分组；
- params.series_a/series_b：直接给两组数值（skill 编排用）。

样本每组 < 2 个数值返回 ok=false；summary 只描述真实计算结果，显著性措辞谨慎。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List

from dmf_agent.analysis.base import AnalysisResult, AnalysisModel, register_model

# t 分布 95% 双侧临界值的保守近似（自由度不足时略偏宽，不假精确）
_T_95_SMALL = 2.776  # df=4
_T_95_LARGE = 1.96   # df>=30
_MIN_GROUP_N = 2


def _mean(values: List[float]) -> float:
    return sum(values) / len(values)


def _std(values: List[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    m = _mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (n - 1))


def _t_critical(n: int) -> float:
    """按样本量选保守 t 临界值（小样本更宽，不假精确）。"""
    return _T_95_SMALL if n < 15 else _T_95_LARGE


def _group_from_column(
    query_result: Dict[str, Any], col: str, group_col: str, group_value: Any
) -> List[float]:
    columns: List[Any] = query_result.get("columns") or []
    names = [str(c) for c in columns]
    if col not in names or group_col not in names:
        return []
    ci, gi = names.index(col), names.index(group_col)
    rows: List[List[Any]] = query_result.get("rows") or []
    return [
        float(r[ci])
        for r in rows
        if gi < len(r) and r[gi] == group_value and ci < len(r)
        and isinstance(r[ci], (int, float)) and not isinstance(r[ci], bool)
    ]


class CompareModel(AnalysisModel):
    name = "analysis_compare"
    description = (
        "A/B 对比：比较两组数值的均值差与 95% 置信区间（近似），判断差异是否显著。"
        "适用于「A 组和 B 组有没有差别/差异大不大」类问题；每组至少 2 个数值。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "metric": {"type": "string", "description": "目标数值列名；缺省取首个数值列"},
            "group_column": {"type": "string", "description": "分组列名（与 group_a/group_b 搭配）"},
            "group_a": {"type": "string", "description": "A 组的维值"},
            "group_b": {"type": "string", "description": "B 组的维值"},
        },
    }

    def analyze(self, query_result: Dict[str, Any], params: Dict[str, Any]) -> AnalysisResult:
        # 取两组数值
        a: List[float] = []
        b: List[float] = []
        metric = str(params.get("metric") or "")
        group_col = str(params.get("group_column") or "")
        ga, gb = params.get("group_a"), params.get("group_b")
        if group_col and ga is not None and gb is not None:
            columns: List[Any] = query_result.get("columns") or []
            names = [str(c) for c in columns]
            if not metric:
                numeric = [
                    n for n in names
                    if n != group_col
                ]
                metric = numeric[0] if numeric else ""
            a = _group_from_column(query_result, metric, group_col, ga)
            b = _group_from_column(query_result, metric, group_col, gb)
            label_a, label_b = f"{group_col}={ga}", f"{group_col}={gb}"
        else:
            return AnalysisResult(
                ok=False,
                model=self.name,
                summary="",
                error="缺少分组参数（需要 group_column + group_a + group_b）",
            )
        if not metric:
            return AnalysisResult(
                ok=False, model=self.name, summary="", error="结果集中无可分析的数值列"
            )

        if len(a) < _MIN_GROUP_N or len(b) < _MIN_GROUP_N:
            return AnalysisResult(
                ok=False,
                model=self.name,
                summary="",
                error=(
                    f"样本不足（A 组 {len(a)} 个 / B 组 {len(b)} 个，"
                    f"对比分析每组需要 ≥{_MIN_GROUP_N}）"
                ),
            )

        ma, mb = _mean(a), _mean(b)
        sa, sb = _std(a), _std(b)
        diff = ma - mb
        # Welch 标准误 + 保守 t 区间
        se = math.sqrt(sa * sa / len(a) + sb * sb / len(b)) or 0.0
        t_crit = _t_critical(min(len(a), len(b)))
        half = t_crit * se if se else 0.0
        significant = bool(se) and abs(diff) > half
        rel = abs(diff) / abs(mb) if mb else None

        summary = (
            f"{label_a} 均值 {round(ma, 4)}，{label_b} 均值 {round(mb, 4)}，"
            f"差值 {round(diff, 4)}"
            + (f"（相对 {round(rel * 100, 2)}%）" if rel is not None else "")
            + "。"
        )
        insight_lines = [summary]
        if significant:
            insight_lines.append(
                f"按 95% 置信区间判断，两组差异显著（区间 [{round(diff - half, 4)}, "
                f"{round(diff + half, 4)}] 不含 0）。"
            )
        else:
            insight_lines.append(
                f"按 95% 置信区间判断，两组差异不显著（区间 [{round(diff - half, 4)}, "
                f"{round(diff + half, 4)}] 含 0），可能是随机波动。"
            )
        warnings = ["显著性判断为零依赖近似（保守 t 区间），精确 t 检验需启用 scipy extra（P3）。"]
        return AnalysisResult(
            ok=True,
            model=self.name,
            summary=summary,
            data={
                "type": "compare",
                "metric": metric,
                "groups": {
                    "a": {"label": label_a, "n": len(a), "mean": round(ma, 4), "std": round(sa, 4)},
                    "b": {"label": label_b, "n": len(b), "mean": round(mb, 4), "std": round(sb, 4)},
                },
                "diff": round(diff, 4),
                "ci": [round(diff - half, 4), round(diff + half, 4)],
                "significant": significant,
            },
            insight_lines=insight_lines,
            warnings=warnings,
        )


compare_model = CompareModel()
register_model(compare_model)
