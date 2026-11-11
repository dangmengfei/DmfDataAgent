"""FR8 PRD 5.9 完整闭环测试：语义模型 YAML 生成 / 落库 / 知识库增量重建 / ⑤⑥ 深度校验闸。

覆盖三处 PRD 5.9 闭环缺口：
(a) 发布指标必须生成 manifest 兼容的 yaml_defn 并落库；
(c) 发布后新指标进入知识库口径字典（权威源）；
(⑤⑥) 深度校验闸真正执行：⑤ 物理列存在性（information_schema）、⑥ 试跑（executor.fetch），
     且缺资源/异常时安全降级为 warning，不阻断主链路。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dmf_agent.config.settings import Settings
from dmf_agent.db.factory import DatabaseBundle
from dmf_agent.db.memory import InMemorySemanticRegistryRepository
from dmf_agent.rag.dictionary import DICTIONARY_SOURCE_URI
from dmf_agent.semantic.governance import (
    DRAFT,
    validate_metric_definition,
)
from dmf_agent.semantic.kb_rebuild import rebuild_dictionary_for_metric
from dmf_agent.semantic.layer import SemanticLayer
from dmf_agent.semantic.starrocks import StarRocksError
from dmf_agent.semantic.yaml_defn import build_layer_with_metric, metric_to_yaml_defn

# 极简种子：一个模型（度量 amt，物理列 amount）+ 一个已发布 simple 指标
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
                {"dimension_key": "region", "name": "地区", "dim_type": "categorical", "expr": "region"}
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


# ---------------- yaml_defn 生成 ----------------


def test_metric_to_yaml_defn_extracts_core_fields():
    brief = {
        "name": "新发指标",
        "metric_type": "simple",
        "type_params": {"measure": "amt"},
        "aliases": ["别名A"],
        "unit": "元",
        "description": "闭环测试",
        "scale": 1.0,
        "round": 2,
        "granularities": ["day", "month"],
        "pop_types": ["mom", "yoy"],
    }
    out = metric_to_yaml_defn(brief)
    m = out["metric"]
    assert m["name"] == "新发指标"
    assert m["type"] == "simple"
    assert m["type_params"] == {"measure": "amt"}
    assert m["aliases"] == ["别名A"]
    assert m["unit"] == "元"
    assert m["scale"] == 1.0 and m["round"] == 2
    assert m["granularities"] == ["day", "month"]
    assert m["pop_types"] == ["mom", "yoy"]


def test_build_layer_with_metric_merges_new_metric():
    layer = SemanticLayer.from_manifest_dict(
        {
            "semantic_models": [
                {
                    "name": "m1",
                    "table": "t_fact",
                    "entities": [{"name": "cust", "type": "primary", "expr": "cust_id"}],
                    "dimensions": [
                        {"name": "region", "type": "categorical", "expr": "region"},
                        {"name": "stat_date", "type": "time", "expr": "stat_date"},
                    ],
                    "measures": [
                        {"name": "amt", "agg": "sum", "expr": "amount"},
                        {"name": "cnt", "agg": "count", "expr": "id"},
                    ],
                }
            ],
            "metrics": [
                {"name": "已发布指标", "type": "simple", "type_params": {"measure": "amt"}}
            ],
        }
    )
    brief = {
        "name": "新发指标",
        "metric_type": "simple",
        "type_params": {"measure": "amt"},
        "aliases": [],
        "unit": "",
        "description": "",
        "scale": 1.0,
        "round": 4,
        "granularities": ["day"],
        "pop_types": ["mom"],
    }
    merged = build_layer_with_metric(layer, brief)
    assert merged is not None
    # 副本层包含新指标，且能展开到底层度量
    assert merged.resolve_metric("新发指标") is not None
    assert merged.base_measures("新发指标") == ["amt"]
    # 原运行时层不受影响
    assert layer.resolve_metric("新发指标") is None
    # 同名覆盖（重发布）也不报错
    assert build_layer_with_metric(layer, brief) is not None


def test_build_layer_with_metric_none_base_returns_none():
    assert build_layer_with_metric(None, {"name": "x", "metric_type": "simple"}) is None


# ---------------- 发布期落库 yaml_defn（in-memory 仓储） ----------------


@pytest.mark.asyncio
async def test_publish_persists_yaml_defn(repo):
    draft = await repo.create_metric_draft(
        {
            "metric_key": "fr8_x",
            "name": "FR8指标",
            "metric_type": "simple",
            "type_params": {"measure": "amt"},
        },
        operator="analyst",
    )
    assert draft["status"] == DRAFT
    await repo.submit_metric_review("fr8_x", operator="analyst")
    published = await repo.publish_metric(
        "fr8_x", operator="admin", approved_by="admin", layer=None, engine=None
    )
    assert published["ok"] is True
    yaml_defn = published["metric"].get("yaml_defn")
    assert yaml_defn is not None
    assert yaml_defn["metric"]["name"] == "FR8指标"
    assert yaml_defn["metric"]["type_params"] == {"measure": "amt"}


# ---------------- 知识库增量重建 ----------------


async def _make_layer_with_new_metric():
    layer = SemanticLayer.from_manifest_dict(
        {
            "semantic_models": [
                {
                    "name": "m1",
                    "table": "t_fact",
                    "entities": [{"name": "cust", "type": "primary", "expr": "cust_id"}],
                    "dimensions": [
                        {"name": "region", "type": "categorical", "expr": "region"},
                        {"name": "stat_date", "type": "time", "expr": "stat_date"},
                    ],
                    "measures": [{"name": "amt", "agg": "sum", "expr": "amount"}],
                }
            ],
            "metrics": [
                {"name": "已发布指标", "type": "simple", "type_params": {"measure": "amt"}}
            ],
        }
    )
    return layer


@pytest.mark.asyncio
async def test_rebuild_dictionary_includes_new_metric():
    layer = await _make_layer_with_new_metric()
    db = DatabaseBundle(mode="memory")
    rt = SimpleNamespace(db=db, settings=Settings(), semantic_layer=layer, vector_store=None)

    brief = {
        "name": "新发指标",
        "metric_type": "simple",
        "type_params": {"measure": "amt"},
        "aliases": [],
        "unit": "元",
        "description": "闭环进入字典",
        "scale": 1.0,
        "round": 4,
        "granularities": ["day"],
        "pop_types": ["mom"],
        "tenant_id": "",
    }
    res = await rebuild_dictionary_for_metric(runtime=rt, metric_brief=brief, tenant_id="")
    assert res["ok"] is True
    assert res["document_id"]
    assert res["chunks"] > 0

    # 权威源文档分块内容应包含新指标名（即新口径已进入知识库）
    docs = await db.document_repo.list(source_uri=DICTIONARY_SOURCE_URI)
    assert docs, "权威源口径字典文档未生成"
    target = next(d for d in docs if d.tenant_id == "")
    chunks = await db.chunk_repo.list_by_document(target.id)
    full_text = "\n".join(c.content for c in chunks)
    assert "新发指标" in full_text
    # 重建是幂等的：再次重建不应产生重复文档
    res2 = await rebuild_dictionary_for_metric(runtime=rt, metric_brief=brief, tenant_id="")
    assert res2["ok"] is True
    docs_after = await db.document_repo.list(source_uri=DICTIONARY_SOURCE_URI)
    assert len([d for d in docs_after if d.tenant_id == ""]) == 1


@pytest.mark.asyncio
async def test_rebuild_dictionary_safe_on_missing_layer():
    db = DatabaseBundle(mode="memory")
    rt = SimpleNamespace(db=db, settings=Settings(), semantic_layer=None, vector_store=None)
    res = await rebuild_dictionary_for_metric(
        runtime=rt,
        metric_brief={"name": "x", "metric_type": "simple", "tenant_id": ""},
        tenant_id="",
    )
    assert res["ok"] is False
    assert res["error"] == "no_merged_layer"


# ---------------- ⑤⑥ 深度校验闸（含降级） ----------------


class _FakeExecutor:
    def __init__(self, info_columns=None, trial_rows=None, trial_fail=False):
        self.info_columns = info_columns or {}
        self.trial_rows = trial_rows if trial_rows is not None else [[1]]
        self.trial_fail = trial_fail

    async def fetch(self, sql, params=None):
        if "information_schema.columns" in sql:
            tname = params[0] if params else None
            cols = self.info_columns.get(tname, [])
            return {
                "columns": ["column_name"],
                "rows": [[c] for c in cols],
                "row_count": len(cols),
                "truncated": False,
                "elapsed_ms": 1.0,
                "sql": sql,
            }
        if self.trial_fail:
            raise StarRocksError("试跑失败：连接超时")
        return {
            "columns": ["x"],
            "rows": self.trial_rows,
            "row_count": len(self.trial_rows),
            "truncated": False,
            "elapsed_ms": 1.0,
            "sql": sql,
        }


class _FakeEngine:
    def __init__(self, executor):
        self.executor = executor

    def compile(self, query):
        return SimpleNamespace(sql="SELECT 1 AS x")


def _brief():
    return {
        "name": "新发指标",
        "metric_type": "simple",
        "type_params": {"measure": "amt"},
        "aliases": [],
        "unit": "",
        "description": "",
        "scale": 1.0,
        "round": 4,
        "granularities": ["day"],
        "pop_types": ["mom"],
    }


def _merged_layer():
    return SemanticLayer.from_manifest_dict(
        {
            "semantic_models": [
                {
                    "name": "m1",
                    "table": "t_fact",
                    "entities": [{"name": "cust", "type": "primary", "expr": "cust_id"}],
                    "dimensions": [
                        {"name": "region", "type": "categorical", "expr": "region"},
                        {"name": "stat_date", "type": "time", "expr": "stat_date"},
                    ],
                    "measures": [{"name": "amt", "agg": "sum", "expr": "amount"}],
                }
            ],
            "metrics": [
                {"name": "占位指标", "type": "simple", "type_params": {"measure": "amt"}}
            ],
        }
    )


async def _validate_deep(engine, info_columns=None, trial_fail=False):
    layer = _merged_layer()
    brief = _brief()
    merged = build_layer_with_metric(layer, brief)
    executor = _FakeExecutor(info_columns=info_columns, trial_fail=trial_fail)
    eng = engine(executor) if engine else None
    return await validate_metric_definition(
        brief,
        metric_key="new_k",
        all_metric_defs={"new_k": {"metric_type": "simple", "type_params": {"measure": "amt"}}},
        measure_keys={"amt"},
        layer=merged or layer,
        engine=eng,
        deep=True,
    )


@pytest.mark.asyncio
async def test_no_engine_deep_gates_warn():
    res = await _validate_deep(None)
    # 无引擎时，⑤⑥ 应降级为 warning，不阻断
    statuses = {g.gate: g.status for g in res.gates}
    assert statuses["physical_columns"] == "warning"
    assert statuses["trial_run"] == "warning"
    assert statuses["sql_dryrun"] == "warning"


@pytest.mark.asyncio
async def test_deep_gates_pass_when_columns_exist_and_trial_ok():
    info = {"t_fact": ["amount", "region", "stat_date", "cust_id"]}
    res = await _validate_deep(_FakeEngine, info_columns=info, trial_fail=False)
    assert res.passed is True
    statuses = {g.gate: g.status for g in res.gates}
    assert statuses["sql_dryrun"] == "ok"
    assert statuses["physical_columns"] == "ok"
    assert statuses["trial_run"] == "ok"


@pytest.mark.asyncio
async def test_deep_physical_column_missing_is_problem():
    # information_schema 中 t_fact 没有任何列 -> amount 缺失 -> ⑤ problem（阻断发布）
    res = await _validate_deep(_FakeEngine, info_columns={}, trial_fail=False)
    assert res.passed is False
    gate = next(g for g in res.gates if g.gate == "physical_columns")
    assert gate.status == "problem"
    assert any("⑤" in p for p in res.problems)


@pytest.mark.asyncio
async def test_deep_trial_run_failure_is_problem():
    # 物理列存在（⑤ ok），但试跑 fetch 抛错 -> ⑥ problem（阻断发布）
    info = {"t_fact": ["amount", "region", "stat_date", "cust_id"]}
    res = await _validate_deep(_FakeEngine, info_columns=info, trial_fail=True)
    assert res.passed is False
    gate = next(g for g in res.gates if g.gate == "trial_run")
    assert gate.status == "problem"
    assert any("⑥" in p for p in res.problems)
