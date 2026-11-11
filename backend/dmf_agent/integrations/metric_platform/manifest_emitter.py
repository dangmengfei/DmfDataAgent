"""HM 指标元数据 -> 语义层 manifest.d 结构导出。

背景：主流程元信息唯一来源已是语义层 ``manifest.d/``（多层 YAML），而
``loader.py`` 历史上只把 HM 指标落 ``metric_definitions`` 表，二者割裂。
本模块补齐 S3 缺口：把 HM 指标平台返回的指标元数据（名称 / 含义 / 单位 /
类型 / 维度）转换为**结构上合法、可被语义层直接加载**的 manifest.d 等价结构，
写入 ``models/<domain>_imported.yml`` 与 ``metrics/<domain>/<group>_imported.yml``。

关于"物理表待绑定"：HM 元信息接口**只给指标级元数据，不给物理表/列映射**。
manifest 的 ``validate()`` 仅校验"模型非空 table + 有时间维度 + measure agg 合法
+ expr 非空"（不校验物理表是否真实存在），因此这里生成的模型 stub 用占位
``table``/``expr``，并显式在 ``description`` 标注"待绑定物理表"。导入后由语义
建模人员替换真实 StarRocks 表/列即可编译取数——从而让 HM 指标真正进入语义层。

映射规则（HM metricsType -> manifest metric type）：
  HM 1 原子 / 2 派生 / 3 衍生 / 4 中间派生  -> 统一导出为 ``simple``
  （manifest 仅支持 simple/ratio/derived；ratio/derived 需要公式/引用，HM 接口
  不提供，故统一以 simple + 生成 measure 承载，类型原文保留在 description 中）。
每个 HM 指标生成唯一 measure 名 ``hm_m_<code>``，并由一个统一的导入模型
``hm_imported_<domain>`` 持有，保证引用完整性（measure 必然存在）。
"""

from __future__ import annotations

import yaml
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

# HM metricsType 枚举（迁移自老系统 hm_meta）：1 原子 / 2 派生 / 3 衍生 / 4 中间派生
HM_METRICS_TYPE_LABELS: Dict[int, str] = {
    1: "原子指标",
    2: "派生指标",
    3: "衍生指标",
    4: "中间派生指标",
}

# manifest 默认时间粒度 / 同环比白名单（与 semantic/manifest.py 对齐）
_DEFAULT_GRANULARITIES = ["day", "week", "month", "quarter", "year"]
_DEFAULT_POP_TYPES = ["wow", "mom", "qoq", "yoy", "pop"]


def _measure_name(code: str) -> str:
    """由 HM 指标 code 派生稳定的 measure 名（manifest 内唯一）。"""
    return f"hm_m_{code}"


def _model_name(domain: str) -> str:
    return f"hm_imported_{domain}"


def hm_metric_to_manifest_metric(raw: Dict[str, Any]) -> Dict[str, Any]:
    """单个 HM 指标对象 -> manifest metric dict（type=simple）。"""
    code = str(raw.get("id") or "").strip()
    name = (raw.get("cnName") or "").strip()
    if not code or not name:
        raise ValueError(f"HM 指标缺 id/cnName：{raw!r}")
    metrics_type = raw.get("metricsType")
    type_label = HM_METRICS_TYPE_LABELS.get(metrics_type, "未知类型")
    return {
        "name": name,
        "type": "simple",
        "type_params": {"measure": _measure_name(code)},
        "aliases": [],
        "unit": (raw.get("unit") or "").strip(),
        "round": 2,
        "description": (
            f"{raw.get('businessMeaning') or name}"
            f"（由 HM 指标平台导入；HM 类型：{type_label}；物理表待绑定）"
        ),
        "granularities": list(_DEFAULT_GRANULARITIES),
        "pop_types": list(_DEFAULT_POP_TYPES),
    }


