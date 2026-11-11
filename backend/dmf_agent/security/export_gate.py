"""PRD FR13 导出治理：导出审批闸门 + 大结果集截断 + 下载水印 + 审计留痕。

三个导出端点（`/obs/export`、`/slang/export`、`/fewshots/export`）共用本模块，
避免各自手写一份「截断 / 水印 / 审计」而口径漂移。放在 `security/` 下是因为它本质是
合规治理的一部分（与 `policy_loader` / `auditor` 同层），而不是某个业务域的能力。

## 设计取舍

- **审批落到权限点，而非独立审批工作流**：与既有 FR13 授权体系（`PERM_*` +
  `require_permission`）同构，不引入新表 / 新状态机。`export_require_approval=True` 时
  导出额外要求 `PERM_EXPORT_APPROVE`（admin 的 `*` 通配放行）；默认关闭，行为与改造前
  **完全一致**（向后兼容）。这样"谁能导出"由 admin 走既有角色管理面授予即可，
  不新增一套需要长期维护的审批单据流。
- **水印要写进导出件本身**：`ElWatermark`（前端）只能给"屏幕上的容器"叠字，管不到落盘
  文件；真正随文件流出的水印必须写进 payload。所以这里在返回值里注入 `watermark` 块，
  前端预览另用 `ElWatermark` 叠加一层屏幕水印（双保险）。
- **审计 best-effort**：审计写入失败只告警，不影响导出结果（与 FR11 / FR13 其余审计一致）。

## 权限口径（已统一，勿再各写一份）

判定一律走 `roles.permissions_grant(effective_permissions(role, permissions), wanted)`：
**身份携带的 `permissions` 优先**（由 `get_current_user` 经 `role_permissions_from` 读
roles 表，DB 异常回落静态定义），permissions 缺失时才按角色名回落 `roles._ROLES`。
端点守卫 `require_permission`、本闸门、前端 `hasPerm` 三处同口径，自定义角色才真正生效。

历史坑（已修）：`require_permission` 曾只读 `_ROLES` 静态字典、不查表，于是"角色管理面
里明明勾了权限，接口仍 403"。当时的表现是 `export_require_approval=true` 退化成
「仅 admin 可导出」——因为自定义角色在到达本闸门前就被端点守卫拦掉了。

注意：角色管理面**拒改内置角色**权限（409「内置角色的权限不可修改」），所以想给
analyst/viewer 放行导出，仍需改 `roles.py` 的 `_ROLES` 静态定义或新建自定义角色。

## 失败语义

- 审批未通过 → 抛 `HTTPException(403)`，带可读原因（前端据此提示"需导出审批"）。
- 运行时不带 security 层（如 `PERMISSION_MODE=off` 的轻量部署）→ 闸门与审计整体跳过，
  只保留截断 + 水印，保证"导出可用性"不被治理组件缺失拖累。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import HTTPException

from dmf_agent.auth.roles import PERM_EXPORT_APPROVE, effective_permissions, permissions_grant
from dmf_agent.schemas.auth import CurrentUser

logger = logging.getLogger(__name__)


def export_policy(runtime: Any) -> Dict[str, Any]:
    """读取导出治理配置（缺省安全：默认不强制审批、5000 行、开水印）。"""
    sec = getattr(getattr(runtime, "settings", None), "security", None)
    return {
        "require_approval": bool(getattr(sec, "export_require_approval", False)),
        "max_rows": int(getattr(sec, "export_max_rows", 5000) or 5000),
        "watermark": bool(getattr(sec, "export_watermark_enabled", True)),
    }


def assert_export_allowed(runtime: Any, current: CurrentUser, *, kind: str) -> None:
    """导出审批闸门：开启且调用者不具备 `export:approve` 时 403。

    `kind` 只用于提示文案（obs / slang / fewshot），便于前端直接展示。

    判定复用 `roles.permissions_grant`——与端点守卫 `require_permission`、前端 `hasPerm`
    **同一口径**（身份携带的 permissions 优先、静态角色定义兜底），避免这里再长一份
    "`*` 或 role==admin" 的私有判据而与之漂移。
    """
    policy = export_policy(runtime)
    if not policy["require_approval"]:
        return
    perms = effective_permissions(
        getattr(current, "role", ""), getattr(current, "permissions", None)
    )
    if permissions_grant(perms, [PERM_EXPORT_APPROVE]):
        return
    raise HTTPException(
        status_code=403,
        detail=(
            f"导出「{kind}」需要审批：当前已启用导出审批闸门"
            f"（security.export_require_approval=true），"
            f"请让管理员为你的角色授予 `{PERM_EXPORT_APPROVE}` 权限点。"
        ),
    )


def cap_rows(rows: Any, *, runtime: Any) -> tuple[list, bool]:
    """把序列截断到 `export_max_rows`；返回 `(截断后的列表, 是否发生截断)`。

    `rows` 允许是任何可切片序列（导出前通常是 list）。超限只截断、不报错 ——
    导出场景"给到上限 + 明确标注" 比直接失败更有用。
    """
    seq = list(rows or [])
    cap = export_policy(runtime)["max_rows"]
    if cap <= 0 or len(seq) <= cap:
        return seq, False
    return seq[:cap], True


def build_watermark(
    runtime: Any,
    current: Optional[CurrentUser],
    *,
    kind: str,
    rows: int,
    truncated: bool,
) -> Dict[str, Any]:
    """构造随导出件一起流出的水印块（导出人 / 租户 / 时间 / 行数 / 截断）。"""
    return {
        "kind": kind,
        "exported_by": (getattr(current, "username", "") or getattr(current, "id", "") or "anonymous"),
        "user_id": getattr(current, "id", "") or "",
        "tenant_id": getattr(current, "tenant_id", "") or "",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "rows": rows,
        "truncated": bool(truncated),
        "max_rows": export_policy(runtime)["max_rows"],
        "notice": "本文件含导出水印，仅限授权范围内使用；请勿外传。",
    }


async def audit_export(
    runtime: Any,
    current: Optional[CurrentUser],
    *,
    kind: str,
    rows: int,
    truncated: bool,
) -> None:
    """导出审计留痕（谁在什么范围导出了多少行）；失败仅告警不阻断。"""
    coord = getattr(runtime, "security", None)
    if coord is None:
        return
    try:
        await coord.audit_security(
            event_type="security/export",
            level="info",
            summary=(
                f"导出「{kind}」：{rows} 行"
                + ("（已按上限截断）" if truncated else "")
            ),
            user_id=getattr(current, "id", "") or "",
            tenant_id=getattr(current, "tenant_id", "") or "",
            detail={"kind": kind, "rows": rows, "truncated": truncated},
        )
    except Exception:  # noqa: BLE001 - 审计失败不影响导出
        logger.warning("导出审计写入失败（不影响导出结果）", exc_info=True)


def apply_export_governance(
    runtime: Any,
    current: Optional[CurrentUser],
    *,
    kind: str,
    rows: Any,
) -> tuple[list, Optional[Dict[str, Any]]]:
    """一站式：截断 → 构造水印（或 None）。

    注意**审批闸门需在取数前单独调用** `assert_export_allowed`（越权时不该先查库）。
    """
    capped, truncated = cap_rows(rows, runtime=runtime)
    if not export_policy(runtime)["watermark"]:
        return capped, None
    return capped, build_watermark(
        runtime, current, kind=kind, rows=len(capped), truncated=truncated
    )
