"""三级黑话词典仓储层单测（内存实现，与 MySQL 版同接口）。

覆盖：增删改查、唯一键去重、层级隔离（公司级 / 用户级互不可见）、版本审计、
`replace_scope_entries` 只动内置种子、命中计数。
"""

from __future__ import annotations

from dmf_agent.db.factory import DatabaseBundle
from dmf_agent.schemas.slang import (
    ENTRY_METRIC,
    ENTRY_STOP,
    SCOPE_INDUSTRY,
    SCOPE_TENANT,
    SCOPE_USER,
    SOURCE_BUILTIN,
    SOURCE_MANUAL,
    STATUS_DRAFT,
    STATUS_PUBLISHED,
    SlangEntryData,
)


def _entry(
    term: str,
    standard: str = "",
    *,
    scope: str = SCOPE_INDUSTRY,
    tenant_id: str = "",
    user_id: str = "",
    entry_type: str = ENTRY_METRIC,
    status: str = STATUS_PUBLISHED,
    source: str = SOURCE_MANUAL,
    priority: int = 100,
    **kw,
) -> SlangEntryData:
    return SlangEntryData(
        scope=scope, tenant_id=tenant_id, user_id=user_id, term=term,
        standard=standard, entry_type=entry_type, status=status, source=source,
        priority=priority, **kw,
    )


async def test_add_get_find_and_duplicate():
    db = DatabaseBundle(mode="memory")
    repo = db.slang_repo
    created = await repo.add(_entry("AUM", "零售存款余额"))
    assert created.id >= 1 and created.version == 1

    assert (await repo.get(created.id)).term == "AUM"
    # find 按唯一键（scope+tenant+user+type+term）
    assert (await repo.find(_entry("AUM"))).id == created.id
    assert (await repo.find(_entry("AUM", scope=SCOPE_USER, user_id="u1"))) is None
    assert (await repo.find(_entry("AUM", entry_type=ENTRY_STOP))) is None
    assert (await repo.get(99999)) is None


async def test_query_filters_paging_keyword():
    db = DatabaseBundle(mode="memory")
    repo = db.slang_repo
    await repo.add(_entry("AUM", "零售存款余额"))
    await repo.add(_entry("贷款余额", "零售贷款余额"))
    await repo.add(_entry("帮我看下", entry_type=ENTRY_STOP, standard=""))
    await repo.add(
        _entry("我行口径", "零售存款余额", scope=SCOPE_TENANT, tenant_id="t1")
    )

    rows, total = await repo.query(limit=10)
    assert total == 4 and len(rows) == 4

    only_metric, mt = await repo.query(entry_type=ENTRY_METRIC, limit=10)
    assert mt == 3

    only_stop, st = await repo.query(entry_type=ENTRY_STOP, limit=10)
    assert st == 1

    only_tenant, tt = await repo.query(scope=SCOPE_TENANT, limit=10)
    assert tt == 1 and only_tenant[0].tenant_id == "t1"

    # keyword 命中 term / standard / remark
    hit, ht = await repo.query(keyword="存款", limit=10)
    assert ht >= 2 and all("存款" in (r.term + r.standard) for r in hit)

    # 分页
    page2, _ = await repo.query(limit=1, offset=1)
    assert len(page2) == 1


async def test_update_partial_and_version_audit():
    db = DatabaseBundle(mode="memory")
    repo = db.slang_repo
    e = await repo.add(_entry("AUM", "零售存款余额"))

    upd = await repo.update(e.id, priority=200, remark="调高优先级", operator="tester")
    assert upd is not None
    assert upd.priority == 200 and upd.remark == "调高优先级"
    assert upd.term == "AUM"  # 未传字段不动
    assert upd.version == 2

    # status 变更 -> change_type 记为 publish
    pub = await repo.update(e.id, status=STATUS_DRAFT, operator="tester")
    assert pub.status == STATUS_DRAFT

    versions = await repo.list_versions(e.id)
    types = [v.change_type for v in versions]
    # 倒序：最新在前
    assert types[0] == "update"  # draft 不算 publish/disable
    assert "create" in types
    assert all(v.entry_id == e.id for v in versions)

    assert await repo.update(99999, term="x") is None


async def test_delete_and_audit():
    db = DatabaseBundle(mode="memory")
    repo = db.slang_repo
    e = await repo.add(_entry("AUM", "零售存款余额"))
    assert await repo.delete(e.id, operator="tester") is True
    assert await repo.delete(e.id) is False
    assert await repo.get(e.id) is None
    versions = await repo.list_versions(e.id)
    assert any(v.change_type == "delete" for v in versions)


