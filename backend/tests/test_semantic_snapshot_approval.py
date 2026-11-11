"""快照级审批流测试（draft → review → published → deprecated + 回滚）。

要点：
- 取数只消费 ``published``；draft/review 对运行时**完全不可见**；
- **草稿不能直接发布**，必须经 review 闸口（与指标级状态机同构）；
- 废弃最新 published 后，运行时自动退回到上一个 published（或全局）；
- 回滚 = 生成新版本而非物理回退（历史不可变、可审计）。
"""

from __future__ import annotations

import pytest
from types import SimpleNamespace

from dmf_agent.db.memory import InMemorySemanticPublishedRepository
from dmf_agent.schemas.semantic_publish import SemanticPublishedData
from dmf_agent.semantic.layer import SemanticLayer
from dmf_agent.semantic.registry import SemanticRegistry

# 全量语义清单的指标数（随 manifest.d 演进，避免把 19 写死导致新增指标后测试误报）
_FULL_METRIC_COUNT = len(SemanticLayer().manifest.metrics)


def _registry() -> SemanticRegistry:
    return SemanticRegistry(InMemorySemanticPublishedRepository())


def _trimmed_raw(metric: str = "GMV", model: str = "trade_day") -> dict:
    raw = SemanticLayer().manifest.to_dict()
    raw["metrics"] = [m for m in raw["metrics"] if m.get("name") == metric]
    raw["semantic_models"] = [m for m in raw["semantic_models"] if m.get("name") == model]
    return raw


async def _stage(reg: SemanticRegistry, **kwargs):
    """起草：默认落 draft。"""
    return await reg.publish(layer=SemanticLayer(), state="draft", **kwargs)


# ---------- 状态机 ----------


async def test_draft_is_invisible_to_runtime_until_approved():
    reg = _registry()
    rec = await _stage(reg, tenant_id="t1", by="analyst")
    assert rec.state == "draft"
    assert await reg.get_published_layer(tenant_id="t1") is None

    await reg.submit(rec.id)
    assert (await reg.get_snapshot(rec.id)).state == "review"
    assert await reg.get_published_layer(tenant_id="t1") is None

    await reg.approve(rec.id)
    got = await reg.get_published_layer(tenant_id="t1")
    assert got is not None and len(got.list_metrics()) == _FULL_METRIC_COUNT


async def test_draft_cannot_be_published_directly():
    reg = _registry()
    rec = await _stage(reg, tenant_id="t1", by="analyst")
    with pytest.raises(ValueError, match="非法状态转移"):
        await reg.approve(rec.id)


async def test_approve_requires_review_state():
    reg = _registry()
    rec = await reg.publish(layer=SemanticLayer(), tenant_id="t1", by="a")  # 已是 published
    with pytest.raises(ValueError, match="非法状态转移"):
        await reg.approve(rec.id)


async def test_submit_twice_is_illegal():
    reg = _registry()
    rec = await _stage(reg, tenant_id="t1", by="a")
    await reg.submit(rec.id)
    with pytest.raises(ValueError, match="非法状态转移"):
        await reg.submit(rec.id)


async def test_transition_on_missing_snapshot_raises_lookup_error():
    reg = _registry()
    with pytest.raises(LookupError):
        await reg.submit(999)


async def test_deprecated_can_be_revived_as_draft():
    reg = _registry()
    rec = await reg.publish(layer=SemanticLayer(), tenant_id="t1", by="a")
    await reg.deprecate(rec.id)
    assert (await reg.get_snapshot(rec.id)).state == "deprecated"
    await reg.revive(rec.id)
    assert (await reg.get_snapshot(rec.id)).state == "draft"


# ---------- 废弃后运行时回落 ----------


async def test_deprecating_latest_published_falls_back_to_previous():
    reg = _registry()
    global_full = await reg.publish(layer=SemanticLayer(), tenant_id="", by="sys")
    trimmed = await reg.publish(
        payload=_trimmed_raw(), tenant_id="", by="sys"
    )
    assert [m.name for m in (await reg.get_published_layer()).list_metrics()] == ["GMV"]

    await reg.deprecate(trimmed.id)
    got = await reg.get_published_layer()
    assert got is not None and len(got.list_metrics()) == _FULL_METRIC_COUNT  # 退回 v1
    assert (await reg.get_snapshot(global_full.id)).state == "published"


# ---------- 回滚 ----------


async def test_rollback_creates_new_version_not_physical_revert():
    reg = _registry()
    v1 = await reg.publish(layer=SemanticLayer(), tenant_id="t1", by="sys")
    v2 = await reg.publish(payload=_trimmed_raw(), tenant_id="t1", by="sys")
    assert (v1.version, v2.version) == (1, 2)

    v3 = await reg.rollback(snapshot_id=v1.id, by="admin")
    assert v3.version == 3
    assert v3.state == "published"

    # 历史不可变：v2 仍在，只是不再是「最新 published」
    assert (await reg.get_snapshot(v2.id)).state == "published"
    got = await reg.get_published_layer(tenant_id="t1")
    assert len(got.list_metrics()) == _FULL_METRIC_COUNT  # 内容等于 v1


