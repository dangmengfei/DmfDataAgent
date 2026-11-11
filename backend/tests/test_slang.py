"""三级黑话词典：解析器（改写）+ 种子 + 管理路由 单测。

不依赖完整 app 启动（milvus / StarRocks / Redis 等重资源），直接对解析器与路由函数
做内存模式集成测试（与 tests/test_fewshots.py 同一套手法，含"必须显式传全 Query 参数"的坑）。

重点覆盖用户明确要求的两条契约：
1. **三级作用域**同名以高层级为准（user > tenant > industry）；
2. **Redis 优先读 + 写穿同步**：读 Redis 命中直接用；未命中回源 MySQL 并回填 Redis；
   写路径"先 MySQL 后 refresh_layer 同步 Redis"。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from dmf_agent.api.routers import slang as slang_router
from dmf_agent.config.settings import SlangSettings
from dmf_agent.db.factory import DatabaseBundle
from dmf_agent.recall.slang import (
    SlangResolver,
    build_slang_resolver,
    normalize,
    validate_standard,
)
from dmf_agent.recall.slang_seed import (
    GUARD_PRIORITY,
    build_guard_entries,
    build_seed_entries,
    collect_guard_sources,
)
from dmf_agent.schemas.auth import CurrentUser
from dmf_agent.schemas.slang import (
    ENTRY_DIM_VALUE,
    ENTRY_METRIC,
    ENTRY_STOP,
    ENTRY_TERM,
    MODE_REPLACE,
    SCOPE_INDUSTRY,
    SCOPE_TENANT,
    SCOPE_USER,
    SOURCE_MANUAL,
    STATUS_DRAFT,
    STATUS_PUBLISHED,
    SlangEntryData,
    SlangImportRequest,
    SlangPatch,
    SlangPreviewRequest,
    SlangUpsert,
)
from dmf_agent.semantic.layer import SemanticLayer


# ---------- 测试替身 ----------


class FakeRedis:
    """与 RedisClient 同接口（get_json / set_json / delete），用于断言缓存行为。"""

    def __init__(self) -> None:
        self.store: dict = {}

    async def get_json(self, key):
        return self.store.get(key)

    async def set_json(self, key, value, ttl=0):
        self.store[key] = value
        return True

    async def delete(self, key):
        self.store.pop(key, None)
        return True


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
    rewrite_mode: str = "expand",
    priority: int = 100,
    dim_value: str = "",
) -> SlangEntryData:
    return SlangEntryData(
        scope=scope, tenant_id=tenant_id, user_id=user_id, term=term,
        standard=standard, entry_type=entry_type, status=status, source=source,
        rewrite_mode=rewrite_mode, priority=priority, dim_value=dim_value,
    )


def _resolver(db: DatabaseBundle, redis: FakeRedis | None = None, **kw) -> SlangResolver:
    return build_slang_resolver(
        db.slang_repo, redis, SlangSettings(**kw)
    )


def _admin() -> CurrentUser:
    return CurrentUser(
        id="admin", username="admin", tenant_id="t1", role="admin", permissions=["*"]
    )


def _analyst(uid: str = "u1", tid: str = "t1") -> CurrentUser:
    return CurrentUser(
        id=uid, username=uid, tenant_id=tid, role="analyst",
        permissions=["slang", "slang:write", "slang:review"],
    )


def _runtime(db: DatabaseBundle, redis: FakeRedis | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        db=db,
        slang_resolver=_resolver(db, redis),
        semantic_layer=SemanticLayer(),
        settings=SimpleNamespace(slang=SlangSettings()),
    )


# ---------- normalize ----------


def test_normalize_fullwidth_space_punct():
    assert normalize("  ＡＵＭ　的走势，，  ") == "AUM 的走势"
    # 全角数字 / 括号也要归一
    assert normalize("（１）") == "(1)"
    # 空串安全
    assert normalize("") == ""


# ---------- 解析器：三模式 + 三级 + 护栏 ----------


async def test_resolve_stop_expand_replace_modes():
    db = DatabaseBundle(mode="memory")
    repo = db.slang_repo
    await repo.add(_entry("帮我看下", entry_type=ENTRY_STOP, standard=""))
    await repo.add(_entry("AUM", "零售存款余额"))
    await repo.add(
        _entry("网银", standard="零售渠道", entry_type=ENTRY_DIM_VALUE, dim_value="网上银行")
    )
    await repo.add(
        _entry("块钱", "元", entry_type=ENTRY_TERM, rewrite_mode=MODE_REPLACE)
    )

    res = await _resolver(db).resolve("帮我看下AUM走势")
    # stop 删除；expand 追加
    assert "帮我看下" not in res.rewritten
    assert "零售存款余额" in res.rewritten
    assert res.mode_counts.get("stop") == 1
    assert res.degraded is False

    # replace：原地替换（"块钱" -> "元"）
    res_r = await _resolver(db).resolve("AUM三块钱")
    assert "块钱" not in res_r.rewritten and "三元" in res_r.rewritten

    # dim_value：追加维值（expand 而非替换）
    res2 = await _resolver(db).resolve("网银的存款")
    assert "网上银行" in res2.rewritten
    assert "网银" in res2.rewritten  # 保留原词


async def test_resolve_longest_priority_guard_blocks_short_slang():
    """护栏：长标准名内部的短黑话不得误命中（"不良" vs "不良贷款率"）。"""
    db = DatabaseBundle(mode="memory")
    repo = db.slang_repo
    await repo.add(_entry("不良", "不良贷款余额"))
    await repo.add(
        _entry("不良贷款率", "不良贷款率", priority=GUARD_PRIORITY,
               rewrite_mode="expand")
    )

    res = await _resolver(db).resolve("不良贷款率是多少")
    assert res.rewritten == "不良贷款率是多少"  # 未被换成余额
    assert all(h.term != "不良" for h in res.hits)

    # 没有长标准名时，短黑话正常命中
    res2 = await _resolver(db).resolve("不良多少")
    assert "不良贷款余额" in res2.rewritten


async def test_resolve_three_tier_override_same_term():
    """同名以高层级为准：user > tenant > industry。"""
    db = DatabaseBundle(mode="memory")
    repo = db.slang_repo
    await repo.add(_entry("口径", "零售存款余额"))  # industry
    await repo.add(_entry("口径", "零售贷款余额", scope=SCOPE_TENANT, tenant_id="t1"))
    await repo.add(
        _entry("口径", "存贷比", scope=SCOPE_USER, tenant_id="t1", user_id="u1")
    )

    res = await _resolver(db).resolve("口径是多少", tenant_id="t1", user_id="u1")
    assert res.hits[0].standard == "存贷比"
    assert res.hits[0].scope == SCOPE_USER

    # 仅公司身份 -> 公司级胜出
    res_t = await _resolver(db).resolve("口径是多少", tenant_id="t1")
    assert res_t.hits[0].standard == "零售贷款余额"
    assert res_t.hits[0].scope == SCOPE_TENANT

    # 无身份 -> 只回行业级
    res_i = await _resolver(db).resolve("口径是多少")
    assert res_i.hits[0].standard == "零售存款余额"


# ---------- 解析器：缓存（Redis 优先 + 写穿） ----------


async def test_resolve_redis_miss_then_hit_and_refresh_write_through():
    db = DatabaseBundle(mode="memory")
    repo = db.slang_repo
    await repo.add(_entry("AUM", "零售存款余额"))
    redis = FakeRedis()
    res = _resolver(db, redis)

    first = await res.resolve("AUM走势")
    assert first.cache_source == "mysql"  # 首次回源
    assert "slang:v1:i:-" in redis.store  # 回填 Redis
    assert redis.store["slang:v1:i:-"]["entries"]  # 快照非空

    second = await res.resolve("AUM走势")
    assert second.cache_source == "redis"  # 命中 Redis
    assert second.rewritten == first.rewritten

    # 新增一条（MySQL 先行）后写穿同步：refresh_layer 覆盖 Redis
    await repo.add(_entry("个贷", "零售贷款余额"))
    n = await res.refresh_layer(SCOPE_INDUSTRY)
    assert n == 2
    cached_terms = {e["term"] for e in redis.store["slang:v1:i:-"]["entries"]}
    assert {"AUM", "个贷"} <= cached_terms

    third = await res.resolve("个贷走势")
    assert third.cache_source == "redis"
    assert "零售贷款余额" in third.rewritten


async def test_resolve_degrades_when_disabled_or_oversize():
    db = DatabaseBundle(mode="memory")
    await db.slang_repo.add(_entry("AUM", "零售存款余额"))

    off = _resolver(db, enabled=False)
    res = await off.resolve("AUM走势")
    assert res.degraded is True and res.cache_source == "disabled"
    assert res.rewritten == normalize("AUM走势")  # 原句透传（仅归一）

    tiny = _resolver(db, max_query_len=3)
    res2 = await tiny.resolve("AUM走势很长很长")
    assert res2.degraded is True and res2.cache_source == "skipped"


async def test_preview_includes_draft_but_online_excludes():
    db = DatabaseBundle(mode="memory")
    repo = db.slang_repo
    await repo.add(_entry("新黑话", "零售存款余额", status=STATUS_DRAFT))
    res = _resolver(db)

    online = await res.resolve("新黑话")  # 线上只认 published
    assert "零售存款余额" not in online.rewritten
    preview = await res.resolve("新黑话", include_draft=True)
    assert "零售存款余额" in preview.rewritten


# ---------- 种子：护栏 + 去重 + 语义层校验 ----------


def test_build_seed_entries_dedup_and_guards():
    entries = build_seed_entries(
        metric_names=["零售存款余额", "不良贷款率"],
        dimension_names=["机构"],
        metric_aliases={"不良率": "不良贷款率", "笔均贷款": "户均贷款"},
    )
    keys = [(e.scope, e.tenant_id, e.user_id, e.entry_type, e.term) for e in entries]
    assert len(keys) == len(set(keys)), "种子不得有重复唯一键"

    guards = [e for e in entries if e.priority == GUARD_PRIORITY]
    guard_terms = {e.term for e in guards}
    assert {"零售存款余额", "不良贷款率", "机构", "不良率"} <= guard_terms
    # 别名护栏映射到标准名
    alias_guard = next(e for e in guards if e.term == "不良率")
    assert alias_guard.standard == "不良贷款率"
    # "笔均贷款" 既是黑话又是别名：去重后只留一条（显式黑话优先，故不是护栏）
    pen = [e for e in entries if e.term == "笔均贷款"]
    assert len(pen) == 1 and pen[0].priority != GUARD_PRIORITY


def test_seed_entries_all_validate_against_semantic_layer():
    """种子里的每条 standard 必须真实存在于语义层（否则改写后反而解析失败）。"""
    layer = SemanticLayer()
    entries = build_seed_entries(**collect_guard_sources(layer))
    bad = [
        (e.term, validate_standard(layer, e.entry_type, e.standard, e.dim_value))
        for e in entries
        if validate_standard(layer, e.entry_type, e.standard, e.dim_value)
    ]
    assert bad == [], f"种子存在无法解析的标准名：{bad}"


def test_guard_entries_self_map_and_alias():
    guards = build_guard_entries(
        metric_names=["零售存款余额"], dimension_names=["机构"],
        metric_aliases={"不良率": "不良贷款率"},
    )
    by_term = {g.term: g for g in guards}
    assert by_term["零售存款余额"].standard == "零售存款余额"
    assert by_term["机构"].standard == "机构"
    assert by_term["不良率"].standard == "不良贷款率"
    assert all(g.priority == GUARD_PRIORITY for g in guards)


# ---------- 管理路由 ----------


async def test_router_crud_roundtrip_with_version_lock():
    rt = _runtime(DatabaseBundle(mode="memory"))
    admin = _admin()

    created = await slang_router.create_entry(
        SlangUpsert(scope=SCOPE_USER, term="AUM", standard="零售存款余额",
                    entry_type=ENTRY_METRIC, status=STATUS_PUBLISHED, priority=120),
        admin, rt,
    )
    assert created.id >= 1 and created.scope == SCOPE_USER
    assert created.tenant_id == "t1" and created.user_id == "admin"

    # 重复 -> 409
    with pytest.raises(HTTPException) as ei:
        await slang_router.create_entry(
            SlangUpsert(scope=SCOPE_USER, term="AUM", standard="零售存款余额",
                        entry_type=ENTRY_METRIC),
            admin, rt,
        )
    assert ei.value.status_code == 409

    # 详情 / 更新（乐观锁）
    got = await slang_router.get_entry(created.id, admin, rt)
    assert got.term == "AUM"
    v0 = created.version  # 内存仓储会原地改写同一对象，先取值再用
    updated = await slang_router.update_entry(
        created.id, SlangPatch(priority=200, version=v0), admin, rt
    )
    assert updated.priority == 200 and updated.version == v0 + 1

    with pytest.raises(HTTPException) as ei2:
        await slang_router.update_entry(
            created.id, SlangPatch(priority=300, version=v0), admin, rt
        )
    assert ei2.value.status_code == 409  # 版本过期

    # 发布 / 停用
    pub = await slang_router.publish_entry(created.id, admin, rt)
    assert pub.status == STATUS_PUBLISHED
    dis = await slang_router.disable_entry(created.id, admin, rt)
    assert dis.status == "disabled"

    # 删除
    out = await slang_router.delete_entry(created.id, admin, rt)
    assert out["deleted"] is True
    with pytest.raises(HTTPException):
        await slang_router.get_entry(created.id, admin, rt)


async def test_router_invalid_standard_422():
    rt = _runtime(DatabaseBundle(mode="memory"))
    with pytest.raises(HTTPException) as ei:
        await slang_router.create_entry(
            SlangUpsert(scope=SCOPE_USER, term="瞎写", standard="不存在的指标",
                        entry_type=ENTRY_METRIC),
            _admin(), rt,
        )
    assert ei.value.status_code == 422


async def test_router_scope_guard_non_admin_cannot_touch_shared_layers():
    rt = _runtime(DatabaseBundle(mode="memory"))
    analyst = _analyst()

    # 非 admin 建公司级 -> 403
    with pytest.raises(HTTPException) as ei:
        await slang_router.create_entry(
            SlangUpsert(scope=SCOPE_TENANT, term="公司词", standard="零售存款余额",
                        entry_type=ENTRY_METRIC),
            analyst, rt,
        )
    assert ei.value.status_code == 403

    # 非 admin 建本人用户级 -> 放行
    own = await slang_router.create_entry(
        SlangUpsert(scope=SCOPE_USER, term="我的词", standard="零售存款余额",
                    entry_type=ENTRY_METRIC, status=STATUS_PUBLISHED),
        analyst, rt,
    )
    assert own.scope == SCOPE_USER and own.user_id == "u1"

    # 非 admin 改他人用户级 -> 403（先由 admin 造一条他人词条）
    other = await rt.db.slang_repo.add(
        _entry("别人的词", "零售贷款余额", scope=SCOPE_USER, tenant_id="t1",
               user_id="u2", status=STATUS_PUBLISHED)
    )
    with pytest.raises(HTTPException) as ei2:
        await slang_router.update_entry(
            other.id, SlangPatch(remark="越权改"), analyst, rt
        )
    assert ei2.value.status_code == 403

    # 非 admin 建行业级 -> 403
    with pytest.raises(HTTPException) as ei3:
        await slang_router.create_entry(
            SlangUpsert(scope=SCOPE_INDUSTRY, term="行业词", standard="零售存款余额",
                        entry_type=ENTRY_METRIC),
            analyst, rt,
        )
    assert ei3.value.status_code == 403


async def test_router_preview_uses_resolver_and_returns_hits():
    rt = _runtime(DatabaseBundle(mode="memory"))
    await rt.db.slang_repo.add(_entry("AUM", "零售存款余额"))
    await rt.db.slang_repo.add(_entry("帮我看下", entry_type=ENTRY_STOP, standard=""))

    out = await slang_router.preview_rewrite(
        SlangPreviewRequest(query="帮我看下AUM"), _admin(), rt
    )
    assert out["original"] == "帮我看下AUM"
    assert "零售存款余额" in out["rewritten"]
    assert {h["term"] for h in out["hits"]} == {"AUM", "帮我看下"}


async def test_router_import_dry_run_and_overwrite():
    rt = _runtime(DatabaseBundle(mode="memory"))
    admin = _admin()
    payload = [
        {"term": "AUM", "standard": "零售存款余额", "entry_type": "metric"},
        {"term": "个贷", "standard": "零售贷款余额", "entry_type": "metric"},
        {"term": "坏词", "standard": "不存在", "entry_type": "metric"},  # 非法
    ]
    # dry_run：不落库，但给出 created/invalid
    dry = await slang_router.import_entries(
        SlangImportRequest(entries=payload, scope=SCOPE_INDUSTRY, dry_run=True), admin, rt
    )
    assert dry.created == 2 and dry.invalid == 1
    assert await rt.db.slang_repo.count() == 0

    # 真正导入
    real = await slang_router.import_entries(
        SlangImportRequest(entries=payload, scope=SCOPE_INDUSTRY, dry_run=False), admin, rt
    )
    assert real.created == 2 and real.invalid == 1
    assert await rt.db.slang_repo.count() == 2

    # 重复导入默认 skipped；overwrite 才更新
    again = await slang_router.import_entries(
        SlangImportRequest(
            entries=[{"term": "AUM", "standard": "零售贷款余额", "entry_type": "metric"}],
            scope=SCOPE_INDUSTRY, dry_run=False,
        ), admin, rt,
    )
    assert again.skipped == 1 and again.conflicts[0]["term"] == "AUM"

    forced = await slang_router.import_entries(
        SlangImportRequest(
            entries=[{"term": "AUM", "standard": "零售贷款余额", "entry_type": "metric"}],
            scope=SCOPE_INDUSTRY, overwrite=True, dry_run=False,
        ), admin, rt,
    )
    assert forced.updated == 1
    found = await rt.db.slang_repo.find(_entry("AUM", scope=SCOPE_INDUSTRY))
    assert found.standard == "零售贷款余额"


async def test_router_stats_facets_export_versions_rollback():
    rt = _runtime(DatabaseBundle(mode="memory"))
    admin = _admin()
    created = await slang_router.create_entry(
        SlangUpsert(scope=SCOPE_INDUSTRY, term="AUM", standard="零售存款余额",
                    entry_type=ENTRY_METRIC, status=STATUS_PUBLISHED),
        admin, rt,
    )
    await rt.db.slang_repo.bump_hit([created.id])

    stats = await slang_router.slang_stats(rt)
    assert stats["total"] == 1 and stats["published"] == 1
    assert stats["by_scope"][SCOPE_INDUSTRY] == 1
    assert stats["by_type"][ENTRY_METRIC] == 1
    assert stats["top_hits"][0]["term"] == "AUM"

    facets = await slang_router.slang_facets(rt)
    assert "零售存款余额" in facets["metric_names"]
    assert "机构" in facets["dimension_names"]
    assert facets["scopes"] == [SCOPE_INDUSTRY, SCOPE_TENANT, SCOPE_USER]

    export = await slang_router.export_entries(
        scope=None, entry_type=None, status=None, source=None, keyword="",
        current=admin, runtime=rt,
    )
    assert export["count"] == 1
    assert export["items"][0]["term"] == "AUM"

    # 改两次后回滚到 v1
    p0 = created.priority  # 100（内存仓储原地改写同一对象，先取值）
    await slang_router.update_entry(
        created.id, SlangPatch(priority=999), admin, rt
    )
    await slang_router.update_entry(
        created.id, SlangPatch(remark="再改一次"), admin, rt
    )
    versions = await slang_router.list_entry_versions(created.id, rt)
    assert versions["count"] >= 3

    rolled = await slang_router.rollback_entry(
        created.id, {"version": 1}, admin, rt
    )
    assert rolled.priority == p0

    # 校验路由参数顺序：静态路径不被 /{entry_id} 吞掉
    paths = [r.path for r in slang_router.router.routes]
    assert paths.index("/slang/stats") < paths.index("/slang/{entry_id}")
    assert paths.index("/slang/preview") < paths.index("/slang/{entry_id}")


async def test_router_list_hides_others_user_entries():
    rt = _runtime(DatabaseBundle(mode="memory"))
    repo = rt.db.slang_repo
    await repo.add(_entry("行业词", "零售存款余额", scope=SCOPE_INDUSTRY))
    await repo.add(
        _entry("我的词", "零售贷款余额", scope=SCOPE_USER, tenant_id="t1", user_id="u1")
    )
    await repo.add(
        _entry("别人的词", "存贷比", scope=SCOPE_USER, tenant_id="t1", user_id="u2")
    )

    out = await slang_router.list_entries(
        scope=None, entry_type=None, status=None, source=None, keyword="", layer=None,
        limit=50, offset=0, current=_analyst("u1"), runtime=rt,
    )
    terms = {i["term"] for i in out["items"]}
    assert terms == {"行业词", "我的词"}  # 看不到别人的用户级


# ---------- FR13：导出截断必须如实上报（回归） ----------


async def test_router_export_reports_truncation_not_silently():
    """回归：slang 导出曾把 DB query 的 limit 直接设成 export_max_rows，
    apply_export_governance 于是只看到「刚好 cap 行」→ truncated 恒为 False，
    超出部分被**静默丢弃**（用户以为拿到了全量）。现在多取一行做溢出探测。"""
    rt = _runtime(DatabaseBundle(mode="memory"))
    rt.settings.security = SimpleNamespace(
        export_require_approval=False, export_max_rows=2, export_watermark_enabled=True
    )
    for i in range(5):
        await rt.db.slang_repo.add(_entry(f"词{i}", f"标准{i}"))

    out = await slang_router.export_entries(
        scope=None, entry_type=None, status=None, source=None, keyword="",
        current=_admin(), runtime=rt,
    )
    assert out["count"] == 2                       # 按上限截断
    assert out["watermark"]["truncated"] is True   # 且明确告知（本回归点）
    assert out["watermark"]["max_rows"] == 2
    assert out["watermark"]["rows"] == 2


async def test_router_export_no_false_truncation_signal():
    """未触顶时不应误报截断（避免"狼来了"式告警）。"""
    rt = _runtime(DatabaseBundle(mode="memory"))
    rt.settings.security = SimpleNamespace(
        export_require_approval=False, export_max_rows=10, export_watermark_enabled=True
    )
    for i in range(3):
        await rt.db.slang_repo.add(_entry(f"词{i}", f"标准{i}"))

    out = await slang_router.export_entries(
        scope=None, entry_type=None, status=None, source=None, keyword="",
        current=_admin(), runtime=rt,
    )
    assert out["count"] == 3
    assert out["watermark"]["truncated"] is False
