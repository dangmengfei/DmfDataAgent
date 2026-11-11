"""局部租户覆盖（overlay）与快照链解析测试。

覆盖三层：
1. 纯函数 —— ``apply_overlay`` / ``normalize_overlay_payload`` / ``normalize_mode``；
2. 快照链 —— ``resolve_chain`` 的 full / overlay / 无基座 / 忽略非 published 四种情形；
3. 端到端 —— ``SemanticRegistry`` 把「全局 full + 租户 overlay」解析成租户生效层，
   以及 ``resolve_layer`` 给出的溯源元信息（scope / base_version / overlay_versions）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dmf_agent.db.memory import InMemorySemanticPublishedRepository
from dmf_agent.schemas.semantic_publish import SemanticPublishedData
from dmf_agent.semantic.layer import SemanticLayer
from dmf_agent.semantic.overlay import (
    apply_overlay,
    normalize_mode,
    normalize_overlay_payload,
    resolve_chain,
    summarize_overlay,
)
from dmf_agent.semantic.registry import SemanticRegistry
from dmf_agent.semantic.runtime import resolve_layer


def _rec(
    version: int,
    payload: dict,
    *,
    mode: str = "full",
    state: str = "published",
    tenant_id: str = "",
) -> SemanticPublishedData:
    return SemanticPublishedData(
        id=version,
        tenant_id=tenant_id,
        version=version,
        state=state,
        mode=mode,
        payload=payload,
    )


def _settings(source: str = "yaml") -> SimpleNamespace:
    return SimpleNamespace(semantic=SimpleNamespace(source=source, manifest_dir=""))


def _gmv(**overrides) -> dict:
    base = {
        "name": "GMV",
        "type": "simple",
        "type_params": {"measure": "gmv_amount"},
        "description": "成交总额（支付口径）",
    }
    base.update(overrides)
    return base


# ---------- 纯函数：apply_overlay ----------


def test_apply_overlay_replaces_by_name_and_appends():
    base = {"version": 1, "metrics": [_gmv(), {"name": "订单数"}]}
    merged, report = apply_overlay(
        base, {"metrics": [_gmv(description="本租户口径"), {"name": "新指标"}]}
    )
    assert [m["name"] for m in merged["metrics"]] == ["GMV", "订单数", "新指标"]
    assert merged["metrics"][0]["description"] == "本租户口径"
    assert report["metrics_replaced"] == ["GMV"]
    assert report["metrics_added"] == ["新指标"]
    # 标量键沿用基座（overlay 不携带）
    assert merged["version"] == 1
    # 不修改入参
    assert base["metrics"][0]["description"] == "成交总额（支付口径）"
    assert len(base["metrics"]) == 2


def test_apply_overlay_models_are_wholesale_replaced():
    base = {
        "semantic_models": [
            {"name": "trade_day", "table": "t", "measures": [{"name": "m1"}]},
            {"name": "user_day", "table": "u", "measures": [{"name": "m2"}]},
        ]
    }
    merged, report = apply_overlay(
        base, {"semantic_models": [{"name": "trade_day", "table": "t2", "measures": [{"name": "m1"}]}]}
    )
    assert [m["name"] for m in merged["semantic_models"]] == ["trade_day", "user_day"]
    assert merged["semantic_models"][0]["table"] == "t2"  # 整条替换，不留旧字段
    assert report["models_replaced"] == ["trade_day"]
    assert report["models_added"] == []


def test_apply_overlay_model_nested_dimensions_measures_overridden():
    """维度/度量嵌套在模型内、无独立租户列；overlay 对模型整条替换，
    因此改了嵌套 dimensions/measures 数组的 overlay 应整体取代基座对应模型。"""
    base = {
        "semantic_models": [
            {
                "name": "trade_day",
                "table": "t",
                "dimensions": [
                    {"name": "统计日期", "type": "time", "expr": "stat_date"},
                    {"name": "渠道", "type": "categorical", "expr": "channel_name"},
                ],
                "measures": [{"name": "gmv_amount", "agg": "sum", "expr": "gmv"}],
            }
        ]
    }
    overlay = {
        "semantic_models": [
            {
                "name": "trade_day",
                # 只改了维度/度量数组（整条替换语义）
                "dimensions": [
                    {"name": "统计日期", "type": "time", "expr": "stat_date"},
                    {"name": "地区", "type": "categorical", "expr": "region_name"},
                ],
                "measures": [{"name": "gmv_amount", "agg": "sum", "expr": "gmv"}],
            }
        ]
    }
    merged, _ = apply_overlay(base, overlay)
    model = merged["semantic_models"][0]
    assert [d["name"] for d in model["dimensions"]] == ["统计日期", "地区"]
    assert "渠道" not in [d["name"] for d in model["dimensions"]]  # 旧维度被整体取代
    # normalize 也应原样保留嵌套数组
    norm = normalize_overlay_payload(overlay)
    assert norm["semantic_models"][0]["dimensions"][1]["name"] == "地区"



def test_apply_overlay_rejects_nameless_entry():
    with pytest.raises(ValueError):
        apply_overlay({"metrics": []}, {"metrics": [{"type": "simple"}]})


def test_apply_overlay_empty_overlay_is_noop():
    base = {"metrics": [_gmv()]}
    merged, report = apply_overlay(base, {})
    assert [m["name"] for m in merged["metrics"]] == ["GMV"]
    assert report == {
        "models_added": [],
        "models_replaced": [],
        "metrics_added": [],
        "metrics_replaced": [],
    }


# ---------- 纯函数：normalize_overlay_payload / normalize_mode / summarize ----------


def test_normalize_overlay_payload_drops_scalars_and_other_keys():
    payload = {
        "version": 9,
        "dimensions": [{"name": "分行"}],  # 非差异键 → 丢弃
        "metrics": [_gmv(description="x")],
    }
    out = normalize_overlay_payload(payload)
    assert set(out) == {"metrics"}
    assert out["metrics"][0]["description"] == "x"


def test_normalize_overlay_payload_rejects_empty_and_nameless():
    with pytest.raises(ValueError):
        normalize_overlay_payload({})
    with pytest.raises(ValueError):
        normalize_overlay_payload({"metrics": [{"type": "simple"}]})


def test_normalize_mode():
    assert normalize_mode(None) == "full"
    assert normalize_mode("") == "full"
    assert normalize_mode("OVERLAY") == "overlay"
    with pytest.raises(ValueError):
        normalize_mode("partial")


def test_summarize_overlay():
    summary = summarize_overlay({"semantic_models": [{"name": "trade_day"}], "metrics": [_gmv()]})
    assert summary == {
        "model_count": 1,
        "metric_count": 1,
        "model_names": ["trade_day"],
        "metric_names": ["GMV"],
    }


# ---------- 快照链：resolve_chain ----------


def test_resolve_chain_newest_full_wins():
    rows = [
        _rec(3, {"metrics": [{"name": "A"}]}),
        _rec(2, {"metrics": [{"name": "B"}]}, mode="overlay"),
        _rec(1, {"metrics": [{"name": "C"}]}),
    ]
    raw, used = resolve_chain(rows)
    assert [m["name"] for m in raw["metrics"]] == ["A"]
    assert [r.version for r in used] == [3]  # 更旧的 overlay 不再叠加


def test_resolve_chain_overlay_lands_on_nearest_full():
    rows = [
        _rec(4, {"metrics": [_gmv(description="最新覆盖")]}, mode="overlay"),
        _rec(3, {"metrics": [_gmv(description="中间覆盖")]}, mode="overlay"),
        _rec(2, {"metrics": [_gmv(), {"name": "订单数"}]}),
        _rec(1, {"metrics": []}),
    ]
    raw, used = resolve_chain(rows)
    assert raw["metrics"][0]["description"] == "最新覆盖"
    assert [m["name"] for m in raw["metrics"]] == ["GMV", "订单数"]
    # used 按版本升序：基座 v2 在前，overlay v3、v4 依序在后
    assert [r.version for r in used] == [2, 3, 4]


def test_resolve_chain_overlay_without_full_uses_base_raw():
    rows = [_rec(2, {"metrics": [_gmv(description="租户覆盖")]}, mode="overlay")]
    base = {"version": 1, "metrics": [_gmv(), {"name": "订单数"}]}
    raw, used = resolve_chain(rows, base)
    assert raw["metrics"][0]["description"] == "租户覆盖"
    assert [m["name"] for m in raw["metrics"]] == ["GMV", "订单数"]
    assert [r.version for r in used] == [2]


def test_resolve_chain_ignores_non_published():
    rows = [
        _rec(2, {"metrics": [{"name": "草稿"}]}, state="draft"),
        _rec(1, {"metrics": [{"name": "已发布"}]}),
    ]
    raw, used = resolve_chain(rows)
    assert [m["name"] for m in raw["metrics"]] == ["已发布"]
    assert [r.version for r in used] == [1]


def test_resolve_chain_empty_returns_base():
    base = {"metrics": [{"name": "基座"}]}
    raw, used = resolve_chain([], base)
    assert raw is base or raw == base
    assert used == []


# ---------- 端到端：全局 full + 租户 overlay ----------


async def test_tenant_overlay_on_global_full():
    reg = SemanticRegistry(InMemorySemanticPublishedRepository())
    await reg.publish(layer=SemanticLayer(), tenant_id="", by="sys")

    await reg.publish(
        tenant_id="t1",
        by="admin",
        mode="overlay",
        payload={"metrics": [_gmv(description="本租户口径")]},
    )

    got = await reg.get_published_layer(tenant_id="t1")
    assert got is not None
    assert got.manifest.metric("GMV").description == "本租户口径"
    assert len(got.list_metrics()) == 22  # 其余指标来自全局基座

    other = await reg.get_published_layer(tenant_id="t2")
    assert other is not None
    assert other.manifest.metric("GMV").description != "本租户口径"


async def test_tenant_full_snapshot_replaces_global_entirely():
    reg = SemanticRegistry(InMemorySemanticPublishedRepository())
    base = SemanticLayer()
    await reg.publish(layer=base, tenant_id="", by="sys")

    raw = base.manifest.to_dict()
    raw["metrics"] = [m for m in raw["metrics"] if m.get("name") == "GMV"]
    raw["semantic_models"] = [
        m for m in raw["semantic_models"] if m.get("name") == "trade_day"
    ]
    await reg.publish(layer=SemanticLayer.from_manifest_dict(raw), tenant_id="t1", by="admin")

    got = await reg.get_published_layer(tenant_id="t1")
    assert [m.name for m in got.list_metrics()] == ["GMV"]


async def test_overlay_version_sequence_shared_with_full():
    reg = SemanticRegistry(InMemorySemanticPublishedRepository())
    layer = SemanticLayer()
    r1 = await reg.publish(layer=layer, tenant_id="t1", by="a")
    r2 = await reg.publish(tenant_id="t1", by="a", mode="overlay", payload={"metrics": [_gmv()]})
    r3 = await reg.publish(layer=layer, tenant_id="t1", by="a")
    assert (r1.version, r2.version, r3.version) == (1, 2, 3)
    assert (r1.mode, r2.mode, r3.mode) == ("full", "overlay", "full")


async def test_version_seq_survives_delete():
    """版本号取「最大版本 + 1」而非 count + 1，删除中间版本后不会碰撞。"""
    reg = SemanticRegistry(InMemorySemanticPublishedRepository())
    layer = SemanticLayer()
    await reg.publish(layer=layer, tenant_id="t1", by="a")
    await reg.publish(layer=layer, tenant_id="t1", by="a")
    await reg.publish(layer=layer, tenant_id="t1", by="a")
    assert await reg.repo.delete(tenant_id="t1", version=2) is True
    nxt = await reg.publish(layer=layer, tenant_id="t1", by="a")
    assert nxt.version == 4


# ---------- 端到端：resolve_layer 溯源元信息 ----------


async def test_resolve_layer_reports_db_chain_metadata():
    reg = SemanticRegistry(InMemorySemanticPublishedRepository())
    await reg.publish(layer=SemanticLayer(), tenant_id="", by="sys")
    await reg.publish(
        tenant_id="t1", by="admin", mode="overlay", payload={"metrics": [_gmv()]}
    )

    resolved = await resolve_layer(settings=_settings("db"), tenant_id="t1", registry=reg)
    assert resolved.effective_source == "db"
    assert resolved.fell_back is False
    assert resolved.scope == "tenant"
    assert resolved.base_version == 1
    # 版本号是**租户内独立序列**：租户的第一条 overlay 就是 v1，与全局 v1 并存
    assert resolved.overlay_versions == [1]
    assert [(c["scope"], c["version"], c["mode"]) for c in resolved.chain] == [
        ("global", 1, "full"),
        ("tenant", 1, "overlay"),
    ]
    assert resolved.to_dict()["metrics"] == 22


async def test_resolve_layer_reports_fallback_when_no_snapshot():
    reg = SemanticRegistry(InMemorySemanticPublishedRepository())
    resolved = await resolve_layer(settings=_settings("db"), tenant_id="t1", registry=reg)
    assert resolved.effective_source == "yaml"
    assert resolved.fell_back is True
    assert resolved.scope == "yaml"
    assert resolved.base_version is None
    assert resolved.to_dict()["chain"] == []


async def test_resolve_layer_yaml_source_is_not_a_fallback():
    resolved = await resolve_layer(settings=_settings("yaml"), registry=None)
    assert resolved.requested_source == "yaml"
    assert resolved.effective_source == "yaml"
    assert resolved.fell_back is False
    assert resolved.manifest_path.endswith("manifest.d")
