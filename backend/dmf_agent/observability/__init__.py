"""观测模块（遵循 OpenTelemetry，trace 的三个落点）。

- **span 落库** otel.py：每轮对话用 OTel SDK 采集 span（root span `chat_round` +
  每个图节点一条 `node.<name>`），轮末落 `trace_spans` 表；**不导出任何外部观测平台**
  （agent-insight / OTLP collector 已移除，相关配置一并取消）；
- **成本与质量（FR11）**：`cost.py`（LLM token 计价 + 预算告警）、
  `quality.py`（有据回答率 / 采纳率 / 降级率 / 幻觉风险）；
- **轮级 trace 落库**：`trace_records` 表（db 的 TraceRepository，MySQL / 内存双实现），
  前端"运行观测"页与对话内观测信息的数据源；
- **文件留痕** archive.py：每轮 trace 落盘 {archive_dir}/{session_id}/{round_id}.json，
  合规审计 / 离线归档，目录由 session.archive_dir 配置（默认 data/sessions）。
"""

from dmf_agent.observability.archive import (
    SessionTraceArchiver,
    build_trace_archiver,
)
from dmf_agent.observability.cost import budget_alerts, estimate_cost, resolve_price
from dmf_agent.observability.otel import (
    NodeSpan,
    RoundTracer,
    build_round_tracer,
    iso_to_ns,
    otel_meta,
)
from dmf_agent.observability.quality import aggregate_quality, round_signals

__all__ = [
    "SessionTraceArchiver",
    "build_trace_archiver",
    "NodeSpan",
    "RoundTracer",
    "build_round_tracer",
    "iso_to_ns",
    "otel_meta",
    "estimate_cost",
    "resolve_price",
    "budget_alerts",
    "round_signals",
    "aggregate_quality",
]