async def test_rollback_of_overlay_keeps_overlay_mode():
    reg = _registry()
    await reg.publish(layer=SemanticLayer(), tenant_id="", by="sys")
    ov = await reg.publish(
        tenant_id="t1",
        by="admin",
        mode="overlay",
        payload={"metrics": [{"name": "GMV", "type": "simple",
                              "type_params": {"measure": "gmv_amount"},
                              "description": "回滚前的覆盖"}]},
    )
    again = await reg.rollback(snapshot_id=ov.id, by="admin")
    assert again.mode == "overlay"
    assert again.version == 2
    got = await reg.get_published_layer(tenant_id="t1")
    assert got.manifest.metric("GMV").description == "回滚前的覆盖"


async def test_rollback_rejects_unloadable_full_payload():
    """历史脏数据（绕过写入口写入的不可装载 full）也不能被回滚成新版本。

    注意：``publish`` 自 2026-09-18 起会在写入口拒绝不可装载的 full payload，
    所以这里刻意**直接经仓储写入**来构造"早已存在的坏快照"——回滚守卫的价值正在于
    防御这类绕过写入口的历史数据，而不是普通调用路径。
    """
    reg = _registry()
    bad = await reg.repo.add(
        SemanticPublishedData(
            tenant_id="t1",
            version=1,
            state="deprecated",
            mode="full",
            payload={
                "version": 1,
                "metrics": [
                    {
                        "name": "幽灵指标",
                        "type": "simple",
                        "type_params": {"measure": "不存在的度量"},
                    }
                ],
                "semantic_models": [],
            },
        )
    )
    with pytest.raises(Exception):
        await reg.rollback(snapshot_id=bad.id, by="admin")


async def test_rollback_missing_snapshot_raises_lookup_error():
    reg = _registry()
    with pytest.raises(LookupError):
        await reg.rollback(snapshot_id=404, by="admin")


# ---------- 路由注册 ----------


def test_snapshot_approval_endpoints_registered():
    from dmf_agent.api.routers.semantic_registry import router

    paths = {r.path for r in router.routes}
    for suffix in (
        "/semantic/registry/snapshots",
        "/semantic/registry/snapshots/{snapshot_id}/submit",
        "/semantic/registry/snapshots/{snapshot_id}/approve",
        "/semantic/registry/snapshots/{snapshot_id}/deprecate",
        "/semantic/registry/snapshots/{snapshot_id}/rollback",
        "/semantic/registry/snapshots/{snapshot_id}/reject",
        "/semantic/registry/snapshots/batch",
    ):
        assert suffix in paths, f"缺少路由：{suffix}"


# ---------- 租户可见性隔离（T198a：防跨租户窥探） ----------


async def _seed_published(repo, tids=("", "t1", "t2", "t3")):
    for tid in tids:
        await repo.add(
            SemanticPublishedData(
                tenant_id=tid, version=1, state="published", mode="full", payload={}
            )
        )


async def test_read_scope_collapses_non_admin_tenant():
    """非 admin 即便在前端/直传里塞了别租户 id，_read_scope 也必须收敛为本租户。"""
    from dmf_agent.api.routers.semantic_registry import _read_scope

    non_admin = SimpleNamespace(tenant_id="t1", role="analyst", permissions=set())
    assert _read_scope(non_admin, "t2") == "t1"
    assert _read_scope(non_admin, None) == "t1"
    assert _read_scope(non_admin, "") == "t1"

    admin = SimpleNamespace(tenant_id="t1", role="admin", permissions={"*"})
    assert _read_scope(admin, None) is None  # 全量视角
    assert _read_scope(admin, "") == ""  # 仅全局
    assert _read_scope(admin, "t9") == "t9"  # 全局 + 该租户


async def test_list_for_visible_scope_isolates_tenants():
    """visible_scope=True 时非 admin 视角只能看到全局+本租户，绝不可见别租户。"""
    repo = InMemorySemanticPublishedRepository()
    await _seed_published(repo)
    rows = await repo.list_for(tenant_id="t1", visible_scope=True)
    got = {r.tenant_id for r in rows}
    assert got == {"", "t1"}
    assert "t2" not in got and "t3" not in got


async def test_list_for_visible_scope_admin_full_when_none():
    repo = InMemorySemanticPublishedRepository()
    await _seed_published(repo, tids=("", "t1", "t2"))
    rows = await repo.list_for(tenant_id=None, visible_scope=True)
    assert {r.tenant_id for r in rows} == {"", "t1", "t2"}


