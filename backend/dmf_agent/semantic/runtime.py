"""运行时语义层装配（混合方案）：按配置选择 YAML 基座或 DB 发布快照。

- ``source=yaml``（默认）：``SemanticLayer(manifest_dir)`` 读 manifest.d 目录或单文件；
- ``source=db``：经 ``SemanticRegistry.resolve_layer()`` 解析「全局链 + 租户 overlay」，
  得到该租户生效的语义层；无任何快照（或反序列化失败、仓储异常）**自动回落 YAML 基座**
  ——取数绝不因发布态缺失而失败。

除语义层本身，``resolve_layer`` 还返回 ``ResolvedLayer`` 元信息（请求源 / 实际生效源 /
是否回落 / 生效版本链 / 加载时间），供管理面「语义层来源」面板展示——
避免出现"以为改的是 YAML、实际读的是 DB 快照"这类静默错配。

与权限正交：按用户/角色的指标白名单 / 行权限 / 列 deny 仍在取数节点
（``agents/metric_agent/nodes/execute_query.py``）按请求实时叠加，与本函数的
"定义来源"选择互不影响。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from dmf_agent.config.settings import Settings
from dmf_agent.semantic.layer import SemanticLayer
from dmf_agent.semantic.overlay import SNAPSHOT_FULL, chain_item, normalize_mode
from dmf_agent.semantic.registry import SemanticRegistry

logger = logging.getLogger(__name__)


@dataclass
class ResolvedLayer:
    """一次语义层装配的结果 + 溯源信息。"""

    layer: SemanticLayer
    requested_source: str  # 配置要求的来源：yaml | db
    effective_source: str  # 实际生效的来源：yaml | db
    fell_back: bool  # 请求 db 但最终用了 YAML
    tenant_id: str = ""
    manifest_path: str = ""
    scope: str = "yaml"  # yaml | global | tenant
    base_version: Optional[int] = None  # 作为基座的 full 快照版本（YAML 基座为 None）
    overlay_versions: List[int] = field(default_factory=list)  # 叠加的 overlay 版本（升序）
    chain: List[Dict[str, Any]] = field(default_factory=list)  # 参与合并的快照（升序）
    loaded_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        """管理面视图（不含层内容本身，避免响应体过重）。"""
        return {
            "requested_source": self.requested_source,
            "effective_source": self.effective_source,
            "fell_back": self.fell_back,
            "tenant_id": self.tenant_id,
            "manifest_path": self.manifest_path,
            "scope": self.scope,
            "base_version": self.base_version,
            "overlay_versions": list(self.overlay_versions),
            "chain": list(self.chain),
            "loaded_at": self.loaded_at.isoformat(timespec="seconds"),
            "models": len(self.layer.manifest.models),
            "metrics": len(self.layer.list_metrics()),
            "dimensions": len(self.layer.list_dimensions()),
        }


def _manifest_path(settings: Settings) -> Optional[str]:
    cfg = getattr(settings, "semantic", None)
    raw = (getattr(cfg, "manifest_dir", "") or "").strip()
    return raw or None


def _chain_item(rec: Any) -> Dict[str, Any]:
    """链上一条快照的展示视图（实现已下沉到 ``semantic/overlay.chain_item``，
    那里同时被 ``registry.effective_chain_signature`` 复用，保证"基座链"与"DB 侧生效链"
    的归一化口径完全一致 —— 两边只要有一处漂移，指纹比对就会退化成永久重建）。"""
    return chain_item(rec)


def _base_version(used: List[Any]) -> Optional[int]:
    for rec in used:
        if normalize_mode(getattr(rec, "mode", SNAPSHOT_FULL)) == SNAPSHOT_FULL:
            return getattr(rec, "version", None)
    return None


async def resolve_layer(
    *,
    settings: Settings,
    tenant_id: str = "",
    registry: Optional[SemanticRegistry] = None,
    base_layer: Optional[SemanticLayer] = None,
) -> ResolvedLayer:
    """按配置装配运行时语义层，并给出溯源信息。

    ``base_layer`` 可复用已构造好的 YAML 基座（避免重复读文件）。
    """
    cfg = getattr(settings, "semantic", None)
    requested = (getattr(cfg, "source", "yaml") or "yaml").strip().lower()
    manifest_path = _manifest_path(settings)
    base = base_layer if base_layer is not None else SemanticLayer(manifest_path)
    path_str = str(base.manifest.path)

    if requested == "db" and registry is not None:
        # 只把**仓储读取**包进 try：装配期的代码缺陷必须暴露，不能被静默降级掩盖
        layer: Optional[SemanticLayer] = None
        meta: Dict[str, Any] = {}
        try:
            layer, meta = await registry.resolve_layer(
                tenant_id=tenant_id, base_layer=base
            )
        except Exception as exc:  # noqa: BLE001 - 仓储故障不阻断启动
            logger.warning("语义层 DB 快照读取失败，回落 YAML 基座：%s", exc)
            layer, meta = None, {}

        used: List[Any] = list(meta.get("used") or [])
        if layer is not None and used:
            resolved = ResolvedLayer(
                layer=layer,
                requested_source=requested,
                effective_source="db",
                fell_back=False,
                tenant_id=tenant_id or "",
                manifest_path=path_str,
                scope=meta.get("scope", ""),
                base_version=_base_version(used),
                overlay_versions=[
                    r.version
                    for r in used
                    if normalize_mode(getattr(r, "mode", SNAPSHOT_FULL)) != SNAPSHOT_FULL
                ],
                chain=[_chain_item(r) for r in used],
            )
            logger.info(
                "语义层来源：DB 快照（tenant=%r scope=%s 基座 v%s + overlay %s）→ 指标 %d 个",
                tenant_id or "(全局)",
                resolved.scope,
                resolved.base_version,
                resolved.overlay_versions or "无",
                len(layer.list_metrics()),
            )
            return resolved
        logger.info("语义层 source=db 但无 published 快照，回落 YAML 基座")

    return ResolvedLayer(
        layer=base,
        requested_source=requested,
        effective_source="yaml",
        fell_back=requested == "db",
        tenant_id=tenant_id or "",
        manifest_path=path_str,
        scope="yaml",
    )


async def build_layer(
    *,
    settings: Settings,
    tenant_id: str = "",
    registry: Optional[SemanticRegistry] = None,
) -> SemanticLayer:
    """按配置装配运行时语义层（``resolve_layer`` 的薄包装，保留旧签名）。"""
    resolved = await resolve_layer(
        settings=settings, tenant_id=tenant_id, registry=registry
    )
    return resolved.layer
