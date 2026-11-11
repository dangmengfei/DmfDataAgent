"""审计器：把 Harness / 安全 / 权限 / 数据访问事件写入审计日志。

PRD FR13 要求"全链路审计：谁（用户/部门/角色）在何时查了什么指标、返回了什么结果、
token 消耗；记忆操作与兜底创建留痕"。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Protocol

logger = logging.getLogger(__name__)


class AuditSink(Protocol):
    async def add(self, event: Dict[str, Any]) -> int: ...


class AuditLogger:
    def __init__(self, sink: Optional[AuditSink] = None, enabled: bool = True) -> None:
        self.sink = sink
        self.enabled = enabled

    async def log(self, event: Dict[str, Any]) -> int:
        if not self.enabled:
            return 0
        # 强制补齐字段，避免 None
        event.setdefault("level", "info")
        event.setdefault("event_type", "unknown")
        event.setdefault("session_id", "")
        event.setdefault("user_id", "")
        event.setdefault("tenant_id", "")
        event.setdefault("round_id", "")
        if not self.sink:
            logger.debug("audit: %s", event)
            return 0
        try:
            return await self.sink.add(event)
        except Exception:  # noqa: BLE001 - 审计失败不阻断主链路
            logger.warning("审计日志写入失败", exc_info=True)
            return 0

    async def data_access(
        self,
        *,
        session_id: str,
        user_id: str,
        tenant_id: str,
        round_id: str,
        metrics: list[str],
        dimensions: list[str],
        row_count: int,
        elapsed_ms: float,
        sql: str = "",
        detail: Optional[Dict[str, Any]] = None,
    ) -> int:
        return await self.log(
            {
                "event_type": "data_access",
                "level": "info",
                "session_id": session_id,
                "user_id": user_id,
                "tenant_id": tenant_id,
                "round_id": round_id,
                "summary": f"查询指标 {metrics}，返回 {row_count} 行",
                "detail_json": {
                    "metrics": metrics,
                    "dimensions": dimensions,
                    "row_count": row_count,
                    "elapsed_ms": elapsed_ms,
                    "sql": sql,
                    **(detail or {}),
                },
            }
        )

    async def security(
        self,
        *,
        event_type: str,
        level: str,
        summary: str,
        session_id: str = "",
        user_id: str = "",
        tenant_id: str = "",
        round_id: str = "",
        detail: Optional[Dict[str, Any]] = None,
    ) -> int:
        return await self.log(
            {
                "event_type": event_type,
                "level": level,
                "session_id": session_id,
                "user_id": user_id,
                "tenant_id": tenant_id,
                "round_id": round_id,
                "summary": summary,
                "detail_json": detail or {},
            }
        )

    async def memory_operation(
        self,
        *,
        operation: str,
        layer: str,
        user_id: str,
        tenant_id: str,
        target: str = "",
        session_id: str = "",
        round_id: str = "",
        summary: str = "",
        detail: Optional[Dict[str, Any]] = None,
    ) -> int:
        """记忆操作留痕（写 / 覆盖 / 删除 / 清空 / 开关变更）。读操作不记。"""
        return await self.log(
            {
                "event_type": "memory/operation",
                "level": "info",
                "session_id": session_id,
                "user_id": user_id,
                "tenant_id": tenant_id,
                "round_id": round_id,
                "summary": summary or f"记忆操作 {operation}（{layer}）",
                "detail_json": {
                    "operation": operation,
                    "layer": layer,
                    "target": target,
                    **(detail or {}),
                },
            }
        )

    async def system_bootstrap(
        self,
        *,
        action: str,
        tenant_id: str = "",
        user_id: str = "",
        target: str = "",
        summary: str = "",
        detail: Optional[Dict[str, Any]] = None,
    ) -> int:
        """系统兜底创建留痕：默认租户 / 默认管理员引导（高可见度，warning 级）。"""
        return await self.log(
            {
                "event_type": "system/bootstrap",
                "level": "warning",
                "session_id": "",
                "user_id": user_id,
                "tenant_id": tenant_id,
                "round_id": "",
                "summary": summary or f"系统兜底创建：{action}",
                "detail_json": {
                    "action": action,
                    "target": target,
                    **(detail or {}),
                },
            }
        )

    async def metric_creation(
        self,
        *,
        action: str,
        metric_key: str,
        name: str,
        user_id: str,
        tenant_id: str,
        status: str = "draft",
        version: str = "",
        detail: Optional[Dict[str, Any]] = None,
    ) -> int:
        """FR8 兜底创建留痕：LLM 生成指标草稿落库（与 system/bootstrap 区分——
        这是用户提问触发的指标创建，不是系统首次启动的默认资源引导）。"""
        return await self.log(
            {
                "event_type": "metric/creation",
                "level": "info",
                "session_id": "",
                "user_id": user_id,
                "tenant_id": tenant_id,
                "round_id": "",
                "summary": f"LLM 兜底创建指标草稿【{name}】（key={metric_key}）",
                "detail_json": {
                    "action": action,
                    "metric_key": metric_key,
                    "name": name,
                    "status": status,
                    "version": version,
                    **(detail or {}),
                },
            }
        )

    async def token_usage(
        self,
        *,
        session_id: str,
        user_id: str,
        tenant_id: str,
        round_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        model: str = "",
        cost: float = 0.0,
        intent: str = "",
        detail: Optional[Dict[str, Any]] = None,
    ) -> int:
        """把每轮 token 消耗并入审计明细（与 data_access 同轮，按 round_id 关联）。"""
        return await self.log(
            {
                "event_type": "token_usage",
                "level": "info",
                "session_id": session_id,
                "user_id": user_id,
                "tenant_id": tenant_id,
                "round_id": round_id,
                "summary": f"本轮消耗 token {total_tokens}（{model or '未知模型'}）",
                "detail_json": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                    "model": model,
                    "cost": cost,
                    "intent": intent,
                    **(detail or {}),
                },
            }
        )