async def test_list_layers_isolation_and_published_only():
    db = DatabaseBundle(mode="memory")
    repo = db.slang_repo
    await repo.add(_entry("行业词", "零售存款余额"))
    await repo.add(_entry("公司词", "零售贷款余额", scope=SCOPE_TENANT, tenant_id="t1"))
    await repo.add(_entry("别的公司词", "存贷比", scope=SCOPE_TENANT, tenant_id="t2"))
    await repo.add(
        _entry("用户词", "不良贷款率", scope=SCOPE_USER, tenant_id="t1", user_id="u1")
    )
    await repo.add(
        _entry("别人用户词", "不良贷款余额", scope=SCOPE_USER, tenant_id="t1", user_id="u2")
    )
    await repo.add(_entry("草稿词", "净利息收入", status=STATUS_DRAFT))

    layers = await repo.list_layers(tenant_id="t1", user_id="u1")
    terms = {e.term for e in layers[SCOPE_INDUSTRY]}
    assert terms == {"行业词"}  # 草稿不参与
    assert {e.term for e in layers[SCOPE_TENANT]} == {"公司词"}
    assert {e.term for e in layers[SCOPE_USER]} == {"用户词"}

    # 无身份 -> 只回行业级
    only_ind = await repo.list_layers()
    assert {e.term for e in only_ind[SCOPE_TENANT]} == set()
    assert {e.term for e in only_ind[SCOPE_USER]} == set()


async def test_count_and_count_by_scope():
    db = DatabaseBundle(mode="memory")
    repo = db.slang_repo
    await repo.add(_entry("a", "零售存款余额"))
    await repo.add(_entry("b", "零售贷款余额", scope=SCOPE_TENANT, tenant_id="t1"))
    await repo.add(_entry("c", "存贷比", status=STATUS_DRAFT))

    assert await repo.count() == 3
    assert await repo.count(status=STATUS_PUBLISHED) == 2
    counts = await repo.count_by_scope()
    assert counts[SCOPE_INDUSTRY] == 2  # a + c（c 为草稿，仍计入条数）
    assert counts[SCOPE_TENANT] == 1
    assert counts[SCOPE_USER] == 0


async def test_replace_scope_entries_only_touches_builtin():
    db = DatabaseBundle(mode="memory")
    repo = db.slang_repo
    # 一条内置 + 一条手工（同层）
    await repo.add(_entry("内置词", "零售存款余额", source=SOURCE_BUILTIN))
    await repo.add(_entry("手工词", "零售贷款余额", source=SOURCE_MANUAL))
    await repo.add(_entry("公司内置", "存贷比", scope=SCOPE_TENANT, tenant_id="t1",
                          source=SOURCE_BUILTIN))

    written = await repo.replace_scope_entries(
        SCOPE_INDUSTRY, "", "", [_entry("新内置1", "零售存款余额"),
                                 _entry("新内置2", "零售贷款余额")]
    )
    assert written == 2
    rows, _ = await repo.query(scope=SCOPE_INDUSTRY, limit=10)
    terms = {r.term for r in rows}
    assert terms == {"手工词", "新内置1", "新内置2"}  # 手工词保留
    assert all(r.source == SOURCE_BUILTIN for r in rows if r.term.startswith("新内置"))
    # 公司级未受影响
    assert (await repo.query(scope=SCOPE_TENANT, limit=10))[1] == 1


async def test_bump_hit():
    db = DatabaseBundle(mode="memory")
    repo = db.slang_repo
    a = await repo.add(_entry("AUM", "零售存款余额"))
    b = await repo.add(_entry("贷款余额", "零售贷款余额"))
    n = await repo.bump_hit([a.id, b.id, 0, None])
    assert n == 2
    assert (await repo.get(a.id)).hit_count == 1
    assert (await repo.get(b.id)).hit_count == 1


async def test_rewrite_feedback_toggle_cancel_and_status():
    """改写反馈：点赞/点踩落库、再次点击取消（feedback 置 NULL）、按 message_id 回显。"""
    db = DatabaseBundle(mode="memory")
    repo = db.slang_repo

    # 改写节点先落一条事件（message_id 此刻未知，为空）
    await repo.log_rewrite_event(
        tenant_id="", user_id="", query_hash="h123", query_text="余额",
        hit_count=1, is_rewritten=True, session_id="s1", message_id="",
    )

    # 用户点 👍：按 message_id 优先定位；无则按 query_hash 命中，回填 message_id
    eid = await repo.set_rewrite_feedback(
        query_hash="h123", is_correct=True, session_id="s1", message_id="m1"
    )
    assert eid >= 1
    fb = await repo.get_rewrite_feedback_by_messages(["m1"])
    assert fb.get("m1") == 1

    # 再点 👍 -> 取消（feedback 置 NULL），状态查询不再回显
    eid2 = await repo.set_rewrite_feedback(
        query_hash="h123", is_correct=True, message_id="m1", cancel=True
    )
    assert eid2 == eid
    assert await repo.get_rewrite_feedback_by_messages(["m1"]) == {}

    # 点 👎：误改写落 -1
    await repo.set_rewrite_feedback(
        query_hash="h123", is_correct=False, message_id="m1"
    )
    assert (await repo.get_rewrite_feedback_by_messages(["m1"])).get("m1") == -1

    # 空 message_id 列表 / 无反馈的消息 -> 空字典
    assert await repo.get_rewrite_feedback_by_messages([]) == {}
    assert await repo.get_rewrite_feedback_by_messages(["nope"]) == {}

