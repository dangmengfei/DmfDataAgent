"""语义注册中心治理（P2：版本 / 口径审批 / 回滚）单测。

覆盖：
- 状态机 `can_transition`（草稿不可直接发布，必须经 review 闸口）；
- 稳定 key 推导（metric / dimension）；
- 六道校验闸（schema / 引用完整性 / DAG 无环 / SQL dry-run / 物理列 / 试跑）；
- 仓储写路径：草稿 → 校验 → 提交审核 → 审批发布 → 版本历史 → 回滚；
- 维度对称写路径；
- 路由层：写接口 + 404/409 + admin 审批守卫（auth 关闭时为合成 admin）。

权限分层校验（口径审批 = 起草与审批分离）：
- semantic:write 可起草/编辑/提交/废弃/校验；
- admin 角色才能审批发布/回滚。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from dmf_agent.api.deps import get_runtime
from dmf_agent.api.routers import semantic_registry as registry_router
from dmf_agent.auth.dependencies import require_role
from dmf_agent.auth.roles import ADMIN
from dmf_agent.db.factory import DatabaseBundle
from dmf_agent.db.memory import InMemorySemanticRegistryRepository
from dmf_agent.schemas.auth import CurrentUser
from dmf_agent.semantic.governance import (
    DEPRECATED,
    DRAFT,
    PUBLISHED,
    REVIEW,
    can_transition,
    derive_dimension_key,
    derive_metric_key,
    slugify,
    validate_dimension_definition,
    validate_metric_definition,
)

# 极简种子：含一个模型（带度量 amt + 维度 region）与一个已发布 simple 指标。
SEED = {
    "models": [
        {
            "model_key": "m1",
            "name": "测试模型",
            "status": "published",
            "version": 1,
            "table": "t1",
            "agg_time_dimension": "stat_date",
            "entities": [],
            "measures": [{"measure_key": "amt", "name": "金额", "agg": "sum"}],
            "dimensions": [
                {
                    "dimension_key": "region",
                    "name": "地区",
                    "dim_type": "categorical",
                    "expr": "region",
                    "time_granularity": "day",
                }
            ],
        }
    ],
    "metrics": [
        {
            "metric_key": "m1_amt",
            "name": "金额指标",
            "metric_type": "simple",
            "type_params": {"measure": "amt"},
            "status": "published",
            "version": 1,
        }
    ],
}


@pytest.fixture
async def repo():
    r = InMemorySemanticRegistryRepository()
    await r.import_seed(SEED, operator="seed")
    return r


# ---------------- 状态机 ----------------


def test_state_machine_enforces_review_gate():
    # 草稿不能直接发布，必须经 review 审批闸口
    assert can_transition(DRAFT, REVIEW) is True
    assert can_transition(DRAFT, PUBLISHED) is False
    assert can_transition(DRAFT, DEPRECATED) is True
    # 审核可审批发布 / 驳回 / 废弃
    assert can_transition(REVIEW, PUBLISHED) is True
    assert can_transition(REVIEW, DRAFT) is True
    assert can_transition(REVIEW, DEPRECATED) is True
    # 已发布仅可废弃，不能退回草稿
    assert can_transition(PUBLISHED, DEPRECATED) is True
    assert can_transition(PUBLISHED, DRAFT) is False
    # 废弃后可重新发布 / 复活为草稿
    assert can_transition(DEPRECATED, PUBLISHED) is True
    assert can_transition(DEPRECATED, DRAFT) is True
    # 同态转移始终允许
    assert can_transition(DRAFT, DRAFT) is True


# ---------------- 稳定 key 推导 ----------------


def test_derive_metric_key():
    assert derive_metric_key({"metric_type": "simple", "type_params": {"measure": "gmv_amount"}}) == "gmv_amount"
    assert (
        derive_metric_key(
            {"metric_type": "ratio", "type_params": {"numerator": "a", "denominator": "b"}}
        )
        == "a__b_ratio"
    )
    k1 = derive_metric_key({"name": "客单价", "metric_type": "derived"})
    k2 = derive_metric_key({"name": "客单价", "metric_type": "derived"})
    assert k1 == k2 and k1.startswith("derived_")


def test_derive_dimension_key_and_slugify():
    # derive_dimension_key 优先用 name（种子维度 name 即英文列名），回退 expr
    assert derive_dimension_key({"name": "region"}) == "region"
    assert derive_dimension_key({"name": "", "expr": "region"}) == "region"
    assert slugify("Paid Rate!") == "paid_rate"
    assert slugify("") == "x"


# ---------------- 校验门禁 ----------------


@pytest.mark.asyncio
async def test_validate_simple_metric_ok(repo):
    defn = {
        "name": "新金额",
        "metric_type": "simple",
        "type_params": {"measure": "amt"},
    }
    all_defs = await repo.list_all_metric_defs()
    measure_keys = await repo.list_all_measure_keys()
    res = await validate_metric_definition(
        defn,
        metric_key="new_amt",
        all_metric_defs=all_defs,
        measure_keys=measure_keys,
        layer=None,
        engine=None,
    )
    assert res.passed is True
    assert res.problems == []
    # 无引擎时 SQL dry-run / 物理列 / 试跑 降级为 warning（不阻断）
    statuses = {g.gate: g.status for g in res.gates}
    assert statuses["sql_dryrun"] == "warning"
    assert statuses["physical_columns"] == "warning"
    assert statuses["trial_run"] == "warning"


@pytest.mark.asyncio
async def test_validate_metric_missing_measure(repo):
    defn = {"name": "坏指标", "metric_type": "simple", "type_params": {"measure": "nope"}}
    res = await validate_metric_definition(
        defn,
        metric_key="bad",
        all_metric_defs=await repo.list_all_metric_defs(),
        measure_keys=await repo.list_all_measure_keys(),
        layer=None,
        engine=None,
    )
    assert res.passed is False
    assert any("②" in p for p in res.problems)


@pytest.mark.asyncio
async def test_validate_ratio_missing_refs(repo):
    defn = {
        "name": "转化率",
        "metric_type": "ratio",
        "type_params": {"numerator": "ghost_a", "denominator": "ghost_b"},
    }
    res = await validate_metric_definition(
        defn,
        metric_key="conv",
        all_metric_defs={},
        measure_keys=set(),
        layer=None,
        engine=None,
    )
    assert res.passed is False
    assert any("②" in p for p in res.problems)


@pytest.mark.asyncio
async def test_validate_derived_cycle():
    defs = {
        "a": {"metric_type": "derived", "type_params": {"metrics": ["b"], "expr": "x"}},
        "b": {"metric_type": "derived", "type_params": {"metrics": ["a"], "expr": "y"}},
    }
    defn = {"name": "a", "metric_type": "derived", "type_params": {"metrics": ["b"], "expr": "x"}}
    res = await validate_metric_definition(
        defn, metric_key="a", all_metric_defs=defs, measure_keys=set(), layer=None, engine=None
    )
    assert res.passed is False
    assert any("③" in p for p in res.problems)


def test_validate_dimension():
    ok = validate_dimension_definition(
        {"name": "地区", "dim_type": "categorical", "expr": "region", "time_granularity": "day"}
    )
    assert ok.passed is True
    bad = validate_dimension_definition({"name": "", "dim_type": "weird", "expr": "", "time_granularity": ""})
    assert bad.passed is False
    assert any("①" in p for p in bad.problems)


# ---------------- 仓储写路径：指标 ----------------


@pytest.mark.asyncio
async def test_metric_lifecycle(repo):
    # 1) 新建草稿
    draft = await repo.create_metric_draft(
        {
            "metric_key": "test_x",
            "name": "测试指标",
            "metric_type": "simple",
            "type_params": {"measure": "amt"},
        },
        operator="analyst",
    )
    assert draft["status"] == DRAFT and draft["version"] == 1

    # 2) 校验通过
    defn = await repo.get_metric_def("test_x")
    res = await validate_metric_definition(
        defn,
        metric_key="test_x",
        all_metric_defs=await repo.list_all_metric_defs(),
        measure_keys=await repo.list_all_measure_keys(),
        layer=None,
        engine=None,
    )
    assert res.passed is True

    # 3) 直接发布被拒（跳过 review 闸口）
    direct = await repo.publish_metric("test_x", operator="admin")
    assert direct.get("ok") is False and direct.get("error") == "illegal_transition"

    # 4) 提交审核
    submitted = await repo.submit_metric_review("test_x", operator="analyst")
    assert submitted["status"] == REVIEW

    # 5) 审批发布
    published = await repo.publish_metric("test_x", operator="admin", approved_by="admin")
    assert published["ok"] is True
    assert published["metric"]["status"] == PUBLISHED
    assert published["metric"]["version"] == 2

    # 6) 版本历史：create(v1) + submit(v1) + publish(v2)
    versions = await repo.list_metric_versions("test_x")
    assert [v["change_type"] for v in versions] == ["create", "submit", "publish"]
    assert len(versions) == 3

    # 7) 回滚到 v1（生成新版本，非物理回退）
    rolled = await repo.rollback_metric("test_x", 1, operator="admin", approved_by="admin")
    assert rolled["status"] == PUBLISHED
    assert rolled["version"] == 3
    assert rolled["name"] == "测试指标"

    # 8) 已发布指标不可编辑
    with pytest.raises(ValueError):
        await repo.update_metric("test_x", {"name": "改名"}, operator="analyst")


@pytest.mark.asyncio
async def test_deprecate_metric(repo):
    await repo.create_metric_draft(
        {"metric_key": "dep_x", "name": "弃用指标", "metric_type": "simple", "type_params": {"measure": "amt"}},
        operator="a",
    )
    await repo.submit_metric_review("dep_x", operator="a")
    pub = await repo.publish_metric("dep_x", operator="admin", approved_by="admin")
    assert pub["metric"]["status"] == PUBLISHED
    dep = await repo.deprecate_metric("dep_x", operator="a")
    assert dep["status"] == DEPRECATED


# ---------------- 仓储写路径：维度 ----------------


@pytest.mark.asyncio
async def test_dimension_lifecycle(repo):
    # 维度必须挂在某模型下
    draft = await repo.create_dimension_draft(
        {
            "model_key": "m1",
            "name": "新区划",
            "dim_type": "categorical",
            "expr": "zone",
            "time_granularity": "day",
        },
        operator="analyst",
    )
    assert draft["status"] == DRAFT and draft["dimension_key"]

    # 校验通过
    defn = await repo.get_dimension_def(draft["dimension_key"])
    assert validate_dimension_definition(defn).passed is True

    # 无模型则拒绝
    with pytest.raises(ValueError):
        await repo.create_dimension_draft({"name": "孤维", "expr": "x", "time_granularity": "day"}, operator="a")

    # 提交 -> 发布 -> 版本 -> 回滚
    await repo.submit_dimension_review(draft["dimension_key"], operator="analyst")
    pub = await repo.publish_dimension(draft["dimension_key"], operator="admin", approved_by="admin")
    assert pub["ok"] is True and pub["dimension"]["status"] == PUBLISHED
    versions = await repo.list_dimension_versions(draft["dimension_key"])
    assert [v["change_type"] for v in versions] == ["create", "submit", "publish"]
    rolled = await repo.rollback_dimension(draft["dimension_key"], 1, operator="admin", approved_by="admin")
    assert rolled["status"] == PUBLISHED and rolled["version"] == 3


# ---------------- 路由层（auth 关闭 = 合成 admin） ----------------


def _build_client(repo) -> TestClient:
    db = DatabaseBundle(mode="memory")
    db.semantic_registry_repo = repo
    rt = SimpleNamespace(
        db=db,
        settings=SimpleNamespace(auth=SimpleNamespace(enabled=False)),
        semantic_layer=None,
        metric_engine=None,
    )
    app = FastAPI()
    app.include_router(registry_router.router)
    app.dependency_overrides[get_runtime] = lambda: rt
    return TestClient(app)


@pytest.fixture
def client(repo) -> TestClient:
    return _build_client(repo)


def test_api_metric_draft_to_rollback(client: TestClient):
    # 新建草稿
    r = client.post(
        "/semantic/registry/metrics/draft",
        json={
            "metric_key": "api_x",
            "name": "API指标",
            "metric_type": "simple",
            "type_params": {"measure": "amt"},
        },
    )
    assert r.status_code == 200, r.text
    key = r.json()["metric"]["metric_key"]
    assert r.json()["metric"]["status"] == DRAFT

    # 直接发布被拒（未提交审核）
    r = client.post(f"/semantic/registry/metrics/{key}/publish")
    assert r.status_code == 409, r.text

    # 校验
    r = client.post(f"/semantic/registry/metrics/{key}/validate")
    assert r.status_code == 200 and r.json()["passed"] is True

    # 提交审核 -> 审批发布
    r = client.post(f"/semantic/registry/metrics/{key}/submit")
    assert r.status_code == 200 and r.json()["metric"]["status"] == REVIEW
    r = client.post(f"/semantic/registry/metrics/{key}/publish")
    assert r.status_code == 200, r.text
    assert r.json()["metric"]["status"] == PUBLISHED

    # 版本历史
    r = client.get(f"/semantic/registry/metrics/{key}/versions")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 3

    # 回滚到 v1
    r = client.post(f"/semantic/registry/metrics/{key}/rollback", json={"version": items[0]["version"]})
    assert r.status_code == 200, r.text
    assert r.json()["metric"]["status"] == PUBLISHED

    # 不存在的指标 -> 404
    r = client.post("/semantic/registry/metrics/nope/submit")
    assert r.status_code == 404


def test_api_dimension_flow(client: TestClient):
    r = client.post(
        "/semantic/registry/dimensions/draft",
        json={
            "model_key": "m1",
            "name": "API维度",
            "dim_type": "categorical",
            "expr": "api_zone",
            "time_granularity": "day",
        },
    )
    assert r.status_code == 200, r.text
    dkey = r.json()["dimension"]["dimension_key"]

    # 缺少模型 -> 400
    r = client.post(
        "/semantic/registry/dimensions/draft",
        json={"name": "孤维", "expr": "x", "time_granularity": "day"},
    )
    assert r.status_code == 400, r.text

    r = client.post(f"/semantic/registry/dimensions/{dkey}/submit")
    assert r.status_code == 200
    r = client.post(f"/semantic/registry/dimensions/{dkey}/publish")
    assert r.status_code == 200, r.text
    assert r.json()["dimension"]["status"] == PUBLISHED


# ---------------- admin 审批守卫 ----------------


@pytest.mark.asyncio
async def test_publish_requires_admin_role():
    admin = CurrentUser(
        id="1", username="admin", tenant_id="", role="admin", permissions=["*"]
    )
    analyst = CurrentUser(
        id="2", username="analyst", tenant_id="", role="analyst", permissions=["semantic:write"]
    )
    # admin 通过
    assert await require_role(ADMIN)(admin) is admin
    # analyst 被拒
    with pytest.raises(HTTPException) as e:
        await require_role(ADMIN)(analyst)
    assert e.value.status_code == 403
