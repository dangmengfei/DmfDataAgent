"""异动检测模型（analysis_anomaly，2026-11-18 P1）。

**包装** [diagnosis/anomaly.py](../diagnosis/anomaly.py) 的 `detect_anomaly`（归因 P1 的
前置判定实现），提供纯序列入口——两处共用同一算法，口径永不漂移（方案 §9.1）。
样本 < 2 个时间点返回 ok=false；3σ 离群点需样本 ≥ 8（与归因口径一致）。
"""

from __future__ import annotations

from typing import Any, Dict, List

from dmf_agent.analysis.base import (
    AnalysisResult,
    AnalysisModel,
    column_values,
    numeric_columns,
    register_model,
)
from dmf_agent.diagnosis.anomaly import detect_anomaly

_MIN_POINTS = 2


class AnomalyModel(AnalysisModel):
    name = "analysis_anomaly"
    description = (
        "异动检测：对时间序列判定波动是否显著（显著/轻度/正常/样本不足），"
        "并给出 3σ 离群点列表。适用于「哪些天异常/有没有突增突降/波动正常吗」类问题；"
        "需要 ≥2 个时间点，离群点检测需 ≥8 个。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "metric": {"type": "string", "description": "目标数值列名；缺省取首个数值列"},
        },
    }

    def analyze(self, query_result: Dict[str, Any], params: Dict[str, Any]) -> AnalysisResult:
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
                error=f"样本不足（{len(values)} 个时间点，异动检测需要 ≥{_MIN_POINTS}）",
            )
        result = detect_anomaly(values, change_rate=None)
        # change_rate=None 时 detect_anomaly 走「结构变化」分支；通用异动检测应
        # 对**全序列**判定——这里改用序列自身的整体变化率（首尾均值对比的等价形式：
        # 用末段均值 vs 首段均值），与归因的期间口径同源。
        half = len(values) // 2
        prev_mean = sum(values[:half]) / half if half else 0.0
        curr_values = values[half:]
        curr_mean = sum(curr_values) / len(curr_values) if curr_values else 0.0
        rate = (curr_mean - prev_mean) / prev_mean if prev_mean else None
        if rate is not None:
            result = detect_anomaly(values, change_rate=rate)

        status = result.get("status")
        status_text = {
            "significant": "存在显著异动",
            "mild": "波动幅度较小",
            "noise": "处于正常波动范围",
            "insufficient": "样本不足以判定",
        }.get(status, str(status))
        summary = f"{metric} {status_text}。{result.get('message', '')}"
        insight_lines = [summary]
        data: Dict[str, Any] = {
            "type": "anomaly",
            "metric": metric,
            "status": status,
            "score": result.get("score"),
            "method": result.get("method"),
            "values": [round(v, 4) for v in values],
        }
        outliers = result.get("outliers") or []
        if outliers:
            insight_lines.append(
                "离群点（3σ）："
                + "、".join(f"第 {o['index'] + 1} 期（{o['value']}，z={o['z']}）" for o in outliers[:5])
            )
            data["outliers"] = outliers
        warnings: List[str] = []
        if len(values) < 8:
            warnings.append("样本 < 8 个时间点，未启用 3σ 离群点检测（仅做波动水平判定）。")
        return AnalysisResult(
            ok=True,
            model=self.name,
            summary=summary,
            data=data,
            insight_lines=insight_lines,
            warnings=warnings,
        )


anomaly_model = AnomalyModel()
register_model(anomaly_model)
