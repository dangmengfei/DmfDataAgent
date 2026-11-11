"""记忆分层管理（L1 摘要列表 / L2 平台视角 / L3 领域知识 / 统计）单测。

直接调用路由函数 + 内存仓储（不启动完整 app），覆盖：
- L1 跨会话摘要列表（补会话标题、租户隔离）
- L2 全平台偏好（补用户名、租户收敛、按主键删除）
- L3 领域知识 CRUD 与审核（status 流转）、few_shot 镜像过滤
- 统计卡各层计数
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from dmf_agent.api.routers import memory as mem_router
from dmf_agent.db.factory import DatabaseBundle
from dmf_agent.schemas.auth import CurrentUser, UserData
from dmf_agent.schemas.feedback import FewShotData
from dmf_agent.schemas.memory import (
    DomainMemoryData,
    DomainMemoryPatch,
    DomainMemoryUpsert,
    MemorySummaryData,
    UserPreferenceData,
)


def _runtime() -> SimpleNamespace:
    """最小 runtime：L1 列表只用到 db；settings 供 _assert_session_owned 读 auth.enabled。"""
    return SimpleNamespace(
        db=DatabaseBundle(mode="memory"),
        settings=SimpleNamespace(auth=SimpleNamespace(enabled=False)),
    )


def _user(user_id: str = "u1", tenant_id: str = "t1", perms=None) -> CurrentUser:
    return CurrentUser(
        id=user_id, username=user_id, tenant_id=tenant_id,
        permissions=perms if perms is not None else ["*"],
    )


# ---------- L1 ----------


async def test_l1_list_fills_session_title():
    rt = _runtime()
    session = await rt.db.session_repo.create(user_id="u1", title="存款专题分析", tenant_id="t1")
    await rt.db.memory_repo.upsert(
        MemorySummaryData(session_id=session.id, summary="用户关注各分行存款余额，粒度按月。")
    )
    rows = await mem_router.list_l1(keyword="", limit=100, current=_user(), runtime=rt)
    assert len(rows) == 1
    assert rows[0].session_id == session.id
    assert rows[0].session_title == "存款专题分析"  # 标题由会话表补齐（表里只有 session_id）
    assert rows[0].user_id == "u1"
    assert "存款余额" in rows[0].summary


async def test_l1_list_keyword_filter_and_count():
    rt = _runtime()
    s1 = await rt.db.session_repo.create(user_id="u1", title="A", tenant_id="t1")
    s2 = await rt.db.session_repo.create(user_id="u1", title="B", tenant_id="t1")
    await rt.db.memory_repo.upsert(MemorySummaryData(session_id=s1.id, summary="存款相关讨论"))
    await rt.db.memory_repo.upsert(MemorySummaryData(session_id=s2.id, summary="贷款相关讨论"))

    rows = await mem_router.list_l1(keyword="贷款", limit=100, current=_user(), runtime=rt)
    assert [r.session_id for r in rows] == [s2.id]
    assert await rt.db.memory_repo.count() == 2


async def test_l1_list_tenant_isolation():
    """摘要列表按租户收敛：用户只看得到本租户会话的记忆。"""
    rt = _runtime()
    mine = await rt.db.session_repo.create(user_id="u1", title="本租户", tenant_id="t1")
    other = await rt.db.session_repo.create(user_id="u9", title="他租户", tenant_id="t2")
    await rt.db.memory_repo.upsert(MemorySummaryData(session_id=mine.id, summary="本租户记忆"))
    await rt.db.memory_repo.upsert(MemorySummaryData(session_id=other.id, summary="他租户记忆"))

    # t1 租户用户只看得到 t1 的会话记忆（含 admin，避免跨租户读到他人对话摘要）
    rows_t1 = await mem_router.list_l1(
        keyword="", limit=100, current=_user(user_id="u1", tenant_id="t1"), runtime=rt
    )
    assert [r.session_id for r in rows_t1] == [mine.id]

    # t2 租户用户只看得到 t2 的
    rows_t2 = await mem_router.list_l1(
        keyword="", limit=100, current=_user(user_id="u9", tenant_id="t2"), runtime=rt
    )
    assert [r.session_id for r in rows_t2] == [other.id]

    # tenant_id 为空（鉴权关闭时的合成用户，本地调试）-> 不过滤，全部可见
    rows_all = await mem_router.list_l1(
        keyword="", limit=100, current=_user(tenant_id=""), runtime=rt
    )
    assert len(rows_all) == 2


async def test_l1_detail_and_clear():
    rt = _runtime()
    session = await rt.db.session_repo.create(user_id="u1", title="S", tenant_id="t1")
    await rt.db.memory_repo.upsert(MemorySummaryData(session_id=session.id, summary="摘要正文"))

    detail = await mem_router.get_l1(session.id, current=_user(), runtime=rt)
    assert detail["summary"] == "摘要正文"
    out = await mem_router.clear_session_memory(session.id, current=_user(), runtime=rt)
    assert out["ok"] is True and out["cleared"] == "l1"
    assert (await mem_router.get_l1(session.id, current=_user(), runtime=rt))["summary"] is None


# ---------- L2 ----------


async def test_l2_list_fills_username_and_filters():
    rt = _runtime()
    await rt.db.user_repo.add(
        UserData(id="u1", username="analyst1", display_name="分析师一", tenant_id="t1"), "h"
    )
    await rt.db.preference_repo.upsert(
        UserPreferenceData(id=0, user_id="u1", tenant_id="t1", key="granularity",
                           value="month", source="user")
    )
    await rt.db.preference_repo.upsert(
        UserPreferenceData(id=0, user_id="u1", tenant_id="t1", key="dimensions",
                           value="大区", source="inferred", confidence=0.6)
    )
    rows = await mem_router.list_l2(
        tenant_id=None, user_id=None, source=None, status=None, limit=200,
        current=_user(tenant_id=""), runtime=rt,
    )
    assert len(rows) == 2
    assert {r.username for r in rows} == {"analyst1"}
    assert {r.display_name for r in rows} == {"分析师一"}

    inferred = await mem_router.list_l2(
        tenant_id=None, user_id=None, source="inferred", status=None, limit=200,
        current=_user(tenant_id=""), runtime=rt,
    )
    assert [r.key for r in inferred] == ["dimensions"]


async def test_l2_tenant_scoped_for_non_admin():
    """非 admin 强制收敛到本租户，避免跨租户读他人偏好。"""
    rt = _runtime()
    await rt.db.preference_repo.upsert(
        UserPreferenceData(id=0, user_id="u1", tenant_id="t1", key="granularity", value="month")
    )
    await rt.db.preference_repo.upsert(
        UserPreferenceData(id=0, user_id="u2", tenant_id="t2", key="granularity", value="day")
    )
    scoped = _user(user_id="u1", tenant_id="t1", perms=["memory"])
    rows = await mem_router.list_l2(
        tenant_id=None, user_id=None, source=None, status=None, limit=200,
        current=scoped, runtime=rt,
    )
    assert [r.user_id for r in rows] == ["u1"]


async def test_l2_delete_by_id_guards_tenant():
    rt = _runtime()
    mine = await rt.db.preference_repo.upsert(
        UserPreferenceData(id=0, user_id="u1", tenant_id="t1", key="granularity", value="month")
    )
    other = await rt.db.preference_repo.upsert(
        UserPreferenceData(id=0, user_id="u2", tenant_id="t2", key="granularity", value="day")
    )
    tenant_user = _user(user_id="u1", tenant_id="t1", perms=["memory", "memory:write"])
    out = await mem_router.delete_l2_by_id(mine.id, current=tenant_user, runtime=rt)
    assert out["deleted"] is True
    assert await rt.db.preference_repo.get("u1", "granularity") is None

    # 跨租户删除 -> 404（不泄露存在性）
    with pytest.raises(HTTPException) as ei:
        await mem_router.delete_l2_by_id(other.id, current=tenant_user, runtime=rt)
    assert ei.value.status_code == 404
    assert await rt.db.preference_repo.get("u2", "granularity") is not None


# ---------- L3 ----------


async def test_l3_crud_and_audit_flow():
    rt = _runtime()
    created = await mem_router.create_l3(
        DomainMemoryUpsert(
            domain="metric_query", type="glossary", question="「日均」口径",
            answer="按自然日平均，不含节假日", tags="口径", weight=1.5,
        ),
        current=_user(user_id="admin", tenant_id="t1"),
        runtime=rt,
    )
    assert created.id >= 1 and created.status == "approved"
    # created_by 取操作人（审计：谁录的）
    assert created.created_by == "admin"

    detail = await mem_router.get_l3(created.id, runtime=rt)
    assert detail.type == "glossary"

    # 审核：驳回
    rejected = await mem_router.update_l3(
        created.id, DomainMemoryPatch(status="rejected"), runtime=rt
    )
    assert rejected.status == "rejected"
    assert await rt.db.domain_memory_repo.list_approved() == []  # 驳回后不再参与召回

    # 审核：通过（改回 approved）
    approved = await mem_router.update_l3(
        created.id, DomainMemoryPatch(status="approved", weight=2.0), runtime=rt
    )
    assert approved.status == "approved" and approved.weight == 2.0
    assert len(await rt.db.domain_memory_repo.list_approved()) == 1

    out = await mem_router.delete_l3(created.id, runtime=rt)
    assert out["deleted"] is True
    with pytest.raises(HTTPException):
        await mem_router.get_l3(created.id, runtime=rt)


async def test_l3_list_filters_and_few_shot_toggle():
    rt = _runtime()
    await mem_router.create_l3(
        DomainMemoryUpsert(type="rule", question="存款口径规则", answer="R"), _user(), rt
    )
    await mem_router.create_l3(
        DomainMemoryUpsert(type="glossary", question="术语解释", answer="G"), _user(), rt
    )
    await rt.db.domain_memory_repo.add(
        DomainMemoryData(
            id=0, domain="metric_query", type="few_shot", question="点赞样本问题",
            answer="A", intent="metric_query", status="approved", created_by="s1",
        )
    )

    all_rows = await mem_router.list_l3(
        domain=None, type=None, status=None, keyword="", include_few_shot=True,
        limit=50, offset=0, runtime=rt,
    )
    assert len(all_rows) == 3

    curated = await mem_router.list_l3(
        domain=None, type=None, status=None, keyword="", include_few_shot=False,
        limit=50, offset=0, runtime=rt,
    )
    assert {r.type for r in curated} == {"rule", "glossary"}  # 点赞镜像被排除

    only_rule = await mem_router.list_l3(
        domain=None, type="rule", status=None, keyword="", include_few_shot=True,
        limit=50, offset=0, runtime=rt,
    )
    assert [r.type for r in only_rule] == ["rule"]

    kw = await mem_router.list_l3(
        domain=None, type=None, status=None, keyword="术语", include_few_shot=True,
        limit=50, offset=0, runtime=rt,
    )
    assert [r.question for r in kw] == ["术语解释"]


# ---------- 统计 ----------


async def test_memory_stats_counts_each_layer():
    rt = _runtime()
    session = await rt.db.session_repo.create(user_id="u1", title="S", tenant_id="t1")
    await rt.db.memory_repo.upsert(MemorySummaryData(session_id=session.id, summary="x"))
    await rt.db.preference_repo.upsert(
        UserPreferenceData(id=0, user_id="u1", tenant_id="t1", key="granularity", value="month")
    )
    await mem_router.create_l3(DomainMemoryUpsert(type="rule", question="规则", answer="R"), _user(), rt)
    await mem_router.create_l3(
        DomainMemoryUpsert(type="rule", question="待审规则", answer="R", status="pending"),
        _user(), rt,
    )
    await rt.db.few_shot_repo.add(
        FewShotData(
            id=0, session_id="s1", message_id="m1", intent="metric_query",
            question="点赞问题", answer="A",
        )
    )

    stats = await mem_router.memory_stats(rt)
    assert stats["l1"]["summaries"] == 1
    assert stats["l2"]["preferences"] == 1
    assert stats["l3"]["total"] == 2
    assert stats["l3"]["pending"] == 1
    assert stats["l3"]["curated"] == 2  # rule 两条（无 few_shot 镜像）
    assert stats["l3"]["by_type"] == {"rule": 2}
    assert stats["few_shot"]["total"] == 1
    assert stats["few_shot"]["like"] == 1 and stats["few_shot"]["manual"] == 0
    assert stats["few_shot"]["active"] == 1 and stats["few_shot"]["disabled"] == 0
