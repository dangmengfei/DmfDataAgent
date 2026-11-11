"""插件注册项与系统提示词版本的存储（MySQL / 内存双态）。

单独成模块（而非并入 DatabaseBundle 的 Protocol 体系）：
这两个存储只在「插件管理」「提示词管理」两个管理端功能中使用，
不参与对话主链路，保持低耦合，MySQL 不可用时自动退化为进程内存。
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dmf_agent.db import models


def _new_id() -> str:
    return uuid.uuid4().hex


def _ts(dt: Any) -> int:
    try:
        return int(dt.timestamp()) if dt else 0
    except Exception:  # noqa: BLE001
        return 0


class PluginStore:
    """Skill / MCP 注册项存储；session_factory 为 None 时使用进程内存。"""

    def __init__(self, session_factory: Optional[async_sessionmaker[AsyncSession]] = None) -> None:
        self._sf = session_factory
        self._mem: List[Dict[str, Any]] = []

    @property
    def persistent(self) -> bool:
        return self._sf is not None

    async def list(self, plugin_type: str) -> List[Dict[str, Any]]:
        if self._sf is None:
            return [dict(r) for r in self._mem if r["type"] == plugin_type]
        async with self._sf() as session:
            stmt = (
                select(models.PluginModel)
                .where(models.PluginModel.plugin_type == plugin_type)
                .order_by(models.PluginModel.created_at.desc())
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [_plugin_to_dict(r) for r in rows]

    async def save(
        self,
        plugin_type: str,
        name: str,
        description: str = "",
        endpoint: str = "",
        config: str = "{}",
        status: str = "enabled",
    ) -> bool:
        """新增或覆盖同名项；返回 True 表示已落 MySQL，False 表示内存态。"""
        if self._sf is None:
            self._mem = [
                r for r in self._mem if not (r["type"] == plugin_type and r["name"] == name)
            ]
            self._mem.append(
                {
                    "type": plugin_type,
                    "name": name,
                    "description": description,
                    "endpoint": endpoint,
                    "config": config,
                    "status": status,
                    "builtin": False,
                    "created_at": int(time.time()),
                }
            )
            return False
        async with self._sf() as session:
            stmt = select(models.PluginModel).where(
                models.PluginModel.plugin_type == plugin_type,
                models.PluginModel.name == name,
            )
            obj = (await session.execute(stmt)).scalars().first()
            if obj is None:
                obj = models.PluginModel(id=_new_id(), plugin_type=plugin_type, name=name)
                session.add(obj)
            obj.description = description
            obj.endpoint = endpoint
            obj.config = config
            obj.status = status
            await session.commit()
            return True

    async def delete(self, plugin_type: str, name: str) -> bool:
        if self._sf is None:
            before = len(self._mem)
            self._mem = [
                r for r in self._mem if not (r["type"] == plugin_type and r["name"] == name)
            ]
            return len(self._mem) < before
        async with self._sf() as session:
            stmt = select(models.PluginModel).where(
                models.PluginModel.plugin_type == plugin_type,
                models.PluginModel.name == name,
            )
            obj = (await session.execute(stmt)).scalars().first()
            if obj is None:
                return False
            await session.delete(obj)
            await session.commit()
            return True


class PromptVersionStore:
    """系统提示词版本存储；session_factory 为 None 时使用进程内存。"""

    def __init__(self, session_factory: Optional[async_sessionmaker[AsyncSession]] = None) -> None:
        self._sf = session_factory
        self._mem: List[Dict[str, Any]] = []

    @property
    def persistent(self) -> bool:
        return self._sf is not None

    async def load_all(self) -> List[Dict[str, Any]]:
        if self._sf is None:
            return [dict(r) for r in self._mem]
        async with self._sf() as session:
            stmt = select(models.PromptVersionModel).order_by(models.PromptVersionModel.id)
            rows = (await session.execute(stmt)).scalars().all()
            return [_prompt_to_dict(r) for r in rows]

    async def load_by_name(self, name: str) -> List[Dict[str, Any]]:
        if self._sf is None:
            return [dict(r) for r in self._mem if r["name"] == name]
        async with self._sf() as session:
            stmt = (
                select(models.PromptVersionModel)
                .where(models.PromptVersionModel.name == name)
                .order_by(models.PromptVersionModel.id)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [_prompt_to_dict(r) for r in rows]

    async def save(self, row: Dict[str, Any]) -> bool:
        """追加一个不可变版本；返回 True 表示已落 MySQL。"""
        if self._sf is None:
            self._mem.append(
                {
                    "name": row["name"],
                    "version": row["version"],
                    "system": row["system"],
                    "human": row.get("human", ""),
                    "description": row.get("description", ""),
                    "source_version": row.get("source_version", ""),
                    "status": row.get("status", "published"),
                    "layer": row.get("layer", ""),
                    "rollout_status": row.get("rollout_status", "full"),
                    "gray_ratio": row.get("gray_ratio", 1.0),
                    "eval_passed": row.get("eval_passed", False),
                    "source": "memory",
                    "created_at": row.get("created_at", int(time.time())),
                    "updated_at": row.get("updated_at", int(time.time())),
                }
            )
            return False
        async with self._sf() as session:
            obj = models.PromptVersionModel(
                name=row["name"],
                version=row["version"],
                system=row["system"],
                human=row.get("human", ""),
                description=row.get("description", ""),
                source_version=row.get("source_version", ""),
                status=row.get("status", "published"),
                layer=row.get("layer", ""),
                rollout_status=row.get("rollout_status", "full"),
                gray_ratio=row.get("gray_ratio", 1.0),
                eval_passed=row.get("eval_passed", False),
            )
            session.add(obj)
            await session.commit()
            return True

    async def update_version(self, name: str, version: str, **fields: Any) -> bool:
        """原地更新某版本的若干字段（如 eval_passed / rollout_status）。返回是否更新成功。"""
        allowed = ("layer", "rollout_status", "gray_ratio", "eval_passed",
                   "description", "system", "human")
        if self._sf is None:
            for r in self._mem:
                if r["name"] == name and r["version"] == version:
                    r.update({k: v for k, v in fields.items() if k in allowed})
                    return True
            return False
        async with self._sf() as session:
            stmt = (
                select(models.PromptVersionModel)
                .where(models.PromptVersionModel.name == name)
                .where(models.PromptVersionModel.version == version)
            )
            obj = (await session.execute(stmt)).scalars().first()
            if obj is None:
                return False
            for k, v in fields.items():
                if hasattr(obj, k):
                    setattr(obj, k, v)
            await session.commit()
            return True


def _plugin_to_dict(m: models.PluginModel) -> Dict[str, Any]:
    return {
        "type": m.plugin_type,
        "name": m.name,
        "description": m.description or "",
        "endpoint": m.endpoint or "",
        "config": m.config or "{}",
        "status": m.status or "enabled",
        "builtin": False,
        "created_at": _ts(m.created_at),
    }


def _prompt_to_dict(m: models.PromptVersionModel) -> Dict[str, Any]:
    return {
        "name": m.name,
        "version": m.version,
        "system": m.system or "",
        "human": m.human or "",
        "description": m.description or "",
        "source_version": m.source_version or "",
        "status": m.status or "published",
        "layer": getattr(m, "layer", "") or "",
        "rollout_status": getattr(m, "rollout_status", "") or "full",
        "gray_ratio": getattr(m, "gray_ratio", 1.0) or 1.0,
        "eval_passed": bool(getattr(m, "eval_passed", False)),
        "source": "mysql",
        "created_at": _ts(m.created_at),
        "updated_at": _ts(m.updated_at or m.created_at),
    }


class LLMConfigStore:
    """对话 LLM 模型配置存储（模型管理）；session_factory 为 None 时使用进程内存。

    与 PluginStore 同款双态（MySQL / 内存）实现：模型管理是管理端功能，
    不参与对话主链路，MySQL 不可用自动退化为进程内存。
    ``is_default`` 唯一性在写路径保证（save / set_default 同事务清其它）。
    """

    def __init__(self, session_factory: Optional[async_sessionmaker[AsyncSession]] = None) -> None:
        self._sf = session_factory
        self._mem: List[Dict[str, Any]] = []

    @property
    def persistent(self) -> bool:
        return self._sf is not None

    async def list(self) -> List[Dict[str, Any]]:
        if self._sf is None:
            return [dict(r) for r in self._mem]
        async with self._sf() as session:
            stmt = select(models.LLMConfigModel).order_by(
                models.LLMConfigModel.created_at.desc()
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [_llm_config_to_dict(r) for r in rows]

    async def get(self, name: str) -> Optional[Dict[str, Any]]:
        if self._sf is None:
            return next((dict(r) for r in self._mem if r["name"] == name), None)
        async with self._sf() as session:
            stmt = select(models.LLMConfigModel).where(models.LLMConfigModel.name == name)
            obj = (await session.execute(stmt)).scalars().first()
            return _llm_config_to_dict(obj) if obj is not None else None

    async def get_default(self) -> Optional[Dict[str, Any]]:
        """当前默认模型条目（is_default=True）；无默认返回 None。"""
        if self._sf is None:
            return next((dict(r) for r in self._mem if r.get("is_default")), None)
        async with self._sf() as session:
            stmt = select(models.LLMConfigModel).where(
                models.LLMConfigModel.is_default.is_(True)
            )
            obj = (await session.execute(stmt)).scalars().first()
            return _llm_config_to_dict(obj) if obj is not None else None

    async def save(self, row: Dict[str, Any]) -> bool:
        """按 name 新增或覆盖；返回 True 表示已落 MySQL，False 表示内存态。

        ``is_default=True`` 时同事务清除其它条目的默认标记（保证唯一默认）；
        未携带 ``last_test`` 时保留原值（更新不覆盖连接测试结果）。
        """
        name = row["name"]
        if self._sf is None:
            prev = next((r for r in self._mem if r["name"] == name), None)
            if row.get("is_default"):
                for r in self._mem:
                    r["is_default"] = False
            self._mem = [r for r in self._mem if r["name"] != name]
            self._mem.append(
                {
                    "id": (prev or {}).get("id") or _new_id(),
                    "name": name,
                    "model": row.get("model") or "",
                    "base_url": row.get("base_url") or "",
                    "api_key": row.get("api_key") or "",
                    "temperature": row.get("temperature") if row.get("temperature") is not None else 0.1,
                    "timeout": row.get("timeout") if row.get("timeout") is not None else 120,
                    "max_retries": row.get("max_retries") if row.get("max_retries") is not None else 2,
                    "is_default": bool(row.get("is_default")),
                    "status": row.get("status") or "enabled",
                    "last_test": row.get("last_test") if "last_test" in row else (prev or {}).get("last_test", "{}"),
                    "created_at": (prev or {}).get("created_at") or int(time.time()),
                    "updated_at": int(time.time()),
                }
            )
            return False
        async with self._sf() as session:
            if row.get("is_default"):
                await session.execute(
                    update(models.LLMConfigModel)
                    .where(models.LLMConfigModel.is_default.is_(True))
                    .values(is_default=False)
                )
            stmt = select(models.LLMConfigModel).where(models.LLMConfigModel.name == name)
            obj = (await session.execute(stmt)).scalars().first()
            if obj is None:
                obj = models.LLMConfigModel(id=_new_id(), name=name)
                session.add(obj)
            obj.model = row.get("model") or ""
            obj.base_url = row.get("base_url") or ""
            obj.api_key = row.get("api_key") or ""
            obj.temperature = row.get("temperature") if row.get("temperature") is not None else 0.1
            obj.timeout = row.get("timeout") if row.get("timeout") is not None else 120
            obj.max_retries = row.get("max_retries") if row.get("max_retries") is not None else 2
            obj.is_default = bool(row.get("is_default"))
            obj.status = row.get("status") or "enabled"
            if "last_test" in row:
                obj.last_test = row.get("last_test") or "{}"
            await session.commit()
            return True

    async def set_default(self, name: str) -> bool:
        """把名为 name 的条目设为唯一默认；条目不存在返回 False。"""
        if self._sf is None:
            target = next((r for r in self._mem if r["name"] == name), None)
            if target is None:
                return False
            for r in self._mem:
                r["is_default"] = r["name"] == name
            return True
        async with self._sf() as session:
            await session.execute(
                update(models.LLMConfigModel)
                .where(models.LLMConfigModel.is_default.is_(True))
                .values(is_default=False)
            )
            stmt = select(models.LLMConfigModel).where(models.LLMConfigModel.name == name)
            obj = (await session.execute(stmt)).scalars().first()
            if obj is None:
                await session.rollback()
                return False
            obj.is_default = True
            await session.commit()
            return True

    async def delete(self, name: str) -> bool:
        if self._sf is None:
            before = len(self._mem)
            self._mem = [r for r in self._mem if r["name"] != name]
            return len(self._mem) < before
        async with self._sf() as session:
            stmt = select(models.LLMConfigModel).where(models.LLMConfigModel.name == name)
            obj = (await session.execute(stmt)).scalars().first()
            if obj is None:
                return False
            await session.delete(obj)
            await session.commit()
            return True


def _llm_config_to_dict(m: models.LLMConfigModel) -> Dict[str, Any]:
    return {
        "id": m.id,
        "name": m.name,
        "model": m.model or "",
        "base_url": m.base_url or "",
        "api_key": m.api_key or "",
        "temperature": m.temperature if m.temperature is not None else 0.1,
        "timeout": m.timeout if m.timeout is not None else 120,
        "max_retries": m.max_retries if m.max_retries is not None else 2,
        "is_default": bool(m.is_default),
        "status": m.status or "enabled",
        "last_test": m.last_test or "{}",
        "created_at": _ts(m.created_at),
        "updated_at": _ts(m.updated_at or m.created_at),
    }
