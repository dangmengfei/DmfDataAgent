"""多租户请求级语义层装配测试。

关键不变量（都是"错了就取错数据"，不是体验问题）：
- 图节点读到的 ``deps.semantic_layer`` 是**本请求**的租户层，并发请求互不串台；
- 作用域必须在 ``finally`` 里复位 —— 残留会让后续请求用别人的口径；
- ``deps`` 的 property 化不改任何既有语义（kwargs / 默认 None / ``is None`` / 热更新赋值）；
- 租户链指纹不变 ⇒ 复用已构建层；变化 ⇒ 重建；仓储异常 ⇒ 回落基座且**不缓存**；
- 层与引擎成对切换（不允许"目录租户化、取数全局化"的半应用状态）；
- **基座也要能"拉"**：别实例发布了新快照 ⇒ 本实例下一请求自动重定基 + 回写 runtime/deps，
  且"没变就不重建"（不 churn 基座对象同一性、不把请求路径变成查询热点）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from dmf_agent.agents.metric_agent.deps import AgentDeps
from dmf_agent.config.settings import StarRocksSettings
from dmf_agent.db.memory import InMemorySemanticPublishedRepository
from dmf_agent.schemas.semantic_publish import SemanticPublishedData
from dmf_agent.semantic.layer import SemanticLayer
from dmf_agent.semantic.overlay import chain_signature_of
from dmf_agent.semantic.registry import SemanticRegistry
from dmf_agent.semantic.runtime import resolve_layer
from dmf_agent.semantic.scope import (
    RequestScope,
    bind_scope,
    current_scope,
    reset_scope,
    use_scope,
)
from dmf_agent.semantic.starrocks import StarRocksClient
from dmf_agent.services.semantic_layer import apply_resolved
from dmf_agent.services.semantic_scope import SemanticScopeService

TENANT_TAG = "租户专属GMV"


# ---------------------------------------------------------------- 脚手架


def _settings(source: str = "db") -> SimpleNamespace:
    return SimpleNamespace(semantic=SimpleNamespace(source=source, manifest_dir=""))


def _full_payload(metric: str = "GMV", model: str = "trade_day") -> dict:
    """整份（裁剪过的）manifest：只留一个指标 + 一个模型，便于断言计数。"""
    raw = SemanticLayer().manifest.to_dict()
    raw["metrics"] = [m for m in raw["metrics"] if m.get("name") == metric]
    raw["semantic_models"] = [m for m in raw["semantic_models"] if m.get("name") == model]
    return raw


def _overlay_payload(tag: str = TENANT_TAG) -> dict:
    """租户 overlay：只替换 GMV 一条（整条替换语义），给它加一个专属别名。"""
    src = next(m for m in SemanticLayer().manifest.to_dict()["metrics"] if m["name"] == "GMV")
    patched = {**src, "aliases": [*(src.get("aliases") or []), tag]}
    return {"metrics": [patched]}


def _client() -> StarRocksClient:
    return StarRocksClient(StarRocksSettings())


async def _service(repo=None, *, source: str = "db", base_tenant: str = "", probe_ttl: float = 1.0):
    """构造 (服务, 注册中心, 基座 ResolvedLayer)；基座按 base_tenant 解析。"""
    repo = repo if repo is not None else InMemorySemanticPublishedRepository()
    settings = _settings(source)
    registry = SemanticRegistry(repo)
    client = _client()
    base = await resolve_layer(settings=settings, tenant_id=base_tenant, registry=registry)
    service = SemanticScopeService(
        settings=settings,
        base=base,
        base_engine="BASE-ENGINE",
        registry=registry,
        client=client,
        base_probe_ttl_s=probe_ttl,
    )
    return service, registry, base


# ---------------------------------------------------------------- ContextVar 基础


async def test_scope_bind_and_reset_roundtrip():
    assert current_scope() is None

    token = bind_scope(RequestScope(tenant_id="t1", layer="L1", engine="E1"))
    scope = current_scope()
    assert scope is not None and scope.tenant_id == "t1" and scope.active is True

    reset_scope(token)
    assert current_scope() is None
    # 幂等：重复复位 / 复位 None 都不应抛 —— reset_scope 只在 finally 里被调用，
    # 从 finally 抛出会顶替真正的业务异常，把根因藏起来
    reset_scope(token)
    reset_scope(None)


async def test_scope_use_context_manager_is_noop_for_none():
    async with use_scope(None) as scope:
        assert scope is None
        assert current_scope() is None
    assert current_scope() is None


async def test_scope_is_isolated_between_concurrent_tasks():
    """并发隔离是这套机制存在的理由：两个请求同时跑，各读各的层。"""
    seen: dict[str, list] = {"a": [], "b": []}

    async def _worker(name: str, layer: str):
        async with use_scope(RequestScope(tenant_id=name, layer=layer)):
            for _ in range(3):
                await asyncio.sleep(0)  # 主动让出，制造交错
                seen[name].append(current_scope().layer)
        return current_scope()

    outer = await asyncio.gather(_worker("a", "LA"), _worker("b", "LB"))

    assert seen["a"] == ["LA", "LA", "LA"]
    assert seen["b"] == ["LB", "LB", "LB"]
    # 任务结束后各自 context 都已复位（gather 的返回值是任务末尾读到的）
    assert outer == [None, None]
    assert current_scope() is None


# ---------------------------------------------------------------- deps property


def test_deps_property_preserves_construction_semantics():
    deps = AgentDeps(settings=None, get_chat_model=lambda: None, semantic_layer="L", metric_engine="E")
    assert deps.semantic_layer == "L" and deps.metric_engine == "E"

    # 默认值仍是 None，`is None` 判定照旧（这是节点里的常见写法）
    fresh = AgentDeps(settings=None, get_chat_model=lambda: None)
    assert fresh.semantic_layer is None and fresh.metric_engine is None

    # 位置/关键字构造、repr、asdict 均不受 property 影响
    import dataclasses

    assert "semantic_layer" in repr(deps)
    assert dataclasses.asdict(deps)["semantic_layer"] == "L"


def test_deps_reads_request_scope_then_falls_back_to_base():
    deps = AgentDeps(
        settings=None, get_chat_model=lambda: None, semantic_layer="BASE", metric_engine="BASE-ENGINE"
    )

    token = bind_scope(RequestScope(tenant_id="t1", layer="TENANT", engine="TENANT-ENGINE"))
    assert deps.semantic_layer == "TENANT"
    assert deps.metric_engine == "TENANT-ENGINE"

    # 热更新"就地改写"写的是基座：作用域覆盖仍在，复位后才看得到新基座
    deps.semantic_layer = "BASE2"
    assert deps.semantic_layer == "TENANT"
    reset_scope(token)
    assert deps.semantic_layer == "BASE2"


def test_deps_scope_with_none_layer_does_not_blank_out_base():
    """作用域只带租户 id、没带层（异常路径）时不能把基座读成 None。"""
    deps = AgentDeps(settings=None, get_chat_model=lambda: None, semantic_layer="BASE")
    token = bind_scope(RequestScope(tenant_id="t1", layer=None, engine=None))
    assert deps.semantic_layer == "BASE"
    reset_scope(token)


# ---------------------------------------------------------------- 解析与缓存


async def test_service_disabled_for_yaml_source_returns_base_untouched():
    service, registry, base = await _service(source="yaml")
    await registry.publish(layer=SemanticLayer(), tenant_id="", by="sys")

    assert service.enabled is False
    assert await service.resolved_for("t1") is base
    assert await service.scope_for("t1") is None
    assert service.stats()["cached_tenants"] == 0


async def test_service_disabled_without_starrocks_client():
    """没有客户端就无法成对切换引擎 → 整体不启用（避免半应用状态）。"""
    repo = InMemorySemanticPublishedRepository()
    settings = _settings("db")
    registry = SemanticRegistry(repo)
    base = await resolve_layer(settings=settings, tenant_id="", registry=registry)
    service = SemanticScopeService(settings=settings, base=base, registry=registry, client=None)
    assert service.enabled is False
    assert await service.resolved_for("t1") is base


async def test_tenant_overlay_applied_and_global_returns_base_identity():
    service, registry, base = await _service()
    await registry.publish(payload=_full_payload(), tenant_id="", by="sys")
    service.rebase(await resolve_layer(settings=_settings(), registry=registry), "BASE-ENGINE")
    base = service.base

    await registry.publish(mode="overlay", payload=_overlay_payload(), tenant_id="t1", by="admin")

    tenant = await service.resolved_for("t1")
    assert tenant.scope == "tenant"
    # 版本号是**租户内独立序列**：租户第一条 overlay 就是 v1，与全局 v1 并存 ——
    # 所以链上每条必须带 scope，否则"全局 v1 + 租户 v1"看起来像同一个东西
    assert tenant.overlay_versions == [1]
    assert resolver_has_alias(tenant.layer, TENANT_TAG)

    # 全局路径（含无 overlay 的租户）必须拿到**基座本体**，而不是新造一个等价对象：
    # 热更新断言与 runtime.semantic_layer 的同一性依赖这一点
    assert await service.resolved_for("") is base
    assert await service.resolved_for("t-no-overlay") is base

    scope = await service.scope_for("t1")
    assert scope is not None and scope.layer is tenant.layer
    assert scope.engine is not None and scope.engine.layer is tenant.layer
    assert scope.describe()["chain"] == ["global:v1", "tenant:v1"]
    # 无 overlay 的租户不需要覆盖，节点直接用基座
    assert await service.scope_for("t-no-overlay") is None


def resolver_has_alias(layer: SemanticLayer, alias: str) -> bool:
    return any(alias in (m.aliases or []) for m in layer.list_metrics())


async def test_source_db_reads_full_snapshot_as_base():
    """source=db 且 DB 有全局 full 快照时，基座本身就是 DB 层（而非 YAML）。"""
    service, registry, base = await _service()
    await registry.publish(payload=_full_payload(), tenant_id="", by="sys")
    resolved = await service.resolved_for("")
    # 基座是启动期解析结果（此时还没有快照 → YAML），服务应识别出 DB 链已变化
    assert base.effective_source == "yaml"
    assert resolved.effective_source == "db"
    assert len(resolved.layer.list_metrics()) == 1


async def test_cache_hit_then_rebuild_after_new_snapshot():
    service, registry, base = await _service()
    await registry.publish(payload=_full_payload(), tenant_id="", by="sys")
    service.rebase(await resolve_layer(settings=_settings(), registry=registry), "BASE-ENGINE")

    await registry.publish(mode="overlay", payload=_overlay_payload("别名A"), tenant_id="t1", by="admin")
    first = await service.resolved_for("t1")
    stats_after_build = service.stats()
    assert stats_after_build["builds"] == 1

    second = await service.resolved_for("t1")
    assert second is first  # 指纹未变 ⇒ 复用同一对象，不重建
    assert service.stats()["hits"] == 1
    assert service.stats()["builds"] == 1

    # 新 overlay 让链指纹变化 ⇒ 重建，且新层带上新别名
    await registry.publish(mode="overlay", payload=_overlay_payload("别名B"), tenant_id="t1", by="admin")
    third = await service.resolved_for("t1")
    assert third is not first
    assert resolver_has_alias(third.layer, "别名B")
    assert service.stats()["builds"] == 2


async def test_invalidate_clears_cache_and_rebuilds():
    service, registry, base = await _service()
    await registry.publish(payload=_full_payload(), tenant_id="", by="sys")
    service.rebase(await resolve_layer(settings=_settings(), registry=registry), "BASE-ENGINE")
    await registry.publish(mode="overlay", payload=_overlay_payload(), tenant_id="t1", by="admin")

    first = await service.resolved_for("t1")
    assert service.invalidate("t1") == 1
    assert service.stats()["cached_tenants"] == 0

    rebuilt = await service.resolved_for("t1")
    assert rebuilt is not first and rebuilt.scope == "tenant"
    # 清空全部
    service.invalidate()
    assert service.stats()["cached_tenants"] == 0


async def test_repo_failure_falls_back_to_base_and_does_not_cache():
    class _BrokenRepo(InMemorySemanticPublishedRepository):
        async def list_for(self, *, tenant_id: str = "", limit: int = 100):  # type: ignore[override]
            raise RuntimeError("仓储不可用")

    service, registry, base = await _service(repo=_BrokenRepo())
    assert await service.resolved_for("t1") is base
    assert service.stats()["errors"] >= 1
    assert service.stats()["cached_tenants"] == 0


async def test_rebase_swaps_base_for_hot_reload():
    """热更新换基座后，未缓存的租户必须重建在**新**基座上（否则会读到旧定义）。"""
    service, registry, base = await _service()
    await registry.publish(mode="overlay", payload=_overlay_payload(TENANT_TAG), tenant_id="t1", by="admin")

    # 基座仍是全量 YAML（19 个指标）
    assert len(service.base.layer.list_metrics()) == 22
    tenant_before = await service.resolved_for("t1")
    assert len(tenant_before.layer.list_metrics()) == 22  # overlay 只改别名，不改条目数

    new_base = await resolve_layer(settings=_settings(), tenant_id="", registry=registry)
    service.rebase(new_base, "NEW-ENGINE")
    assert service.base is new_base
    assert service.stats()["cached_tenants"] == 0  # rebase 顺带清缓存

    tenant_after = await service.resolved_for("t1")
    assert tenant_after is not tenant_before


async def test_stats_reports_tenant_chain_summary():
    service, registry, base = await _service()
    await registry.publish(payload=_full_payload(), tenant_id="", by="sys")
    service.rebase(await resolve_layer(settings=_settings(), registry=registry), "BASE-ENGINE")
    await registry.publish(mode="overlay", payload=_overlay_payload(), tenant_id="t1", by="admin")
    await service.resolved_for("t1")

    stats = service.stats()
    assert stats["enabled"] is True
    row = next(t for t in stats["tenants"] if t["tenant_id"] == "t1")
    assert row["scope"] == "tenant"
    assert row["overlay_versions"] == [1]
    assert stats["cached_tenants"] == 1


# ---------------------------------------------------------------- 端到端：图执行期读到租户层


class _RecordingGraph:
    """假装是 LangGraph：把执行期读到的「本请求作用域 / deps 层」记下来。"""

    def __init__(self, deps: AgentDeps) -> None:
        self._deps = deps
        self.seen: dict = {}

    async def astream(self, state, config, stream_mode=None):
        self.seen["scope"] = current_scope()
        # 节点里的写法就是这样读层（deps.semantic_layer 是 property）
        self.seen["layer"] = self._deps.semantic_layer
        self.seen["engine"] = self._deps.metric_engine
        yield ("updates", {})


def _chat_service(deps: AgentDeps, scope_service) -> tuple:
    from dmf_agent.config.settings import Settings
    from dmf_agent.db.memory import InMemoryMessageRepository, InMemorySessionRepository
    from dmf_agent.services.chat_service import ChatService

    graph = _RecordingGraph(deps)
    service = ChatService(
        graph=graph,
        session_repo=InMemorySessionRepository(),
        message_repo=InMemoryMessageRepository(),
        settings=Settings(),
        semantic_scope=scope_service,
    )
    return service, graph


async def _drain(service, tenant_id: str) -> None:
    async for _event in service.astream_chat(
        session_id=None, user_id="u1", message="GMV 是多少", tenant_id=tenant_id
    ):
        pass


async def test_graph_runs_on_tenant_layer_and_scope_is_reset_afterwards():
    service_scope, registry, base = await _service()
    await registry.publish(payload=_full_payload(), tenant_id="", by="sys")
    service_scope.rebase(
        await resolve_layer(settings=_settings(), registry=registry), "BASE-ENGINE"
    )
    await registry.publish(mode="overlay", payload=_overlay_payload(), tenant_id="t1", by="admin")
    tenant = await service_scope.resolved_for("t1")

    deps = AgentDeps(
        settings=None,
        get_chat_model=lambda: None,
        semantic_layer=service_scope.base.layer,
        metric_engine="BASE-ENGINE",
    )
    service, graph = _chat_service(deps, service_scope)

    await _drain(service, "t1")

    # 图执行期：作用域 + 层 + 引擎三者都切到该租户口径
    assert graph.seen["scope"].tenant_id == "t1"
    assert graph.seen["layer"] is tenant.layer
    assert graph.seen["engine"] is not None
    assert resolver_has_alias(graph.seen["layer"], TENANT_TAG)
    # 执行完毕必须复位，否则残留会让后续请求用别人的口径
    assert current_scope() is None
    assert deps.semantic_layer is service_scope.base.layer


async def test_graph_falls_back_to_base_without_tenant():
    service_scope, registry, base = await _service()
    await registry.publish(payload=_full_payload(), tenant_id="", by="sys")
    service_scope.rebase(
        await resolve_layer(settings=_settings(), registry=registry), "BASE-ENGINE"
    )
    deps = AgentDeps(
        settings=None,
        get_chat_model=lambda: None,
        semantic_layer="BASE-LAYER",
        metric_engine="BASE-ENGINE",
    )
    service, graph = _chat_service(deps, service_scope)

    await _drain(service, "")

    assert graph.seen["scope"] is None
    assert graph.seen["layer"] == "BASE-LAYER"
    assert current_scope() is None


async def test_graph_survives_scope_resolution_failure(monkeypatch):
    """作用域解析炸了也必须能继续跑（回落全局层），不能把整轮问答弄挂。"""

    class _Broken:
        async def scope_for(self, tenant_id: str = ""):
            raise RuntimeError("解析服务不可用")

    deps = AgentDeps(
        settings=None, get_chat_model=lambda: None, semantic_layer="BASE-LAYER"
    )
    service, graph = _chat_service(deps, _Broken())

    await _drain(service, "t1")

    assert graph.seen["scope"] is None
    assert graph.seen["layer"] == "BASE-LAYER"
    assert current_scope() is None


# ---------------------------------------------------------------- 路由层（读接口租户化）


def _runtime_for(service, base_layer) -> SimpleNamespace:
    return SimpleNamespace(
        semantic_scope=service,
        semantic_layer=base_layer,
        metric_engine="BASE-ENGINE",
    )


def _current(tenant_id: str):
    from dmf_agent.schemas.auth import CurrentUser

    return CurrentUser(id="u1", username="u1", tenant_id=tenant_id, role="viewer")


async def test_router_layer_and_engine_follow_tenant():
    from dmf_agent.api.routers import semantic as sr

    service, registry, base = await _service()
    await registry.publish(payload=_full_payload(), tenant_id="", by="sys")
    service.rebase(await resolve_layer(settings=_settings(), registry=registry), "BASE-ENGINE")
    await registry.publish(mode="overlay", payload=_overlay_payload(), tenant_id="t1", by="admin")
    tenant = await service.resolved_for("t1")
    rt = _runtime_for(service, service.base.layer)

    t1_layer = await sr._layer_for(rt, _current("t1"))
    assert t1_layer is tenant.layer
    t1_engine = await sr._engine_for(rt, _current("t1"))
    assert t1_engine.layer is tenant.layer

    # 无租户 / 别的租户拿到的都是基座本体（不泄露他人口径）
    assert await sr._layer_for(rt, _current("")) is service.base.layer
    assert await sr._layer_for(rt, _current("t2")) is service.base.layer


async def test_router_falls_back_when_service_missing_or_broken():
    from dmf_agent.api.routers import semantic as sr

    base_layer = SemanticLayer()
    # 未装配解析服务（旧部署 / 测试）：直接用进程级全局层
    bare = SimpleNamespace(semantic_scope=None, semantic_layer=base_layer, metric_engine="E")
    assert await sr._layer_for(bare, _current("t1")) is base_layer

    class _Broken:
        async def scope_for(self, tenant_id: str = ""):
            raise RuntimeError("boom")

    broken = _runtime_for(_Broken(), base_layer)
    assert await sr._layer_for(broken, _current("t1")) is base_layer


async def test_router_metrics_payload_shapes_share_one_builder():
    """指标目录结构由单一构造器产出，避免路由与其它读接口各写一份。"""
    from dmf_agent.api.routers import semantic as sr

    service, registry, base = await _service(source="yaml")
    payload = sr._metrics_payload(SemanticLayer())
    gmv = next(m for m in payload if m["name"] == "GMV")
    assert gmv["type"] == "simple" and gmv["base_measures"]


# ---------------------------------------------------------------- 基座"拉"式自愈（免广播）


async def test_base_probe_does_not_rebuild_when_chain_unchanged():
    """启动后第一次探测不该白重建：基座刚从 DB 解析出来，那一刻它就是新的。

    这条同时锁住**对象同一性**——如果首探无脑重建，``deps.semantic_layer`` /
    ``runtime.semantic_layer`` 会在第一个请求上被换成一个等价但不同的对象，
    热更新断言与"拿到基座本体"的约定全部失效。
    """
    repo = InMemorySemanticPublishedRepository()
    registry = SemanticRegistry(repo)
    await registry.publish(payload=_full_payload(), tenant_id="", by="sys")
    settings = _settings("db")
    base = await resolve_layer(settings=settings, tenant_id="", registry=registry)
    assert base.effective_source == "db"

    service = SemanticScopeService(
        settings=settings,
        base=base,
        base_engine="BASE-ENGINE",
        registry=registry,
        client=_client(),
        base_probe_ttl_s=0.0,  # 每请求都探，把"探了但没重建"与"没探"区分开
    )
    assert await service.resolved_for("") is base
    stats = service.stats()
    assert stats["base_rebuilds"] == 0
    assert stats["base_signature_len"] == 1


async def test_base_self_heal_picks_up_other_instance_publish():
    """别实例发布了新的全局 full 快照 ⇒ 本实例下一请求自动重定基（"拉"，免广播）。

    这是多实例部署的核心不变量：不依赖任何广播/消息，谁改了 DB，其他实例自己会看到。
    """
    service, registry, base = await _service(probe_ttl=0.0)
    assert base.effective_source == "yaml"  # 启动时 DB 还没有快照

    applied: list = []
    service.on_base_changed = lambda resolved, engine: applied.append((resolved, engine))

    await registry.publish(payload=_full_payload(), tenant_id="", by="other-instance")

    resolved = await service.resolved_for("")
    assert resolved is service.base and resolved is not base
    assert resolved.effective_source == "db"
    assert [m.name for m in resolved.layer.list_metrics()] == ["GMV"]
    assert service.stats()["base_rebuilds"] == 1
    # 回写钩子必须被调用：否则 runtime/deps 还指着旧基座，全局请求读到的仍是旧口径
    assert len(applied) == 1
    assert applied[0][0] is resolved
    assert applied[0][1] is not None and applied[0][1].layer is resolved.layer

    # 再探不该重复重建（观测值已记住）
    assert await service.resolved_for("") is resolved
    assert service.stats()["base_rebuilds"] == 1


async def test_scope_for_global_request_refreshes_base():
    """全局请求**不需要**作用域覆盖，但基座必须被刷新 —— 它读的正是进程级基座。"""
    service, registry, base = await _service(probe_ttl=0.0)
    applied: list = []
    service.on_base_changed = lambda resolved, engine: applied.append(resolved)

    await registry.publish(payload=_full_payload(), tenant_id="", by="other")
    assert await service.scope_for("") is None  # 无覆盖
    assert service.base is not base  # 但基座已经换了
    assert applied and applied[0] is service.base


async def test_base_probe_is_throttled_within_window():
    """TTL 窗口内共享一次探测：请求路径不能被变成"每请求一次 DB 探测"。"""

    class _CountingRegistry(SemanticRegistry):
        def __init__(self, repo):
            super().__init__(repo)
            self.probes = 0

        async def effective_chain_signature(self, *, tenant_id: str = ""):
            self.probes += 1
            return await super().effective_chain_signature(tenant_id=tenant_id)

    repo = InMemorySemanticPublishedRepository()
    settings = _settings("db")
    registry = _CountingRegistry(repo)
    base = await resolve_layer(settings=settings, tenant_id="", registry=registry)
    service = SemanticScopeService(
        settings=settings,
        base=base,
        base_engine="BASE-ENGINE",
        registry=registry,
        client=_client(),
        base_probe_ttl_s=60.0,
    )

    await service.resolved_for("")
    assert registry.probes == 1
    for _ in range(5):
        await service.resolved_for("t1")
        await service.scope_for("t2")
    assert registry.probes == 1  # 窗口内不再探测
    assert service.stats()["base_probe_ttl_s"] == 60.0


async def test_base_probe_failure_keeps_old_base_and_counts_error():
    class _BrokenRepo(InMemorySemanticPublishedRepository):
        async def list_for(self, *, tenant_id: str = "", limit: int = 100):  # type: ignore[override]
            raise RuntimeError("仓储不可用")

    service, registry, base = await _service(repo=_BrokenRepo(), probe_ttl=0.0)
    assert await service.resolved_for("") is base  # 探测失败不换基座
    stats = service.stats()
    assert stats["errors"] >= 1
    assert stats["base_rebuilds"] == 0


async def test_rebase_records_signature_without_notifying():
    """``rebase`` 不回写（断开 rebase→on_base_changed→rebase 自环），且下次探测不白重建。"""
    service, registry, base = await _service(probe_ttl=0.0)
    called: list = []
    service.on_base_changed = lambda resolved, engine: called.append(resolved)

    await registry.publish(payload=_full_payload(), tenant_id="", by="sys")
    new_base = await resolve_layer(settings=_settings(), tenant_id="", registry=registry)
    service.rebase(new_base, "NEW-ENGINE")

    assert called == []  # 没有自环
    assert service.base is new_base
    assert await service.resolved_for("") is new_base
    assert service.stats()["base_rebuilds"] == 0  # 指纹取自新基座自己的链 ⇒ 无需重建


async def test_effective_chain_signature_matches_resolved_chain():
    """两套归一化必须同形：只要有一处漂移，基座比对就退化成"每个窗口重建一次"。"""
    service, registry, base = await _service(probe_ttl=0.0)
    await registry.publish(payload=_full_payload(), tenant_id="", by="sys")

    resolved = await service.resolved_for("")
    signature = await registry.effective_chain_signature(tenant_id="")
    assert signature != ()
    assert signature == chain_signature_of(resolved.chain)


async def test_corrupt_payload_falls_back_without_probe_churn():
    """全局快照 payload 损坏 ⇒ 基座回落 YAML，但"已探过"要记住，否则每个窗口都重建。"""
    repo = InMemorySemanticPublishedRepository()
    registry = SemanticRegistry(repo)
    # 故意绕过写入口自校验直接落库：守卫要防的正是"绕过写入口的坏数据"
    await repo.add(
        SemanticPublishedData(
            tenant_id="",
            version=1,
            state="published",
            mode="full",
            payload={"semantic_models": "not-a-list"},
        )
    )
    settings = _settings("db")
    base = await resolve_layer(settings=settings, tenant_id="", registry=registry)
    assert base.effective_source == "yaml"  # 坏 payload → 回落

    service = SemanticScopeService(
        settings=settings,
        base=base,
        base_engine="BASE-ENGINE",
        registry=registry,
        client=_client(),
        base_probe_ttl_s=0.0,
    )
    first = await service.resolved_for("")
    assert service.stats()["base_rebuilds"] == 1  # DB 链与基座不同 → 建一次
    second = await service.resolved_for("")
    assert second is first
    assert service.stats()["base_rebuilds"] == 1  # 不 churn


def _fail_if_rebased():  # pragma: no cover - 只在断言失败时被调用
    raise AssertionError("apply_resolved 不得回调 rebase（会形成自环）")


def test_apply_resolved_writes_through_runtime_and_deps():
    """回写只做 runtime/deps 同步，**不含** rebase —— 这正是"不自环"的结构性保证。"""
    deps = AgentDeps(settings=None, get_chat_model=lambda: None, semantic_layer="OLD")
    runtime = SimpleNamespace(
        semantic_layer="OLD",
        metric_engine="OLD-ENGINE",
        semantic_resolution=None,
        agent_deps=deps,
        # 陷阱：一旦 apply_resolved 里偷偷 rebase 就会炸，把"自环"变成红测
        semantic_scope=SimpleNamespace(rebase=_fail_if_rebased),
    )
    resolved = SimpleNamespace(layer="NEW")

    apply_resolved(runtime, resolved, "NEW-ENGINE")

    assert runtime.semantic_layer == "NEW"
    assert runtime.metric_engine == "NEW-ENGINE"
    assert runtime.semantic_resolution is resolved
    assert deps.semantic_layer == "NEW"
    assert deps.metric_engine == "NEW-ENGINE"


def test_apply_resolved_tolerates_missing_deps():
    """未装配 deps 的运行时（部分只读场景）不该因回写而报错。"""
    runtime = SimpleNamespace(semantic_layer=None, metric_engine=None, semantic_resolution=None)
    resolved = SimpleNamespace(layer="NEW")
    apply_resolved(runtime, resolved, "E")
    assert runtime.semantic_layer == "NEW" and runtime.metric_engine == "E"
