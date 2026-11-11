"""P2-5 结果元信息补全：data_as_of 数据截至时间 + 口径水印（caliber）。

验证：
- build_caliber 从语义清单构造口径水印（指标口径 / 维度 / 数据源 / 数据截至）；
- engine.query 返回结果稳定携带 data_as_of 与 caliber，且失败时静默降级。
"""

import asyncio
from types import SimpleNamespace

from dmf_agent.config.settings import Settings
from dmf_agent.semantic.engine import MetricFlowEngine, build_caliber
from dmf_agent.semantic.layer import SemanticLayer


def _fake_executor():
    """假执行器：主查询与 data_as_of 探查共用同一返回，让 MAX 探查拿到一个日期。"""
    async def fetch(sql):
        return {
            "columns": ["c", "v"],
            "rows": [["2026-09-14", 100]],
            "row_count": 1,
            "truncated": False,
            "elapsed_ms": 1.0,
        }

    settings = Settings().starrocks
    return SimpleNamespace(fetch=fetch, settings=settings)


def test_build_caliber_simple_metric():
    layer = SemanticLayer()
    cal = build_caliber(
        layer.manifest,
        ["零售存款余额"],
        ["机构"],
        ["dws_retail_deposit"],
        data_as_of="2026-09-14",
    )
    assert cal["metrics"], "应产出指标口径"
    m0 = cal["metrics"][0]
    assert m0["name"] == "零售存款余额"
    # simple 指标应含度量说明
    assert "度量" in m0["detail"]
    # 水印应串联指标 / 维度 / 数据源 / 数据截至
    assert "零售存款余额" in cal["watermark"]
    assert "机构" in cal["watermark"]
    assert "dws_retail_deposit" in cal["watermark"]
    assert "2026-09-14" in cal["watermark"]


def test_build_caliber_unknown_metric_kept():
    layer = SemanticLayer()
    cal = build_caliber(layer.manifest, ["不存在的指标X"], [], [])
    assert cal["metrics"][0]["type"] == "unknown"
    assert "不存在的指标X" in cal["watermark"]


def test_engine_query_carries_meta():
    layer = SemanticLayer()
    engine = MetricFlowEngine(layer, _fake_executor())
    res = asyncio.run(
        engine.query(
            {
                "metrics": [{"name": "零售存款余额"}],
                "dimensions": ["机构"],
                "time_range": {"granularity": "month"},
            }
        )
    )
    # 结果元信息稳定透传
    assert "data_as_of" in res
    assert res["data_as_of"] == "2026-09-14"
    assert "caliber" in res and res["caliber"]["metrics"]
    # 数据源 / 模型应被记录
    assert res["caliber"]["models"]
    assert "零售存款余额" in res["caliber"]["watermark"]