def hm_metrics_to_manifest_dict(
    metrics: List[Dict[str, Any]], domain: str = "imported", group: str = "hm"
) -> Dict[str, Any]:
    """HM 指标列表 -> 合并后的 manifest dict（含模型 stub + 指标）。

    返回的 dict 可直接 ``SemanticManifest.from_dict`` 加载（通过结构校验）。
    """
    # 按 code 去重（与 loader 一致），保留最后一条
    seen: Dict[str, Dict[str, Any]] = {}
    for m in metrics:
        code = str(m.get("id") or "").strip()
        if code:
            seen[code] = m
    uniq = list(seen.values())  # 去重后仅保留每个 code 的最后一条

    metric_dicts: List[Dict[str, Any]] = []
    measures: List[Dict[str, Any]] = []
    dim_names: Dict[str, str] = {}  # 维度名 -> 占位 expr
    for raw in uniq:
        code = str(raw.get("id")).strip()
        metric_dicts.append(hm_metric_to_manifest_metric(raw))
        measures.append(
            {
                "name": _measure_name(code),
                "agg": "sum",
                # 物理列未知，用占位 expr（validate 只校验非空，不校验真实性）
                "expr": f"col_{code}",
                "unit": (raw.get("unit") or "").strip(),
                "description": f"HM 指标【{raw.get('cnName')}】对应的度量（物理列待绑定）",
            }
        )
        for d in raw.get("dimensions") or []:
            dn = (d.get("cnName") or "").strip()
            if dn:
                dim_names.setdefault(dn, dn)

    model_dims = [
        {"name": "统计日期", "type": "time", "expr": "stat_date",
         "type_params": {"time_granularity": "day"}},
        {"name": "主键", "type": "primary", "expr": "stat_date"},
    ]
    # 把 HM 维度也作为语义维度挂到 stub 模型（expr 占位），便于后续按维度下钻
    for dn in dim_names:
        model_dims.append({"name": dn, "type": "categorical", "expr": dn})

    model = {
        "name": _model_name(domain),
        "description": (
            f"由 HM 指标平台导入自动生成的语义模型（domain={domain} group={group}）。"
            f"物理表为占位，需替换为真实 StarRocks 表后方可编译取数。"
        ),
        "table": _model_name(domain),  # 占位，导入后由语义建模人员替换
        "agg_time_dimension": "统计日期",
        "entities": [{"name": "主键", "type": "primary", "expr": "stat_date"}],
        "dimensions": model_dims,
        "measures": measures,
    }

    return {
        "semantic_models": [model],
        "metrics": metric_dicts,
    }


def write_manifest_to_dir(
    manifest: Dict[str, Any], out_dir: Path, domain: str = "imported", group: str = "hm"
) -> List[Path]:
    """把 manifest dict 写入 out_dir：models/<domain>_imported.yml + metrics/<domain>/<group>_imported.yml。

    返回写入的文件路径列表。已存在同名文件会被覆盖（幂等重入）。
    """
    out_dir = Path(out_dir)
    models_dir = out_dir / "models"
    metrics_dir = out_dir / "metrics" / domain
    models_dir.mkdir(parents=True, exist_ok=True)
    metrics_dir.mkdir(parents=True, exist_ok=True)

    header = (
        "# 由 HM 指标平台导入自动生成（S3）。\n"
        "# 模型 table / 度量 expr 为占位，需替换为真实 StarRocks 表/列后方可编译取数。\n"
        "# 校验：python -m dmf_agent.semantic check\n"
    )
    models_file = models_dir / f"{domain}_imported.yml"
    metrics_file = metrics_dir / f"{group}_imported.yml"
    models_file.write_text(
        header + yaml.safe_dump({"semantic_models": manifest.get("semantic_models", [])},
                                allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    metrics_file.write_text(
        header + yaml.safe_dump({"metrics": manifest.get("metrics", [])},
                                allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return [models_file, metrics_file]


@dataclass
class EmitResult:
    source: str
    parsed: int
    model_file: Optional[Path]
    metrics_file: Optional[Path]
    dry_run: bool = False


def emit_manifest(
    metrics: List[Dict[str, Any]],
    out_dir: str | Path,
    domain: str = "imported",
    group: str = "hm",
    dry_run: bool = False,
) -> EmitResult:
    """把 HM 指标导出为 manifest.d YAML。

    - metrics: 已解析的 HM 指标原始字典列表（无需去重，内部按 code 去重）
    - out_dir: 目标目录（绝对或相对），会在其下写 models/ 与 metrics/<domain>/
    - dry_run: True 时只返回路径规划，不落盘
    """
    manifest = hm_metrics_to_manifest_dict(metrics, domain=domain, group=group)
    if dry_run:
        return EmitResult(
            source="hm", parsed=len(manifest.get("metrics", [])),
            model_file=None, metrics_file=None, dry_run=True,
        )
    files = write_manifest_to_dir(manifest, Path(out_dir), domain=domain, group=group)
    return EmitResult(
        source="hm", parsed=len(manifest.get("metrics", [])),
        model_file=files[0], metrics_file=files[1],
    )
