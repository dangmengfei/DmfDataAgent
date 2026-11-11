"""混合方案测试：语义层发布快照（DB）+ 运行时装配（YAML 基座 / DB 快照）。

覆盖：
- `SemanticRegistry.publish / get_published_layer / list_versions`（InMemory 仓储）；
- 版本按租户自增、租户专属优先于全局、全局回落；
- `build_layer` 按 `SEMANTIC__SOURCE` 选择来源，DB 缺失/异常自动回落 YAML；
- `semantic_registry` 路由已注册发布/查询端点。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dmf_agent.db.memory import InMemorySemanticPublishedRepository
from dmf_agent.semantic.layer import SemanticLayer
from dmf_agent.semantic.registry import SemanticRegistry
from dmf_agent.semantic.runtime import build_layer


def _settings(source: str = "yaml", manifest_dir: str = "") -> SimpleNamespace:
    return SimpleNamespace(semantic=SimpleNamespace(source=source, manifest_dir=manifest_dir))


def _registry() -> SemanticRegistry:
    return SemanticRegistry(InMemorySemanticPublishedRepository())


def _trimmed_raw(metric: str = "GMV", model: str = "trade_day") -> dict:
    """裁剪过的合法 manifest：只留一个模型 + 一个指标（仍通过引用完整性校验）。"""
    raw = SemanticLayer().manifest.to_dict()
    raw["metrics"] = [m for m in raw["metrics"] if m.get("name") == metric]
    raw["semantic_models"] = [m for m in raw["semantic_models"] if m.get("name") == model]
    return raw


# ---------- 注册中心：发布 / 读取 / 版本 ----------


async def test_publish_and_get_published_layer():
    reg = _registry()
    layer = SemanticLayer()
    rec = await reg.publish(layer=layer, tenant_id="", by="tester")
    assert rec.version == 1
    assert rec.state == "published"

    got = await reg.get_published_layer(tenant_id="")
    assert got is not None
    assert len(got.list_metrics()) == len(layer.list_metrics())
    assert got.manifest.metric("GMV") is not None


async def test_version_increments_per_tenant():
    reg = _registry()
    layer = SemanticLayer()
    r1 = await reg.publish(layer=layer, tenant_id="t1", by="a")
    r2 = await reg.publish(layer=layer, tenant_id="t1", by="a")
    r3 = await reg.publish(layer=layer, tenant_id="t2", by="a")
    assert (r1.version, r2.version, r3.version) == (1, 2, 1)


async def test_get_returns_none_when_empty():
    reg = _registry()
    assert await reg.get_published_layer(tenant_id="t1") is None


async def test_tenant_falls_back_to_global():
    reg = _registry()
    layer = SemanticLayer()
    await reg.publish(layer=layer, tenant_id="", by="sys")  # 全局
    got = await reg.get_published_layer(tenant_id="t1")
    assert got is not None
    assert len(got.list_metrics()) == len(layer.list_metrics())


async def test_tenant_specific_preferred_over_global():
    reg = _registry()
    repo = reg.repo
    global_layer = SemanticLayer()
    await reg.publish(layer=global_layer, tenant_id="", by="sys")

    # 构造一个"只保留 GMV"的租户专属快照（通过裁剪 payload 区分全局）
    raw = global_layer.manifest.to_dict()
    raw["metrics"] = [m for m in raw["metrics"] if m.get("name") == "GMV"]
    raw["semantic_models"] = [m for m in raw["semantic_models"] if m.get("name") == "trade_day"]
    tenant_layer = SemanticLayer.from_manifest_dict(raw)
    await reg.publish(layer=tenant_layer, tenant_id="t1", by="admin")

    got = await reg.get_published_layer(tenant_id="t1")
    assert got is not None
    assert [m.name for m in got.list_metrics()] == ["GMV"]
    # 其它租户仍拿到全局（19 个）
    other = await reg.get_published_layer(tenant_id="t2")
    assert other is not None and len(other.list_metrics()) > 1

    # 仓储方法自检
    assert await repo.count_for(tenant_id="t1") == 1
    assert await repo.count_for(tenant_id="") == 1


async def test_publish_invalid_state_raises():
    reg = _registry()
    with pytest.raises(ValueError):
        await reg.publish(layer=SemanticLayer(), tenant_id="", by="x", state="bogus")


async def test_list_versions():
    reg = _registry()
    layer = SemanticLayer()
    await reg.publish(layer=layer, tenant_id="t1", by="a")
    await reg.publish(layer=layer, tenant_id="t1", by="a")
    rows = await reg.list_versions(tenant_id="t1")
    assert [r.version for r in rows] == [2, 1]  # 版本倒序


# ---------- 运行时装配（build_layer） ----------


async def test_build_layer_yaml_default():
    layer = await build_layer(settings=_settings("yaml"))
    assert len(layer.list_metrics()) == 22


async def test_build_layer_db_uses_published_snapshot():
    reg = _registry()
    base = SemanticLayer()
    raw = base.manifest.to_dict()
    raw["metrics"] = [m for m in raw["metrics"] if m.get("name") == "GMV"]
    raw["semantic_models"] = [m for m in raw["semantic_models"] if m.get("name") == "trade_day"]
    await reg.publish(layer=SemanticLayer.from_manifest_dict(raw), tenant_id="", by="admin")

    layer = await build_layer(settings=_settings("db"), registry=reg)
    assert [m.name for m in layer.list_metrics()] == ["GMV"]


async def test_build_layer_db_falls_back_to_yaml_when_empty():
    layer = await build_layer(settings=_settings("db"), registry=_registry())
    # 无任何快照 -> 回落 YAML 基座（19 个），不失败
    assert len(layer.list_metrics()) == 22


async def test_build_layer_db_without_registry_falls_back():
    layer = await build_layer(settings=_settings("db"), registry=None)
    assert len(layer.list_metrics()) == 22


# ---------- 写入口自校验 ----------


async def test_publish_rejects_full_payload_without_models():
    """想发 overlay 却忘了 mode='overlay' 是典型误操作。

    若放行，故障会在每次装载时表现为"快照反序列化失败 → 静默回落 YAML"，
    排查成本极高；写入口直接拒绝，让错误停在产生它的地方。
    """
    reg = _registry()
    with pytest.raises(ValueError, match="mode='overlay'"):
        await reg.publish(
            payload={"metrics": [{"name": "GMV", "type": "simple",
                                  "type_params": {"measure": "gmv_amount"}}]},
            tenant_id="t1",
            by="admin",
        )
    # 同样的 payload 显式声明 overlay 就是合法用法
    rec = await reg.publish(
        mode="overlay",
        payload={"metrics": [{"name": "GMV", "type": "simple",
                              "type_params": {"measure": "gmv_amount"}}]},
        tenant_id="t1",
        by="admin",
    )
    assert rec.mode == "overlay"


async def test_publish_rejects_unloadable_full_payload():
    """引用不存在的 measure / 空模型的 full 快照在写入口被拒。"""
    reg = _registry()
    raw = SemanticLayer().manifest.to_dict()
    raw["metrics"] = [
        {"name": "幽灵指标", "type": "simple", "type_params": {"measure": "不存在"}}
    ]
    with pytest.raises(ValueError, match="无法装载"):
        await reg.publish(payload=raw, tenant_id="", by="admin")


async def test_publish_overlay_without_payload_is_rejected():
    """overlay 只给 layer（整份定义）没给 payload 时，错误必须指对方向。

    放行的话会在更深的地方报"overlay 不含任何条目"，把"忘了传 payload"
    伪装成"payload 是空的"。
    """
    reg = _registry()
    with pytest.raises(ValueError, match="必须提供 payload"):
        await reg.publish(layer=SemanticLayer(), tenant_id="t1", by="admin", mode="overlay")


async def test_publish_valid_full_payload_still_accepted():
    """校验只挡坏数据，正常裁剪过的 payload（单模型单指标）照常写入。"""
    reg = _registry()
    rec = await reg.publish(payload=_trimmed_raw(), tenant_id="t1", by="admin")
    assert rec.mode == "full"
    got = await reg.get_published_layer(tenant_id="t1")
    assert got is not None and [m.name for m in got.list_metrics()] == ["GMV"]


# ---------- 路由注册（发布/查询端点） ----------


def test_publish_endpoints_registered():
    from dmf_agent.api.routers.semantic_registry import router

    paths = {r.path for r in router.routes}
    assert "/semantic/registry/publish" in paths
    assert "/semantic/registry/published" in paths
