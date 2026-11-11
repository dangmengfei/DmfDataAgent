"""语义层（MetricFlow 规范）—— 从"元信息 + 查询"到"StarRocks SQL"的完整实现。

模块职责：
- manifest.yml / manifest.py : 语义清单（semantic_models + metrics，含物理表映射）
- layer.py                   : SemanticLayer（指标/维度解析、白名单与可用性校验）
- query.py                   : MetricQuery（Agent 与编译器之间的查询中间表示）
- compiler.py                : MetricFlowSqlCompiler（语义查询 -> 方言 SQL）
- dialects.py                : SQL 方言适配（starrocks/mysql/postgresql/oracle）
- starrocks.py               : StarRocksClient（MySQL 协议执行器 + 只读守卫）
- engine.py                  : MetricFlowEngine（compile / query 统一入口）
- __main__.py                : CLI（list-metrics / compile / query / check）

约定：主流程不出现任何外部指标平台编码；指标平台接入见
dmf_agent/integrations/metric_platform（离线元信息导入，后续开发）。
"""

from dmf_agent.semantic.compiler import (
    CompiledQuery,
    CompileError,
    MetricFlowSqlCompiler,
    lag_delta,
)
from dmf_agent.semantic.dialects import SUPPORTED_DIALECTS, SqlDialect, get_dialect
from dmf_agent.semantic.engine import ENGINE_NAME, MetricFlowEngine, SemanticQueryResult
from dmf_agent.semantic.layer import SemanticLayer
from dmf_agent.semantic.manifest import (
    MEASURE_AGGS,
    METRIC_TYPES,
    POP_TYPES,
    TIME_GRANULARITIES,
    Dimension,
    ManifestError,
    Measure,
    Metric,
    SemanticManifest,
    SemanticModel,
)
from dmf_agent.semantic.query import MetricOp, MetricQuery, QueryFilter, normalize_filter
from dmf_agent.semantic.starrocks import (
    StarRocksClient,
    StarRocksError,
    assert_read_only,
)

__all__ = [
    "ENGINE_NAME",
    "MEASURE_AGGS",
    "METRIC_TYPES",
    "POP_TYPES",
    "SUPPORTED_DIALECTS",
    "TIME_GRANULARITIES",
    "CompileError",
    "CompiledQuery",
    "Dimension",
    "ManifestError",
    "Measure",
    "Metric",
    "MetricFlowEngine",
    "MetricFlowSqlCompiler",
    "MetricOp",
    "MetricQuery",
    "QueryFilter",
    "SemanticLayer",
    "SemanticManifest",
    "SemanticModel",
    "SemanticQueryResult",
    "SqlDialect",
    "StarRocksClient",
    "StarRocksError",
    "assert_read_only",
    "get_dialect",
    "lag_delta",
    "normalize_filter",
]
