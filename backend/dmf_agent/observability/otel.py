"""OpenTelemetry 观测采集：span 直接落库，不接任何外部观测平台。

设计（严格遵循 OTel Trace API 语义）：

- 每轮对话构建**一个独立的 TracerProvider**（不与全局 provider 混用，避免多轮/测试串扰），
  挂一个自定义 `SpanExporter` 把 span 收集到进程内列表，轮末统一转成 `SpanData` 落
  `trace_spans` 表；
- 一轮对话 = root span `chat_round`（SpanKind.SERVER），每个图节点 = 一条子 span
  `node.<name>`（SpanKind.INTERNAL，parent 指向 root）；
- span 的 trace_id / span_id / parent_span_id 由 SDK 生成（真正符合 OTel 规范的
  128bit TraceId + 64bit SpanId），属性、状态、开始/结束时间均按规范写入。

**不再导出到 agent-insight / 任何 OTLP collector**：原先的 OTLPSpanExporter 与
`observability.otlp_endpoint` 配置已移除；`enabled=false` 或依赖缺失时
`build_round_tracer()` 返回 None，ChatService 退化为"仅轮级 trace 落库"（不阻断主链路）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from dmf_agent.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# OTel 属性只接受标量；其余类型序列化为字符串
_SCALAR = (str, bool, int, float)


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _sanitize_attributes(attrs: Dict[str, Any], limit: int) -> Dict[str, Any]:
    """把任意 Python 值收敛成 OTel 可接受的属性值（标量 / 标量列表）。"""
    out: Dict[str, Any] = {}
    for key, value in (attrs or {}).items():
        if value is None:
            continue
        if isinstance(value, _SCALAR):
            out[key] = _truncate(value, limit) if isinstance(value, str) else value
        elif isinstance(value, (list, tuple)):
            if not value:
                continue
            scalars = [v for v in value if isinstance(v, _SCALAR)]
            if scalars:
                out[key] = [
                    _truncate(v, limit) if isinstance(v, str) else v for v in scalars
                ]
            else:
                out[key] = _truncate(str(value), limit)
        else:
            out[key] = _truncate(str(value), limit)
    return out


def _ns_to_datetime(ns: Optional[int]) -> Optional[datetime]:
    if not ns:
        return None
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).replace(tzinfo=None)


def iso_to_ns(value: Any) -> Optional[int]:
    """把 ISO8601 时间字符串（LangGraph 事件 timestamp）转成 OTel 的纳秒时间戳。"""
    if not value:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1e9)
    except (TypeError, ValueError):
        return None


class _RoundSpanExporter:
    """把 SDK 产出的 ReadableSpan 收集到进程内列表（占位实现，按需惰性导入可测试）。"""

    def __init__(self) -> None:
        self.spans: List[Any] = []

    # ---- opentelemetry.sdk.trace.export.SpanExporter 协议 ----
    def export(self, spans) -> Any:  # noqa: ANN001 - SDK 协议
        self.spans.extend(spans)
        return self._success

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # noqa: ARG002
        return True

    @property
    def _success(self) -> Any:
        from opentelemetry.sdk.trace.export import SpanExportResult

        return SpanExportResult.SUCCESS


class NodeSpan:
    """节点子 span 的轻量句柄：结束时写入状态与属性。"""

    def __init__(self, span: Any, name: str, limit: int) -> None:
        self._span = span
        self.name = name
        self._limit = limit

    def set_attribute(self, key: str, value: Any) -> None:
        if self._span is None or value is None:
            return
        sanitized = _sanitize_attributes({key: value}, self._limit)
        if key in sanitized:
            self._span.set_attribute(key, sanitized[key])

    def set_attributes(self, attrs: Dict[str, Any]) -> None:
        if self._span is None:
            return
        for key, value in _sanitize_attributes(attrs, self._limit).items():
            self._span.set_attribute(key, value)

    def add_event(self, name: str, attributes: Optional[Dict[str, Any]] = None) -> None:
        if self._span is None:
            return
        self._span.add_event(
            name, attributes=_sanitize_attributes(attributes or {}, self._limit)
        )

    def end(
        self,
        *,
        error: bool = False,
        message: str = "",
        end_time_ns: Optional[int] = None,
    ) -> None:
        if self._span is None:
            return
        _set_status(self._span, error, message)
        if end_time_ns:
            self._span.end(end_time=end_time_ns)
        else:
            self._span.end()


class RoundTracer:
    """一轮对话的 OTel tracer：开始 root span、派生节点子 span、轮末导出 `SpanData`。"""

    def __init__(
        self,
        *,
        service_name: str,
        max_attribute_length: int = 2048,
        max_spans_per_round: int = 64,
    ) -> None:
        self.service_name = service_name
        self.max_attribute_length = max_attribute_length
        self.max_spans_per_round = max_spans_per_round
        self.trace_id: Optional[str] = None
        self._exporter = _RoundSpanExporter()
        self._provider = None
        self._tracer = None
        self._root = None
        self._closed = False
        self._root_ended = False
        self._root_session_id: Optional[str] = None

    # ---------- 生命周期 ----------
    def _ensure_provider(self) -> bool:
        if self._tracer is not None:
            return True
        try:
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        except ImportError as exc:  # pragma: no cover - OTel 依赖未安装
            logger.warning("opentelemetry-sdk 未安装，span 采集不可用：%s", exc)
            return False
        provider = TracerProvider(
            resource=Resource.create({"service.name": self.service_name})
        )
        provider.add_span_processor(SimpleSpanProcessor(self._exporter))
        self._provider = provider
        self._tracer = provider.get_tracer(self.service_name)
        return True

    def start_round(
        self, *, session_id: str, user_id: str = "", message: str = ""
    ) -> bool:
        """开始 root span（chat_round）；返回是否成功开启。"""
        if not self._ensure_provider():
            return False
        from opentelemetry.trace import SpanKind

        self._root = self._tracer.start_span("chat_round", kind=SpanKind.SERVER)
        self.trace_id = format(self._root.get_span_context().trace_id, "032x")
        self._root.set_attribute("session_id", session_id)
        if user_id:
            self._root.set_attribute("user_id", user_id)
        if message:
            self._root.set_attribute(
                "input.message", _truncate(message, self.max_attribute_length)
            )
        return True

    def start_node(
        self,
        name: str,
        attributes: Optional[Dict[str, Any]] = None,
        *,
        start_time_ns: Optional[int] = None,
    ) -> NodeSpan:
        """派生一个图节点子 span（parent = root）。SDK 不可用时返回空句柄。

        `start_time_ns` 允许用外部时间源（LangGraph 节点事件的 timestamp）作为 span
        开始时间，保证 span 时间与图执行的真实起止一致。
        """
        if self._root is None or not self._ensure_provider():
            return NodeSpan(None, name, self.max_attribute_length)
        from opentelemetry.trace import SpanKind, set_span_in_context

        span = self._tracer.start_span(
            f"node.{name}",
            context=set_span_in_context(self._root),
            kind=SpanKind.INTERNAL,
            start_time=start_time_ns,
        )
        handle = NodeSpan(span, f"node.{name}", self.max_attribute_length)
        if attributes:
            handle.set_attributes(attributes)
        return handle

    def set_root_attribute(self, key: str, value: Any) -> None:
        if self._root is None or value is None:
            return
        sanitized = _sanitize_attributes({key: value}, self.max_attribute_length)
        if key in sanitized:
            self._root.set_attribute(key, sanitized[key])

    def end_round(
        self, *, error: bool = False, message: str = "", attributes: Optional[Dict[str, Any]] = None
    ) -> None:
        if self._root is None:
            return
        if attributes:
            for key, value in _sanitize_attributes(
                attributes, self.max_attribute_length
            ).items():
                self._root.set_attribute(key, value)
        self._root_ended = True
        _set_status(self._root, error, message)
        self._root.end()

    # ---------- 导出 ----------
    def finish(self) -> List[Any]:
        """关闭 provider（flush span）并返回 `SpanData` 列表；不可用时返回空列表。

        返回顺序按 span 开始时间升序（root `chat_round` 在最前），便于前端画瀑布图。
        """
        if self._closed:
            return []
        self._closed = True
        # 未显式 end_round 时兜底收尾，避免 root span 丢失
        if self._root is not None and not self._root_ended:
            self._root_ended = True
            try:
                self._root.end()
            except Exception:  # pragma: no cover - 已结束的 span 忽略
                pass
        try:
            if self._provider is not None:
                self._provider.shutdown()
        except Exception as exc:  # noqa: BLE001 - 导出失败不影响主链路
            logger.warning("span flush 失败：%s", exc)
        spans = self._to_span_data(self._exporter.spans)
        spans.sort(key=lambda s: s.start_time or datetime.min)
        return spans

    def _to_span_data(self, raw_spans: List[Any]) -> List[Any]:
        from dmf_agent.schemas.trace import SpanData

        out: List[Any] = []
        for raw in raw_spans[: self.max_spans_per_round]:
            try:
                ctx = raw.get_span_context()
                start_ns = getattr(raw, "start_time", None)
                end_ns = getattr(raw, "end_time", None)
                duration_ms = None
                if start_ns and end_ns:
                    duration_ms = round((end_ns - start_ns) / 1e6, 2)
                parent = getattr(raw, "parent", None)
                status = getattr(raw, "status", None)
                out.append(
                    SpanData(
                        trace_id=format(ctx.trace_id, "032x"),
                        span_id=format(ctx.span_id, "016x"),
                        parent_span_id=(
                            format(parent.span_id, "016x") if parent is not None else None
                        ),
                        session_id=str(
                            (raw.attributes or {}).get("session_id")
                            or self._root_session_id
                            or ""
                        ),
                        round_id="",  # 由 ChatService 在落库前回填（轮次 id 由消息仓储生成）
                        name=raw.name,
                        kind=getattr(getattr(raw, "kind", None), "name", "INTERNAL"),
                        start_time=_ns_to_datetime(start_ns),
                        end_time=_ns_to_datetime(end_ns),
                        duration_ms=duration_ms,
                        status_code=getattr(
                            getattr(status, "status_code", None), "name", "UNSET"
                        ),
                        status_message=str(getattr(status, "description", "") or ""),
                        attributes=_sanitize_attributes(
                            dict(raw.attributes or {}), self.max_attribute_length
                        ),
                        events=[
                            {
                                "name": getattr(ev, "name", ""),
                                "timestamp": _ns_to_datetime(
                                    getattr(ev, "timestamp", None)
                                ),
                                "attributes": _sanitize_attributes(
                                    dict(getattr(ev, "attributes", None) or {}),
                                    self.max_attribute_length,
                                ),
                            }
                            for ev in (getattr(raw, "events", None) or [])
                        ],
                        service_name=self.service_name,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 单条 span 转换失败不影响其它
                logger.debug("span 转换失败，已跳过：%s", exc)
        return out

    def set_session_id(self, session_id: str) -> None:
        self._root_session_id = session_id


def _set_status(span: Any, error: bool, message: str) -> None:
    from opentelemetry.trace import StatusCode

    try:
        if error:
            span.set_status(StatusCode.ERROR, message or "error")
        else:
            span.set_status(StatusCode.OK, message or "")
    except Exception:  # pragma: no cover - 状态设置失败无碍
        pass


def build_round_tracer(settings: Optional[Settings] = None) -> Optional[RoundTracer]:
    """按配置构建一轮对话的 tracer；关闭或依赖缺失时返回 None（优雅降级）。

    注意：本函数**不做任何网络初始化**，仅构造 provider；真正的 span 导出目标是
    进程内的 `trace_spans` 表（落库），不存在外部 collector 依赖。
    """
    s = settings or get_settings()
    cfg = s.observability
    if not cfg.enabled:
        return None
    return RoundTracer(
        service_name=cfg.service_name,
        max_attribute_length=cfg.max_attribute_length,
        max_spans_per_round=cfg.max_spans_per_round,
    )


def otel_meta(tracer: Optional[RoundTracer], spans: List[Any]) -> Dict[str, Any]:
    """汇总写入 `trace_records.trace["otel"]` 的元信息（前端展示 trace_id / span 数 / 节点时间线）。"""
    if tracer is None:
        return {}
    node_spans = [
        {
            "name": s.name,
            "duration_ms": s.duration_ms,
            "status_code": s.status_code,
        }
        for s in spans
        if s.name != "chat_round"
    ]
    return {
        "trace_id": tracer.trace_id,
        "service_name": tracer.service_name,
        "span_count": len(spans),
        "node_spans": node_spans,
    }
