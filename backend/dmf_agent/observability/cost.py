"""LLM 成本计价与预算告警（PRD FR11 可观测补齐）。

设计要点（与项目既有"确定性优先、失败降级"风格一致）：

1. **纯估算、不外调**：只依据 LLM 响应里的 token usage 与本地单价表算钱，
   不对接任何厂商账单接口；未启用 / 无 token / 未知模型都返回 0 而不是抛错。
2. **单价可配**：`DMF_COST-PRICE_TABLE` 为 JSON（模型 -> {input, output}），
   单位统一为「每 100 万 token 的金额」，币种 `currency`（默认 CNY）。
3. **模型名匹配**：精确匹配 -> 最长前缀匹配（`deepseek-chat-0506` 命中
   `deepseek-chat`）-> 兜底价。前缀匹配让带日期后缀的模型名也能计价。
4. **预算告警**：日 / 月累计成本达 `alert_ratio`（默认 80%）即告警，
   超 100% 为 `exceeded`；预算上限为 0 表示不限制，永远不告警。

落库字段见 `trace_records.cost` / `.model`（迁移 0021）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# 单价表的单位：每 1_000_000 token
_PER_MILLION = 1_000_000.0


def resolve_price(model: str, price_table: Optional[Dict[str, Dict[str, float]]]) -> tuple[float, float, str]:
    """查模型单价；返回 (input_price, output_price, 命中来源)。

    命中来源 `exact` / `prefix:<key>` / `default`，便于看板解释"这钱怎么算的"。
    """
    table = price_table or {}
    key = (model or "").strip()
    if not key:
        return 0.0, 0.0, "default"

    row = table.get(key)
    if isinstance(row, dict):
        return _as_float(row.get("input"), 0.0), _as_float(row.get("output"), 0.0), "exact"

    # 前缀匹配：取最长命中键（模型名常带版本号 / 日期后缀）
    best_key = ""
    for name in table:
        if key.startswith(name) and len(name) > len(best_key):
            best_key = name
    if best_key:
        row = table[best_key]
        if isinstance(row, dict):
            return (
                _as_float(row.get("input"), 0.0),
                _as_float(row.get("output"), 0.0),
                f"prefix:{best_key}",
            )
    return 0.0, 0.0, "default"


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def estimate_cost(
    model: str,
    prompt_tokens: Optional[int],
    completion_tokens: Optional[int],
    *,
    settings: Any = None,
) -> Dict[str, Any]:
    """按模型单价估算一轮对话的 LLM 成本。

    返回 `{"cost", "currency", "model", "price_source", "pricing_enabled"}`。
    任何异常输入（None / 负数 / 未启用）都返回 cost=0，不抛错——成本是观测指标，
    绝不能反过来把主链路搞挂。
    """
    cost_settings = _cost_settings(settings)
    currency = str(getattr(cost_settings, "currency", "CNY") or "CNY")
    enabled = bool(getattr(cost_settings, "enabled", True))
    name = (model or "").strip()
    p_in = max(0, _as_int(prompt_tokens))
    p_out = max(0, _as_int(completion_tokens))

    if not enabled or (p_in + p_out) == 0:
        return {
            "cost": 0.0,
            "currency": currency,
            "model": name,
            "price_source": "none",
            "pricing_enabled": enabled,
        }

    in_price, out_price, source = resolve_price(name, getattr(cost_settings, "price_table", None))
    if source == "default":
        in_price = _as_float(getattr(cost_settings, "default_input_price", 0.0))
        out_price = _as_float(getattr(cost_settings, "default_output_price", 0.0))

    cost = (p_in / _PER_MILLION) * in_price + (p_out / _PER_MILLION) * out_price
    return {
        "cost": round(cost, 6),
        "currency": currency,
        "model": name,
        "price_source": source,
        "pricing_enabled": True,
    }


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _cost_settings(settings: Any) -> Any:
    """取 cost 配置；未装配 settings 时回落到默认 CostSettings（不引入硬依赖）。"""
    if settings is not None and getattr(settings, "cost", None) is not None:
        return settings.cost
    try:
        from dmf_agent.config.settings import CostSettings

        return CostSettings()
    except Exception:  # noqa: BLE001 - 极端情况下（配置模块不可用）走极简兜底
        return _FALLBACK


class _Fallback:
    enabled = True
    currency = "CNY"
    price_table: Dict[str, Dict[str, float]] = {}
    default_input_price = 0.0
    default_output_price = 0.0
    daily_budget = 0.0
    monthly_budget = 0.0
    alert_ratio = 0.8


_FALLBACK = _Fallback()


def budget_alerts(
    *,
    daily_cost: float,
    monthly_cost: float = 0.0,
    settings: Any = None,
) -> List[Dict[str, Any]]:
    """按日 / 月预算生成告警列表（空列表 = 无告警）。

    单项结构：`{"period": "daily"|"monthly", "level": "warn"|"exceeded",
    "limit", "used", "ratio", "message"}`。预算为 0（不限制）时不产出告警。
    """
    cs = _cost_settings(settings)
    ratio_threshold = _as_float(getattr(cs, "alert_ratio", 0.8), 0.8)
    out: List[Dict[str, Any]] = []
    for period, limit, used in (
        ("daily", _as_float(getattr(cs, "daily_budget", 0.0)), _as_float(daily_cost)),
        ("monthly", _as_float(getattr(cs, "monthly_budget", 0.0)), _as_float(monthly_cost)),
    ):
        if limit <= 0:
            continue
        ratio = used / limit if limit else 0.0
        if ratio >= 1.0:
            level = "exceeded"
        elif ratio >= ratio_threshold:
            level = "warn"
        else:
            continue
        label = "今日" if period == "daily" else "本月"
        out.append(
            {
                "period": period,
                "level": level,
                "limit": round(limit, 4),
                "used": round(used, 4),
                "ratio": round(ratio, 4),
                "message": (
                    f"{label}成本已达预算 {round(ratio * 100, 1)}%"
                    f"（{round(used, 4)} / {round(limit, 4)}）"
                    if level == "warn"
                    else f"{label}成本已超预算（{round(used, 4)} / {round(limit, 4)}）"
                ),
            }
        )
    return out
