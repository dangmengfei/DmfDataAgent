"""分析节点：二次分析判断 + 高级分析 + 统计分析与解读。

迁移自老系统：
- plugin/get_indicators/get_analysis_judgement/if_secondary_analysis.py
  （二次分析判断：聚合/求均值/求和/top-n/占比 -> 触发高级分析）
- plugin/get_indicators/get_advanced_analysis/（AnalysisPrompt 生成
  metric_result/groupby_filed/ranking_method，v2 以结构化输出承载）

取数实现（2026-09 主流程脱离指标平台后）：二次分析与基础取数走同一条
MetricFlow -> SQL -> StarRocks 链路，二次分析的聚合覆盖 / 占比 / 排名 /
top-n / 指标过滤由语义编译器转成 SQL（见 semantic/compiler.py 的 calc 层）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from dmf_agent.agents.metric_agent.deps import AgentDeps
from dmf_agent.agents.metric_agent.empty_trace import diagnose_empty, is_effectively_empty
from dmf_agent.agents.metric_agent.sorting import detect_rank_scope, parse_limit
from dmf_agent.agents.prompt_loader import load_prompt
from dmf_agent.analysis import compute_distribution, compute_stats, format_distribution
from dmf_agent.core.state import AgentState, user_query
from dmf_agent.llm.structured import astructured_invoke
from dmf_agent.schemas.agent import AdvancedAnalysis

logger = logging.getLogger(__name__)

# 需要 calc 层（编译器窗口/聚合覆盖）才触发二次分析的信号：
# 平均/均值 -> agg 覆盖；占比 -> 比例窗口；分布 -> Python 侧分桶（进 calc 只为把数据取全）。
# 注意：最高/最低/TopN/排名/求和/总和 **不在此列**——它们是排序/TopN（FR1 已并入首次取数
# 的 ORDER BY+LIMIT）或默认 sum 聚合，首次取数即可满足，不再二次查询。
_CALC_KEYWORDS = ("平均", "均值", "占比", "分布")


def _needs_secondary_analysis(state: AgentState) -> bool:
    """是否触发二次分析：复杂计算技能，或聚合覆盖（均值）/占比等 calc 层能力。

    纯排名 / TopN / 最高最低 / 求和已由首次取数的排序与默认聚合满足，不再触发。
    """
    if "complex_calculation" in (state.get("skills") or []):
        return True
    text = user_query(state)
    return any(keyword in text for keyword in _CALC_KEYWORDS)


def _extract_limit(text: str) -> Optional[int]:
    """解析 top-n / 前n / 第n 的限制个数（委托共享排序解析，时间表达式不误伤）。"""
    return parse_limit(text)


def _generate_advanced_rules(text: str, elements: Dict[str, Any]) -> Dict[str, Any]:
    """规则推断的二次分析配置（LLM 不可用时的兜底，非假数据）。"""
    metrics = elements.get("metrics") or []
    dims = elements.get("dimensions") or []
    metric_result: List[dict] = []
    for name in metrics:
        cfg = {
            "metric_name": name,
            "agg": "sum",
            "filter_operator": "",
            "condition_value": "",
            "orderby": "",
            "limit": "",
            "proportion": False,
        }
        if any(w in text for w in ("平均", "均值")):
            cfg["agg"] = "avg"
        if "占比" in text:
            cfg["proportion"] = True
        limit = _extract_limit(text)
        if limit:
            # 与聚合覆盖/占比叠加时（如"平均GMV前5"）仍下推排名截断；纯排名不会进到二次分析
            cfg["orderby"] = (
                "desc" if any(w in text for w in ("最高", "最大", "多", "top", "TOP", "前")) else "asc"
            )
            cfg["limit"] = str(limit)
        metric_result.append(cfg)
    groupby_filed = list(dims) if dims else []
    # FR1：排名范围（组内排名 vs 全局排名）交由共享规则判定，"每月排名"不再被当成全局
    rank_scope = detect_rank_scope(text)
    ranking_method = (
        "group"
        if rank_scope == "group"
        else ("global" if any(w in text for w in ("排名", "前", "top", "TOP")) else "")
    )
    return {
        "metric_result": metric_result,
        "groupby_filed": groupby_filed,
        "ranking_method": ranking_method,
    }


def _complete_metric_result(
    config: Dict[str, Any], metrics: List[str]
) -> Dict[str, Any]:
    """补齐 LLM/规则漏掉的指标（老 AnalysisPrompt.check：默认 sum）。"""
    existing = {item["metric_name"] for item in config.get("metric_result", [])}
    for name in metrics:
        if name not in existing:
            config["metric_result"].append(
                {
                    "metric_name": name,
                    "agg": "sum",
                    "filter_operator": "",
                    "condition_value": "",
                    "orderby": "",
                    "limit": "",
                    "proportion": False,
                }
            )
    return config


def _format_adv_input(text: str, elements: Dict[str, Any]) -> str:
    """二次分析 LLM 输入（迁移自老 AnalysisPrompt.get_prompt 的入参段）。"""
    tr = elements.get("time_range") or {}
    return (
        f"用户输入: {text}\n"
        f"指标字段: {elements.get('metrics') or []}\n"
        f"维度字段: {elements.get('dimensions') or []}\n"
        f"时间字段: [{tr.get('granularity') or 'day'}]\n"
        f"输出："
    )


async def _generate_advanced_analysis(
    text: str, elements: Dict[str, Any], deps: AgentDeps
) -> Dict[str, Any]:
    """生成二次分析配置（结构化输出，失败/未配置回退规则）。"""
    llm = deps.get_chat_model()
    if llm is not None:
        try:
            result: AdvancedAnalysis = await astructured_invoke(
                llm,
                AdvancedAnalysis,
                [
                    SystemMessage(content=load_prompt("analysis_adv")["system"]),
                    HumanMessage(content=_format_adv_input(text, elements)),
                ],
            )
            config = result.model_dump()
        except Exception as exc:  # noqa: BLE001 - LLM 失败回退规则
            logger.warning("LLM 二次分析配置失败，回退规则：%s", exc)
            config = _generate_advanced_rules(text, elements)
    else:
        config = _generate_advanced_rules(text, elements)
    return _complete_metric_result(config, elements.get("metrics") or [])


def _to_markdown(query_result: Dict[str, Any]) -> str:
    columns = query_result.get("columns", [])
    rows = query_result.get("rows", [])
    lines = ["| " + " | ".join(str(c) for c in columns) + " |",
             "|" + "---|" * len(columns)]
    for row in rows:
        lines.append("| " + " | ".join(str(v) for v in row) + " |")
    return "\n".join(lines)


def _stats_summary(stats: Dict[str, Dict[str, float]]) -> str:
    """LLM 不可用时的统计摘要（仅描述真实统计量）。"""
    if not stats:
        return "暂无可分析的查询数据。"
    parts = []
    for name, s in stats.items():
        parts.append(
            f"{name}：均值 {s['avg']}，最高 {s['max']}，最低 {s['min']}，合计 {s['total']}"
        )
    return "统计摘要：" + "；".join(parts) + "。"


def build_analyze_data_node(deps: AgentDeps):
    async def analyze_data(state: AgentState) -> Dict[str, Any]:
        query_result = state.get("query_result") or {}
        user_text = user_query(state)
        elements = state.get("query_elements") or {}
        dsl = state.get("dsl") or {}

        # 二次分析：判断 -> 生成配置 -> 二次取数（聚合/排名/占比/指标过滤）
        advanced: Optional[Dict[str, Any]] = None
        advanced_result: Optional[Dict[str, Any]] = None
        if _needs_secondary_analysis(state):
            advanced = await _generate_advanced_analysis(user_text, elements, deps)
            if advanced.get("metric_result") and deps.metric_engine is not None:
                dsl2 = {**dsl, "advanced": advanced}
                try:
                    advanced_result = await deps.metric_engine.query(dsl2)
                except Exception as exc:  # noqa: BLE001 - 二次取数失败降级用基础结果
                    logger.warning("二次取数失败，改用基础取数结果：%s", exc)
                    advanced_result = None
            logger.info("二次分析配置：%s", advanced)

        summary_src = advanced_result or query_result
        stats = compute_stats(summary_src)
        error = summary_src.get("error")
        llm = deps.get_chat_model()

        result: Dict[str, Any] = {
            "stats": stats,
            "secondary_analysis": advanced is not None and bool(advanced.get("metric_result")),
            "advanced": advanced,
            "advanced_result": advanced_result,
        }

        # 分布分析（FR2）：问句提到"分布"且取数成功 -> 对首个指标列做 Python 侧
        # 确定性分桶（先算后说，不做 SQL 下推）。
        # 分桶基于**基础取数结果**（每维度成员一行）；二次分析结果可能按时间重分组，
        # 语义不是"成员取值分布"。样本不足时 compute_distribution 返回 None。
        if "分布" in user_text and not error:
            metrics_names = elements.get("metrics") or []
            result["distribution"] = compute_distribution(
                query_result, metrics_names[0] if metrics_names else None
            )

        # 空数据回溯（PRD FR5）：取数成功但实质为空 -> 6 项原因诊断，绝不静默重试；
        # 时间越界且能给出「最新可得窗口」时，诊断附带 redirect，图随后转 ask_time_redirect
        # 找用户确认。已重定向过一次仍为空（如过滤过严）则不再下发重定向卡片，避免循环。
        # 注意"实质为空"：无 GROUP BY 的聚合在零匹配行时仍返回一行全 NULL（SUM(空)=NULL），
        # 只看 row_count 会把"该时间窗没有数据"误判成有结果，卡片便永不触发。
        if not error and is_effectively_empty(query_result):
            try:
                diag = await diagnose_empty(
                    query_result=query_result,
                    query_elements=elements,
                    dsl=dsl,
                    engine=deps.metric_engine,
                    tenant_id=state.get("tenant_id"),
                    already_redirected=bool(elements.get("time_redirected")),
                )
                result["empty_diagnosis"] = diag.to_dict()
            except Exception as exc:  # noqa: BLE001 - 诊断失败不影响主流程
                logger.warning("空数据回溯诊断失败（降级跳过）：%s", exc)

        if error:
            # 取数失败：不编造结论，如实说明（由答复节点呈现）
            result["summary"] = f"取数未成功，无法分析：{error}"
            return {"analysis": result}

        if llm is not None:
            try:
                context = json.dumps(
                    {
                        "elements": elements,
                        "stats": stats,
                        # 分布分桶为 Python 确定性统计（先算后说），LLM 仅组织语言
                        "distribution": result.get("distribution"),
                    },
                    ensure_ascii=False,
                )
                # 归因意图：分析侧重原因拆解（完整归因下钻链路见 diagnosis 子图）
                diagnosis_hint = (
                    "\n注意：用户意图为指标归因（diagnosis），请侧重对指标变化原因的拆解与假设。"
                    if state.get("intent") == "diagnosis"
                    else ""
                )
                resp = await llm.ainvoke(
                    [
                        SystemMessage(content=load_prompt("analysis")["system"]),
                        HumanMessage(
                            content=f"查询要素与统计量：\n{context}\n\n明细数据：\n{_to_markdown(summary_src)}{diagnosis_hint}"
                        ),
                    ]
                )
                result["summary"] = (
                    resp.content if isinstance(resp.content, str) else str(resp.content)
                )
                return {"analysis": result}
            except Exception as exc:  # noqa: BLE001 - LLM 失败降级统计摘要
                logger.warning("LLM 分析失败，回退统计摘要：%s", exc)

        dist_text = format_distribution(result.get("distribution"))
        result["summary"] = (
            _stats_summary(stats) + (f"\n\n{dist_text}" if dist_text else "")
        )
        return {"analysis": result, "mock_mode": True}

    return analyze_data