async def test_list_for_visible_scope_admin_global_only():
    repo = InMemorySemanticPublishedRepository()
    await _seed_published(repo, tids=("", "t1", "t2"))
    rows = await repo.list_for(tenant_id="", visible_scope=True)
    assert {r.tenant_id for r in rows} == {""}


async def test_list_versions_visible_scope_blocks_cross_tenant():
    """经 service 层：非 admin 即便传入别租户 id，也看不到对方快照。"""
    reg = SemanticRegistry(InMemorySemanticPublishedRepository())
    await _seed_published(reg.repo, tids=("", "t1", "t2"))
    rows = await reg.list_versions(tenant_id="t1", visible_scope=True)
    got = {r.tenant_id for r in rows}
    assert got == {"", "t1"}
    assert "t2" not in got


async def test_list_for_default_exact_match_unchanged():
    """兼容内部调用：不传 visible_scope 时仍按精确 tenant_id 过滤。"""
    repo = InMemorySemanticPublishedRepository()
    await _seed_published(repo)
    rows = await repo.list_for(tenant_id="t2")
    assert {r.tenant_id for r in rows} == {"t2"}


# ---------- T198d：驳回原因 + 批量审批 ----------


async def test_reject_requires_reason_and_only_from_review():
    reg = _registry()
    snap = await reg.publish(tenant_id="t1", by="u1", state="draft", mode="overlay",
                             payload={"metrics": [{"name": "GMV", "type": "simple",
                                                  "type_params": {"measure": "gmv_amount"}}]})
    sid = snap.id
    # draft 状态不能直接驳回
    with pytest.raises(ValueError):
        await reg.reject(sid, by="admin", reason="不行")
    await reg.submit(sid)
    # review 状态可驳回，但必须有原因
    with pytest.raises(ValueError):
        await reg.reject(sid, by="admin", reason="")
    rec = await reg.reject(sid, by="admin", reason="口径与总行口径冲突")
    assert rec.state == "draft"
    assert rec.rejected_by == "admin"
    assert "冲突" in rec.reject_reason


async def test_approve_records_operator():
    reg = _registry()
    snap = await reg.publish(tenant_id="", by="u1", state="draft", mode="overlay",
                             payload={"metrics": [{"name": "GMV", "type": "simple",
                                                  "type_params": {"measure": "gmv_amount"}}]})
    sid = snap.id
    await reg.submit(sid)
    rec = await reg.approve(sid, by="admin-zhang")
    assert rec.state == "published"
    assert rec.approved_by == "admin-zhang"


async def test_batch_approve_and_reject():
    reg = _registry()
    a = await reg.publish(tenant_id="t1", by="u1", state="draft", mode="overlay",
                          payload={"metrics": [{"name": "GMV", "type": "simple",
                                               "type_params": {"measure": "gmv_amount"}}]})
    b = await reg.publish(tenant_id="t1", by="u1", state="draft", mode="overlay",
                          payload={"metrics": [{"name": "日活跃用户数", "type": "simple",
                                               "type_params": {"measure": "dau"}}]})
    await reg.submit(a.id)
    await reg.submit(b.id)
    res = await reg.batch_approve([a.id, b.id], by="admin")
    assert set(res["approved"]) == {a.id, b.id}
    assert res["failed"] == []
    # 批量通过也必须记录审批人（by 需透传到 _transition）
    assert (await reg.get_snapshot(a.id)).approved_by == "admin"
    assert (await reg.get_snapshot(b.id)).approved_by == "admin"

    # 再发两份驳回
    c = await reg.publish(tenant_id="t2", by="u2", state="draft", mode="overlay",
                          payload={"metrics": [{"name": "GMV", "type": "simple",
                                               "type_params": {"measure": "gmv_amount"}}]})
    d = await reg.publish(tenant_id="t2", by="u2", state="draft", mode="overlay",
                          payload={"metrics": [{"name": "日活跃用户数", "type": "simple",
                                               "type_params": {"measure": "dau"}}]})
    await reg.submit(c.id)
    await reg.submit(d.id)
    res2 = await reg.batch_reject([c.id, d.id], by="admin", reason="待补充口径")
    assert set(res2["rejected"]) == {c.id, d.id}
    assert (await reg.get_snapshot(c.id)).rejected_by == "admin"
    assert (await reg.get_snapshot(c.id)).reject_reason == "待补充口径"


async def test_batch_reject_rejects_empty_reason():
    reg = _registry()
    a = await reg.publish(tenant_id="t1", by="u1", state="draft", mode="overlay",
                          payload={"metrics": [{"name": "GMV", "type": "simple",
                                               "type_params": {"measure": "gmv_amount"}}]})
    await reg.submit(a.id)
    with pytest.raises(ValueError):
        await reg.batch_reject([a.id], by="admin", reason="")
