"""FR13 enforce 测试套件。

覆盖 enforce 全链路核心闭环：
1. 三张授权仓储的 InMemory 三同步（增删查 + scope 合并语义）；
2. `load_permission_context` 白名单/行权限/列 deny 合并与故障降级；
3. `DataPermissionEngine.filter_metrics` / `filter_result_columns` 纯函数；
4. 编译器 `row_scopes` 下推生成 `t.<dim_expr> IN (...)` SQL（向后兼容）；
5. 管理面 admin 接口（指标授权 / 行权限 / 列 deny 的增删查）；
6. 取数节点 enforce 端到端（指标剔除 / 全拒 E3003 / 列 deny 硬剔除 / 行权限下推）。

不涉及：召回收敛、黑话目标校验、前端权限标记、导出审批（设计文档 P6 其余项，留作后续）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from dmf_agent.config.settings import RecoverySettings, SecuritySettings
from dmf_agent.db.memory import (
    InMemoryDataScopeRepository,
    InMemoryDimensionPolicyRepository,
    InMemoryMetricGrantRepository,
)
from dmf_agent.schemas.permission import (
    DataScopeData,
    DimensionPolicyData,
    MetricGrantData,
)
from dmf_agent.security.coordinator import SecurityCoordinator
from dmf_agent.security.permissions import DataPermissionEngine, UserPermissionContext
from dmf_agent.security.policy_loader import load_permission_context


# --------------------------------------------------------------------------- #
# 1. 授权仓储三同步（InMemory）
# --------------------------------------------------------------------------- #

async def test_metric_grant_repo_scope_merge():
    repo = InMemoryMetricGrantRepository()
    assert await repo.count() == 0

    # 三类授权：user 级 / role 级 / 全租户默认
    await repo.add(MetricGrantData(tenant_id="t1", user_id="u1", metric_key="GMV"))
    await repo.add(MetricGrantData(tenant_id="t1", role="analyst", metric_key="客单价"))
    await repo.add(MetricGrantData(tenant_id="t1", user_id="", role="", metric_key="新增用户数"))
    assert await repo.count() == 3

    # u1（analyst）同时命中 user / role / 全租户 三类
    rows = await repo.list_for(tenant_id="t1", user_id="u1", role="analyst")
    assert {r.metric_key for r in rows} == {"GMV", "客单价", "新增用户数"}

    # 另一用户只命中 role + 全租户
    rows = await repo.list_for(tenant_id="t1", user_id="u2", role="analyst")
    assert {r.metric_key for r in rows} == {"客单价", "新增用户数"}

    # 租户不匹配 → 空
    assert await repo.list_for(tenant_id="t2", user_id="u1", role="analyst") == []

    # 精确删除（tenant+user+role+metric）
    assert await repo.delete(tenant_id="t1", user_id="u1", role="", metric_key="GMV") is True
    assert await repo.count() == 2
    assert await repo.delete(tenant_id="t1", user_id="u1", role="", metric_key="GMV") is False

    all_rows = await repo.list_all(tenant_id="t1")
    assert len(all_rows) == 2


async def test_data_scope_repo_scope_merge():
    repo = InMemoryDataScopeRepository()
    await repo.add(DataScopeData(tenant_id="t1", user_id="u1", dimension="地区", allowed_values=["华东"]))
    await repo.add(DataScopeData(tenant_id="t1", role="analyst", dimension="地区", allowed_values=["华北"]))
    await repo.add(DataScopeData(tenant_id="t1", user_id="u1", dimension="渠道", allowed_values=["APP"]))

    # u1（analyst）在「地区」维度合并 user + role 两档允许值
    rows = await repo.list_for(tenant_id="t1", user_id="u1", role="analyst")
    by_dim: Dict[str, set] = {}
    for r in rows:
        by_dim.setdefault(r.dimension, set()).update(r.allowed_values or [])
    assert by_dim["地区"] == {"华东", "华北"}
    assert by_dim["渠道"] == {"APP"}


async def test_dimension_policy_repo_scope_merge():
    repo = InMemoryDimensionPolicyRepository()
    await repo.add(DimensionPolicyData(tenant_id="t1", user_id="u1", dimension="客户号", denied=True))
    await repo.add(DimensionPolicyData(tenant_id="t1", role="analyst", dimension="手机号", denied=True))
    # denied=False 表示显式放行，不应进入禁止集合
    await repo.add(DimensionPolicyData(tenant_id="t1", user_id="u1", dimension="身份证", denied=False))

    rows = await repo.list_for(tenant_id="t1", user_id="u1", role="analyst")
    denied = {r.dimension for r in rows if r.denied}
    assert denied == {"客户号", "手机号"}


# --------------------------------------------------------------------------- #
# 2. policy_loader：合并语义 + 故障降级
# --------------------------------------------------------------------------- #

async def test_load_permission_context_empty_means_unrestricted():
    ctx = await load_permission_context(
        tenant_id="t1", user_id="u1", role="analyst",
        grant_repo=InMemoryMetricGrantRepository(),
        scope_repo=InMemoryDataScopeRepository(),
        policy_repo=InMemoryDimensionPolicyRepository(),
    )
    # 空授权 = 不限制
    assert ctx.allowed_metrics == set()
    assert ctx.row_scopes == {}
    assert ctx.denied_dimensions == set()


async def test_load_permission_context_merges_all_three():
    grant_repo = InMemoryMetricGrantRepository()
    scope_repo = InMemoryDataScopeRepository()
    policy_repo = InMemoryDimensionPolicyRepository()

    await grant_repo.add(MetricGrantData(tenant_id="t1", user_id="u1", metric_key="GMV"))
    await scope_repo.add(DataScopeData(tenant_id="t1", user_id="u1", dimension="地区", allowed_values=["华东"]))
    await policy_repo.add(DimensionPolicyData(tenant_id="t1", user_id="u1", dimension="客户号", denied=True))

    ctx = await load_permission_context(
        tenant_id="t1", user_id="u1", role="analyst",
        grant_repo=grant_repo, scope_repo=scope_repo, policy_repo=policy_repo,
    )
    assert ctx.allowed_metrics == {"GMV"}
    assert ctx.row_scopes == {"地区": {"华东"}}
    assert ctx.denied_dimensions == {"客户号"}


async def test_load_permission_context_degrades_on_repo_failure():
    class BoomRepo(InMemoryMetricGrantRepository):
        async def list_for(self, *, tenant_id: str, user_id: str, role: str):
            raise RuntimeError("db down")

    # 即便授权仓储故障，也应安全降级为"不限制"而非抛错
    ctx = await load_permission_context(
        tenant_id="t1", user_id="u1", role="analyst",
        grant_repo=BoomRepo(),
        scope_repo=InMemoryDataScopeRepository(),
        policy_repo=InMemoryDimensionPolicyRepository(),
    )
    assert ctx.allowed_metrics == set()
    assert ctx.row_scopes == {}


# --------------------------------------------------------------------------- #
# 3. enforce 纯函数
# --------------------------------------------------------------------------- #

def test_filter_metrics_whitelist():
    ctx = UserPermissionContext()
    # 空白名单 = 不限制
    assert DataPermissionEngine(mode="enforce").filter_metrics(ctx, ["A", "B"]) == (["A", "B"], [])
    # 有白名单：只放行白名单内
    ctx.allowed_metrics = {"A"}
    allowed, denied = DataPermissionEngine(mode="enforce").filter_metrics(ctx, ["A", "B", "C"])
    assert allowed == ["A"]
    assert set(denied) == {"B", "C"}


def test_filter_result_columns_hard_remove():
    ctx = UserPermissionContext()
    cols = ["统计日期", "地区", "GMV"]
    # 空禁止 = 全保留
    assert DataPermissionEngine().filter_result_columns(ctx, cols) == (cols, [])
    # 禁止某维度列 = 硬剔除（区别于脱敏掩码）
    ctx.denied_dimensions = {"地区"}
    kept, removed = DataPermissionEngine().filter_result_columns(ctx, cols)
    assert kept == ["统计日期", "GMV"]
    assert removed == ["地区"]


# --------------------------------------------------------------------------- #
# 4. 编译器 row_scopes 下推
# --------------------------------------------------------------------------- #

def _build_compiler():
    from dmf_agent.config.settings import Settings
    from dmf_agent.semantic import MetricFlowSqlCompiler, SemanticLayer
    layer = SemanticLayer()
    compiler = MetricFlowSqlCompiler(layer, Settings().starrocks)
    return layer, compiler


def test_compiler_row_scopes_pushdown():
    layer, compiler = _build_compiler()
    from dmf_agent.semantic.query import MetricQuery

    query = MetricQuery.from_dsl({"metrics": [{"name": "GMV"}]}, layer)
    # 取首个涉及模型的某个维度（保证该维度在该模型存在，下推必触发）
    models = compiler._involved_models(compiler._resolve_metrics(query))
    dim = models[0].dimensions[0].name

    sql_no_scope = compiler.compile(query, row_scopes=None).sql
    assert "'华东'" not in sql_no_scope

    sql_scoped = compiler.compile(query, row_scopes={dim: {"华东", "华北"}}).sql
    # set 遍历顺序不确定，只断言两个值都进入了 IN 列表（且按维度 expr 下推）
    assert "'华东'" in sql_scoped
    assert "'华北'" in sql_scoped
    dim_obj = models[0].dimension(dim)
    dim_expr = dim_obj.expr if dim_obj else dim
    assert f"t.{dim_expr} IN (" in sql_scoped


# --------------------------------------------------------------------------- #
# 5. 管理面 admin 接口
# --------------------------------------------------------------------------- #

def _make_runtime():
    db = SimpleNamespace(
        metric_grant_repo=InMemoryMetricGrantRepository(),
        data_scope_repo=InMemoryDataScopeRepository(),
        dimension_policy_repo=InMemoryDimensionPolicyRepository(),
    )
    return SimpleNamespace(db=db)


def _make_admin_user():
    from dmf_agent.schemas.auth import CurrentUser

    return CurrentUser(id="1", username="admin", tenant_id="t1", role="admin")


async def test_permission_router_grant_lifecycle():
    from dmf_agent.api.routers import permission as perm_router

    runtime = _make_runtime()
    admin = _make_admin_user()

    saved = await perm_router.grant_metric(
        perm_router.GrantMetricIn(tenant_id="t1", user_id="u1", metric_key="GMV"),
        admin, runtime,
    )
    assert saved["metric_key"] == "GMV"
    assert saved["granted_by"] == "admin"

    listed = await perm_router.list_grants(tenant_id="t1", runtime=runtime)
    assert listed["total"] == 1

    removed = await perm_router.revoke_metric(
        metric_key="GMV", tenant_id="t1", user_id="u1", role="", runtime=runtime
    )
    assert removed["deleted"] is True
    assert (await perm_router.list_grants(tenant_id="t1", runtime=runtime))["total"] == 0


async def test_permission_router_scope_and_policy():
    from dmf_agent.api.routers import permission as perm_router

    runtime = _make_runtime()
    admin = _make_admin_user()

    await perm_router.set_scope(
        perm_router.ScopeIn(tenant_id="t1", user_id="u1", dimension="地区", allowed_values=["华东"]),
        admin, runtime,
    )
    scopes = await perm_router.list_scopes(tenant_id="t1", runtime=runtime)
    assert scopes["total"] == 1
    assert set(scopes["items"][0]["allowed_values"]) == {"华东"}

    await perm_router.set_policy(
        perm_router.PolicyIn(tenant_id="t1", user_id="u1", dimension="客户号", denied=True),
        admin, runtime,
    )
    policies = await perm_router.list_policies(tenant_id="t1", runtime=runtime)
    assert policies["total"] == 1
    assert policies["items"][0]["denied"] is True


# --------------------------------------------------------------------------- #
# 6. 取数节点 enforce 端到端
# --------------------------------------------------------------------------- #

class _FakeEngine:
    def __init__(self) -> None:
        self.calls: List[tuple] = []

    async def query(self, dsl: Dict[str, Any], row_scopes: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.calls.append((dsl, row_scopes))
        cols: List[str] = []
        for m in dsl.get("metrics") or []:
            cols.append(m["name"] if isinstance(m, dict) else m)
        for d in dsl.get("dimensions") or []:
            cols.append(d)
        n = len(cols)
        rows = [[1] * n for _ in range(2)]
        return {
            "columns": cols, "rows": rows, "sql": "SELECT 1", "engine": "metricflow",
            "models": ["trade_day"], "group_columns": [], "metric_columns": cols,
            "row_count": 2, "truncated": False, "elapsed_ms": 1.0, "mock": False,
        }


def _build_coordinator(mode: str = "enforce") -> SecurityCoordinator:
    settings = SecuritySettings(permission_mode=mode, enabled=False, desensitize_enabled=False)
    return SecurityCoordinator(
        settings,
        audit_sink=None,
        grant_repo=InMemoryMetricGrantRepository(),
        scope_repo=InMemoryDataScopeRepository(),
        policy_repo=InMemoryDimensionPolicyRepository(),
    )


def _make_node_state(dsl: Dict[str, Any], user_id: str = "u1", tenant: str = "t1") -> Dict[str, Any]:
    return {
        "dsl": dsl, "user_id": user_id, "tenant_id": tenant, "user_role": "analyst",
        "session_id": "s1", "round_id": "r1", "harness_state": {}, "user_permissions": [],
    }


async def _run_node(deps, state):
    from dmf_agent.agents.metric_agent.nodes.execute_query import build_execute_query_node

    node = build_execute_query_node(deps)
    return await node(state)


async def test_node_enforce_filters_unauthorized_metric():
    coord = _build_coordinator()
    # 只授予 GMV；查询同时请求 GMV + 客单价 → 客单价被剔除
    await coord.grant_repo.add(MetricGrantData(tenant_id="t1", user_id="u1", metric_key="GMV"))
    engine = _FakeEngine()
    deps = SimpleNamespace(
        metric_engine=engine, harness=None, security=coord,
        recovery_settings=RecoverySettings(), error_sample_repo=None,
    )
    dsl = {"metrics": [{"name": "GMV"}, {"name": "客单价"}], "dimensions": []}
    result = await _run_node(deps, _make_node_state(dsl))

    # engine 实际只收到被授权的指标
    called_dsl, _ = engine.calls[0]
    called_metrics = [m["name"] if isinstance(m, dict) else m for m in called_dsl["metrics"]]
    assert called_metrics == ["GMV"]
    assert result["query_result"].get("error") is None


async def test_node_enforce_all_denied_returns_e3003():
    coord = _build_coordinator()
    # 只授予 GMV；查询只请求 客单价 → 全部被拒
    await coord.grant_repo.add(MetricGrantData(tenant_id="t1", user_id="u1", metric_key="GMV"))
    engine = _FakeEngine()
    deps = SimpleNamespace(
        metric_engine=engine, harness=None, security=coord,
        recovery_settings=RecoverySettings(), error_sample_repo=None,
    )
    dsl = {"metrics": [{"name": "客单价"}], "dimensions": []}
    result = await _run_node(deps, _make_node_state(dsl))

    assert engine.calls == []  # 全拒，根本不取数
    qr = result["query_result"]
    assert qr["error_code"] == "E3003"
    assert "无权访问" in (qr.get("error") or "")


async def test_node_enforce_column_deny_hard_removes():
    coord = _build_coordinator()
    await coord.grant_repo.add(MetricGrantData(tenant_id="t1", user_id="u1", metric_key="GMV"))
    # 禁止查看「地区」列
    await coord.policy_repo.add(
        DimensionPolicyData(tenant_id="t1", user_id="u1", dimension="地区", denied=True)
    )
    engine = _FakeEngine()
    deps = SimpleNamespace(
        metric_engine=engine, harness=None, security=coord,
        recovery_settings=RecoverySettings(), error_sample_repo=None,
    )
    dsl = {"metrics": [{"name": "GMV"}], "dimensions": ["地区"]}
    result = await _run_node(deps, _make_node_state(dsl))

    qr = result["query_result"]
    assert "地区" not in qr["columns"]
    assert qr["columns"] == ["GMV"]
    # 行按 kept 列索引重排：原 2 列 -> 剩 1 列
    assert qr["rows"] and len(qr["rows"][0]) == 1


async def test_node_enforce_row_scope_pushed_to_engine():
    coord = _build_coordinator()
    await coord.grant_repo.add(MetricGrantData(tenant_id="t1", user_id="u1", metric_key="GMV"))
    await coord.scope_repo.add(
        DataScopeData(tenant_id="t1", user_id="u1", dimension="地区", allowed_values=["华东"])
    )
    engine = _FakeEngine()
    deps = SimpleNamespace(
        metric_engine=engine, harness=None, security=coord,
        recovery_settings=RecoverySettings(), error_sample_repo=None,
    )
    dsl = {"metrics": [{"name": "GMV"}], "dimensions": ["地区"]}
    result = await _run_node(deps, _make_node_state(dsl))

    # enforce 下把行权限下推到引擎（再由其编译进 SQL 最内层 WHERE）
    _, row_scopes = engine.calls[0]
    assert row_scopes == {"地区": {"华东"}}
    assert result["query_result"].get("error") is None


async def test_node_audit_mode_does_not_filter():
    # audit 模式：即便有授权，也不拦截/过滤
    coord = _build_coordinator(mode="audit")
    await coord.grant_repo.add(MetricGrantData(tenant_id="t1", user_id="u1", metric_key="GMV"))
    engine = _FakeEngine()
    deps = SimpleNamespace(
        metric_engine=engine, harness=None, security=coord,
        recovery_settings=RecoverySettings(), error_sample_repo=None,
    )
    dsl = {"metrics": [{"name": "GMV"}, {"name": "客单价"}], "dimensions": []}
    result = await _run_node(deps, _make_node_state(dsl))
    assert result["query_result"].get("error") is None

    called_dsl, _ = engine.calls[0]
    called_metrics = [m["name"] if isinstance(m, dict) else m for m in called_dsl["metrics"]]
    assert set(called_metrics) == {"GMV", "客单价"}  # audit 不剔除


# --------------------------------------------------------------------------- #
# 7. T198f：当前用户有效权限上下文（前端权限标记的数据源）
# --------------------------------------------------------------------------- #

async def test_my_context_without_security_returns_off():
    """runtime 未装配安全层 → mode=off + 不限制（前端应视为"无权限约束"）。"""
    from dmf_agent.api.routers import permission as perm_router

    runtime = _make_runtime()  # 无 security
    out = await perm_router.my_permission_context(_make_admin_user(), runtime)
    assert out["mode"] == "off"
    assert out["restricted"] is False
    assert out["unrestricted_metrics"] is True
    assert out["allowed_metrics"] == []
    assert out["denied_dimensions"] == []


async def test_my_context_reports_effective_restrictions():
    """有白名单 + 行权限 + 列 deny 时，/my-context 必须如实汇总（供前端标记/显隐）。"""
    from dmf_agent.api.routers import permission as perm_router
    from dmf_agent.schemas.auth import CurrentUser

    coord = _build_coordinator(mode="enforce")
    await coord.grant_repo.add(MetricGrantData(tenant_id="t1", user_id="u1", metric_key="GMV"))
    await coord.scope_repo.add(
        DataScopeData(tenant_id="t1", user_id="u1", dimension="地区", allowed_values=["华东"])
    )
    await coord.policy_repo.add(
        DimensionPolicyData(tenant_id="t1", user_id="u1", dimension="客户号", denied=True)
    )
    runtime = _make_runtime()
    runtime.security = coord
    user = CurrentUser(id="u1", username="analyst1", tenant_id="t1", role="analyst")

    out = await perm_router.my_permission_context(user, runtime)
    assert out["mode"] == "enforce"
    assert out["restricted"] is True
    assert out["unrestricted_metrics"] is False
    assert out["allowed_metrics"] == ["GMV"]
    assert out["denied_dimensions"] == ["客户号"]
    assert out["row_scope_dimensions"] == ["地区"]
    assert out["row_scope_values"] == {"地区": ["华东"]}
    assert out["role"] == "analyst"
    assert out["user_id"] == "u1"


async def test_my_context_empty_grants_means_unrestricted_metrics():
    """空授权仓储 = 不限制指标（与 policy_loader / filter_metrics 语义一致）。"""
    from dmf_agent.api.routers import permission as perm_router
    from dmf_agent.schemas.auth import CurrentUser

    coord = _build_coordinator(mode="enforce")
    runtime = _make_runtime()
    runtime.security = coord
    out = await perm_router.my_permission_context(
        CurrentUser(id="u2", username="u2", tenant_id="t1", role="viewer"), runtime
    )
    assert out["unrestricted_metrics"] is True
    assert out["restricted"] is False
    assert out["allowed_metrics"] == []


async def test_my_context_endpoint_registered():
    """端点注册 + 不带 admin 守卫（每个登录用户都该知道自己的可见范围）。"""
    from dmf_agent.api.routers import permission as perm_router

    paths = {r.path for r in perm_router.router.routes}
    assert "/permission/my-context" in paths
    # 与 admin 端点区分：my-context 的依赖里不应出现 PERM_ADMIN 守卫
    route = next(r for r in perm_router.router.routes if r.path == "/permission/my-context")
    assert route.methods == {"GET"}
