"""语义层热更新测试（不重启进程即让新快照生效）。

关键不变量：
- 刷新后就地改写 ``runtime.semantic_layer`` **与** ``deps.semantic_layer``
  （图节点在调用时读 ``deps.semantic_layer``，两者必须指向同一对象）；
- 刷新失败时**保留旧层**——先解析成功再改写，绝不出现"刷新把线上语义层弄没了"；
- 仓储故障不抛错，降级为 YAML 基座（取数不阻断）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from dmf_agent.db.memory import InMemorySemanticPublishedRepository
from dmf_agent.semantic.layer import SemanticLayer
from dmf_agent.services import semantic_layer as sl


def _trimmed_raw(metric: str = "GMV", model: str = "trade_day") -> dict:
    raw = SemanticLayer().manifest.to_dict()
    raw["metrics"] = [m for m in raw["metrics"] if m.get("name") == metric]
    raw["semantic_models"] = [m for m in raw["semantic_models"] if m.get("name") == model]
    return raw


def _runtime(source: str = "db", repo=None) -> SimpleNamespace:
    repo = repo if repo is not None else InMemorySemanticPublishedRepository()
    deps = SimpleNamespace(semantic_layer=SemanticLayer(), metric_engine="old-engine")
    return SimpleNamespace(
        settings=SimpleNamespace(
            semantic=SimpleNamespace(source=source, manifest_dir="")
        ),
        db=SimpleNamespace(semantic_published_repo=repo),
        semantic_layer=deps.semantic_layer,
        metric_engine="old-engine",
        semantic_resolution=None,
        starrocks_client=None,
        semantic_refresh_lock=asyncio.Lock(),
        agent_deps=deps,
    )


async def test_refresh_swaps_layer_and_deps_together():
    rt = _runtime()
    from dmf_agent.semantic.registry import SemanticRegistry

    reg = SemanticRegistry(rt.db.semantic_published_repo)
    await reg.publish(payload=_trimmed_raw(), tenant_id="", by="sys")

    resolved = await sl.refresh_semantic_layer(rt)

    assert resolved.effective_source == "db"
    assert [m.name for m in resolved.layer.list_metrics()] == ["GMV"]
    # runtime 与 deps 必须是**同一个**新对象，否则图节点仍用旧定义
    assert rt.semantic_layer is resolved.layer
    assert rt.agent_deps.semantic_layer is resolved.layer
    assert rt.semantic_resolution is resolved
    assert rt.agent_deps.metric_engine is rt.metric_engine


async def test_refresh_picks_up_newly_approved_snapshot():
    rt = _runtime()
    from dmf_agent.semantic.registry import SemanticRegistry

    reg = SemanticRegistry(rt.db.semantic_published_repo)
    rec = await reg.publish(payload=_trimmed_raw(), tenant_id="", state="draft", by="analyst")

    # 草稿态：运行时看不到
    await sl.refresh_semantic_layer(rt)
    assert len(rt.semantic_layer.list_metrics()) == 22

    await reg.submit(rec.id)
    await reg.approve(rec.id)
    await sl.refresh_semantic_layer(rt)
    assert [m.name for m in rt.semantic_layer.list_metrics()] == ["GMV"]


async def test_refresh_on_yaml_source_does_not_touch_db():
    rt = _runtime(source="yaml")
    resolved = await sl.refresh_semantic_layer(rt)
    assert resolved.effective_source == "yaml"
    assert resolved.fell_back is False
    assert rt.semantic_layer is resolved.layer
    assert rt.agent_deps.semantic_layer is resolved.layer


async def test_refresh_failure_keeps_old_layer(monkeypatch):
    rt = _runtime()
    before_layer = rt.semantic_layer
    before_engine = rt.metric_engine

    async def _boom(**_kwargs):
        raise RuntimeError("装配爆炸")

    monkeypatch.setattr(sl, "resolve_layer", _boom)

    with pytest.raises(RuntimeError):
        await sl.refresh_semantic_layer(rt)

    # 旧层原样保留，进程继续可用
    assert rt.semantic_layer is before_layer
    assert rt.metric_engine == before_engine
    assert rt.agent_deps.semantic_layer is before_layer


async def test_refresh_degrades_to_yaml_when_repo_broken():
    class _BrokenRepo(InMemorySemanticPublishedRepository):
        async def list_for(self, *, tenant_id: str = "", limit: int = 100):  # type: ignore[override]
            raise RuntimeError("仓储不可用")

    rt = _runtime(repo=_BrokenRepo())
    resolved = await sl.refresh_semantic_layer(rt)
    assert resolved.effective_source == "yaml"
    assert resolved.fell_back is True
    assert len(resolved.layer.list_metrics()) == 22


async def test_refresh_without_lock_still_works():
    rt = _runtime()
    rt.semantic_refresh_lock = None
    resolved = await sl.refresh_semantic_layer(rt)
    assert resolved.layer is not None


async def test_concurrent_refresh_is_serialized():
    rt = _runtime()
    from dmf_agent.semantic.registry import SemanticRegistry

    reg = SemanticRegistry(rt.db.semantic_published_repo)
    await reg.publish(payload=_trimmed_raw(), tenant_id="", by="sys")

    results = await asyncio.gather(*(sl.refresh_semantic_layer(rt) for _ in range(4)))
    # 每次刷新各建一个新层对象（幂等重载），内容必须一致
    assert all(len(r.layer.list_metrics()) == 1 for r in results)
    # 最终运行时持有的是「最后完成的那次」的结果，且 deps 与 runtime 未脱钩
    assert rt.semantic_layer in [r.layer for r in results]
    assert rt.agent_deps.semantic_layer is rt.semantic_layer
    assert rt.agent_deps.metric_engine is rt.metric_engine


def test_runtime_and_reload_endpoints_registered():
    from dmf_agent.api.routers.semantic_registry import router

    paths = {r.path for r in router.routes}
    assert "/semantic/registry/runtime" in paths
    assert "/semantic/registry/reload" in paths


# ------------------------------------------------- "推"与"拉"的接线（同一份回写，不自环）


async def test_refresh_rebases_scope_service_without_self_loop():
    """热更新（"推"）必须把请求级解析服务的基座一起换掉并清缓存。

    漏了这一步会出现最难受的失效：面板显示已更新，但租户缓存被清空后拿**旧基座**重建，
    取数继续用旧定义。同时 rebase 不得回调 on_base_changed（那会自环）。
    """
    from dmf_agent.semantic.registry import SemanticRegistry
    from dmf_agent.semantic.runtime import resolve_layer
    from dmf_agent.services.semantic_scope import SemanticScopeService

    rt = _runtime()
    registry = SemanticRegistry(rt.db.semantic_published_repo)
    await registry.publish(payload=_trimmed_raw(), tenant_id="", by="sys")

    stale_base = await resolve_layer(settings=rt.settings, tenant_id="", registry=registry)
    scope = SemanticScopeService(
        settings=rt.settings,
        base=stale_base,
        base_engine="STALE-ENGINE",
        registry=registry,
        client=object(),  # 只需非 None：enabled 要求层与引擎成对切换
        base_probe_ttl_s=0.0,
    )
    notified: list = []
    scope.on_base_changed = lambda resolved, engine: notified.append(resolved)
    rt.semantic_scope = scope

    # 让租户缓存里先有一份条目，验证 rebase 会清掉
    await scope.resolved_for("t1")
    assert scope.stats()["cached_tenants"] == 1

    # 发布第二份快照（换成另一个指标）→ 全局链变化
    await registry.publish(payload=_trimmed_raw(metric="订单数"), tenant_id="", by="sys")
    resolved = await sl.refresh_semantic_layer(rt)

    assert scope.base is resolved  # 基座已换成新解析结果
    assert scope.stats()["cached_tenants"] == 0  # 租户缓存被清
    assert notified == []  # 没有 rebase→on_base_changed→rebase 自环
    # 且下一次探测不会白重建（rebase 把指纹记成了新基座自己的链）
    assert await scope.resolved_for("") is resolved
    assert scope.stats()["base_rebuilds"] == 0
