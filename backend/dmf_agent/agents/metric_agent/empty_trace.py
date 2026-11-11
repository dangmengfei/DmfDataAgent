"""空数据回溯诊断（PRD FR5 `empty_trace` Skill）。

当取数成功但实质为空（无匹配行、或纯聚合只返回一行全 NULL，无 error）时，
按 PRD 要求的 6 项原因做确定性回溯，给出「数据截至时间」提示与可操作的排查建议，
**绝不静默重试** —— 只把诊断结果回写给答复节点，由用户确认后再决定是否放宽条件重查。

6 项原因（与 PRD FR5 对齐）：
  1. time_out_of_range   时间越界：查询窗口晚于数据最新日期
  2. data_not_updated    数据未更新：数据截至日早于今天，当日数据尚未入库
  3. filter_too_strict   过滤过严：维值过滤条件过窄，把数据全部筛掉
  4. granularity_mismatch 口径粒度不匹配：粗粒度聚合 + 时间窗跨度不足导致分桶为空
  5. row_permission      行级/租户权限过滤：当前账号可见数据范围为空
  6. model_config_anomaly 模型配置异常：底表/时间列缺失或语义配置异常

诊断结果按置信等级排序（high > medium > low），便于答复节点优先呈现最可能原因。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# 置信等级
HIGH = "high"
MEDIUM = "medium"
LOW = "low"
_LEVEL_RANK = {HIGH: 3, MEDIUM: 2, LOW: 1}

# 6 项原因编码
CAUSE_TIME_OUT_OF_RANGE = "time_out_of_range"
CAUSE_DATA_NOT_UPDATED = "data_not_updated"
CAUSE_FILTER_TOO_STRICT = "filter_too_strict"
CAUSE_GRANULARITY_MISMATCH = "granularity_mismatch"
CAUSE_ROW_PERMISSION = "row_permission"
CAUSE_MODEL_CONFIG_ANOMALY = "model_config_anomaly"

_CAUSE_LABELS = {
    CAUSE_TIME_OUT_OF_RANGE: "时间范围越界",
    CAUSE_DATA_NOT_UPDATED: "数据尚未更新",
    CAUSE_FILTER_TOO_STRICT: "过滤条件过严",
    CAUSE_GRANULARITY_MISMATCH: "口径粒度不匹配",
    CAUSE_ROW_PERMISSION: "行级/租户权限过滤",
    CAUSE_MODEL_CONFIG_ANOMALY: "模型配置异常",
}


@dataclass
class EmptyCause:
    code: str
    level: str
    detail: str
    suggestion: str

    @property
    def label(self) -> str:
        return _CAUSE_LABELS.get(self.code, self.code)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "label": self.label,
            "level": self.level,
            "detail": self.detail,
            "suggestion": self.suggestion,
        }


@dataclass
class EmptyDiagnosis:
    is_empty: bool
    data_as_of: Optional[str] = None
    causes: List[EmptyCause] = field(default_factory=list)
    note: str = ""
    # 时间越界时的「改查最新数据」结构化建议（见 compute_time_redirect）；
    # 非时间原因 / 已重定向过一次为 None
    redirect: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_empty": self.is_empty,
            "data_as_of": self.data_as_of,
            "causes": [c.to_dict() for c in self.causes],
            "note": self.note,
            "redirect": self.redirect,
        }


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def is_effectively_empty(query_result: Dict[str, Any]) -> bool:
    """取数结果是否"实质为空"（时间窗内没有数据）。

    SQL 层面 `SUM(...)` 这类无 GROUP BY 的聚合在零匹配行时**仍返回一行全 NULL**
    （`row_count=1, rows=[[None]]`），并非有数据；若只判 `row_count != 0` 会把
    "时间窗内无数据"误判成有结果，导致空数据回溯（FR5 / 改查最新数据卡片）永不触发。
    故：行数为 0，或每一行的每个单元都是 None，都算空。

    含 GROUP BY 的正常分桶查询在无匹配时直接返回 0 行，也走同一判据。
    """
    rows = query_result.get("rows") or []
    if not rows:
        return True
    return all(all(v is None for v in row) for row in rows)


def _granularity_days(granularity: Optional[str]) -> Optional[int]:
    """粒度对应的最小天数跨度（用于判断时间窗是否够分桶）。"""
    return {"day": 1, "week": 7, "month": 28, "quarter": 90, "year": 365}.get(
        (granularity or "").lower()
    )


# 时间窗智能重定向的窗口形态（决定调整策略与前端文案）
REDIRECT_SINGLE = "single"   # 单日查询（start==end）-> 取截至日当天
REDIRECT_PERIOD = "period"   # 本月/本季/本年（起点为月首且 start<=截至日）-> 保留起点截断
REDIRECT_SPAN = "span"       # 近 N 天 / 自定义区间 -> 整体平移保跨度
REDIRECT_FUTURE = "future"   # 整窗都在未来（start>截至日）-> 取截至日当天

_REDIRECT_KIND_LABELS = {
    REDIRECT_SINGLE: "数据截至日当天",
    REDIRECT_PERIOD: "本期截至目前",
    REDIRECT_SPAN: "最近可得区间",
    REDIRECT_FUTURE: "数据截至日当天",
}


def _window_label(kind: str, adjusted: Dict[str, Any], as_of: date) -> str:
    """调整后窗口的人类可读标签（前端按钮 / 确认摘要共用）。"""
    start, end = adjusted["start"], adjusted["end"]
    if kind in (REDIRECT_SINGLE, REDIRECT_FUTURE):
        return f"数据截至日（{as_of.isoformat()}）的最新数据"
    if kind == REDIRECT_PERIOD:
        return f"{start} ~ {end}（本期截至目前）"
    # span：平移后的实际跨度
    s = _parse_date(start)
    span = (as_of - s).days + 1 if s else None
    tail = f"，最近 {span} 天" if span else ""
    return f"{start} ~ {end}（数据截至 {as_of.isoformat()}{tail}）"


def compute_time_redirect(
    time_range: Dict[str, Any],
    data_as_of: Optional[str],
) -> Optional[Dict[str, Any]]:
    """空数据因「查询时间晚于数据截至日」时，计算改查最新数据的建议窗口（纯函数）。

    智能口径（与产品确认）：
    - 单日查询（start==end，晚于截至日）-> 取截至日当天，kind=single；
    - 周期型（start 为月首，含季首/年首）：start<=截至日（如「本月」）-> 保留起点、
      结束日截到截至日，kind=period；start>截至日（如「下月」整窗在未来）-> 取截至日当天，
      kind=future；
    - 其余有界区间（「近 7 天」等，无论起点是否已晚于截至日）-> 整体平移保跨度：
      end'=截至日、start'=start-(end-截至日)，kind=span。

    不适用（无 end / 无 data_as_of / end 未晚于截至日）返回 None。
    """
    start = _parse_date((time_range or {}).get("start"))
    end = _parse_date((time_range or {}).get("end"))
    as_of = _parse_date(data_as_of)
    if end is None or as_of is None or end <= as_of:
        return None
    granularity = (time_range or {}).get("granularity") or "day"
    original = {
        "start": start.isoformat() if start else "",
        "end": end.isoformat(),
        "granularity": granularity,
    }

    if start is None or start == end:
        kind, adj_start = REDIRECT_SINGLE, as_of
    elif start.day == 1 and start > as_of:
        # 整窗在未来的月首周期（如下月/下季）-> 直接取截至日
        kind, adj_start = REDIRECT_FUTURE, as_of
    elif start.day == 1:
        # 本月/本季/本年 -> 保留起点截到截至日
        kind, adj_start = REDIRECT_PERIOD, start
    else:
        # 近 N 天 / 自定义区间（含起点已晚于截至日的滚动窗口）-> 平移保跨度
        kind = REDIRECT_SPAN
        adj_start = start - (end - as_of)

    adjusted = {
        "start": adj_start.isoformat(),
        "end": as_of.isoformat(),
        "granularity": granularity,
    }
    return {
        "data_as_of": as_of.isoformat(),
        "original": original,
        "adjusted": adjusted,
        "window_kind": kind,
        "kind_label": _REDIRECT_KIND_LABELS[kind],
        "label": _window_label(kind, adjusted, as_of),
    }


async def _probe_data_as_of(engine: Any, model_names: List[str]) -> Tuple[Optional[str], bool]:
    """探查数据截至时间；返回 (最新日期, 是否探查失败)。"""
    if engine is None or not hasattr(engine, "data_as_of"):
        return None, False
    try:
        return await engine.data_as_of(model_names or []), False
    except Exception as exc:  # noqa: BLE001
        logger.warning("data_as_of 探查异常：%s", exc)
        return None, True


def _build_causes(
    *,
    time_range: Dict[str, Any],
    filters: List[Dict[str, Any]],
    dimensions: List[str],
    data_as_of: Optional[str],
    probe_failed: bool,
    models: List[str],
    tenant_id: Optional[str],
    today: date,
) -> List[EmptyCause]:
    causes: List[EmptyCause] = []
    start = _parse_date(time_range.get("start"))
    end = _parse_date(time_range.get("end"))
    granularity = time_range.get("granularity")

    # 1. 时间越界：查询结束日 > 数据最新日期
    if end is not None and data_as_of is not None:
        as_of = _parse_date(data_as_of)
        if as_of is not None and end > as_of:
            causes.append(
                EmptyCause(
                    CAUSE_TIME_OUT_OF_RANGE,
                    HIGH,
                    f"查询结束日 {end.isoformat()} 晚于数据最新日期 {as_of.isoformat()}，"
                    "该时间窗内没有数据。",
                    f"把时间窗调整到数据覆盖范围内（建议结束日 ≤ {as_of.isoformat()}）。",
                )
            )

    # 2. 数据未更新：数据截至日 < 今天，且查询覆盖到近期
    if data_as_of is not None:
        as_of = _parse_date(data_as_of)
        if as_of is not None and as_of < today:
            covers_recent = end is None or end >= (today - _days(2))
            if covers_recent:
                causes.append(
                    EmptyCause(
                        CAUSE_DATA_NOT_UPDATED,
                        HIGH,
                        f"数据仅更新至 {as_of.isoformat()}，今天（{today.isoformat()}）"
                        "的数据尚未入库，近期日期查不到。",
                        f"确认数据刷新任务是否完成；可先查询 {as_of.isoformat()} 及之前的日期。",
                    )
                )

    # 3. 过滤过严：存在维值过滤条件（非仅分组维度）可能把数据全部筛掉
    value_filters = [f for f in (filters or []) if f.get("values")]
    if value_filters:
        fields = "、".join(str(f.get("field")) for f in value_filters[:5])
        causes.append(
            EmptyCause(
                CAUSE_FILTER_TOO_STRICT,
                MEDIUM,
                f"命中 {len(value_filters)} 个维值过滤条件（{fields} 等），"
                "可能把符合条件的数据全部筛掉。",
                f"尝试去掉部分过滤条件（如 {value_filters[0].get('field')}）后重试。",
            )
        )

    # 4. 口径粒度不匹配：粗粒度聚合 + 时间窗跨度不足导致分桶为空
    if start is not None and end is not None and granularity:
        span = (end - start).days + 1
        need = _granularity_days(granularity)
        if need and span < need:
            causes.append(
                EmptyCause(
                    CAUSE_GRANULARITY_MISMATCH,
                    MEDIUM,
                    f"时间窗 {start.isoformat()}~{end.isoformat()} 仅 {span} 天，"
                    f"按「{granularity}」粒度聚合会分桶为空。",
                    f"改用更细粒度（如 day）或把时间窗扩大到至少 {need} 天。",
                )
            )

    # 5. 行级/租户权限过滤：仅当存在租户上下文且其它高优原因未命中时作为排查项
    if tenant_id and not any(c.code in (CAUSE_TIME_OUT_OF_RANGE, CAUSE_DATA_NOT_UPDATED) for c in causes):
        causes.append(
            EmptyCause(
                CAUSE_ROW_PERMISSION,
                LOW,
                f"当前账号（租户 {tenant_id}）可见的数据范围可能为空，"
                "行级/租户权限把数据全部过滤掉。",
                "确认当前账号对该指标可见的数据范围（维度值权限是否配置）。",
            )
        )

    # 6. 模型配置异常：数据截至时间探查失败（表/时间列/语义配置异常）
    if probe_failed or (data_as_of is None and models):
        model_txt = "、".join(models[:3])
        causes.append(
            EmptyCause(
                CAUSE_MODEL_CONFIG_ANOMALY,
                MEDIUM,
                "无法探测数据截至时间，语义模型对应的底表可能未建、时间列缺失或语义配置异常"
                f"（涉及模型：{model_txt}）。",
                f"检查语义清单中 {model_txt} 的 table 与时间维度配置，确认底表已建且已灌数。",
            )
        )

    # 兜底：没有任何高/中优原因时，给一个通用排查项，避免"空诊断"
    if not causes:
        causes.append(
            EmptyCause(
                CAUSE_MODEL_CONFIG_ANOMALY,
                LOW,
                "未命中明确的空数据原因，可能是底表无对应数据或查询条件组合异常。",
                "建议确认指标/维度/时间窗组合，或检查底表是否已灌入数据。",
            )
        )

    causes.sort(key=lambda c: _LEVEL_RANK.get(c.level, 0), reverse=True)
    return causes


def _days(n: int) -> object:
    from datetime import timedelta

    return timedelta(days=n)


async def diagnose_empty(
    *,
    query_result: Dict[str, Any],
    query_elements: Dict[str, Any],
    dsl: Dict[str, Any],
    engine: Any = None,
    tenant_id: Optional[str] = None,
    today: Optional[date] = None,
    already_redirected: bool = False,
) -> EmptyDiagnosis:
    """空数据回溯诊断入口。

    入参：
    - query_result: execute_query 节点的产出（含 columns/rows/error/models）
    - query_elements: extract_elements 产出（含 time_range/filters/dimensions）
    - dsl: 取数 DSL（备用）
    - engine: 语义引擎（需具备 data_as_of 方法，可空）
    - tenant_id: 当前租户（行权限排查用，可空）
    - today: 注入用「今天」，便于测试
    - already_redirected: 上一轮已做过「改查最新数据」（用户确认后的续跑），
      本次仍为空则不再下发重定向卡片，避免循环澄清。

    返回 EmptyDiagnosis：is_empty=False 表示"非空集/有错误"，无需回溯。
    """
    # 取数失败不属于"空数据"，交给错误分析链路，不在此诊断
    if query_result.get("error"):
        return EmptyDiagnosis(is_empty=False, note="取数返回错误，非空数据回溯范畴。")
    if not is_effectively_empty(query_result):
        return EmptyDiagnosis(is_empty=False, note="取数有结果，无需空数据回溯。")

    today = today or date.today()
    models = query_result.get("models") or []
    data_as_of, probe_failed = await _probe_data_as_of(engine, models)

    tr = (query_elements.get("time_range") or {}) if isinstance(query_elements, dict) else {}
    filters = query_elements.get("filters") or []
    dimensions = query_elements.get("dimensions") or []

    causes = _build_causes(
        time_range=tr,
        filters=filters,
        dimensions=dimensions,
        data_as_of=data_as_of,
        probe_failed=probe_failed,
        models=models,
        tenant_id=tenant_id,
        today=today,
    )

    # 时间越界（查询结束日晚于截至日）-> 给「改查最新数据」结构化建议；
    # 仅 data_not_updated 而 end 未越界（如无界查询）不重定向（compute 内亦有同口径兜底）。
    redirect: Optional[Dict[str, Any]] = None
    if not already_redirected and any(
        c.code in (CAUSE_TIME_OUT_OF_RANGE, CAUSE_DATA_NOT_UPDATED) for c in causes
    ):
        redirect = compute_time_redirect(tr, data_as_of)

    note = "取数成功但结果为空，已执行 6 项空数据回溯（不静默重试，请用户确认后重查）。"
    return EmptyDiagnosis(
        is_empty=True,
        data_as_of=data_as_of,
        causes=causes,
        note=note,
        redirect=redirect,
    )


__all__ = [
    "EmptyCause",
    "EmptyDiagnosis",
    "diagnose_empty",
    "compute_time_redirect",
    "is_effectively_empty",
    "CAUSE_TIME_OUT_OF_RANGE",
    "CAUSE_DATA_NOT_UPDATED",
    "CAUSE_FILTER_TOO_STRICT",
    "CAUSE_GRANULARITY_MISMATCH",
    "CAUSE_ROW_PERMISSION",
    "CAUSE_MODEL_CONFIG_ANOMALY",
]
