"""多文件夹语义清单加载器测试（分层组织 + 跨文件重名检测 + 字典往返）。

覆盖 `dmf_agent/semantic/manifest.py` 的目录加载能力：
- 默认 manifest.d 目录（models/ + metrics/<域>/）递归合并；
- 跨文件 model / metric / measure 重名 → ManifestError；
- 单文件仍兼容；to_dict/from_dict 无损往返。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dmf_agent.semantic.manifest import ManifestError, SemanticManifest

_HEADER = "version: 1\n"


def _write(dir_path: Path, rel: str, text: str) -> Path:
    f = dir_path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    return f


_MODEL_A = """
semantic_models:
  - name: m_a
    table: t_a
    agg_time_dimension: 统计日期
    dimensions:
      - name: 统计日期
        type: time
        expr: stat_date
    measures:
      - name: measure_a
        agg: sum
        expr: v_a
"""


def _metric(name: str, measure: str) -> str:
    return f"""
metrics:
  - name: {name}
    type: simple
    type_params: {{ measure: {measure} }}
"""


# ---------- 默认多层目录（真实 manifest.d） ----------


def test_default_dir_loads_and_merges():
    m = SemanticManifest()  # 默认 manifest.d 目录
    assert m.path.is_dir()
    assert len(m.models) == 4
    assert len(m.metrics) == 22
    # 指标（metrics/trade/gmvs.yml）跨文件引用模型（models/trade_day.yml）里的 measure
    assert m.measure_owner("gmv_amount") is not None
    assert m.metric("GMV") is not None
    # 派生指标引用同域其它指标（跨文件引用合并后仍可达）
    assert m.metric("存贷比") is not None


def test_default_dir_validation_clean():
    m = SemanticManifest()
    assert m.validate() + m.reference_problems() == []


# ---------- 目录合并 + 跨文件重名检测 ----------


def test_dir_merges_across_subfolders(tmp_path: Path):
    _write(tmp_path, "00_header.yml", _HEADER)
    _write(tmp_path, "models/a.yml", _MODEL_A)
    _write(tmp_path, "metrics/domain1/simple.yml", _metric("指标1", "measure_a"))
    _write(tmp_path, "metrics/domain2/simple.yml", _metric("指标2", "measure_a"))
    m = SemanticManifest(tmp_path)
    assert [x.name for x in m.models] == ["m_a"]
    assert sorted(x.name for x in m.metrics) == ["指标1", "指标2"]


def test_duplicate_model_across_files_raises(tmp_path: Path):
    _write(tmp_path, "a.yml", _MODEL_A)
    _write(tmp_path, "b.yml", _MODEL_A)  # 同名 model
    with pytest.raises(ManifestError, match="semantic_model 名跨文件重复"):
        SemanticManifest(tmp_path)


def test_duplicate_metric_across_files_raises(tmp_path: Path):
    _write(tmp_path, "models/a.yml", _MODEL_A)
    _write(tmp_path, "metrics/x.yml", _metric("同名指标", "measure_a"))
    _write(tmp_path, "metrics/y.yml", _metric("同名指标", "measure_a"))
    with pytest.raises(ManifestError, match="metric 名跨文件重复"):
        SemanticManifest(tmp_path)


def test_duplicate_measure_across_files_raises(tmp_path: Path):
    _write(tmp_path, "models/a.yml", _MODEL_A)
    # 另一个模型用了相同的 measure 名（measure_a）
    other = """
semantic_models:
  - name: m_b
    table: t_b
    agg_time_dimension: 统计日期
    dimensions:
      - name: 统计日期
        type: time
        expr: stat_date
    measures:
      - name: measure_a
        agg: sum
        expr: v_b
"""
    _write(tmp_path, "models/b.yml", other)
    with pytest.raises(ManifestError, match="measure 名跨文件重复"):
        SemanticManifest(tmp_path)


def test_empty_dir_raises(tmp_path: Path):
    with pytest.raises(ManifestError, match="为空"):
        SemanticManifest(tmp_path)


# ---------- 单文件兼容 + 字典往返 ----------


def test_single_file_still_supported(tmp_path: Path):
    f = _write(tmp_path, "single.yml", _HEADER + _MODEL_A + _metric("指标1", "measure_a"))
    m = SemanticManifest(f)
    assert m.path.is_file()
    assert [x.name for x in m.models] == ["m_a"]
    assert [x.name for x in m.metrics] == ["指标1"]


def test_to_dict_from_dict_roundtrip():
    m = SemanticManifest()
    raw = m.to_dict()
    m2 = SemanticManifest.from_dict(raw)
    assert len(m2.models) == len(m.models)
    assert len(m2.metrics) == len(m.metrics)
    assert m2.metric("GMV") is not None
    # 往返字典与原始一致（无损）
    assert m2.to_dict() == raw


def test_from_dict_invalid_raises():
    with pytest.raises(ManifestError):
        SemanticManifest.from_dict({"semantic_models": [], "metrics": []})
