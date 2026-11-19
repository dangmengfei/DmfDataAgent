"""分析模型统一契约（2026-11-18 数据分析能力提升 P1，doc/数据分析能力提升方案.md §3.1）。

每个分析模型 = 一个独立模块 + `AnalysisModel` 实现 + 注册进 `MODEL_REGISTRY`：
- 只依赖 `{columns, rows}` 契约，不感知数据来源（与 stats/distribution 一致）；
- 纯 Python 实现，默认零第三方数值依赖；
- 输出 `summary` 必须只描述真实计算结果（预测类必须标注「估算」）；
- 模型执行异常 / 样本不足返回 `ok=false` + `error/warnings`，绝不中断主链路、绝不编造结论。

统一执行入口见 tools.py（Harness 工具 `run_analysis` 的实现）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

# 时间列与非数值列的判定（与 analysis.stats 同源，公开复用）
from dmf_agent.analysis.stats import is_time_column

__all__ = [
    "AnalysisResult",
    "AnalysisModel",
    "MODEL_REGISTRY",
    "register_model",
    "get_model",
    "model_names",
    "numeric_columns",
    "column_values",
    "is_time_column",
]


@dataclass
class AnalysisResult:
    """分析模型的统一输出（Harness output_schema 强校验的对象）。"""

    ok: bool
    model: str                    # analysis_trend / ...
    summary: str                  # 确定性文本（无 LLM 也能直接答复）
    data: Dict[str, Any] = field(default_factory=dict)   # 结构化结果（前端绘图 spec）
    insight_lines: List[str] = field(default_factory=list)  # 要点（供 LLM 组织语言 / 模板答复）
    warnings: List[str] = field(default_factory=list)   # 样本不足/方法不适用等如实提示
    error: str = ""               # ok=false 时的原因

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "model": self.model,
            "summary": self.summary,
            "data": self.data,
            "insight_lines": list(self.insight_lines),
            "warnings": list(self.warnings),
            "error": self.error,
        }


@runtime_checkable
class AnalysisModel(Protocol):
    """分析模型协议：name/description 喂给 LLM 做选择，input_schema 供参数校验。"""

    name: str
    description: str
    input_schema: Dict[str, Any]

    def analyze(self, query_result: Dict[str, Any], params: Dict[str, Any]) -> AnalysisResult: ...


# ---------------- 注册器 ----------------

MODEL_REGISTRY: Dict[str, AnalysisModel] = {}


def register_model(model: AnalysisModel) -> None:
    """注册分析模型（重名即抛错——注册发生在模块导入期，重名是代码错误）。"""
    if model.name in MODEL_REGISTRY:
        raise ValueError(f"分析模型重名：{model.name}")
    MODEL_REGISTRY[model.name] = model


def get_model(name: str) -> Optional[AnalysisModel]:
    return MODEL_REGISTRY.get(str(name or ""))


def model_names() -> List[str]:
    return list(MODEL_REGISTRY.keys())


# ---------------- 结果集公共工具（各模型模块复用） ----------------


def numeric_columns(query_result: Dict[str, Any]) -> List[str]:
    """取结果集中「存在数值」的非时间列（列名 + 数值样例非空即算）。"""
    columns: List[Any] = query_result.get("columns") or []
    rows: List[List[Any]] = query_result.get("rows") or []
    out: List[str] = []
    for idx, col in enumerate(columns):
        name = str(col)
        values = [r[idx] for r in rows if idx < len(r)]
        if is_time_column(name, values):
            continue
        if any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
            out.append(name)
    return out


def column_values(query_result: Dict[str, Any], col_name: str) -> List[float]:
    """取某列的数值（非数值项跳过）；列不存在返回空列表。"""
    columns: List[Any] = query_result.get("columns") or []
    if col_name not in [str(c) for c in columns]:
        return []
    idx = [str(c) for c in columns].index(col_name)
    rows: List[List[Any]] = query_result.get("rows") or []
    return [
        float(r[idx])
        for r in rows
        if idx < len(r) and isinstance(r[idx], (int, float)) and not isinstance(r[idx], bool)
    ]


def category_column_values(
    query_result: Dict[str, Any], col_name: str
) -> List[List[Any]]:
    """取分类列的 (行序号, 原始值) 列表（帕累托等按维值聚合的模型用）。"""
    columns: List[Any] = query_result.get("columns") or []
    names = [str(c) for c in columns]
    if col_name not in names:
        return []
    idx = names.index(col_name)
    rows: List[List[Any]] = query_result.get("rows") or []
    return [[i, r[idx]] for i, r in enumerate(rows) if idx < len(r) and r[idx] is not None]
