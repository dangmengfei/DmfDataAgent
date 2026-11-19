"""帕累托分析模型（analysis_pareto，2026-11-18 P1）。

「二八分析」：把某指标在维度成员上的取值降序排列，算累计占比，找出贡献 80% 的头部成员。
只依赖 `{columns, rows}`；params 指定 metric（数值列）与 dimension（分类列），缺省取
首个数值列 + 首个非时间分类列。

前 K 个成员累计占比首次 ≥ 80% 时 K 即头部规模；summary 只描述真实计算结果。
"""

from __future__ import annotations

from typing import Any, Dict, List

from dmf_agent.analysis.base import (
    AnalysisResult,
    AnalysisModel,
    is_time_column,
    numeric_columns,
    register_model,
)

_MIN_ROWS = 3


class ParetoModel(AnalysisModel):
    name = "analysis_pareto"
    description = (
        "帕累托（二八）分析：按维度成员对指标降序排列并算累计占比，找出贡献 80% 的头部成员。"
        "适用于「哪些门店/渠道贡献了大部分 GMV/头部集中度」类问题；需要 ≥3 行分组数据。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "metric": {"type": "string", "description": "目标数值列名；缺省取首个数值列"},
            "dimension": {"type": "string", "description": "分类列名；缺省取首个非时间分类列"},
            "threshold": {"type": "number", "description": "累计占比阈值（默认 0.8）"},
        },
    }

    def analyze(self, query_result: Dict[str, Any], params: Dict[str, Any]) -> AnalysisResult:
        columns: List[Any] = query_result.get("columns") or []
        rows: List[List[Any]] = query_result.get("rows") or []
        if not columns or len(rows) < _MIN_ROWS:
            return AnalysisResult(
                ok=False,
                model=self.name,
                summary="",
                error=f"样本不足（{len(rows)} 行，帕累托分析需要 ≥{_MIN_ROWS} 行分组数据）",
            )

        metric = str(params.get("metric") or "")
        if not metric:
            cols = numeric_columns(query_result)
            if not cols:
                return AnalysisResult(
                    ok=False, model=self.name, summary="", error="结果集中无可分析的数值列"
                )
            metric = cols[0]
        m_idx = [str(c) for c in columns].index(metric) if metric in [str(c) for c in columns] else -1
        if m_idx < 0:
            return AnalysisResult(
                ok=False, model=self.name, summary="", error=f"数值列[{metric}]不在结果集中"
            )

        dimension = str(params.get("dimension") or "")
        if not dimension:
            for i, c in enumerate(columns):
                name = str(c)
                if name == metric or is_time_column(name, [r[i] for r in rows if i < len(r)]):
                    continue
                dimension = name
                break
        d_idx = [str(c) for c in columns].index(dimension) if dimension in [str(c) for c in columns] else -1
        if d_idx < 0:
            return AnalysisResult(
                ok=False, model=self.name, summary="", error="结果集中无可用的分类维度列"
            )

        # 按维值聚合（结果可能同一维值多行，如多时间点）
        agg: Dict[Any, float] = {}
        for r in rows:
            if d_idx >= len(r):
                continue
            key = r[d_idx]
            v = r[m_idx]
            if key is None or not isinstance(v, (int, float)) or isinstance(v, bool):
                continue
            agg[key] = agg.get(key, 0.0) + float(v)
        if len(agg) < _MIN_ROWS:
            return AnalysisResult(
                ok=False,
                model=self.name,
                summary="",
                error=f"有效分组不足（{len(agg)} 个，需要 ≥{_MIN_ROWS}）",
            )

        total = sum(agg.values())
        if total == 0:
            return AnalysisResult(
                ok=False, model=self.name, summary="", error="指标合计为 0，无法计算占比"
            )

        threshold = float(params.get("threshold") or 0.8)
        ordered = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)
        cumulative = 0.0
        head_count = len(ordered)  # 全部加总必然 ≥ 阈值
        items: List[Dict[str, Any]] = []
        for i, (value, amount) in enumerate(ordered):
            cumulative += amount
            share = amount / total
            items.append(
                {
                    "value": value,
                    "amount": round(amount, 4),
                    "share": round(share, 4),
                    "cumulative_share": round(cumulative / total, 4),
                }
            )
            if cumulative / total >= threshold and head_count == len(ordered):
                head_count = i + 1

        head_share = round(sum(i["share"] for i in items[:head_count]), 4)
        summary = (
            f"{metric} 按 {dimension} 的帕累托分析：共 {len(ordered)} 个成员，"
            f"前 {head_count} 个（{'、'.join(str(i['value']) for i in items[: min(head_count, 3)])}"
            f"{'等' if head_count > 3 else ''}）贡献了 {round(head_share * 100, 2)}% 的总量。"
        )
        insight_lines = [summary]
        tail_share = round(1 - head_share, 4)
        if len(ordered) - head_count > 0:
            insight_lines.append(
                f"其余 {len(ordered) - head_count} 个长尾成员合计仅贡献 {round(tail_share * 100, 2)}%。"
            )
        if head_count / len(ordered) <= 0.3:
            insight_lines.append("头部集中度高（≤30% 的成员贡献 80% 产出），符合二八分布。")
        return AnalysisResult(
            ok=True,
            model=self.name,
            summary=summary,
            data={
                "type": "pareto",
                "metric": metric,
                "dimension": dimension,
                "threshold": threshold,
                "head_count": head_count,
                "total": round(total, 4),
                "items": items[:50],  # 大基数维度截断（明细进 trace 前还有统一裁剪）
            },
            insight_lines=insight_lines,
            warnings=[],
        )


pareto_model = ParetoModel()
register_model(pareto_model)
