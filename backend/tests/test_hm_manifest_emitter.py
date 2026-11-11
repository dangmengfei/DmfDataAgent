"""S3：HM 指标元数据 -> 语义层 manifest.d 导出测试。

覆盖 manifest_emitter 与 loader 接线：
- 单指标映射（统一 simple + 生成 measure）
- 合并 dict 可被 SemanticManifest.from_dict 加载（结构 + 引用校验通过）
- 维度上卷为模型语义维度
- 按 code 去重（保留最后一条）
- write_manifest_to_dir / emit_manifest 落盘两文件且可被目录加载
- loader.load 带 emit_manifest_dir 实际落盘并进入语义层
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dmf_agent.integrations.metric_platform.manifest_emitter import (
    EmitResult,
    emit_manifest,
    hm_metric_to_manifest_metric,
    hm_metrics_to_manifest_dict,
    write_manifest_to_dir,
)
from dmf_agent.semantic.manifest import SemanticManifest

# 取自 seed_metrics.json 的简化 HM 指标样本
SAMPLE_HM = [
    {
        "id": 1001,
        "cnName": "GMV",
        "businessMeaning": "成交总额",
        "metricsType": 2,
        "unit": "元",
        "dimensions": [
            {"id": 66, "cnName": "消费渠道"},
            {"id": 67, "cnName": "地区"},
        ],
    },
    {
        "id": 1002,
        "cnName": "日活跃用户数",
        "businessMeaning": "DAU",
        "metricsType": 1,
        "unit": "人",
        "dimensions": [{"id": 66, "cnName": "消费渠道"}],
    },
    {"id": 1003, "cnName": "新增用户数", "metricsType": 1, "unit": "人"},
]


def test_single_metric_mapping_is_simple():
    d = hm_metric_to_manifest_metric(SAMPLE_HM[0])
    assert d["type"] == "simple"
    assert d["type_params"]["measure"] == "hm_m_1001"
    assert d["unit"] == "元"
    assert "物理表待绑定" in d["description"]
    assert d["granularities"] and d["pop_types"]


def test_metric_missing_id_or_name_raises():
    with pytest.raises(ValueError):
        hm_metric_to_manifest_metric({"id": 1})
    with pytest.raises(ValueError):
        hm_metric_to_manifest_metric({"cnName": "X"})


def test_dict_is_loadable_by_from_dict():
    manifest = hm_metrics_to_manifest_dict(SAMPLE_HM, domain="imported", group="hm")
    # from_dict 等价于加载，应通过结构 + 引用校验
    loaded = SemanticManifest.from_dict(manifest)
    assert len(loaded.models) == 1
    assert loaded.models[0].name == "hm_imported_imported"
    assert len(loaded.metrics) == 3
    names = {m.name for m in loaded.metrics}
    assert {"GMV", "日活跃用户数", "新增用户数"} <= names
    # 每个指标引用的 measure 都存在（引用完整性）
    for m in loaded.metrics:
        assert m.measure is not None
        assert loaded.measure_owner(m.measure) is not None


def test_dimensions_surfaced_as_model_dims():
    manifest = hm_metrics_to_manifest_dict(SAMPLE_HM, domain="imported", group="hm")
    model = manifest["semantic_models"][0]
    dim_names = {d["name"] for d in model["dimensions"]}
    # 时间维度 + 主键 + HM 维度（消费渠道/地区）
    assert "统计日期" in dim_names
    assert "主键" in dim_names
    assert "消费渠道" in dim_names
    assert "地区" in dim_names


def test_dedup_by_code_keeps_last():
    dup = list(SAMPLE_HM) + [dict(SAMPLE_HM[0], cnName="GMV_改")]
    manifest = hm_metrics_to_manifest_dict(dup, domain="imported", group="hm")
    # code=1001 去重，总条数仍为 3
    assert len(manifest["metrics"]) == 3
    names = {m["name"] for m in manifest["metrics"]}
    assert "GMV_改" in names  # 同 code 保留最后一条


def test_write_manifest_to_dir_and_dir_load(tmp_path: Path):
    manifest = hm_metrics_to_manifest_dict(SAMPLE_HM, domain="shop", group="hm")
    files = write_manifest_to_dir(manifest, tmp_path, domain="shop", group="hm")
    assert len(files) == 2
    models_file, metrics_file = files
    assert models_file.exists() and metrics_file.exists()
    # 目录加载（与运行时一致）
    loaded = SemanticManifest(tmp_path)
    assert loaded.models[0].name == "hm_imported_shop"
    assert len(loaded.metrics) == 3


def test_emit_manifest_dry_run_no_files(tmp_path: Path):
    res = emit_manifest(SAMPLE_HM, tmp_path, domain="shop", group="hm", dry_run=True)
    assert isinstance(res, EmitResult)
    assert res.dry_run is True
    assert res.parsed == 3
    assert res.model_file is None and res.metrics_file is None
    assert not (tmp_path / "models").exists()


def test_emit_manifest_writes_files(tmp_path: Path):
    res = emit_manifest(SAMPLE_HM, tmp_path, domain="bank", group="hm")
    assert res.dry_run is False
    assert res.model_file is not None and res.metrics_file is not None
    assert res.model_file.exists() and res.metrics_file.exists()
    # 跨文件引用合并后加载通过
    loaded = SemanticManifest(tmp_path)
    assert loaded.models[0].name == "hm_imported_bank"
    assert len(loaded.metrics) == 3


async def test_loader_wiring_emits_manifest(tmp_path: Path):
    """loader.load 带 emit_manifest_dir 时实际落盘两个 yml，且可被语义层加载。"""
    from dmf_agent.config.settings import get_settings
    from dmf_agent.db.memory import InMemoryMetricRepository
    from dmf_agent.integrations.metric_platform.loader import MetricMetadataLoader

    settings = get_settings()
    repo = InMemoryMetricRepository()  # 内存仓储，不触达 MySQL
    loader = MetricMetadataLoader(repo, settings)
    seed = (
        Path(__file__).resolve().parent.parent
        / "dmf_agent" / "integrations" / "metric_platform" / "seed_metrics.json"
    )
    out = tmp_path / "manifest_out"
    result = await loader.load(
        source="file",
        file_path=str(seed),
        emit_manifest_dir=str(out),
        domain="seed",
        group="hm",
    )
    assert result.emitted > 0
    assert result.manifest_dir == str(out)
    # 落盘的两份 yml 可被语义层直接加载
    loaded = SemanticManifest(out)
    assert loaded.models[0].name == "hm_imported_seed"
    assert len(loaded.metrics) == result.emitted


async def test_loader_dry_run_emits_parsed_count(tmp_path: Path):
    from dmf_agent.config.settings import get_settings
    from dmf_agent.db.memory import InMemoryMetricRepository
    from dmf_agent.integrations.metric_platform.loader import MetricMetadataLoader

    settings = get_settings()
    repo = InMemoryMetricRepository()
    loader = MetricMetadataLoader(repo, settings)
    seed = (
        Path(__file__).resolve().parent.parent
        / "dmf_agent" / "integrations" / "metric_platform" / "seed_metrics.json"
    )
    out = tmp_path / "manifest_dry"
    result = await loader.load(
        source="file", file_path=str(seed), dry_run=True,
        emit_manifest_dir=str(out), domain="seed", group="hm",
    )
    assert result.dry_run is True
    assert result.emitted == result.fetched  # dry_run 也解析并导出
    assert not out.exists()  # dry_run 不落盘
