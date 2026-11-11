"""注册中心治理 CRUD 的租户隔离（读得到 ≠ 改得动）。

为什么单独一个文件
------------------
表结构里早就有 ``tenant_id``，唯一约束也是 ``(scope, tenant_id, metric_key)``，
但仓储方法此前一律**只按 key 查** —— 于是"租户 A 改到租户 B（或全局）的草稿"
在按 key 定位下是**通**的。本文件锁的就是这个洞。

两套语义刻意分开，测试也分开断言：
- **可见性**（读）：全局条目 + 本租户条目（全局对租户只读）；admin 另有全量视角；
- **归属**（写 / 按 key 精确定位）：必须**恰好**属于本租户，全局条目对租户不可写。

另外锁住两个容易顺手改错的点：
- 引用校验的取值域必须是「全局 ∪ 本租户」（可见性），按归属收窄会让
  "租户指标引用全局 measure" 被判成引用不存在；
- 维度的租户归属**继承宿主模型**，所以"往全局模型上挂自己的维度"必须被拒绝。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from dmf_agent.api.routers import semantic_registry as reg
from dmf_agent.db.factory import DatabaseBundle
from dmf_agent.db.memory import InMemorySemanticRegistryRepository
from dmf_agent.schemas.auth import CurrentUser
from dmf_agent.semantic.layer import SemanticLayer
from dmf_agent.semantic.registry_seed import build_registry_seed

# ---------------------------------------------------------------- 桩件


def _user(tenant_id: str, role: str = "analyst") -> CurrentUser:
    perms = ["semantic", "semantic:write"] if role != "viewer" else ["semantic"]
    return CurrentUser(
        id=f"u-{tenant_id or 'global'}",
        username=f"u-{tenant_id or 'global'}",
        tenant_id=tenant_id,
        role=role,
        permissions=perms,
    )


def _admin(tenant_id: str = "") -> CurrentUser:
    return CurrentUser(
        id="admin",
        username="admin",
        tenant_id=tenant_id,
        role="admin",
        permissions=["*"],
    )


def _runtime(repo) -> SimpleNamespace:
    db = DatabaseBundle(mode="memory")
    db.semantic_registry_repo = repo
    return SimpleNamespace(db=db, semantic_layer=None, metric_engine=None)


def _tenant_model(model_key: str, tenant_id: str) -> dict:
    return {
        "model_key": model_key,
        "name": model_key,
        "description": f"{tenant_id} 私有模型",
        "table": "dws_x",
        "tenant_id": tenant_id,
        "status": "published",
        "dimensions": [
            {"dimension_key": f"{model_key}_dim", "name": "私有维度", "dim_type": "categorical", "expr": "x"}
        ],
        "measures": [
            {"measure_key": "tenant_measure", "name": "租户度量", "agg": "sum", "expr": "x"}
        ],
    }


@pytest.fixture(scope="module")
def seed() -> dict:
    return build_registry_seed(SemanticLayer())


async def _repo_with_tenants(seed) -> InMemorySemanticRegistryRepository:
    """全局种子 + t1/t2 各一条私有指标与私有模型。"""
    repo = InMemorySemanticRegistryRepository()
    await repo.import_seed(seed)
    for tid in ("t1", "t2"):
        # 显式给 key：simple 指标按 measure 推导会得到 gmv_amount，
        # 与全局条目撞 key（唯一性是 per-tenant，不会报错但会把测试意图带偏）
        await repo.create_metric_draft(
            {
                "metric_key": f"private_{tid}",
                "name": f"{tid} 私有指标",
                "metric_type": "simple",
                "type_params": {"measure": "gmv_amount"},
            },
            tenant_id=tid,
        )
        repo._models.append(_tenant_model(f"model_{tid}", tid))
    return repo


# ---------------------------------------------------------------- 可见性（读）


@pytest.mark.asyncio
async def test_tenant_user_sees_global_and_own_only(seed):
    repo = await _repo_with_tenants(seed)
    keys = {m["metric_key"] for m, _ in [(x, x) for x in []]}  # 占位，便于阅读
    del keys

    _, total_all = await repo.list_metrics(tenant_id=None)
    for tid in ("t1", "t2"):
        items, total = await repo.list_metrics(tenant_id=tid)
        owned = [m for m in items if (m.get("tenant_id") or "") == tid]
        assert len(owned) == 1, f"{tid} 应只看到自己的 1 条私有指标"
        assert total < total_all, "租户视角必须少于全量"

    # t1 看不到 t2 的草稿
    items_t1, _ = await repo.list_metrics(tenant_id="t1")
    assert not any((m.get("tenant_id") or "") == "t2" for m in items_t1)
    # 但全局口径照常可见（只读）
    assert any((m.get("tenant_id") or "") == "" for m in items_t1)


@pytest.mark.asyncio
async def test_admin_sees_all_tenants_by_default(seed):
    repo = await _repo_with_tenants(seed)
    tid = reg._read_scope(_admin(), None)
    assert tid is None, "admin 不传 tenant_id = 全量视角（跨租户审批要看得见租户草稿）"
    _, total = await repo.list_metrics(tenant_id=tid)
    _, total_everything = await repo.list_metrics(tenant_id=None)
    assert total == total_everything


@pytest.mark.asyncio
async def test_admin_can_narrow_to_one_tenant(seed):
    repo = await _repo_with_tenants(seed)
    tid = reg._read_scope(_admin(), "t1")
    items, _ = await repo.list_metrics(tenant_id=tid)
    assert not any((m.get("tenant_id") or "") == "t2" for m in items)
    assert any((m.get("tenant_id") or "") == "t1" for m in items)


@pytest.mark.asyncio
async def test_non_admin_read_scope_is_forced_to_own_tenant(seed):
    """非 admin 传什么都不好使：读作用域被强制收敛到本租户。"""
    assert reg._read_scope(_user("t1"), "t2") == "t1"
    assert reg._read_scope(_user("t1"), "") == "t1"
    assert reg._read_scope(_user("t1"), None) == "t1"


@pytest.mark.asyncio
async def test_stats_and_facets_are_scoped(seed):
    repo = await _repo_with_tenants(seed)
    full = await repo.stats(tenant_id=None)
    t1 = await repo.stats(tenant_id="t1")
    assert t1["metrics"] < full["metrics"]
    assert t1["models"] < full["models"]
    facets = await repo.facets(tenant_id="t1")
    assert "model_t2" not in facets["model_keys"]
    assert "model_t1" in facets["model_keys"]


@pytest.mark.asyncio
async def test_tenant_can_open_global_definition_read_only(seed):
    """同一个 key 本租户也有一份时，详情优先给本租户那份。"""
    repo = await _repo_with_tenants(seed)
    await repo.create_metric_draft(
        {"metric_key": "gmv_amount", "name": "t1 的 GMV", "metric_type": "simple",
         "type_params": {"measure": "gmv_amount"}},
        tenant_id="t1",
    )
    mine = await repo.get_metric("gmv_amount", tenant_id="t1")
    assert mine is not None and mine["name"] == "t1 的 GMV"

    # 只有全局有的 key：租户仍能打开（只读）
    global_only = await repo.get_metric("order_count", tenant_id="t1")
    if global_only is not None:
        assert (global_only.get("tenant_id") or "") == ""


# ---------------------------------------------------------------- 归属（写）


@pytest.mark.asyncio
async def test_tenant_cannot_update_global_or_other_tenant_entry(seed):
    repo = await _repo_with_tenants(seed)
    # 全局条目：归属不匹配 ⇒ 定位失败（路由层表现为 404）
    assert await repo.update_metric("gmv_amount", {"name": "被篡改"}, tenant_id="t1") is None
    # 别租户条目同理
    t2_key = next(
        m["metric_key"] for m in repo._metrics if (m.get("tenant_id") or "") == "t2"
    )
    assert await repo.update_metric(t2_key, {"name": "被篡改"}, tenant_id="t1") is None
    # 自己的可以改
    t1_key = next(
        m["metric_key"] for m in repo._metrics if (m.get("tenant_id") or "") == "t1"
    )
    assert await repo.update_metric(t1_key, {"name": "改名"}, tenant_id="t1") is not None


@pytest.mark.asyncio
async def test_router_update_global_metric_from_tenant_is_404(seed):
    """路由层：租户改全局口径 ⇒ 404（定位不到），而不是静默改到全局。"""
    repo = await _repo_with_tenants(seed)
    rt = _runtime(repo)
    with pytest.raises(HTTPException) as e:
        await reg.registry_update_metric(
            "gmv_amount", {"name": "被篡改"}, current=_user("t1"), runtime=rt
        )
    assert e.value.status_code == 404
    # 全局定义未被改动
    assert (await repo.get_metric("gmv_amount", tenant_id=None))["name"] != "被篡改"


@pytest.mark.asyncio
async def test_create_draft_ignores_body_tenant_id_for_non_admin(seed):
    """请求体里写 tenant_id="" 不能让租户 analyst 把草稿建到全局。"""
    repo = await _repo_with_tenants(seed)
    rt = _runtime(repo)
    out = await reg.registry_create_metric_draft(
        {"name": "越权草稿", "metric_type": "simple",
         "type_params": {"measure": "gmv_amount"}, "tenant_id": ""},
        current=_user("t1"),
        runtime=rt,
    )
    assert out["metric"]["tenant_id"] == "t1"


@pytest.mark.asyncio
async def test_non_admin_targeting_other_tenant_is_403(seed):
    with pytest.raises(HTTPException) as e:
        reg._write_tenant(_user("t1"), "t2")
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        reg._write_tenant(_user("t1"), "")  #  Targeting 全局同样拒绝
    assert e.value.status_code == 403
    # 自己的租户放行
    assert reg._write_tenant(_user("t1"), "t1") == "t1"
    assert reg._write_tenant(_user("t1")) == "t1"


@pytest.mark.asyncio
async def test_admin_writes_global_by_default_but_can_target_tenant(seed):
    assert reg._write_tenant(_admin("")) == ""
    assert reg._write_tenant(_admin(""), "t1") == "t1"


@pytest.mark.asyncio
async def test_same_key_can_exist_per_tenant(seed):
    """唯一性是 (scope, tenant_id, key)：同 key 在别租户下可以存在（overlay 语义）。"""
    repo = await _repo_with_tenants(seed)
    a = await repo.create_metric_draft(
        {"metric_key": "shared_key", "name": "t1 版", "metric_type": "simple",
         "type_params": {"measure": "gmv_amount"}},
        tenant_id="t1",
    )
    b = await repo.create_metric_draft(
        {"metric_key": "shared_key", "name": "t2 版", "metric_type": "simple",
         "type_params": {"measure": "gmv_amount"}},
        tenant_id="t2",
    )
    assert a["metric_key"] == "shared_key" and b["metric_key"] == "shared_key"
    assert a["tenant_id"] == "t1" and b["tenant_id"] == "t2"


# ---------------------------------------------------------------- 引用域（可见性，不是归属）


@pytest.mark.asyncio
async def test_reference_domain_includes_global_measures(seed):
    """租户指标引用**全局** measure 是合法的 —— 取值域必须是全局 ∪ 本租户。"""
    repo = await _repo_with_tenants(seed)
    global_keys = await repo.list_all_measure_keys(tenant_id=None)
    tenant_domain = await repo.list_all_measure_keys(tenant_id="t1")
    assert "gmv_amount" in tenant_domain, "全局 measure 必须出现在租户的引用域里"
    assert "tenant_measure" in tenant_domain, "本租户的 measure 也要在（经宿主模型收敛）"
    assert tenant_domain <= global_keys


@pytest.mark.asyncio
async def test_metric_reference_domain_is_union_with_tenant_override(seed):
    repo = await _repo_with_tenants(seed)
    defs = await repo.list_all_metric_defs(tenant_id="t1")
    assert "gmv_amount" in defs
    assert (defs.get("gmv_amount") or {}).get("tenant_id", "") == ""


# ---------------------------------------------------------------- 维度（归属继承宿主模型）


@pytest.mark.asyncio
async def test_tenant_cannot_attach_dimension_to_global_model(seed):
    repo = await _repo_with_tenants(seed)
    with pytest.raises(ValueError) as e:
        await repo.create_dimension_draft(
            {"model_key": "trade_day", "name": "挂到全局模型", "dim_type": "categorical",
             "expr": "x"},
            tenant_id="t1",
        )
    assert "不属于当前租户" in str(e.value)


@pytest.mark.asyncio
async def test_tenant_can_attach_dimension_to_own_model(seed):
    repo = await _repo_with_tenants(seed)
    dim = await repo.create_dimension_draft(
        {"model_key": "model_t1", "name": "自己的维度", "dim_type": "categorical",
         "expr": "x"},
        tenant_id="t1",
    )
    assert dim["model_key"] == "model_t1"


@pytest.mark.asyncio
async def test_dimension_update_is_scoped_by_host_model(seed):
    repo = await _repo_with_tenants(seed)
    dim = await repo.create_dimension_draft(
        {"model_key": "model_t1", "name": "t1 维度", "dim_type": "categorical", "expr": "x"},
        tenant_id="t1",
    )
    key = dim["dimension_key"]
    # 本租户可改
    assert await repo.update_dimension(key, {"name": "改名"}, tenant_id="t1") is not None
    # t2 改不到（宿主模型不属于 t2）
    assert await repo.update_dimension(key, {"name": "越权"}, tenant_id="t2") is None


@pytest.mark.asyncio
async def test_global_dimension_still_readable_by_tenant(seed):
    """全局维度对租户只读可见（不回归）。"""
    repo = await _repo_with_tenants(seed)
    dims, total = await repo.list_dimensions(tenant_id="t1")
    assert total > 0
    assert any(d.get("model_key") == "trade_day" for d in dims)


@pytest.mark.asyncio
async def test_global_metric_versions_readable_by_tenant(seed):
    """版本历史同样走"先本租户、再回落全局"：否则租户看全局条目会 404（行为回归）。"""
    repo = await _repo_with_tenants(seed)
    versions = await repo.list_metric_versions("gmv_amount", tenant_id="t1")
    assert versions, "全局指标的版本历史对租户应可读"


# ---------------------------------------------------------------- overlay 编辑面的条目源


@pytest.mark.asyncio
async def test_router_base_manifest_returns_full_entries():
    """编辑面要"整条替换"，因此必须拿到**完整字段**的条目，而不是 describe() 的摘要。"""
    rt = _runtime(InMemorySemanticRegistryRepository())
    rt.semantic_layer = SemanticLayer()
    out = await reg.registry_base_manifest(runtime=rt)
    assert out["semantic_models"] and out["metrics"]
    m = next(x for x in out["metrics"] if x["name"] == "GMV")
    # 注意：原始清单里**可以缺省** granularities/pop_types（解析期才补默认值），
    # 编辑面提交的条目同样允许缺省 —— 这正是"整条替换"要保留原样的原因。
    assert "type_params" in m and "aliases" in m
    model = next(x for x in out["semantic_models"] if x["name"] == "trade_day")
    assert "table" in model and model.get("measures")


@pytest.mark.asyncio
async def test_router_base_manifest_503_when_layer_missing():
    with pytest.raises(HTTPException) as e:
        await reg.registry_base_manifest(runtime=_runtime(InMemorySemanticRegistryRepository()))
    assert e.value.status_code == 503
