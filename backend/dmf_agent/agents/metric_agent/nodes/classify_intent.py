"""意图识别节点。

v2-migration(2026-08-27): 迁移自老系统 planner/intention_recognition.py。
- 老系统输出"插件执行序列"（A取数/B绘图/C解读/D归因/E复杂计算/F维度查询，可重复），
  v2 映射为结构化 IntentResult：intent（路由）+ skills（查询后技能）+ multi_query（多次取数标记）；
- 候选指标注入：老系统 get_metric_candidates（BM25 + AC 自动机召回），当前以指标元数据
  关键词匹配近似（完整召回 Phase 2 迁移）；
- 规则修正（迁移老系统"方案3"与兜底逻辑）：
  1. 单一归因/绘图/解读意图自动补前置取数 -- v2 中 dimension_query/diagnosis 统一路由进
     取数链路，等价实现老系统"分析插件前插 A"；
  2. LLM 判为 chitchat 但 query 与候选指标完全一致 -> 转取数（老 find_max_match 兜底）；
  3. 金融领域关键词兜底 -> 取数（老 check_result）；
- 老系统动态示例召回（DynamicExamplePrompt 经验库）由 experience.store.ExampleStore 承接（BM25 召回）；
- 老系统 intention_message（多次取数提示）-> state.multi_query，由 ChatService 落入 trace。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from dmf_agent.agents.metric_agent.deps import AgentDeps
from dmf_agent.agents.prompt_loader import load_prompt
from dmf_agent.core.state import AgentState, user_query
from dmf_agent.llm.structured import astructured_invoke
from dmf_agent.harness import enforce_output_schema
from dmf_agent.memory.retriever import MemoryRetriever, MemoryRerankConfig
from dmf_agent.schemas.agent import IntentResult

logger = logging.getLogger(__name__)

# 老系统 check_result 的金融领域关键词（兜底触发取数链路）
_FINANCE_KEYWORDS = (
    "存款", "贷款", "余额", "税", "收入", "支出", "利息", "息差", "利润", "成本",
    "资产", "资金", "负债", "拨备", "科目", "客户", "融资", "基金", "货币", "债券",
)

_KNOWLEDGE_WORDS = ("什么是", "是什么", "有哪些", "怎么", "如何", "为什么", "文档", "制度",
                    "流程", "规范", "策略", "介绍", "说明", "解释", "口径", "定义")
_METRIC_WORDS = ("多少", "趋势", "同比", "环比", "增长", "下降", "统计", "查询", "查一下",
                 "看看", "指标", "排名", "占比", "gmv", "dau", "用户数", "销售额",
                 "转化率", "客单价", "金额")
_DIAGNOSIS_WORDS = ("归因", "异动")
_DIMENSION_WORDS = ("门店", "渠道", "地区", "品类", "分行", "网点", "会员等级")
# create：用户希望新建/创建/定义/新增一个指标（而非查询已有指标）
_CREATE_WORDS = ("创建指标", "新建指标", "新增指标", "定义指标", "定义一个指标", "创建一个指标",
                "新建一个指标", "新增一个指标", "加个指标", "加一个指标", "注册指标")
# help：操作指引 / 能力介绍；exit：结束会话（均为控制类意图，直接答复，不走取数）
_HELP_WORDS = (
    "怎么用", "如何使用", "使用说明", "操作指引", "帮助", "help", "你能做什么",
    "你会什么", "能做什么", "会做什么", "功能介绍", "有什么功能", "你是谁", "你是干嘛",
    "能干什么", "怎么操作",
)
_EXIT_WORDS = (
    "退出登录", "退出系统", "结束对话", "结束会话", "再见", "拜拜", "bye", "exit", "quit",
)
_SKILL_RULES = (
    (("画图", "画个图", "绘图", "可视化", "图表"), "chart_visualization"),
    (("解读", "总结一下", "数据总结"), "data_interpretation"),
)


async def _recall_few_shots(
    deps: AgentDeps, text: str, top_k: int = 3, user_id: str = ""
) -> str:
    """FR9 分层并行召回：L2 用户偏好 + L3 领域知识合并 BM25 召回，按重排公式
    （α·sim+β·recency+γ·freq+δ·level−ε·privacy）排序后注入意图识别 prompt。

    无 L3 库时退化为仅 L2（若有），无 L2 时退化为仅 L3（与旧行为一致）；
    两库皆空 / 未配置时返回空串（跳过）。这是「点赞 -> 沉淀 L3 -> 意图识别命中」
    与「用户偏好 -> 意图识别遵循」两条闭环的最后一环。
    """
    if (
        deps.few_shot_repo is None
        and deps.domain_memory_repo is None
        and deps.user_pref_repo is None
    ):
        return ""
    # settings 可能为 None（测试 / 降级）：回落默认重排权重，绝不因配置缺失阻断召回
    rerank = _rerank_config_from_settings(deps.settings)
    retriever = MemoryRetriever(
        deps.few_shot_repo,
        deps.domain_memory_repo,
        user_pref_repo=deps.user_pref_repo,
        top_k=top_k,
        rerank=rerank,
    )
    try:
        outcome = await retriever.recall_unified(query=text, user_id=user_id, top_k=top_k)
        return outcome.injected_text
    except Exception as exc:  # noqa: BLE001 - 反馈库异常不影响主链路
        logger.warning("分层记忆召回失败，跳过意图示例注入：%s", exc)
        return ""


def _rerank_config_from_settings(settings) -> Optional[MemoryRerankConfig]:
    """从 MemorySettings 取重排权重；settings/内存配置缺失时回落默认（不抛异常）。"""
    try:
        mem = getattr(settings, "memory", None)
        if mem is None:
            return MemoryRerankConfig()
        return MemoryRerankConfig(
            alpha=mem.rerank_alpha,
            beta=mem.rerank_beta,
            gamma=mem.rerank_gamma,
            delta=mem.rerank_delta,
            epsilon=mem.rerank_epsilon,
            recency_lambda=mem.recency_lambda,
            level_weight_l2=mem.level_weight_l2,
            level_weight_l3=mem.level_weight_l3,
            budget_chars=mem.recall_budget_chars,
        )
    except Exception:  # noqa: BLE001 - 配置异常退化为默认，不阻断召回
        return MemoryRerankConfig()


async def _metric_candidates(deps: AgentDeps, text: str) -> List[str]:
    """候选指标召回（语义层指标名 + 别名 -> full/black_talk/part 三路召回）。

    老系统 get_metric_candidates（BM25/AC/Jaccard/embedding 召回）；embedding 通道后置。
    未命中时回退全量指标名（截断），保证 prompt 始终携带候选供 LLM 判断相关性。
    """
    layer = deps.semantic_layer
    if layer is None:
        return []
    keys = layer.metric_search_keys()
    if deps.recaller is not None:
        recalled = deps.recaller.recall(text, keys)
        names: List[str] = []
        for item in recalled:
            metric = layer.resolve_metric(str(item.name))
            if metric is not None and metric.name not in names:
                names.append(metric.name)
        if names:
            return names[:20]
    return layer.metric_names()[:20]


def _rule_based_intent(text: str, metric_names: List[str]) -> IntentResult:
    """LLM 不可用 / 调用失败时的关键词兜底（迁移自老系统规则链）。"""
    lowered = text.lower()
    stripped = text.strip()

    # 追加技能
    skills = [skill for words, skill in _SKILL_RULES if any(w in text for w in words)]

    # 新建指标意图：明确"创建/新建/定义/新增 + 指标"语义，优先级高于纯取数
    # （"查一下新建客户数"这类查询已存在指标的不含 _CREATE_WORDS，仍走 metric_query）
    if any(w in text for w in _CREATE_WORDS):
        return IntentResult(
            intent="create", skills=skills, multi_query=False,
            confidence=0.9, reason="[mock] 命中新建指标关键词",
        )

    # 打分对比（保留老系统 scoring 语义：知识类与指标类同时命中时，知识类优先，
    # 如"什么是北极星指标"中"指标"一词不应导致误路由）
    knowledge_score = sum(1 for w in _KNOWLEDGE_WORDS if w in text)
    metric_score = sum(1 for w in _METRIC_WORDS if w in lowered)
    metric_score += sum(1 for name in metric_names if name in text)
    if stripped in metric_names:
        metric_score += 2
    finance_hit = any(w in text for w in _FINANCE_KEYWORDS)

    # 控制类意图（帮助 / 退出）：仅在无明确取数信号时判定，避免"帮我查下GMV"被 help 截胡
    if metric_score == 0 and not finance_hit:
        lowered_text = lowered
        if any(w in lowered_text for w in _EXIT_WORDS):
            return IntentResult(intent="exit", skills=[], multi_query=False,
                                confidence=0.9, reason="[mock] 命中结束会话意图")
        if any(w in text for w in _HELP_WORDS):
            return IntentResult(intent="help", skills=[], multi_query=False,
                                confidence=0.9, reason="[mock] 命中帮助/能力咨询意图")

    # 归因：明确归因/异动词，或"为什么+涨跌变化"
    is_diagnosis = any(w in text for w in _DIAGNOSIS_WORDS) or (
        "为什么" in text
        and any(w in text for w in ("下降", "上升", "涨", "跌", "变化", "波动"))
    )
    # 维度查询：问"有哪些/多少个"+ 具体维度实体，且无指标诉求
    is_dimension = (
        any(w in text for w in ("有哪些", "哪几个", "多少个"))
        and any(w in text for w in _DIMENSION_WORDS)
        and metric_score == 0
    )

    if is_diagnosis:
        confidence = 0.9 if any(w in text for w in _DIAGNOSIS_WORDS) else 0.75
        return IntentResult(intent="diagnosis", skills=skills, multi_query=False,
                            confidence=confidence, reason="[mock] 命中归因关键词")
    if is_dimension:
        return IntentResult(intent="dimension_query", skills=skills, multi_query=False,
                            confidence=0.85, reason="[mock] 命中维度查询关键词")
    if knowledge_score > metric_score:
        return IntentResult(intent="knowledge_qa", skills=[], multi_query=False,
                            confidence=0.8, reason="[mock] 命中知识咨询关键词")
    if metric_score > 0 or finance_hit:
        multi_query = text.count("？") + text.count("?") > 1
        if stripped in metric_names:
            confidence = 0.95
        elif any(name in text for name in metric_names):
            confidence = 0.9
        elif metric_score > 0:
            confidence = 0.7
        else:
            confidence = 0.55  # 仅金融领域关键词兜底
        return IntentResult(intent="metric_query", skills=skills, multi_query=multi_query,
                            confidence=confidence, reason="[mock] 命中指标查询关键词")
    return IntentResult(intent="chitchat", skills=[], multi_query=False,
                        confidence=0.4, reason="[mock] 默认闲聊")


def _apply_fixups(result: IntentResult, text: str, metric_names: List[str]) -> IntentResult:
    """LLM 结果的规则修正（迁移老系统"方案3"兜底，语义适配 v2）。"""
    if result.intent == "chitchat":
        stripped = text.strip()
        # 老 find_max_match：query 与候选指标完全一致 -> 直接取数
        if metric_names and stripped in metric_names:
            result.intent = "metric_query"
            result.confidence = max(result.confidence or 0.0, 0.9)
            result.reason += "（修正：query 命中候选指标，转取数）"
        # 老 check_result：金融领域关键词兜底
        elif any(k in text for k in _FINANCE_KEYWORDS):
            result.intent = "metric_query"
            result.confidence = max(result.confidence or 0.0, 0.6)
            result.reason += "（修正：命中金融领域关键词，转取数）"
    # 老方案3"单一分析插件前插取数"：dimension_query/diagnosis 在 v2 图路由中
    # 统一先走取数链路，等价实现，无需在此处理
    return result


def build_classify_intent_node(deps: AgentDeps):
    async def classify_intent(state: AgentState) -> Dict[str, Any]:
        # 查询改写后的规范问句（rewrite_query 节点产出；未命中/未跑时回退原句）
        user_text = user_query(state)
        metric_names = await _metric_candidates(deps, user_text)
        llm = deps.get_chat_model()

        if llm is None:
            result = _rule_based_intent(user_text, metric_names)
            logger.info("[mock] 意图识别：%s -> %s", user_text, result.model_dump())
            return {
                "intent": result.intent,
                "intent_confidence": result.confidence,
                "skills": list(result.skills),
                "multi_query": result.multi_query,
                "mock_mode": True,
            }

        async def _primary() -> Dict[str, Any]:
            # FR9 分层记忆：L2 用户偏好 + L3 领域知识并行召回，注入意图识别 prompt
            few_shot_block = await _recall_few_shots(
                deps, user_text, top_k=3, user_id=state.get("user_id") or ""
            )
            human_content = (
                f"候选指标：{json.dumps(metric_names, ensure_ascii=False)}\n"
            )
            if few_shot_block:
                human_content += f"{few_shot_block}\n"
            human_content += f"用户输入：{user_text}"

            result: IntentResult = await astructured_invoke(
                llm,
                IntentResult,
                [
                    SystemMessage(content=load_prompt("intent")["system"]),
                    HumanMessage(content=human_content),
                ],
            )
            result = _apply_fixups(result, user_text, metric_names)
            # FR15 输出约束：在 harness 边界强制按 IntentResult Schema 解析，
            # 自由格式/漏网 dict 会被拦截 -> recover_parse 触发关键词兜底降级。
            await enforce_output_schema(IntentResult, result)
            logger.info(
                "意图识别：%s -> intent=%s skills=%s multi_query=%s（%s）",
                user_text, result.intent, result.skills, result.multi_query, result.reason,
            )
            return {
                "intent": result.intent,
                "intent_confidence": result.confidence,
                "skills": list(result.skills),
                "multi_query": result.multi_query,
            }

        async def _fallback() -> Dict[str, Any]:
            logger.warning("LLM 意图识别失败，限次自愈：回退关键词规则")
            fallback = _rule_based_intent(user_text, metric_names)
            return {
                "intent": fallback.intent,
                "intent_confidence": fallback.confidence,
                "skills": list(fallback.skills),
                "multi_query": fallback.multi_query,
                "mock_mode": True,
            }

        # 节点级回写错误样本池（only_recovered=True：自愈成功才采样）
        ctx = {
            "session_id": state.get("session_id") or "",
            "user_id": state.get("user_id") or "",
            "tenant_id": state.get("tenant_id") or "",
            "round_id": state.get("round_id") or "",
            "node": "classify_intent",
            "trace_snippet": user_text[:500],
        }
        from dmf_agent.agents.metric_agent.recovery import (
            make_recovery_hook,
            recover_parse,
        )

        hook = make_recovery_hook(deps.error_sample_repo, ctx, only_recovered=True)
        return await recover_parse(
            "classify_intent", _primary, _fallback, deps.recovery_settings, on_recovery=hook
        )

    return classify_intent
