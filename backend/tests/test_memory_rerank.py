"""FR9 记忆分层重排引擎单测：完整公式 α·sim+β·recency+γ·freq+δ·level−ε·privacy、
L2+L3 并行合并召回、Token 预算截断、以及 L3 单路向后兼容。
"""

import asyncio
from datetime import datetime

from dmf_agent.db.memory import (
    InMemoryFewShotRepository,
    InMemoryUserPreferenceRepository,
)
from dmf_agent.memory.retriever import (
    MemoryRerankConfig,
    MemoryRetriever,
    RecalledSample,
)
from dmf_agent.schemas.feedback import FewShotData
from dmf_agent.schemas.memory import UserPreferenceData


def _run(coro):
    return asyncio.run(coro)


def _sample(layer="L3", *, sim=0.5, freq=0.3, recency=0.0, privacy=0.0, weight=1.0):
    return RecalledSample(
        id=1,
        source="domain" if layer == "L3" else "user_pref",
        layer=layer,
        question="q",
        weight=weight,
        freq_norm=freq,
        recency_days=recency,
        privacy=privacy,
        bm25=sim,  # 归一化 sim 直接以 bm25 传入（max_bm25=1.0 时 sim==bm25）
    )


def _retriever(**kwargs):
    cfg = MemoryRerankConfig(**kwargs)
    return MemoryRetriever(None, None, top_k=3, rerank=cfg)


def test_score_monotonic_in_sim():
    r = _retriever()
    lo = r._score(_sample(sim=0.3), max_bm25=1.0)
    hi = r._score(_sample(sim=0.9), max_bm25=1.0)
    assert hi > lo


def test_score_recency_decay():
    r = _retriever()
    fresh = r._score(_sample(recency=0.0), max_bm25=1.0)
    stale = r._score(_sample(recency=60.0), max_bm25=1.0)
    assert fresh > stale


def test_score_level_priority_l3_over_l2():
    r = _retriever()
    l3 = r._score(_sample(layer="L3", sim=0.5, freq=0.3, recency=0.0), max_bm25=1.0)
    l2 = r._score(_sample(layer="L2", sim=0.5, freq=0.3, recency=0.0), max_bm25=1.0)
    assert l3 > l2


def test_score_privacy_penalty():
    r = _retriever()
    clean = r._score(_sample(privacy=0.0), max_bm25=1.0)
    dirty = r._score(_sample(privacy=1.0), max_bm25=1.0)
    assert clean > dirty


def test_score_freq_weight():
    r = _retriever()
    low = r._score(_sample(freq=0.1), max_bm25=1.0)
    high = r._score(_sample(freq=0.9), max_bm25=1.0)
    assert high > low


def _build_repos():
    few = InMemoryFewShotRepository()
    _run(
        few.add(
            FewShotData(
                id=0,
                question="零售存款余额是多少",
                answer="xxx",
                intent="metric_query",
                category="intention",
                status="active",
                source="manual",
                weight=1.0,
            )
        )
    )
    pref = InMemoryUserPreferenceRepository()
    _run(
        pref.upsert(
            UserPreferenceData(
                user_id="u1",
                key="granularity",
                value="month",
                source="user",
                confidence=0.9,
                status="active",
                created_at=datetime(2026, 9, 1),
                updated_at=datetime(2026, 9, 15),
            )
        )
    )
    return few, pref


def test_recall_unified_merges_l2_and_l3():
    few, pref = _build_repos()
    r = MemoryRetriever(few, None, user_pref_repo=pref, top_k=5)
    outcome = _run(r.recall_unified(query="零售存款余额 month 按月", user_id="u1", top_k=5))
    assert outcome.candidates > 0
    layers = {h.layer for h in outcome.hits}
    assert "L3" in layers, "应召回 L3 已验证样本"
    assert "L2" in layers, "应召回 L2 用户偏好"
    text = outcome.injected_text
    assert "【用户偏好】" in text
    assert "【已验证样本】" in text
    assert "用户偏好[granularity]=month" in text
    assert "识别意图：metric_query" in text


def test_recall_unified_budget_truncation():
    few, pref = _build_repos()
    for i in range(6):
        _run(
            few.add(
                FewShotData(
                    id=0,
                    question=f"指标查询样例{i} 按月",
                    answer="a",
                    intent="metric_query",
                    status="active",
                    source="manual",
                    weight=1.0,
                )
            )
        )
    r = MemoryRetriever(
        few, None, user_pref_repo=pref, top_k=10,
        rerank=MemoryRerankConfig(budget_chars=120),
    )
    outcome = _run(r.recall_unified(query="指标查询 按月", user_id="u1", top_k=10))
    assert len(outcome.hits) >= 1
    assert len(outcome.injected_text) <= 120 + 60


def test_recall_l3_only_backward_compat():
    few, _ = _build_repos()
    r = MemoryRetriever(few, None, top_k=5)
    outcome = _run(r.recall(query="零售存款余额", top_k=5))
    assert outcome.injected_text.startswith("以下是与当前问题语义相近的已验证样本")
    assert "【用户偏好】" not in outcome.injected_text
    assert "识别意图：metric_query" in outcome.injected_text


def test_recall_unified_no_user_pref_degrades_to_l3():
    few, _ = _build_repos()
    r = MemoryRetriever(few, None, user_pref_repo=None, top_k=5)
    outcome = _run(r.recall_unified(query="零售存款余额", user_id="", top_k=5))
    assert all(h.layer == "L3" for h in outcome.hits)
    assert "【用户偏好】" not in outcome.injected_text
