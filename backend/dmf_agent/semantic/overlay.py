"""语义层局部覆盖（overlay）与快照链解析（混合方案 · 局部租户覆盖）。

背景：``semantic_published`` 的一条快照有两种模式

- ``full``    —— 整份已合并 manifest（``SemanticManifest.to_dict()`` 的结果）；
- ``overlay`` —— 只含**被改动**的 ``semantic_models`` / ``metrics`` 条目。

设计取舍（**按 name 整条替换，不做字段级 merge**）
------------------------------------------------
字段级 merge 会产生"半新半旧"的口径：例如只改了指标的 ``measure`` 却没同步
``filters`` / ``description``，或只改了模型的 ``columns`` 却没同步它下面的
``measures``。更关键的是 model 与 metric 之间存在引用关系（metric 经 measure
指向 model 的列），局部字段替换既难解释也容易破坏引用完整性。

整条替换的语义则非常清晰：**要么这条指标/模型是你租户自己定义的，要么就用基座里的**。
合并结果随后仍会走一遍 ``validate() + reference_problems()``，跨条目引用问题
（如 overlay 新增的 measure 与基座重名）会在那里被暴露成 ``ManifestError``。

快照链解析
----------
运行时只消费 ``state="published"`` 的快照。从最新往旧遍历：

1. 最新的若是 ``full`` —— 直接成为基座，链上不再叠加更旧的 overlay；
2. 最新的若是 ``overlay`` —— 继续往旧找**最近的一个 full** 作为基座，
   途中的所有 overlay 按**版本升序**依次叠加；
3. 一路到底没有 full —— 以调用方传入的基座（全局链解出的层 / YAML 基座）起算。

于是"全局发布一份 full + 各租户只发自己的差异 overlay"成为自然用法，
而某个租户想完全脱离全局时，直接发一份 ``full`` 即可整体取代。
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

SNAPSHOT_FULL = "full"
SNAPSHOT_OVERLAY = "overlay"
SNAPSHOT_MODES: Tuple[str, ...] = (SNAPSHOT_FULL, SNAPSHOT_OVERLAY)

# manifest 里以 "name" 为身份的条目列表（其余标量键由基座持有，overlay 不覆盖）
_LIST_KEYS: Tuple[str, ...] = ("semantic_models", "metrics")

_REPORT_KEYS: Dict[str, Tuple[str, str]] = {
    "semantic_models": ("models_added", "models_replaced"),
    "metrics": ("metrics_added", "metrics_replaced"),
}


def normalize_mode(mode: Optional[str]) -> str:
    """归一化并校验快照模式；None/空串视为 ``full``。"""
    value = (mode or SNAPSHOT_FULL).strip().lower()
    if value not in SNAPSHOT_MODES:
        raise ValueError(f"非法快照模式：{mode!r}（允许 {list(SNAPSHOT_MODES)}）")
    return value


def _index_by_name(items: Sequence[Any]) -> Dict[str, int]:
    """条目列表 → {name: 下标}（无名条目跳过，交由后续校验报错）。"""
    out: Dict[str, int] = {}
    for idx, item in enumerate(items):
        if isinstance(item, dict) and item.get("name"):
            out[item["name"]] = idx
    return out


def normalize_overlay_payload(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """清洗 overlay payload：只保留 ``semantic_models`` / ``metrics`` 两个差异列表。

    目的是让 overlay **永远不会**悄悄携带一份完整定义集——传进来的标量键
    （``version`` 等）与其它列表键一律丢弃，避免"以为只改了 3 个指标、
    实际把整份模型也覆盖了"。空 overlay 视为非法（无意义且易掩盖误操作）。
    """
    out: Dict[str, Any] = {}
    for key in _LIST_KEYS:
        entries = (payload or {}).get(key) or []
        if not entries:
            continue
        for item in entries:
            if not isinstance(item, dict) or not item.get("name"):
                raise ValueError(f"overlay 的 {key} 条目缺少 name：{item!r}")
        out[key] = [copy.deepcopy(item) for item in entries]
    if not out:
        raise ValueError("overlay 快照不含任何条目（semantic_models / metrics 均为空）")
    return out


def summarize_overlay(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """给管理面用的 overlay 摘要：条目名清单 + 计数。"""
    models = [i.get("name") for i in ((payload or {}).get("semantic_models") or []) if isinstance(i, dict)]
    metrics = [i.get("name") for i in ((payload or {}).get("metrics") or []) if isinstance(i, dict)]
    return {
        "model_count": len(models),
        "metric_count": len(metrics),
        "model_names": models,
        "metric_names": metrics,
    }


def chain_item(rec: Any) -> Dict[str, Any]:
    """链上一条快照的管理面视图（``ResolvedLayer.chain`` 的元素即此形状）。

    版本号是**租户内独立序列**，故必须带上 tenant_id 与 scope，
    否则「全局 v1 + 租户 v1」看起来会像同一个东西。
    """
    tenant_id = getattr(rec, "tenant_id", "") or ""
    return {
        "id": getattr(rec, "id", None),
        "tenant_id": tenant_id,
        "scope": "tenant" if tenant_id else "global",
        "version": getattr(rec, "version", None),
        "state": getattr(rec, "state", "") or "",
        "mode": normalize_mode(getattr(rec, "mode", SNAPSHOT_FULL)),
        "created_by": getattr(rec, "created_by", "") or "",
    }


def chain_signature_of(chain: Optional[Sequence[Any]]) -> Tuple[Any, ...]:
    """把一条链（``chain_item`` 列表 / ``ResolvedLayer.chain``）归一成可等值比较的指纹。

    之所以放在 overlay 模块：``registry`` 与 ``runtime`` 都需要它，而 ``runtime`` 反过来
    导入 ``registry`` —— 放在这两者任一侧都会绕成循环导入。overlay 是链语义的归属模块，
    且不依赖任何上层。

    只取身份与状态（id/租户/版本/状态/模式），**不读 payload**：比指纹不该付反序列化的钱。
    """
    return tuple(
        sorted(
            (
                c.get("tenant_id", "") or "",
                c.get("id") or 0,
                c.get("version") or 0,
                c.get("state", "") or "",
                c.get("mode", "") or "",
            )
            for c in (chain or [])
        )
    )


def apply_overlay(
    base: Optional[Dict[str, Any]], overlay: Optional[Dict[str, Any]]
) -> Tuple[Dict[str, Any], Dict[str, List[str]]]:
    """把 overlay 叠加到 base 上，返回 ``(merged, report)``。

    - 按 ``name`` 命中 → **整条替换**（记入 ``*_replaced``）；
    - 未命中 → 追加（记入 ``*_added``）；
    - 不修改入参（深拷贝），标量键（如 ``version``）一律沿用 base。
    """
    merged: Dict[str, Any] = copy.deepcopy(base) if base else {}
    report: Dict[str, List[str]] = {
        "models_added": [],
        "models_replaced": [],
        "metrics_added": [],
        "metrics_replaced": [],
    }

    for key in _LIST_KEYS:
        entries = (overlay or {}).get(key) or []
        target = merged.setdefault(key, [])
        if not entries:
            continue
        positions = _index_by_name(target)
        added_key, replaced_key = _REPORT_KEYS[key]
        for item in entries:
            if not isinstance(item, dict) or not item.get("name"):
                raise ValueError(f"overlay 的 {key} 条目缺少 name：{item!r}")
            name = item["name"]
            if name in positions:
                target[positions[name]] = copy.deepcopy(item)
                report[replaced_key].append(name)
            else:
                positions[name] = len(target)
                target.append(copy.deepcopy(item))
                report[added_key].append(name)

    return merged, report


def resolve_chain(
    rows: Sequence[Any],
    base_raw: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], List[Any]]:
    """解析同一租户的快照链，返回 ``(raw, used)``。

    - ``rows``：按 ``version`` **降序**排列的快照序列（仓储 ``list_for`` 的顺序）；
    - ``base_raw``：链上找不到 ``full`` 时的兜底基座（全局链结果 / YAML 基座）；
    - ``used``：实际参与合并的快照，**按版本升序**（含作为基座的那条 full）。

    没有任何 ``published`` 快照时返回 ``(base_raw, [])``。
    """
    published = [r for r in rows if getattr(r, "state", "") == "published"]
    if not published:
        return base_raw, []

    newest = published[0]
    if normalize_mode(getattr(newest, "mode", SNAPSHOT_FULL)) == SNAPSHOT_FULL:
        return dict(newest.payload or {}), [newest]

    overlays: List[Any] = []
    base_snapshot: Optional[Any] = None
    for row in published:
        if normalize_mode(getattr(row, "mode", SNAPSHOT_FULL)) == SNAPSHOT_OVERLAY:
            overlays.append(row)
            continue
        base_snapshot = row
        break

    raw: Optional[Dict[str, Any]] = (
        dict(base_snapshot.payload or {}) if base_snapshot is not None else (base_raw or None)
    )
    # overlays 收集顺序为版本降序 → 反转成升序后依次叠加
    overlays.reverse()
    for row in overlays:
        if raw is None:
            # 没有基座可叠（既无 full 也无兜底）——把该 overlay 自身当作起点
            raw = {}
        raw, report = apply_overlay(raw, row.payload or {})
        logger.debug(
            "overlay v%s 已叠加：%s",
            getattr(row, "version", "?"),
            {k: v for k, v in report.items() if v},
        )

    used: List[Any] = list(overlays)
    if base_snapshot is not None:
        used = [base_snapshot, *overlays]
    return raw, used
