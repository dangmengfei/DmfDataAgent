"""指标取数工具（HM 取数客户端，v1/standard-api/sa/gainData/simplified）。

迁移自老系统：
- plugin/get_indicators/hm_api/hm_api.py::change_to_hm_api（DSL -> 请求体）
  + get_hmApi_analysis（二次分析 metricsConfigList）
- metadata/hm_meta/hm_gain_data.py::HmGainData（请求/响应解析）
- 鉴权头复用 metadata/client.py::build_hm_headers（X_tenant / Access-Token 双模式）

未配置 api_base 时返回确定性 mock 数据，保证框架链路可端到端联调；
配置后失败重试（指数退避），重试仍失败抛 HmDataError，由 execute_query 降级为错误结果。
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import httpx

from dmf_agent.config.settings import Settings
from dmf_agent.integrations.metric_platform.client import HmMetaClient, build_hm_headers
from dmf_agent.integrations.metric_platform.endpoints import hm_path

logger = logging.getLogger(__name__)

# 取数接口路径（统一接口注册表，见 metadata/endpoints.py）
GAIN_DATA_URL = hm_path("gain_data")

# 各时间粒度支持的同环比 lagOffsetType（迁移自老 hm_api.py::STATTIME_2_POP）
STATTIME_2_POP = {
    "day": ["wow", "mom", "qoq", "yoy", "pop"],
    "week": ["mom", "qoq", "yoy", "pop"],
    "month": ["qoq", "yoy", "pop"],
    "quarter": ["yoy", "pop"],
    "year": ["pop"],
}

# 时间粒度字段（二次分析上卷判定，老 prompt_analysis.py::TIME_GRANULARITY）
TIME_GRANULARITIES = ["day", "week", "month", "quarter", "year"]

# HM 空值占位（迁移自老 hm_gain_data.py::udf_convert_to_float）
_EMPTY_PLACEHOLDERS = {"", "--", "-", " "}


class HmDataError(Exception):
    """HM 取数失败（重试耗尽或接口返回异常码）。"""


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# LLM FilterCondition.operator -> 老 KV filter_operator（条件值语义一致，仅格式归一）
_OPERATOR_ALIASES = {
    "=": "=",
    "==": "=",
    "!=": "!=",
    ">": ">",
    "<": "<",
    ">=": ">=",
    "<=": "<=",
    "in": "in",
    "not_in": "not in",
    "not in": "not in",
}


def normalize_filter(item: dict) -> dict:
    """任意形态过滤条件 -> 统一 KV 形式 {dimension, dimension_value, filter_operator}。

    兼容 LLM 的 field/operator/values 与 mock 召回的老 KV 键；
    build_dsl 与语义层适配器共用。
    """
    dim = item.get("field") or item.get("dimension")
    values = item.get("values")
    if values is None:
        values = item.get("dimension_value")
    operator = _OPERATOR_ALIASES.get(str(item.get("operator") or "in"), "in")
    return {
        "dimension": dim,
        "dimension_value": list(values) if values else [],
        "filter_operator": operator,
    }


def _metric_id_map(dsl: Dict[str, Any]) -> Dict[str, int]:
    """DSL 指标 name -> HM id 映射（code 即 HM 指标 id，字符串化）。"""
    return {
        m.get("name"): mid
        for m in dsl.get("metrics") or []
        if (mid := _safe_int(m.get("code"))) is not None
    }


async def load_dim_name2id(client: Optional[HmMetaClient]) -> Dict[str, int]:
    """从 HM 维度元数据构建 dim 中文名 -> id 映射；未配置/失败返回空 dict。"""
    if client is None or not client.configured:
        return {}
    try:
        dims = await client.fetch_dimensions()
    except Exception as exc:  # noqa: BLE001 - 维度元数据失败不阻断取数
        logger.warning("维度元数据拉取失败，维度/过滤降级为不编码：%s", exc)
        return {}
    return {
        d.get("cnName"): d.get("id")
        for d in dims
        if d.get("cnName") and d.get("id") is not None
    }


def build_gain_request(
    dsl: Dict[str, Any],
    dim_name2id: Dict[str, int],
    settings: Settings,
) -> Dict[str, Any]:
    """查询 DSL -> HM 取数请求体（迁移自老 change_to_hm_api + get_api_args 核心）。

    - metrics: [{code, name}] -> chosenMetricsIds（code 即 HM 指标 id，字符串化）
    - dimensions: [name] -> chosenDimensionIds（dim_name2id 映射）
    - time_range -> timePeriodCondition{statTime, beginTime, endTime}
    - filters（已归一为 {dimension, dimension_value, filter_operator}）-> condition.member
    - advanced（二次分析 metric_result/groupby_filed/ranking_method）-> metricsConfigList /
      analysisTimeDimension / analysisDimIds / havingCondition（老 get_hmApi_analysis）
    - period_comparison + period_comparison_type -> precomputationInfos（同环比，服务端计算）
    - 排序走默认规则：时间降序 + 首个指标降序（老 process_orderby_by_rule）
    """
    chosen_metrics_ids: List[int] = []
    for m in dsl.get("metrics") or []:
        mid = _safe_int(m.get("code"))
        if mid is not None:
            chosen_metrics_ids.append(mid)

    chosen_dim_ids: List[int] = []
    for dim in dsl.get("dimensions") or []:
        dim_id = dim_name2id.get(dim)
        if dim_id is not None:
            chosen_dim_ids.append(dim_id)

    time_period = _build_time_period(dsl.get("time_range"))

    body: Dict[str, Any] = {
        "chosenMetricsIds": chosen_metrics_ids,
        "chosenDimensionIds": chosen_dim_ids,
        "timePeriodCondition": time_period,
        "pageSize": settings.hm.page_size,
        "pageNum": 1,
    }

    # 过滤条件 -> condition（member 非空才带上）
    condition = _build_condition(dsl.get("filters") or [], dim_name2id)
    if condition["member"]:
        body["condition"] = condition

    # 二次分析（聚合/排名/占比）-> metricsConfigList 等（老 get_hmApi_analysis）
    advanced_body = _build_advanced_config(dsl, dim_name2id)
    if advanced_body:
        body.update(advanced_body)

    # 排序：默认 时间降序 + 首个指标降序（老 process_orderby_by_rule，最后统一覆盖）
    order_bys = _apply_default_order_bys(body, chosen_metrics_ids)
    if order_bys:
        body["orderBys"] = order_bys

    # 同环比（服务端计算，一次请求返回 base + growthRate/growthValue/baseValue）
    precomputation_infos = _build_precomputation_infos(
        dsl, chosen_metrics_ids, time_period, settings
    )
    if precomputation_infos:
        body["precomputationInfos"] = precomputation_infos

    return body


def _build_time_period(time_range: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """time_range -> timePeriodCondition；起止倒置时自动交换（老 get_hmApi_time）。"""
    tr = time_range or {}
    begin = str(tr.get("start") or "")
    end = str(tr.get("end") or "")
    if begin and end and begin > end:
        begin, end = end, begin
    return {
        "statTime": tr.get("granularity") or "day",
        "beginTime": begin,
        "endTime": end,
    }


def _build_condition(filters: List[dict], dim_name2id: Dict[str, int]) -> Dict[str, Any]:
    """归一化 filters -> condition {son, opType, member:[{id, keys, operator}]}。"""
    condition: Dict[str, Any] = {"son": [], "opType": "AND", "member": []}
    for item in filters:
        dim = item.get("dimension")
        dim_id = dim_name2id.get(dim) if dim else None
        if dim_id is None:
            continue
        condition["member"].append(
            {
                "id": dim_id,
                "keys": item.get("dimension_value") or [],
                "operator": item.get("filter_operator") or "in",
            }
        )
    return condition


def _apply_default_order_bys(
    body: Dict[str, Any], metric_ids: List[int]
) -> List[Dict[str, Any]]:
    """默认排序：时间(dt)降序 + 首个指标降序（老 process_orderby_by_rule）。

    二次分析有排名列时，把首个指标的 indexId 挂到排序上使排名列生效。
    """
    order_bys: List[Dict[str, Any]] = [{"subjectType": "dt", "order": "DESC"}]
    if not metric_ids:
        return order_bys
    first_order: Dict[str, Any] = {
        "subjectType": "metrics",
        "subjectKey": metric_ids[0],
        "order": "DESC",
    }
    for cfg in body.get("metricsConfigList") or []:
        if cfg.get("id") == metric_ids[0]:
            first_order["indexId"] = cfg.get("indexId")
            break
    order_bys.append(first_order)
    return order_bys


def _build_precomputation_infos(
    dsl: Dict[str, Any],
    metric_ids: List[int],
    time_period: Dict[str, Any],
    settings: Settings,
) -> List[Dict[str, Any]]:
    """同环比参数：period_comparison=true 时对每个指标生成 precomputationInfos。"""
    if not dsl.get("period_comparison") or not metric_ids:
        return []
    stat_time = time_period.get("statTime") or "day"
    allowed = STATTIME_2_POP.get(stat_time, [])
    lag = dsl.get("period_comparison_type") or settings.hm.pop_type
    if lag not in allowed:
        lag = "pop"
    return [
        {
            "metricsId": mid,
            "operators": "growthRate,growthValue,baseValue",
            "lagOffsetType": lag,
        }
        for mid in metric_ids
    ]


def _config_exists(configs: List[Dict[str, Any]], new_config: Dict[str, Any]) -> bool:
    """判断配置是否已存在（排除 indexId 比较，老 hm_api.py::is_config_exists）。"""
    keys = [k for k in new_config if k != "indexId"]
    return any(all(cfg.get(k) == new_config[k] for k in keys) for cfg in configs)


def _build_advanced_config(
    dsl: Dict[str, Any], dim_name2id: Dict[str, int]
) -> Dict[str, Any]:
    """二次分析 -> metricsConfigList / analysisTimeDimension / analysisDimIds / havingCondition。

    迁移自老 hm_api.py::get_hmApi_analysis 的 metric_result 分支：
    - metric_result 每项 -> metricsConfigList 配置（countWay 聚合 / proportion 占比 /
      orderby+limit 排名列 rankFunc + havingCondition 截断）
    - groupby_filed -> analysisTimeDimension（时间上卷）与 analysisDimIds
    """
    advanced = dsl.get("advanced") or {}
    metric_result = advanced.get("metric_result") or []
    if not metric_result:
        return {}
    groupby_filed = advanced.get("groupby_filed") or []
    ranking_method = advanced.get("ranking_method") or ""
    metric_name2id = _metric_id_map(dsl)
    stat_time = (dsl.get("time_range") or {}).get("granularity") or "day"

    time_gby = [g for g in groupby_filed if g in TIME_GRANULARITIES]
    if time_gby:
        analysis_time_dimension = {
            "isDimension": True,
            "dateFormat": time_gby[0] if time_gby[0] != stat_time else "",
        }
    else:
        analysis_time_dimension = {"isDimension": False, "dateFormat": ""}
    analysis_dim_ids = [dim_name2id[d] for d in groupby_filed if d in dim_name2id]

    metrics_config: List[Dict[str, Any]] = []
    having_children: List[Dict[str, Any]] = []
    index_id = 1
    for item in metric_result:
        mid = metric_name2id.get(item.get("metric_name"))
        if mid is None:
            continue
        count_way = "percentile_cont" if item.get("agg") == "median" else (item.get("agg") or "sum")
        tmp: Dict[str, Any] = {"id": mid, "countWay": count_way, "indexId": index_id}
        if not _config_exists(metrics_config, tmp):
            metrics_config.append(tmp)
            index_id += 1
            tmp = dict(tmp)
            tmp["indexId"] = index_id  # 预占下一个配置位（老逻辑同款）

        # 占比（groupPercentFunc）
        if item.get("proportion"):
            prop = dict(tmp)
            prop["metricAdvanceFunc"] = {"type": "groupPercentFunc", "groupPercentage": True}
            if not _config_exists(metrics_config, prop):
                metrics_config.append(prop)
                index_id += 1

        # 同环比算子（老 process_other_precomputation 三算子展开暂未迁移，见改造记录）
        if item.get("operators"):
            tmp["operators"] = item.get("operators")
            if item.get("lagOffsetType"):
                allowed = STATTIME_2_POP.get(stat_time, [])
                tmp["lagOffsetType"] = (
                    "pop" if item["lagOffsetType"] not in allowed else item["lagOffsetType"]
                )

        # 排名（rankFunc + havingCondition 截断，top-n）
        if item.get("orderby") and item.get("limit"):
            rank = dict(tmp)
            rank["metricAdvanceFunc"] = {
                "type": "rankFunc",
                "rankMethod": 2 if ranking_method == "group" else 1,
                "sort": 1 if str(item["orderby"]).upper() == "ASC" else 2,
                "sortMethod": 1,
            }
            rank["indexId"] = index_id
            metrics_config.append(rank)
            if item.get("limit"):
                having_children.append(
                    {
                        "opType": "AND",
                        "children": [
                            {
                                "opType": "AND",
                                "baseContent": {
                                    "type": 1,
                                    "id": mid,
                                    "operator": "less_equal_than",
                                    "content": item["limit"],
                                    "indexId": index_id,
                                },
                            }
                        ],
                    }
                )
            index_id += 1

    advanced_body: Dict[str, Any] = {
        "metricsConfigList": metrics_config,
        "analysisTimeDimension": analysis_time_dimension,
        "analysisDimIds": analysis_dim_ids,
    }
    if having_children:
        advanced_body["havingCondition"] = {"opType": "AND", "children": having_children}
    return advanced_body


def _coerce_value(value: Any) -> Any:
    """HM 空值占位 -> None（老 udf_convert_to_float；数值保持 HM 原样 JSON 类型）。"""
    if isinstance(value, str) and value in _EMPTY_PLACEHOLDERS:
        return None
    return value


def parse_gain_response(data: Dict[str, Any]) -> Dict[str, Any]:
    """HM gainData 响应 data 段 -> {columns, rows, total, sql, msg}。

    schema 为 {原始key: 中文列名} 映射（dim0->"门店"、统计时间、指标中文名）；
    列顺序取 schema 顺序且该 key 存在于首行（老 hm_gain_df 同款）。
    """
    rows = data.get("list") or []
    schema = data.get("schema") or {}
    total = int(data.get("total") or 0)
    result: Dict[str, Any] = {
        "columns": [],
        "rows": [],
        "total": total,
        "sql": data.get("sql") or "",
        "msg": data.get("msg") or "",
    }
    if not rows or not schema:
        return result
    first = rows[0] if isinstance(rows[0], dict) else {}
    col_keys = [k for k in schema.keys() if k in first]
    result["columns"] = [schema[k] for k in col_keys]
    result["rows"] = [
        [_coerce_value(row.get(k)) for k in col_keys] if isinstance(row, dict) else []
        for row in rows
    ]
    return result


class HmGainClient:
    """HM 取数客户端；api_base 未配置时由 fetch_metric_data 走 mock。"""

    def __init__(self, settings: Settings) -> None:
        self._base = (settings.hm.api_base or "").rstrip("/")
        self._settings = settings

    @property
    def configured(self) -> bool:
        return bool(self._base)

    async def _post(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """POST gainData，带指数退避重试；成功返回 data 段，重试耗尽抛 HmDataError。"""
        url = f"{self._base}/{GAIN_DATA_URL}"
        headers = build_hm_headers(self._settings)
        timeout = self._settings.hm.timeout
        retry_times = self._settings.hm.retry_times

        payload: Optional[Dict[str, Any]] = None
        for attempt in range(retry_times + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(url, json=body, headers=headers)
                    resp.raise_for_status()
                    payload = resp.json()
                break
            except Exception as exc:  # noqa: BLE001 - 网络/超时/连接类瞬时错误
                if attempt >= retry_times:
                    raise HmDataError(f"HM 取数失败（已重试 {retry_times} 次）：{exc}") from exc
                wait = 0.5 * (attempt + 1)
                logger.warning(
                    "HM 取数失败（第 %d/%d 次，%.1fs 后重试）：%s",
                    attempt + 1,
                    retry_times + 1,
                    wait,
                    exc,
                )
                await asyncio.sleep(wait)

        code = payload.get("code")
        if code not in (200, "00000"):
            msg = payload.get("msg") or ""
            raise HmDataError(f"HM 取数接口返回异常码 {code}：{msg}")
        return payload.get("data") or {}


async def fetch_metric_data(
    dsl: dict[str, Any],
    settings: Settings,
    dim_name2id: Optional[Dict[str, int]] = None,
    client: Optional[HmGainClient] = None,
) -> Dict[str, Any]:
    """执行指标查询，返回 {columns, rows, mock, ...}。

    api_base 未配置时返回 mock；配置后真实取数（dim_name2id 由 execute_query
    从 HM 维度元数据预取，缺失时维度/过滤降级为不编码，指标仍可取数）。
    """
    if not settings.hm.api_base:
        return _mock_query_result(dsl)
    c = client or HmGainClient(settings)
    body = build_gain_request(dsl, dim_name2id or {}, settings)
    data = await c._post(body)
    parsed = parse_gain_response(data)
    return {**parsed, "mock": False, "request": body}


def _mock_query_result(dsl: dict[str, Any]) -> dict[str, Any]:
    """指标平台未配置时的确定性模拟数据（**仅供本接入层自测**）。

    注意：主流程（Agent 取数）已改为 MetricFlow -> StarRocks，不再走本函数；
    保留它是为了在完全没有指标平台与 StarRocks 的环境下验证本包的请求组装逻辑。
    """
    today = date.today()
    dates = [(today - timedelta(days=6 - i)).isoformat() for i in range(7)]
    metrics = dsl.get("metrics") or [{"code": "mock", "name": "示例指标"}]
    columns = ["date"] + [m.get("name", m.get("code", "")) for m in metrics]
    rows = []
    for i, d in enumerate(dates):
        row: list[Any] = [d]
        for j in range(len(metrics)):
            # 确定性的伪随机走势，便于联调时肉眼校验
            row.append(round(1000 + 300 * math.sin(i / 2 + j), 1))
        rows.append(row)
    return {"columns": columns, "rows": rows, "mock": True}


# ---------------------------------------------------------------------------
# 说明：统计辅助（compute_stats / describe_data）已迁至 dmf_agent.analysis.stats，
# 由分析/答复节点使用。此处保留同签名实现，仅为本包自测与历史对照，请勿在
# 主流程中引用（避免两份实现漂移）。
# ---------------------------------------------------------------------------


def compute_stats(query_result: dict[str, Any]) -> dict[str, dict[str, float]]:
    """对查询结果按指标列计算基础统计量。"""
    columns: list[str] = query_result.get("columns", [])
    rows: list[list[Any]] = query_result.get("rows", [])
    stats: dict[str, dict[str, float]] = {}
    for idx, col in enumerate(columns):
        if idx == 0 or col == "date":
            continue
        values = [r[idx] for r in rows if isinstance(r[idx], (int, float))]
        if not values:
            continue
        stats[col] = {
            "total": round(sum(values), 2),
            "avg": round(sum(values) / len(values), 2),
            "max": round(max(values), 2),
            "min": round(min(values), 2),
        }
    return stats


def describe_data(query_result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """描述性统计 + 变异系数（迁移自老 df_analysis_report 的 df.describe() + cv_info）。

    报告生成用：count/mean/std/min/max + cv（|std/mean|，衡量数据波动，
    老系统取 cv 最大的 top5 作为"值得关注的指标"）。
    """
    columns: list[str] = query_result.get("columns", [])
    rows: list[list[Any]] = query_result.get("rows", [])
    result: dict[str, dict[str, Any]] = {}
    for idx, col in enumerate(columns):
        values = [r[idx] for r in rows if isinstance(r[idx], (int, float))]
        if not values:
            continue
        mean = sum(values) / len(values)
        std = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5
        result[col] = {
            "count": len(values),
            "mean": round(mean, 4),
            "std": round(std, 4),
            "min": round(min(values), 4),
            "max": round(max(values), 4),
            "cv": round(abs(std / mean), 4) if mean else 0,
        }
    return result
