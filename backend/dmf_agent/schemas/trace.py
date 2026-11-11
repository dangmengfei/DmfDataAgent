"""对话 trace 观测记录（落库模型，供前端"运行观测"面板查询）。

两层数据结构，均遵循 OpenTelemetry 语义：

- `SpanData`：单条 span（`trace_spans` 表），字段对齐 OTel Span
  （trace_id / span_id / parent_span_id / name / kind / start|end_time /
  status / attributes / events）；一轮对话 = root span `chat_round`
  + 每个图节点一条子 span `node.<name>`。
- `TraceData`：轮级汇总（`trace_records` 表），保留原有列结构不变
  （前端既有展示契约不变），OTel 元信息（trace_id / span_count …）落在
  `trace["otel"]` 里，避免对既有表做 ALTER。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SpanData(BaseModel):
    """单条 OTel span（落库形态，字段与 OpenTelemetry Trace API 一一对应）。"""

    id: Optional[int] = None
    trace_id: str  # OTel TraceId（32 位十六进制）
    span_id: str  # OTel SpanId（16 位十六进制）
    parent_span_id: Optional[str] = None  # root span 为 None
    session_id: str
    round_id: str  # 助手消息 id（= 该轮 root span 所属对话轮次）
    name: str  # chat_round / node.execute_query / …
    kind: str = "INTERNAL"  # SpanKind：INTERNAL | SERVER | CLIENT | PRODUCER | CONSUMER
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration_ms: Optional[float] = None
    status_code: str = "UNSET"  # StatusCode：UNSET | OK | ERROR
    status_message: str = ""
    attributes: Dict[str, Any] = Field(default_factory=dict)
    events: List[Dict[str, Any]] = Field(default_factory=list)
    service_name: str = ""
    created_at: Optional[datetime] = None


class TraceData(BaseModel):
    """一轮对话的完整 trace（与 messages.node_trace 同源，另含汇总字段便于查询）。"""

    id: Optional[int] = None
    session_id: str
    round_id: str  # 助手消息 id
    intent: Optional[str] = None
    mock_mode: bool = False
    skills: List[str] = Field(default_factory=list)
    elapsed_ms: Optional[float] = None
    has_error: bool = False
    # token 消耗（从 LLM 响应 usage 累计；mock 模式为 None）
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    # ---- FR11 成本与归属（迁移 0021） ----
    user_id: str = ""
    tenant_id: str = ""
    # 本轮使用的 LLM 模型名（按模型归集成本）
    model: str = ""
    # 本轮估算成本（币种见 DMF_COST-CURRENCY）
    cost: float = 0.0
    # 完整 trace（要素 / DSL / 取数 / 分析 / 归因 / final_answer / otel 等）
    trace: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None

    @property
    def otel(self) -> Dict[str, Any]:
        """trace 中携带的 OTel 元信息（trace_id / span_count / service_name）。"""
        meta = (self.trace or {}).get("otel")
        return meta if isinstance(meta, dict) else {}

    @property
    def trace_id(self) -> Optional[str]:
        return self.otel.get("trace_id")

    @property
    def span_count(self) -> int:
        try:
            return int(self.otel.get("span_count") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def node_spans(self) -> List[Dict[str, Any]]:
        """本轮各节点 span 的轻量时间线（name / duration_ms / status）。"""
        spans = self.otel.get("node_spans")
        return spans if isinstance(spans, list) else []


def _percentile(values: List[float], pct: float) -> float:
    """线性插值分位数（pct 取 0~1）；空列表返回 0。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 1)
    pos = pct * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return round(ordered[low] + (ordered[high] - ordered[low]) * frac, 1)


