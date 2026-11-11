"""记忆分层召回（L1/L2/L3 并行召回 + 重排 + Token 预算截断）。

PRD FR9（§5.11）：分层并行召回 → 候选 → 重排 `score = α·sim + β·recency + γ·freq + δ·level − ε·privacy`
→ Token 预算截断注入。

- **L1 会话记忆**：由 history.HistoryAssembler 负责（滚动压缩 + 相关性 + 改写），本模块不重复；
- **L2 用户偏好**：从 `user_pref_repo` 取该用户 active 偏好，按 recency + confidence 参与重排；
- **L3 领域知识**：合并 few_shot_examples（已验证样本，含点赞沉淀与手工录入）与
  domain_memories(approved)，以用户问题为语料做 BM25 召回，再按重排分排序。

重排公式（对齐 PRD）：
    score = α·sim + β·recency + γ·freq + δ·level − ε·privacy
- sim      : 该候选 BM25 分在当轮候选里的归一化（0..1）
- recency  : exp(−λ·(now − last_updated).days)，越近越高
- freq     : L2=confidence（0..1）/ L3=min(weight/3, 1)
- level    : L2=level_weight_l2(默认1.0) < L3=level_weight_l3(默认2.0) 固定权重
- privacy  : 候选携带敏感标记时扣分（默认 0，预留 PIPL 合规扩展位）

与分级回流的关系：点赞 -> 写 few_shot_examples 且并行写 domain_memories(type=few_shot,
status=approved)；二者在此处合并召回，形成「点赞 -> 沉淀 L3 -> 意图识别命中」闭环。
**同一问答对会在两张表各存一份**，因此合并后按（问题, 意图）去重 —— 否则同一个问题
会占掉 top_k 里两个名额（样本少时尤其明显：top_k=3 实际只覆盖 2 个不同问题）。

管理面「Few-Shot 试召回」复用 `recall`（L3 单路，与线上意图识别注入同源）；
意图识别阶段走 `recall_unified`（L2+L3 合并重排），保证页面与线上**同源同口径**。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from dmf_agent.db.base import DomainMemoryRepository, FewShotRepository, UserPreferenceRepository
from dmf_agent.rag.bm25 import BM25Index

logger = logging.getLogger(__name__)

# 注入文本的标题行（前端「试召回」用同一份文本预览）
INJECT_HEADER = "以下是与当前问题语义相近的已验证样本（请参考其意图判定）："


@dataclass
class MemoryRerankConfig:
    """重排评分权重（PRD FR9：α·sim+β·recency+γ·freq+δ·level−ε·privacy）。

    全部可在 ``MemorySettings`` 覆盖；这里给一组经验默认（domain 知识略优先于个人偏好，
    recency 衰减较弱以免新鲜样本被长青样本淹没）。
    """

    alpha: float = 1.0  # sim 权重（语义相关性）
    beta: float = 0.3  # recency 权重（时效）
    gamma: float = 0.5  # freq 权重（频次/置信）
    delta: float = 0.4  # level 权重（层固定加成）
    epsilon: float = 0.2  # privacy 惩罚
    recency_lambda: float = 0.05  # 天数衰减系数（越小越慢遗忘）
    level_weight_l2: float = 1.0
    level_weight_l3: float = 2.0
    budget_chars: int = 1500  # Token 预算截断上限（≤1.5K 字符，PRD §5.11.2）


@dataclass
class RecalledSample:
    """一条被召回的候选样本（含解释性分数，便于管理页展示"为什么它排在前面"）。"""

    id: int
    source: str  # few_shot | domain | user_pref
    question: str
    answer: str = ""
    intent: Optional[str] = None
    category: str = "intention"
    tags: str = ""
    weight: float = 1.0
    score: int = 1  # few-shot 点赞次数；domain 记忆恒为 1
    bm25: float = 0.0
    rerank: float = 0.0
    # --- FR9 分层扩展 ---
    layer: str = "L3"  # L2 | L3（当前实现覆盖这两层）
    recency_days: float = 0.0  # 距 last_updated 的天数（重排 recency 用）
    freq_norm: float = 0.0  # 归一化频次（0..1），L2=confidence / L3=min(weight/3,1)
    privacy: float = 0.0  # 隐私/敏感扣分（0..1，默认 0）


@dataclass
class RecallOutcome:
    """召回结果：候选总数 + 命中样本 + 实际注入文本。"""

    query: str
    candidates: int = 0
    hits: List[RecalledSample] = field(default_factory=list)
    injected_text: str = ""


def _dedupe_key(question: str, intent: Optional[str]) -> Tuple[str, str]:
    """同一问答对（问题 + 意图）视为同一条，用于跨表去重。"""
    return (question.strip(), (intent or "").strip())


class MemoryRetriever:
    """分层记忆召回器（L2/L3 并行召回 + 重排 + Token 预算截断）。

    与现有装配兼容：``recall`` / ``recall_l3`` 维持 L3 单路（管理面试召回同源）；
    ``recall_unified`` 为 FR9 新路径，合并 L2+L3 后按完整重排公式排序并截断预算。
    """

    def __init__(
        self,
        few_shot_repo: Optional[FewShotRepository],
        domain_memory_repo: Optional[DomainMemoryRepository],
        user_pref_repo: Optional[UserPreferenceRepository] = None,
        top_k: int = 3,
        rerank: Optional[MemoryRerankConfig] = None,
    ) -> None:
        self._few = few_shot_repo
        self._dom = domain_memory_repo
        self._pref = user_pref_repo
        self._top_k = top_k
        self._rerank = rerank or MemoryRerankConfig()

    # ------------------------------------------------------------------ 公开 API

    async def recall_l3(self, query: str, top_k: Optional[int] = None) -> str:
        """从 L3 领域知识召回与 query 相近的参考示例，拼成意图识别注入文本；
        无样本 / 异常时返回空串（跳过注入）。"""
        outcome = await self.recall(query, top_k)
        return outcome.injected_text

    async def recall(self, query: str, top_k: Optional[int] = None) -> RecallOutcome:
        """L3 单路结构化召回（管理面「试召回」与 recall_l3 共用，口径一致）。"""
        k = top_k or self._top_k
        outcome = RecallOutcome(query=query)
        candidates = await self._collect_l3()
        outcome.candidates = len(candidates)
        if not candidates or not (query or "").strip():
            return outcome
        hits = self._bm25_hits(candidates, query, k)
        if not hits:
            return outcome
        scored = self._score_and_sort(candidates, hits, k)
        outcome.hits = scored
        outcome.injected_text = self._render(scored)
        return outcome

    async def recall_unified(
        self,
        *,
        query: str,
        user_id: Optional[str] = None,
        intent: Optional[str] = None,
        top_k: Optional[int] = None,
    ) -> RecallOutcome:
        """FR9 统一召回：L2（用户偏好）+ L3（领域知识）并行召回后合并重排。

        - 无 user_id / 未配置偏好库时自动退化为 L3 单路（向后兼容）；
        - 重排后按 ``budget_chars`` 截断，保证注入可控（≤1.5K）。
        """
        k = top_k or self._top_k
        outcome = RecallOutcome(query=query)
        l2 = await self._recall_l2(user_id or "", query)
        l3 = await self._collect_l3()
        candidates = l2 + l3
        outcome.candidates = len(candidates)
        if not candidates or not (query or "").strip():
            return outcome
        hits = self._bm25_hits(candidates, query, k)
        if not hits:
            return outcome
        # 过取 3×top_k 再预算截断，避免高分长样本挤掉其余
        scored = self._score_and_sort(candidates, hits, k * 3)
        outcome.hits = self._budget_truncate(scored, self._rerank.budget_chars)
        outcome.injected_text = self._render(outcome.hits)
        return outcome

    # ------------------------------------------------------------------ 召回分支

    async def _recall_l2(self, user_id: str, query: str) -> List[RecalledSample]:
        """收集该用户 active 偏好，作为 L2 候选（layer=L2，freq=confidence）。"""
        if self._pref is None or not user_id:
            return []
        try:
            prefs = await self._pref.list_by_user(user_id, status="active")
        except Exception as exc:  # noqa: BLE001 - 偏好库异常不影响主链路
            logger.warning("加载 L2 用户偏好失败，跳过 L2 召回：%s", exc)
            return []
        now = datetime.now(timezone.utc)
        out: List[RecalledSample] = []
        for p in prefs:
            ts = p.updated_at or p.created_at or now
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            recency_days = max(0.0, (now - ts).total_seconds() / 86400.0)
            conf = float(p.confidence or 0.0)
            freq_norm = min(1.0, max(0.0, conf))
            out.append(
                RecalledSample(
                    id=p.id,
                    source="user_pref",
                    layer="L2",
                    question=f"用户偏好[{p.key}]={p.value}",
                    answer=p.value,
                    intent=None,
                    category="preference",
                    tags=p.source or "user",  # user | inferred
                    weight=conf if conf else 1.0,
                    score=0,
                    recency_days=recency_days,
                    freq_norm=freq_norm,
                    privacy=0.0,
                )
            )
        return out

    async def _collect_l3(self) -> List[RecalledSample]:
        """收集 L3 候选样本（跨表去重：同一问答对只保留一条，few-shot 优先、权重取较大者）。"""
        merged: "dict[Tuple[str, str], RecalledSample]" = {}

        def _put(sample: RecalledSample) -> None:
            key = _dedupe_key(sample.question, sample.intent)
            prev = merged.get(key)
            if prev is None:
                merged[key] = sample
                return
            # few_shot 优先级高于 domain 镜像（前者带点赞次数与手工维护的标签）
            if prev.source == "domain" and sample.source == "few_shot":
                sample.weight = max(sample.weight, prev.weight)
                merged[key] = sample
            else:
                prev.weight = max(prev.weight, sample.weight)
                prev.score = max(prev.score, sample.score)

        # 1) few-shot 样本（点赞沉淀 + 手工录入；停用的不参与召回）
        if self._few is not None:
            try:
                for e in await self._few.list_all(status="active"):
                    if e.question and e.intent:
                        _put(
                            RecalledSample(
                                id=e.id,
                                source="few_shot",
                                layer="L3",
                                question=e.question,
                                answer=e.answer or "",
                                intent=e.intent,
                                category=e.category or "intention",
                                tags=e.tags or "",
                                weight=float(e.weight or 1.0),
                                score=int(e.score or 1),
                            )
                        )
            except Exception as exc:  # noqa: BLE001 - 反馈库异常不影响主链路
                logger.warning("加载 few-shot 样本失败，跳过 L3 召回：%s", exc)
        # 2) domain_memories 审核通过的领域知识（含历史点赞镜像，靠上面的去重收敛）
        if self._dom is not None:
            try:
                for d in await self._dom.list_approved():
                    if d.question:
                        _put(
                            RecalledSample(
                                id=d.id,
                                source="domain",
                                layer="L3",
                                question=d.question,
                                answer=d.answer or "",
                                intent=d.intent,
                                category="intention" if d.type == "few_shot" else d.type,
                                tags=d.tags or "",
                                weight=float(d.weight or 1.0),
                            )
                        )
            except Exception as exc:  # noqa: BLE001
                logger.warning("加载 domain_memories 失败，跳过 L3 召回：%s", exc)
        # 补充分层扩展字段（recency / freq_norm / privacy）。
        # L3 为团队级长青知识，不随单人访问时效衰减 → recency_days 退化为 0；
        # freq 以 weight 近似（归一化到 0..1）；privacy 默认 0（PIPL 合规位预留）。
        for c in merged.values():
            c.recency_days = 0.0
            c.freq_norm = min(1.0, max(0.0, c.weight / 3.0))
            c.privacy = 0.0
        return list(merged.values())

    # ------------------------------------------------------------------ 重排 / 截断

    def _bm25_hits(
        self, candidates: List[RecalledSample], query: str, top_k: int
    ) -> List[Tuple[int, float]]:
        index = BM25Index([(c.question, str(i)) for i, c in enumerate(candidates)])
        return index.search_indices(query, max(top_k * 3, 1))

    def _score_and_sort(
        self, candidates: List[RecalledSample], hits: List[Tuple[int, float]], top_k: int
    ) -> List[RecalledSample]:
        max_bm25 = max((bm for _, bm in hits), default=0.0) or 1.0
        scored: List[Tuple[float, RecalledSample]] = []
        for idx, bm in hits:
            c = candidates[idx]
            c.bm25 = float(bm)
            s = self._score(c, max_bm25)
            c.rerank = round(s, 6)
            scored.append((s, c))
        scored.sort(key=lambda x: -x[0])
        return [c for _, c in scored[:top_k]]

    def _score(self, c: RecalledSample, max_bm25: float) -> float:
        """PRD FR9 重排分：α·sim + β·recency + γ·freq + δ·level − ε·privacy。"""
        sim = c.bm25 / max_bm25 if max_bm25 > 0 else 0.0
        sim = min(1.0, max(0.0, sim))
        recency = math.exp(-self._rerank.recency_lambda * max(0.0, c.recency_days))
        freq = min(1.0, max(0.0, c.freq_norm))
        level_w = self._level_weight(c.layer)
        return (
            self._rerank.alpha * sim
            + self._rerank.beta * recency
            + self._rerank.gamma * freq
            + self._rerank.delta * level_w
            - self._rerank.epsilon * c.privacy
        )

    def _level_weight(self, layer: str) -> float:
        if layer == "L3":
            return self._rerank.level_weight_l3
        if layer == "L2":
            return self._rerank.level_weight_l2
        return 0.0

    def _budget_truncate(
        self, ranked: List[RecalledSample], budget_chars: int
    ) -> List[RecalledSample]:
        """按重排降序累加每条候选的渲染长度，超预算即丢弃尾部（保证注入可控）。"""
        out: List[RecalledSample] = []
        used = 0
        for c in ranked:
            n = self._estimate_chars(c)
            if out and used + n > budget_chars:
                break
            out.append(c)
            used += n
        return out

    @staticmethod
    def _estimate_chars(c: RecalledSample) -> int:
        """单条候选注入文本长度估计（含分隔符，略宽松以贴近真实渲染）。"""
        if c.layer == "L2":
            return len(f"- {c.question}（来源：{c.tags or c.source}）") + 4
        intent_line = f" 识别意图：{c.intent}" if c.intent else ""
        return len(f"用户问题：{c.question}{intent_line}") + 4

    # ------------------------------------------------------------------ 渲染

    @staticmethod
    def _render(hits: List[RecalledSample]) -> str:
        if not hits:
            return ""
        l2 = [c for c in hits if c.layer == "L2"]
        l3 = [c for c in hits if c.layer == "L3"]
        lines: List[str] = []
        if l2:
            lines.append("【用户偏好】以下为当前用户的长期偏好，请优先遵循：")
            for c in l2:
                lines.append(f"- {c.question}（来源：{c.tags or c.source}）")
        if l3:
            if l2:
                lines.append(
                    "【已验证样本】以下是与当前问题语义相近的已验证样本（请参考其意图判定）："
                )
            else:
                lines.append(INJECT_HEADER)
            for c in l3:
                intent_line = f"\n识别意图：{c.intent}" if c.intent else ""
                lines.append(f"用户问题：{c.question}{intent_line}")
        return "\n".join(lines)
