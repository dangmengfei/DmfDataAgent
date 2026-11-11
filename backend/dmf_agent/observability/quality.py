"""回答质量指标：准确率（有据回答率）、采纳率、降级率、幻觉风险（PRD FR11）。

**为什么是"有据回答率"而不是 LLM-as-judge**：
PRD 要求准确率指标，但在线对每轮答案再做一次 judge 调用会翻倍成本与延迟。
本项目主链路是"先算后说"——答案由确定性取数结果驱动，因此这里用**可观测的
确定性信号**作为准确率代理：

- `grounded`（有据）：无错误、未降级 mock、且（取数类请求确实取到了数据 /
  非取数类请求产出了答复）。这是"答案有真实依据"的必要条件。
- `hallucination_risk`（幻觉风险）：mock 模式下仍生成了答复（无真实数据支撑），
  或本轮出错却依然吐了答案。PRD 幻觉率目标 0，本指标即其在线哨兵。

采纳率直接取用户反馈：点赞 / (点赞 + 点踩)（`few_shot_examples.source=like`
与 `bad_cases`），无需额外埋点。
"""

from __future__ import annotations

from typing import Any, Dict, List

from dmf_agent.schemas.trace import TraceData

# 不要求取数的意图（有答复即算"有据"）
_NON_QUERY_INTENTS = {"help", "exit", "chitchat", "chat", "greeting", "create", "unknown"}


def _rows_of(result: Any) -> List[Any]:
    if isinstance(result, dict):
        rows = result.get("rows")
        return rows if isinstance(rows, list) else []
    return []


def round_signals(r: TraceData) -> Dict[str, Any]:
    """从一条轮级 trace 提取质量信号（确定性，不调 LLM）。"""
    trace = r.trace or {}
    qr = trace.get("query_result")
    result_present = isinstance(qr, dict) and bool(qr.get("columns") or qr)
    rows = _rows_of(qr)
    has_rows = len(rows) > 0
    query_error = bool(isinstance(qr, dict) and qr.get("error"))
    error = bool(r.has_error or query_error or trace.get("error"))
    mock = bool(r.mock_mode)
    clarified = bool(trace.get("confirmation"))
    answer = str(trace.get("final_answer") or "").strip()
    answered = bool(answer)

    intent = r.intent or ""
    needs_data = intent not in _NON_QUERY_INTENTS and bool(trace.get("dsl") or qr)
    empty = bool(result_present and not has_rows and not query_error)

    # 有据：没出错、没降级，且（需要取数的确实取到了数据 / 不需要取数的给出了答复）
    if needs_data:
        grounded = (not error) and (not mock) and has_rows
    else:
        grounded = (not error) and (not mock) and answered

    # 幻觉风险：无真实数据支撑却仍在作答
    hallucination_risk = bool(mock and answered) or bool(error and answered)

    return {
        "round_id": r.round_id,
        "intent": intent or "unknown",
        "error": error,
        "mock": mock,
        "empty": empty,
        "clarified": clarified,
        "answered": answered,
        "grounded": grounded,
        "hallucination_risk": hallucination_risk,
        "cost": float(getattr(r, "cost", 0.0) or 0.0),
        "total_tokens": int(r.total_tokens or 0),
    }


def _rate(part: int, total: int) -> float:
    return round(part / total * 100, 1) if total else 0.0


def _bucket(signals: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(signals)
    if not total:
        return {
            "total": 0,
            "grounded": 0,
            "grounded_rate": 0.0,
            "error_rate": 0.0,
            "mock_rate": 0.0,
            "empty_rate": 0.0,
            "clarify_rate": 0.0,
            "hallucination_risk_rate": 0.0,
        }
    grounded = sum(1 for s in signals if s["grounded"])
    return {
        "total": total,
        "grounded": grounded,
        "grounded_rate": _rate(grounded, total),
        "error_rate": _rate(sum(1 for s in signals if s["error"]), total),
        "mock_rate": _rate(sum(1 for s in signals if s["mock"]), total),
        "empty_rate": _rate(sum(1 for s in signals if s["empty"]), total),
        "clarify_rate": _rate(sum(1 for s in signals if s["clarified"]), total),
        "hallucination_risk_rate": _rate(
            sum(1 for s in signals if s["hallucination_risk"]), total
        ),
    }


def aggregate_quality(
    records: List[TraceData],
    *,
    likes: int = 0,
    dislikes: int = 0,
) -> Dict[str, Any]:
    """汇总质量看板指标（内存 / MySQL 共用同一份 Python 聚合，保证口径一致）。"""
    signals = [round_signals(r) for r in records]
    overall = _bucket(signals)

    by_intent: Dict[str, List[Dict[str, Any]]] = {}
    for s in signals:
        by_intent.setdefault(s["intent"], []).append(s)
    intents = []
    for name, items in sorted(by_intent.items(), key=lambda kv: -len(kv[1])):
        b = _bucket(items)
        b["intent"] = name
        intents.append(b)

    feedback_total = int(likes) + int(dislikes)
    return {
        "total": overall["total"],
        # 准确率代理：有据回答率（答案由真实取数 / 确定性结果支撑的比例）
        "accuracy": {
            "grounded": overall["grounded"],
            "grounded_rate": overall["grounded_rate"],
            "note": "有据回答率（无错误·未降级·确有数据支撑），作为准确率的在线代理指标",
        },
        # 采纳率：用户点赞 / 全部反馈
        "acceptance": {
            "likes": int(likes),
            "dislikes": int(dislikes),
            "feedback_total": feedback_total,
            "rate": _rate(int(likes), feedback_total),
        },
        "error_rate": overall["error_rate"],
        "mock_rate": overall["mock_rate"],
        "empty_rate": overall["empty_rate"],
        "clarify_rate": overall["clarify_rate"],
        "hallucination_risk_rate": overall["hallucination_risk_rate"],
        "by_intent": intents,
    }