def aggregate_traces(records: list[TraceData]) -> dict[str, Any]:
    """汇总 KPI：总量 / mock 数 / 错误数 / 耗时（均值·分位）/ 意图与技能分布（SQL/内存共用）。

    返回键保持向后兼容（前端既有观测面板不变的展示契约）：
    total / mock_count / error_count / avg_elapsed_ms / prompt_tokens /
    completion_tokens / total_tokens / intent_distribution / skill_distribution；
    另追加 success_rate / error_rate / p50_elapsed_ms / p95_elapsed_ms /
    max_elapsed_ms / span_count / avg_span_count / token_rounds（有 token 的轮次）。
    """
    from collections import Counter, defaultdict

    intents: Counter = Counter()
    skills: Counter = Counter()
    elapsed: list[float] = []
    mock_count = 0
    error_count = 0
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    token_rounds = 0
    span_total = 0
    span_rounds = 0
    daily = defaultdict(lambda: {"total": 0, "error": 0, "elapsed": []})
    for r in records:
        if r.intent:
            intents[r.intent] += 1
        if r.mock_mode:
            mock_count += 1
        if r.has_error:
            error_count += 1
        for s in r.skills:
            skills[s] += 1
        if isinstance(r.elapsed_ms, (int, float)):
            elapsed.append(float(r.elapsed_ms))
        if isinstance(r.prompt_tokens, int):
            prompt_tokens += r.prompt_tokens
        if isinstance(r.completion_tokens, int):
            completion_tokens += r.completion_tokens
        if isinstance(r.total_tokens, int):
            total_tokens += r.total_tokens
            token_rounds += 1
        if r.span_count:
            span_total += r.span_count
            span_rounds += 1
        if r.created_at is not None:
            key = r.created_at.strftime("%Y-%m-%d")
            bucket = daily[key]
            bucket["total"] += 1
            if r.has_error:
                bucket["error"] += 1
            if isinstance(r.elapsed_ms, (int, float)):
                bucket["elapsed"].append(float(r.elapsed_ms))

    total = len(records)
    return {
        "total": total,
        "mock_count": mock_count,
        "error_count": error_count,
        "avg_elapsed_ms": round(sum(elapsed) / len(elapsed), 1) if elapsed else 0,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "intent_distribution": dict(intents.most_common()),
        "skill_distribution": dict(skills.most_common()),
        # ---- 扩展指标（专页汇总用；不影响既有面板） ----
        "success_rate": round((total - error_count) / total * 100, 1) if total else 0.0,
        "error_rate": round(error_count / total * 100, 1) if total else 0.0,
        "p50_elapsed_ms": _percentile(elapsed, 0.5),
        "p95_elapsed_ms": _percentile(elapsed, 0.95),
        "max_elapsed_ms": round(max(elapsed), 1) if elapsed else 0,
        "avg_span_count": round(span_total / span_rounds, 1) if span_rounds else 0,
        "token_rounds": token_rounds,
        # 按天时间序列（升序），供专页趋势图
        "daily": [
            {
                "date": day,
                "total": bucket["total"],
                "error": bucket["error"],
                "avg_elapsed_ms": (
                    round(sum(bucket["elapsed"]) / len(bucket["elapsed"]), 1)
                    if bucket["elapsed"]
                    else 0
                ),
            }
            for day, bucket in sorted(daily.items())
        ],
    }


def aggregate_spans(spans: list[SpanData]) -> dict[str, Any]:
    """按 span 名称汇总节点级观测（耗时排行 / 错误数），供专页"性能明细"。"""
    from collections import defaultdict

    buckets: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "errors": 0, "elapsed": []}
    )
    for s in spans:
        b = buckets[s.name]
        b["count"] += 1
        if s.status_code == "ERROR":
            b["errors"] += 1
        if isinstance(s.duration_ms, (int, float)):
            b["elapsed"].append(float(s.duration_ms))
    items = []
    for name, b in buckets.items():
        items.append(
            {
                "name": name,
                "count": b["count"],
                "errors": b["errors"],
                "avg_ms": (
                    round(sum(b["elapsed"]) / len(b["elapsed"]), 1) if b["elapsed"] else 0
                ),
                "max_ms": round(max(b["elapsed"]), 1) if b["elapsed"] else 0,
            }
        )
    items.sort(key=lambda x: x["avg_ms"], reverse=True)
    return {"total": len(spans), "nodes": items}

