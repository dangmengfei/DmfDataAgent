"""因子归因（P3，doc/归因分析优化方案.md §7.1）——逐因子替换法，纯 Python。

前提：语义层 metric 清单声明了 factors 公式，如：

    - name: GMV
      type: simple
      type_params: { measure: gmv_amount }
      factors:
        expr: "uv * cvr * arpu"
        factors: { uv: 访客数, cvr: 转化率, arpu: 客单价 }

口径说明（重要）：
- factors 引用的是**其它指标名/measure 名**（如 访客数），各因子按自身口径独立取数；
- 逐因子替换法：以**上期各因子值为基线**，按声明顺序逐个替换为本期值，
  每步的差值即该因子的贡献；轮换到最后一步后仍有残差（交互项），
  归入 interaction 并显式输出（不摊进任何因子）；
- 变化恒等式：sum(各因子贡献) + interaction = 指标本期总量 - 上期总量。

无 LLM 依赖；数据不足 / 因子取数失败时返回 ok=false 的结构（调用方降级跳过，
绝不编造因子数值）。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# expr 允许的字符：因子键（中文/字母/数字/下划线）、*、(、)、空格
_EXPR_ALLOWED = re.compile(r"^[\w一-鿿*()\s]+$")


def parse_factor_expr(expr: str, factor_names: Dict[str, str]) -> Optional[List[str]]:
    """解析因子算式为**因子键的有序列表**（逐因子替换的执行顺序）。

    仅支持乘法算式（因子键 + * ( )）；出现未声明因子键、除法/加减等不支持
    的运算时返回 None（调用方跳过因子归因并如实说明）。
    """
    expr = str(expr or "").strip()
    if not expr or not _EXPR_ALLOWED.match(expr):
        return None
    tokens = re.findall(r"[\w一-鿿]+|\*|\(|\)", expr)
    ordered: List[str] = []
    seen: set = set()
    for tok in tokens:
        if tok in ("*", "(", ")"):
            continue
        if tok not in factor_names:
            return None  # 未声明因子
        if tok not in seen:
            seen.add(tok)
            ordered.append(tok)
    return ordered or None


def factor_contribution(
    baseline: Dict[str, float],
    current: Dict[str, float],
    ordered_keys: List[str],
) -> Dict[str, Any]:
    """逐因子替换法计算各因子贡献（纯算术，可单测）。

    - baseline/current：因子键 -> 因子值（上期 / 本期，同口径聚合）；
    - 替换顺序 = ordered_keys（声明顺序）；每步把一个因子从基线值换成本期值，
      计算替换前后的总量差；
    - interaction = 总变化 - Σ各因子贡献（顺序尾差，显式输出不摊派）。

    返回 {factors: [{factor, ref, baseline, current, contribution, rate}],
    interaction, total_change, baseline_total, current_total}。
    """
    def _total(vals: Dict[str, float]) -> float:
        """按算式语义计算总量：纯乘法算式 = 各因子连乘。

        这里与 expr 的连乘语义一致（parse_factor_expr 只放行乘法）。
        """
        product = 1.0
        for k in ordered_keys:
            product *= vals.get(k, 0.0)
        return product

    base_total = _total(baseline)
    curr_total = _total(current)
    working = dict(baseline)
    items: List[Dict[str, Any]] = []
    for key in ordered_keys:
        old = working.get(key, 0.0)
        new = current.get(key, 0.0)
        if old == new:
            contribution = 0.0
        else:
            before = _total(working)
            working[key] = new
            after = _total(working)
            contribution = after - before
        items.append(
            {
                "factor": key,
                "baseline": round(old, 4),
                "current": round(new, 4),
                "contribution": round(contribution, 4),
                "rate": round(contribution / base_total, 4) if base_total else None,
            }
        )
    total_change = curr_total - base_total
    interaction = round(total_change - sum(i["contribution"] for i in items), 4)
    return {
        "factors": items,
        "interaction": interaction,
        "total_change": round(total_change, 4),
        "baseline_total": round(base_total, 4),
        "current_total": round(curr_total, 4),
    }


def summarize_factors(
    metric_name: str, result: Dict[str, Any]
) -> str:
    """因子归因的确定性文本（无 LLM 时模板答复用；仅描述真实计算结果）。"""
    factors = result.get("factors") or []
    if not factors:
        return ""
    parts = []
    for f in factors:
        sign = "+" if (f["contribution"] or 0) >= 0 else ""
        parts.append(
            f"{f['factor']}（{f['baseline']}→{f['current']}）贡献 {sign}{f['contribution']}"
        )
    text = (
        f"{metric_name} 因子拆解（逐因子替换法，基线=上期）："
        + "；".join(parts)
    )
    if result.get("interaction"):
        text += f"；因子交互（未摊派）{result['interaction']}"
    text += f"。合计变化 {result.get('total_change', 0)}。"
    return text
