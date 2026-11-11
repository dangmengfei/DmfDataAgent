"""语义注册中心（P1 骨架）单测。

覆盖：
- metric_key 推导（simple=measure 名 / ratio=引用组合 / derived=稳定哈希）；
- 种子构建与 manifest 等价性（模型数 / 指标数 / 字段无损）；
- 内存仓储：幂等导入 / 列表过滤 / 详情 / 统计 / facets；
- 路由层：只读接口 + 404（静态路径先于 /{key} 注册）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dmf_agent.api.routers import semantic_registry as registry_router
from dmf_agent.db.factory import DatabaseBundle
from dmf_agent.db.memory import InMemorySemanticRegistryRepository
from dmf_agent.semantic.layer import SemanticLayer
from dmf_agent.semantic.registry_seed import (
    build_registry_seed,
    derive_metric_key,
)


@pytest.fixture(scope="module")
def layer() -> SemanticLayer:
    return SemanticLayer()


@pytest.fixture(scope="module")
def seed(layer) -> dict:
    return build_registry_seed(layer)


# ---------- metric_key 推导 ----------


def test_metric_key_simple_uses_measure_name(layer):
    assert derive_metric_key("GMV", layer) == "gmv_amount"
    assert derive_metric_key("零售存款余额", layer) == "retail_deposit_balance"


def test_metric_key_ratio_combines_refs(layer):
    # 付费转化率 = 付费用户数 / 日活跃用户数 -> 两个 simple 指标的 measure 名组合
    assert derive_metric_key("付费转化率", layer) == "paid_user_count__active_user_count_ratio"


def test_metric_key_derived_stable_hash(layer):
    k1 = derive_metric_key("客单价", layer)
    k2 = derive_metric_key("客单价", layer)
    assert k1 == k2 and k1.startswith("derived_")


def test_metric_key_unique_across_all(layer):
    keys = [derive_metric_key(m.name, layer) for m in layer.list_metrics()]
    assert len(keys) == len(set(keys)), "metric_key 必须全局唯一"


def test_metric_key_unknown_metric_raises(layer):
    with pytest.raises(ValueError):
        derive_metric_key("不存在的指标", layer)


# ---------- 种子等价性（P1 验收核心：DB 读出与 manifest.yml 等价） ----------


def test_seed_covers_manifest(layer, seed):
    assert len(seed["models"]) == len(layer.manifest.models)
    assert len(seed["metrics"]) == len(layer.list_metrics())


def test_seed_fields_lossless(layer, seed):
    by_key = {m["metric_key"]: m for m in seed["metrics"]}
    gmv = by_key["gmv_amount"]
    m = layer.resolve_metric("GMV")
    assert gmv["name"] == "GMV"
    assert gmv["metric_type"] == "simple"
    assert gmv["type_params"] == {"measure": "gmv_amount"}
    assert gmv["aliases"] == list(m.aliases)
    assert gmv["unit"] == m.unit
    assert gmv["round"] == m.round
    assert gmv["granularities"] == list(m.granularities)
    assert gmv["pop_types"] == list(m.pop_types)

    trade = next(x for x in seed["models"] if x["model_key"] == "trade_day")
    raw = next(x for x in layer.manifest.models if x.name == "trade_day")
    assert trade["table"] == raw.table
    assert trade["agg_time_dimension"] == raw.agg_time_dimension
    assert len(trade["dimensions"]) == len(raw.dimensions)
    assert len(trade["measures"]) == len(raw.measures)
    assert trade["entities"] == [
        {"name": e.name, "entity_type": e.type, "expr": e.expr} for e in raw.entities
    ]


def test_dimension_key_uses_expr(layer, seed):
    trade = next(x for x in seed["models"] if x["model_key"] == "trade_day")
    dims = {d["name"]: d["dimension_key"] for d in trade["dimensions"]}
    assert dims["渠道"] == "channel_name"
    assert dims["统计日期"] == "stat_date"


# ---------- 内存仓储 ----------


@pytest.mark.asyncio
async def test_import_seed_idempotent(seed):
    repo = InMemorySemanticRegistryRepository()
    r1 = await repo.import_seed(seed, operator="t")
    assert r1["models_imported"] == len(seed["models"])
    assert r1["metrics_imported"] == len(seed["metrics"])
    r2 = await repo.import_seed(seed, operator="t")
    assert r2["models_imported"] == 0 and r2["metrics_imported"] == 0
    assert r2["models_skipped"] == r1["models_imported"]
    assert r2["metrics_skipped"] == r1["metrics_imported"]
    stats = await repo.stats()
    assert stats["models"] == len(seed["models"])
    assert stats["metrics"] == len(seed["metrics"])


@pytest.mark.asyncio
async def test_list_metrics_filters(seed):
    repo = InMemorySemanticRegistryRepository()
    await repo.import_seed(seed)
    # 按类型
    ratios, total = await repo.list_metrics(metric_type="ratio")
    assert total > 0 and all(m["metric_type"] == "ratio" for m in ratios)
    # keyword 命中别名
    hits, _ = await repo.list_metrics(keyword="日活")
    assert any("日活" in (m["aliases"] or []) or "日活" in m["name"] for m in hits)
    # keyword 命中 key
    hits, _ = await repo.list_metrics(keyword="gmv_amount")
    assert any(m["metric_key"] == "gmv_amount" for m in hits)
    # 分页
    page1, total = await repo.list_metrics(limit=5, offset=0)
    assert len(page1) == 5 and total >= len(seed["metrics"])


@pytest.mark.asyncio
async def test_get_metric_with_host(seed):
    repo = InMemorySemanticRegistryRepository()
    await repo.import_seed(seed)
    detail = await repo.get_metric("gmv_amount")
    assert detail is not None
    assert detail["host_models"] == [{"model_key": "trade_day", "name": "trade_day"}]
    assert await repo.get_metric("no_such_key") is None


@pytest.mark.asyncio
async def test_get_model_detail(seed):
    repo = InMemorySemanticRegistryRepository()
    await repo.import_seed(seed)
    detail = await repo.get_model("trade_day")
    assert detail is not None
    assert detail["table"] == "dws_trade_day"
    assert any(d["name"] == "渠道" for d in detail["dimensions"])
    assert any(x["measure_key"] == "gmv_amount" for x in detail["measures"])
    assert await repo.get_model("no_such") is None


@pytest.mark.asyncio
async def test_list_dimensions_by_model(seed):
    repo = InMemorySemanticRegistryRepository()
    await repo.import_seed(seed)
    dims, total = await repo.list_dimensions(model_key="retail_deposit_day")
    names = [d["name"] for d in dims]
    assert "机构" in names and "客户等级" in names
    assert total == len(dims)
    # 全量维度 >= 单模型维度
    all_dims, all_total = await repo.list_dimensions()
    assert all_total > total


@pytest.mark.asyncio
async def test_stats_and_facets(seed):
    repo = InMemorySemanticRegistryRepository()
    await repo.import_seed(seed)
    stats = await repo.stats()
    assert stats["metrics_by_type"]["simple"] > 0
    assert stats["metrics_by_status"].get("published") == len(seed["metrics"])
    facets = await repo.facets()
    assert set(facets["metric_types"]) <= {"simple", "ratio", "derived"}
    assert "trade_day" in facets["model_keys"]


# ---------- 路由层（只读 + 404） ----------


def _runtime(repo) -> SimpleNamespace:
    db = DatabaseBundle(mode="memory")
    db.semantic_registry_repo = repo
    return SimpleNamespace(db=db)


@pytest.mark.asyncio
async def test_router_read_endpoints(seed):
    repo = InMemorySemanticRegistryRepository()
    await repo.import_seed(seed)
    rt = _runtime(repo)

    stats = await registry_router.registry_stats(runtime=rt)
    assert stats["models"] == len(seed["models"])

    page = await registry_router.registry_metrics(
        status=None, metric_type="simple", keyword=None, page=1, page_size=10, runtime=rt
    )
    assert page["total"] > 0 and page["page"] == 1

    detail = await registry_router.registry_metric_detail("gmv_amount", runtime=rt)
    assert detail["metric_key"] == "gmv_amount"


@pytest.mark.asyncio
async def test_router_404(seed):
    repo = InMemorySemanticRegistryRepository()
    await repo.import_seed(seed)
    rt = _runtime(repo)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        await registry_router.registry_metric_detail("missing", runtime=rt)
    assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        await registry_router.registry_model_detail("missing", runtime=rt)
    assert e.value.status_code == 404


@pytest.mark.asyncio
async def test_router_static_paths_before_key(seed):
    """stats / facets 是静态路径，必须可正常访问（不被 /{metric_key} 吞掉）。"""
    repo = InMemorySemanticRegistryRepository()
    await repo.import_seed(seed)
    rt = _runtime(repo)
    facets = await registry_router.registry_facets(runtime=rt)
    assert "model_keys" in facets
    stats = await registry_router.registry_stats(runtime=rt)
    assert stats["metrics"] > 0
