"""指标树归因（P3，doc/归因分析优化方案.md §7.2）——沿树自顶向下分解，纯 Python。

前提：语义清单声明了 metrics_tree（父子聚合关系），如：

    metrics_tree:
      - node: GMV
        children:
          - 订单数
          - 客单价

语义（保守、确定）：树表达的是**分解关系**——父指标的变化由各子指标的变化构成，
子指标贡献 = 子指标自身期间变化量；父节点的「未解释部分」（父变化 - Σ子变化，
可能来自口径差/其它未列子指标）显式输出为 residual，不摊派。

无 LLM 依赖；树缺失 / 子指标不在取数结果中时逐项跳过（在 warnings 说明），
绝不编造数值。树数据缺失时该能力整体静默跳过（与降级矩阵一致）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def tree_edges(manifest_tree: List[Dict[str, Any]], root: str) -> List[Dict[str, Any]]:
    """取以 root 为父节点的直接子边（单层分解；深层递归留待树数据成熟后扩展）。"""
    return [e for e in (manifest_tree or []) if e.get("parent") == root]


def tree_attribution(
    manifest_tree: List[Dict[str, Any]],
    root: str,
    period_change_by_metric: Dict[str, Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """对 root 指标做单层树分解。

    - period_change_by_metric：指标名 -> {change_value, metrics_value,
      compare_metrics_value}（根与各子指标的期间对比结果；缺失的子指标跳过）；
    - 输出 {root, root_change, children: [{child, change, share, available}],
      residual}：
      - share = 子变化 / Σ可用子变化（子指标间相对贡献，分母为可用子集，避免
        被缺失子指标稀释）；
      - residual = 根变化 - Σ可用子变化（父口径与子口径的差，如实输出不摊派）；
    - 根指标无变化数据 / 无可用子指标时返回 None。
    """
    root_change_info = period_change_by_metric.get(root)
    if not root_change_info:
        return None
    edges = tree_edges(manifest_tree, root)
    if not edges:
        return None

    children: List[Dict[str, Any]] = []
    for edge in edges:
        child = str(edge.get("child") or "")
        info = period_change_by_metric.get(child)
        if info is None:
            children.append(
                {"child": child, "available": False, "change": None, "share": None}
            )
            continue
        children.append(
            {
                "child": child,
                "available": True,
                "change": round(float(info.get("change_value") or 0.0), 4),
                "share": None,  # 占比在收集完可用子指标后归一
                "metrics_value": info.get("metrics_value"),
                "compare_metrics_value": info.get("compare_metrics_value"),
            }
        )
    available = [c for c in children if c["available"]]
    if not available:
        return None
    total_child_change = sum(c["change"] for c in available)
    denom = sum(abs(c["change"]) for c in available)
    for c in available:
        c["share"] = (
            round(abs(c["change"]) / denom, 4) if denom else 0.0
        )
    root_change = round(float(root_change_info.get("change_value") or 0.0), 4)
    return {
        "root": root,
        "root_change": root_change,
        "children": children,
        "residual": round(root_change - total_child_change, 4),
    }


def summarize_tree(result: Dict[str, Any]) -> str:
    """指标树归因的确定性文本（无 LLM 模板用；仅描述真实计算）。"""
    children = [c for c in (result.get("children") or []) if c.get("available")]
    if not children:
        return ""
    parts = [
        f"{c['child']} 变化 {c['change']}（相对贡献 {c['share']}）" for c in children
    ]
    missing = [
        c["child"] for c in (result.get("children") or []) if not c.get("available")
    ]
    text = f"{result.get('root')} 树分解：" + "；".join(parts)
    if result.get("residual"):
        text += f"；未解释残差 {result['residual']}"
    if missing:
        text += f"（子指标缺失未计入：{'、'.join(missing)}）"
    return text
