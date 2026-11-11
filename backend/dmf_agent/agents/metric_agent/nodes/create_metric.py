"""指标创建节点（FR8：自然语言定义新指标 -> 草稿起草）。

流程：LLM 从用户问句提出指标定义草案（MetricDraftProposal）-> 治理六道闸 schema/引用校验
-> 语义注册中心落草稿（status=draft）-> 返回 draft 供用户/管理员走「提交审核 -> 发布」审批流。

设计要点：
- 复用 P1-6 已建好的语义注册中心草稿 -> 六道闸 -> 审批状态机，不重造；
- 仅做 schema + 引用完整性 + DAG 三道可在无数据源时校验的闸（④SQL dry-run /
  ⑤物理列 / ⑥试跑 属发布期 deep 校验，草稿阶段跳过，发布时由 admin 触发）；
- 无 LLM / 校验不通过 / 注册中心未就绪时优雅降级（不落库，返回问题清单请用户补充）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Set, Tuple

from langchain_core.messages import HumanMessage, SystemMessage

from dmf_agent.agents.metric_agent.deps import AgentDeps
from dmf_agent.core.state import AgentState, user_query
from dmf_agent.llm.structured import astructured_invoke
from dmf_agent.schemas.agent import MetricDraftProposal
from dmf_agent.semantic.governance import derive_metric_key, validate_metric_definition

logger = logging.getLogger(__name__)

_CREATE_SYSTEM = (
    "你是指标平台的指标定义起草助手。用户希望新建一个业务指标，请用结构化字段描述其口径。\n"
    "规则：\n"
    "1. simple 指标必须指定 measure（须为语义层已有的度量名，可参考下方候选度量列表）；\n"
    "2. ratio 指标需指定 numerator / denominator（指标名或度量名）；\n"
    "3. derived 指标需给出 expr 与引用的 metrics；\n"
    "4. name 用准确的中文业务名；description 写清口径；unit 无则留空；aliases 可列别名。"
)


def _proposal_to_defn(p: MetricDraftProposal) -> Dict[str, Any]:
    """把 LLM 提案转换为语义注册中心草稿 brief（type_params 归一）。"""
    tp: Dict[str, Any] = {}
    if p.metric_type == "simple":
        tp["measure"] = p.measure
    elif p.metric_type == "ratio":
        tp["numerator"] = p.numerator
        tp["denominator"] = p.denominator
    elif p.metric_type == "derived":
        tp["expr"] = p.expr
        tp["metrics"] = list(p.metrics)
    return {
        "name": p.name,
        "description": p.description,
        "metric_type": p.metric_type,
        "type_params": tp,
        "unit": p.unit,
        "aliases": list(p.aliases),
        "owner": p.owner,
    }


def _layer_refs(layer: Any) -> Tuple[Set[str], Dict[str, Any]]:
    """从语义层抽取校验所需的 measure_keys 与 all_metric_defs。"""
    measure_keys: Set[str] = set()
    all_metric_defs: Dict[str, Any] = {}
    if layer is not None:
        for model in layer.manifest.models:
            for m in model.measures:
                measure_keys.add(m.name)
        for mt in layer.list_metrics():
            all_metric_defs[mt.name] = {
                "metric_type": mt.type,
                "type_params": dict(mt.type_params),
            }
    return measure_keys, all_metric_defs


def build_create_metric_node(deps: AgentDeps):
    async def create_metric(state: AgentState) -> Dict[str, Any]:
        text = user_query(state)
        layer = deps.semantic_layer
        measure_keys, all_metric_defs = _layer_refs(layer)

        llm = deps.get_chat_model()
        if llm is None:
            # 无 LLM：无法自动解释自然语言的口径定义，返回引导信息（不落库）
            logger.info("[mock] create 意图但无 LLM，返回引导信息")
            return {
                "create_result": {
                    "ok": False,
                    "needs_llm": True,
                    "message": (
                        "创建指标需要 LLM 解析您的自然语言口径描述并生成结构化定义草稿。"
                        "当前未配置 LLM，请补充指标名称与口径（如『月活客户数，按交易日统计去重客户数』）。"
                    ),
                }
            }

        # 候选度量/指标作为上下文，约束 LLM 只引用真实存在的对象
        context_lines = []
        if measure_keys:
            context_lines.append("候选度量（measure）：" + "、".join(sorted(measure_keys)[:60]))
        if all_metric_defs:
            context_lines.append("已有指标：" + "、".join(sorted(all_metric_defs)[:60]))
        human = (text or "") + ("\n\n" + "\n".join(context_lines) if context_lines else "")

        try:
            proposal = await astructured_invoke(
                llm,
                MetricDraftProposal,
                [
                    SystemMessage(content=_CREATE_SYSTEM),
                    HumanMessage(content=human),
                ],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("create 意图结构化提案失败：%s", exc)
            return {
                "create_result": {
                    "ok": False,
                    "message": f"无法解析为新指标定义：{exc}",
                }
            }

        defn = _proposal_to_defn(proposal)
        metric_key = derive_metric_key(defn)

        # 六道闸（schema + 引用完整性 + DAG）；④⑤⑥ 属发布期 deep 校验，草稿阶段跳过
        validation = await validate_metric_definition(
            defn,
            metric_key=metric_key,
            all_metric_defs=all_metric_defs,
            measure_keys=measure_keys,
            layer=layer,
            engine=None,
        )
        if not validation.passed:
            logger.info("create 草稿校验未通过：%s", validation.problems)
            return {
                "create_result": {
                    "ok": False,
                    "metric_key": metric_key,
                    "defn": defn,
                    "problems": validation.problems,
                    "warnings": validation.warnings,
                    "message": "指标定义校验未通过，请修正后重试：" + "；".join(validation.problems),
                }
            }

        repo = deps.semantic_registry_repo
        if repo is None:
            return {
                "create_result": {
                    "ok": False,
                    "metric_key": metric_key,
                    "defn": defn,
                    "message": "语义注册中心未就绪，无法保存草稿（请联系管理员）。",
                }
            }

        operator = state.get("user_id") or "system"
        item = await repo.create_metric_draft(defn, operator=operator)
        logger.info("create 指标草稿已落库：%s", item.get("metric_key"))
        # P2-6 兜底创建审计：LLM 生成草稿落库留痕（best-effort，失败不阻断）
        security = getattr(deps, "security", None)
        if security is not None:
            try:
                await security.audit_metric_creation(
                    action="llm_draft",
                    metric_key=str(item.get("metric_key") or metric_key),
                    name=str(item.get("name") or defn.get("name") or ""),
                    user_id=operator,
                    tenant_id=state.get("tenant_id") or "",
                    status=str(item.get("status") or "draft"),
                    version=str(item.get("version") or ""),
                )
            except Exception:  # noqa: BLE001
                logger.warning("指标草稿创建审计写入失败（跳过）", exc_info=True)
        return {
            "create_result": {
                "ok": True,
                "metric_key": item.get("metric_key"),
                "name": item.get("name"),
                "status": item.get("status"),
                "version": item.get("version"),
                "defn": defn,
                "message": (
                    f"已为你生成指标草稿【{item.get('name')}】（key={item.get('metric_key')}，"
                    f"状态 draft）。草稿需经管理员『提交审核 -> 发布』后方可用于查询。"
                ),
            }
        }

    return create_metric
