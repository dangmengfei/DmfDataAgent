"""模型管理服务：对话 LLM 配置的 CRUD + 连接测试 + 运行时生效覆盖。

- 配置入库（``llm_configs`` 表，MySQL / 内存双态，见 ``db/stores.LLMConfigStore``）；
- ``is_default`` 且 ``status=enabled`` 的条目作为**运行时生效模型**，经
  ``get_chat_model(settings, llm_override=...)`` 覆盖环境变量 ``DMF_LLM-*``；
  无默认模型 / DB 异常时回落环境变量（降级矩阵，绝不阻断对话主链路）；
- 生效覆盖为**内存缓存**（同步读取，供 ``get_chat_model`` lambda 用），
  启动 / 管理面变更（增删改、设默认）即时刷新，另由 ``api/app.py`` 的后台
  探活周期重读，收敛多实例（与语义层基座「拉」式自愈同思路）；
- 连接测试：对 OpenAI 兼容端点发一条极短 chat completion，失败不冒泡、
  一律 ``ok=False`` + 可读原因（对齐 ``PluginRegistry.probe_mcp`` 的惯用法）。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from dmf_agent.config.settings import LLMSettings
from dmf_agent.db.stores import LLMConfigStore

logger = logging.getLogger(__name__)


def mask_api_key(key: str) -> str:
    """API Key 脱敏：保留后 4 位，其余打星；空值原样返回。"""
    if not key:
        return ""
    if len(key) <= 4:
        return "*" * len(key)
    return "*" * (len(key) - 4) + key[-4:]


def normalize_base_url(url: str) -> str:
    """规范化 OpenAI 兼容端点：去首尾空白与尾部斜杠（拼接 /chat/completions 不出现双斜杠）。"""
    return (url or "").strip().rstrip("/")


def _valid_base_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:  # noqa: BLE001
        return False


async def probe_chat_model(
    model: str, base_url: str, api_key: str = "", timeout: float = 30.0
) -> Dict[str, Any]:
    """对 OpenAI 兼容端点发一条极短 chat completion，验证连通性。

    失败不抛错，一律返回 ``{"ok", "message", "elapsed_ms", "model"}``。
    """
    started = time.monotonic()
    url = f"{normalize_base_url(base_url)}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }
    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
        elapsed = int((time.monotonic() - started) * 1000)
        if resp.status_code == 200:
            return {
                "ok": True,
                "message": "连接成功，模型可用",
                "elapsed_ms": elapsed,
                "model": model,
            }
        # 尽量给出可读错误：OpenAI 兼容端点错误体通常是 {error: {message}}
        detail = ""
        try:
            body = resp.json()
            err = (body or {}).get("error") or {}
            detail = err.get("message") or body.get("message") or ""
        except Exception:  # noqa: BLE001
            detail = (resp.text or "")[:200]
        return {
            "ok": False,
            "message": f"HTTP {resp.status_code}：{detail or '请求被拒绝'}",
            "elapsed_ms": elapsed,
            "model": model,
        }
    except Exception as exc:  # noqa: BLE001 - 连接失败需回传原因，不冒泡
        elapsed = int((time.monotonic() - started) * 1000)
        logger.warning("模型连接测试失败: %s", exc, exc_info=True)
        return {
            "ok": False,
            "message": f"连接失败：{exc}",
            "elapsed_ms": elapsed,
            "model": model,
        }


class ModelRegistry:
    """模型配置注册表 + 运行时生效覆盖缓存。

    生效覆盖 ``_override``（LLMSettings 或 None）为内存态，同步读取（供
    ``get_chat_model`` lambda 使用）；``refresh()`` 从存储重读默认条目重建覆盖，
    由启动 / 管理面变更 / 后台探活三处调用。
    """

    def __init__(self, store: LLMConfigStore) -> None:
        self._store = store
        self._override: Optional[LLMSettings] = None
        self._override_name: Optional[str] = None

    # ---------- 运行时生效覆盖 ----------
    @property
    def effective_llm(self) -> Optional[LLMSettings]:
        """当前生效的默认模型覆盖（同步，供 get_chat_model lambda 读取）。"""
        return self._override

    @property
    def effective_name(self) -> Optional[str]:
        return self._override_name

    async def refresh(self) -> Optional[LLMSettings]:
        """从存储重读默认条目并重建覆盖；DB 异常保留旧覆盖（降级不阻断）。"""
        try:
            row = await self._store.get_default()
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取默认模型失败，保留上次生效覆盖：%s", exc)
            return self._override
        self._override = self._build_override(row) if row else None
        self._override_name = (row or {}).get("name")
        return self._override

    def _build_override(self, row: Dict[str, Any]) -> Optional[LLMSettings]:
        if not row:
            return None
        if row.get("status") != "enabled":
            return None
        if not (row.get("model") and row.get("base_url")):
            return None
        return LLMSettings(
            model=row["model"],
            base_url=row["base_url"],
            api_key=row.get("api_key") or "",
            temperature=float(row["temperature"]) if row.get("temperature") is not None else 0.1,
            timeout=int(row.get("timeout") or 120),
            max_retries=int(row.get("max_retries") or 2),
        )

    # ---------- 列表 / 详情 ----------
    async def list(self) -> Dict[str, Any]:
        try:
            rows = await self._store.list()
        except Exception as exc:  # noqa: BLE001 - 列表失败返回空，不阻断管理面
            logger.warning("模型列表读取失败：%s", exc)
            rows = []
        items = [self._masked(r) for r in rows]
        default = next((r.get("name") for r in rows if r.get("is_default")), None)
        return {
            "items": items,
            "count": len(items),
            "default": default,       # 库中 is_default 标记名
            "effective": self.effective_name,  # 实际生效（is_default 且 enabled 且配置完整）
        }

    async def get(self, name: str) -> Optional[Dict[str, Any]]:
        try:
            row = await self._store.get(name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("模型详情读取失败: %s", exc)
            return None
        return self._masked(row) if row else None

    async def seed_from_env(self, llm_cfg: LLMSettings) -> Dict[str, Any]:
        """把环境变量配置的对话 LLM **引导**为库内默认模型（幂等，一次性迁移）。

        用于「模型配置从 env 迁到模型管理」：env 配了 LLM（``DMF_LLM-*``）且库内
        尚无默认模型时，把当前生效配置落成一条 ``is_default`` 模型（名称=model 名），
        之后即可从 env 移除 chat 侧 LLM 键，运行时仍走该库内默认模型。

        跳过条件（任一命中即不写库，绝不覆盖管理面改动）：
        - env 未配置 chat LLM；
        - 库内已存在同名条目，或已存在同 ``(base_url, model)`` 条目；
        - 库内已有默认模型（管理面已接管）。
        """
        if not llm_cfg.chat_configured:
            return {"ok": False, "skipped": "env_chat_not_configured"}
        try:
            rows = await self._store.list()
        except Exception as exc:  # noqa: BLE001 - 引导失败不阻断启动
            logger.warning("读取模型列表失败，跳过 env 引导：%s", exc)
            return {"ok": False, "skipped": "store_error"}
        for r in rows:
            if r.get("name") == llm_cfg.model:
                return {"ok": False, "skipped": "same_name_exists"}
            if r.get("base_url") == llm_cfg.base_url and r.get("model") == llm_cfg.model:
                return {"ok": False, "skipped": "same_endpoint_exists"}
        if any(r.get("is_default") for r in rows):
            return {"ok": False, "skipped": "default_exists"}
        row = {
            "name": llm_cfg.model,
            "model": llm_cfg.model,
            "base_url": llm_cfg.base_url,
            "api_key": llm_cfg.api_key or "",
            "temperature": llm_cfg.temperature,
            "timeout": llm_cfg.timeout,
            "max_retries": llm_cfg.max_retries,
            "status": "enabled",
            "is_default": True,
        }
        await self._store.save(row)
        await self.refresh()
        logger.info(
            "已把环境变量 LLM 引导为库内默认模型：%s（可从 env 移除 DMF_LLM-*）",
            llm_cfg.model,
        )
        return {"ok": True, "item": await self.get(row["name"])}

    def _masked(self, row: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(row)
        out["api_key"] = mask_api_key(row.get("api_key") or "")
        try:
            out["last_test"] = json.loads(row.get("last_test") or "{}")
        except (json.JSONDecodeError, TypeError):
            out["last_test"] = {}
        return out

    # ---------- CRUD ----------
    async def create(self, body: Any) -> Dict[str, Any]:
        name = (body.name or "").strip()
        if not name:
            return {"ok": False, "message": "模型名称不能为空"}
        model = (body.model or "").strip()
        base_url = normalize_base_url(body.base_url)
        if not model:
            return {"ok": False, "message": "模型名（model）不能为空"}
        if not base_url:
            return {"ok": False, "message": "base_url 不能为空"}
        if not _valid_base_url(base_url):
            return {"ok": False, "message": "base_url 需为 http(s):// 开头的合法地址"}
        existing = await self._store.get(name)
        if existing is not None:
            return {"ok": False, "message": f"模型「{name}」已存在，请直接编辑"}
        row = {
            "name": name,
            "model": model,
            "base_url": base_url,
            "api_key": (body.api_key or "").strip(),
            "temperature": body.temperature,
            "timeout": body.timeout,
            "max_retries": body.max_retries,
            "status": body.status or "enabled",
            "is_default": bool(body.is_default),
        }
        await self._store.save(row)
        await self.refresh()
        return {"ok": True, "message": f"模型「{name}」已保存", "item": await self.get(name)}

    async def update(self, name: str, body: Any) -> Dict[str, Any]:
        name = (name or "").strip()
        existing = await self._store.get(name)
        if existing is None:
            return {"ok": False, "not_found": True, "message": f"未找到模型「{name}」"}
        base_url = existing.get("base_url")
        if body.base_url is not None:
            base_url = normalize_base_url(body.base_url)
            if not base_url:
                return {"ok": False, "message": "base_url 不能为空"}
            if not _valid_base_url(base_url):
                return {"ok": False, "message": "base_url 需为 http(s):// 开头的合法地址"}
        model = existing.get("model")
        if body.model is not None:
            model = (body.model or "").strip()
            if not model:
                return {"ok": False, "message": "模型名（model）不能为空"}
        api_key = existing.get("api_key")
        if body.api_key:  # 非空才更新（列表接口只回脱敏 key，编辑界面留空 = 保留原 key）
            api_key = (body.api_key or "").strip()
        row = {
            "name": name,
            "model": model,
            "base_url": base_url,
            "api_key": api_key,
            "temperature": body.temperature if body.temperature is not None else existing.get("temperature"),
            "timeout": body.timeout if body.timeout is not None else existing.get("timeout"),
            "max_retries": body.max_retries if body.max_retries is not None else existing.get("max_retries"),
            "status": body.status if body.status is not None else existing.get("status"),
            "is_default": body.is_default if body.is_default is not None else existing.get("is_default"),
        }
        await self._store.save(row)
        await self.refresh()
        return {"ok": True, "message": f"模型「{name}」已更新", "item": await self.get(name)}

    async def delete(self, name: str) -> Dict[str, Any]:
        name = (name or "").strip()
        deleted = await self._store.delete(name)
        if not deleted:
            return {"ok": False, "not_found": True, "message": f"未找到模型「{name}」"}
        await self.refresh()
        return {"ok": True, "message": f"模型「{name}」已删除"}

    async def set_default(self, name: str) -> Dict[str, Any]:
        name = (name or "").strip()
        row = await self._store.get(name)
        if row is None:
            return {"ok": False, "not_found": True, "message": f"未找到模型「{name}」"}
        if row.get("status") != "enabled":
            return {"ok": False, "message": f"模型「{name}」未启用，请先启用再设为默认"}
        if not (row.get("model") and row.get("base_url")):
            return {"ok": False, "message": f"模型「{name}」缺少 model/base_url，无法作为运行时生效模型"}
        ok = await self._store.set_default(name)
        if not ok:
            return {"ok": False, "message": f"设置默认失败：未找到模型「{name}」"}
        await self.refresh()
        return {"ok": True, "message": f"已把「{name}」设为默认模型，运行时立即生效"}

    # ---------- 连接测试 ----------
    async def probe(self, body: Any) -> Dict[str, Any]:
        """用**给定配置**做连接测试，不落库（新增/编辑弹窗预检）。

        编辑场景：``body.name`` 存在且表单字段留空时，用库中已存配置补齐
        （尤其 api_key 列表只回脱敏值，前端不回传）。
        """
        model = (body.model or "").strip()
        base_url = normalize_base_url(body.base_url)
        api_key = (body.api_key or "").strip()
        if body.name:
            try:
                saved = await self._store.get(body.name)
            except Exception:  # noqa: BLE001
                saved = None
            if saved is not None:
                if not model:
                    model = saved.get("model") or ""
                if not base_url:
                    base_url = saved.get("base_url") or ""
                if not api_key:
                    api_key = saved.get("api_key") or ""
        if not model:
            return {"ok": False, "message": "请填写模型名（model）", "model": model}
        if not base_url:
            return {"ok": False, "message": "请填写 base_url", "model": model}
        return await probe_chat_model(model, base_url, api_key, timeout=body.timeout)

    async def test(self, name: str) -> Dict[str, Any]:
        """连接测试已保存的模型：探测后把结果回写 ``last_test``。

        失败不冒泡（与 ``probe`` 一致），但会把失败也记进 ``last_test``，
        供前端展示"上次测试时间 / 原因"。
        """
        name = (name or "").strip()
        row = await self._store.get(name)
        if row is None:
            return {"ok": False, "message": f"未找到模型「{name}」"}
        probe_timeout = max(min(float(row.get("timeout") or 30.0), 60.0), 5.0)
        result = await probe_chat_model(
            row.get("model") or "",
            row.get("base_url") or "",
            row.get("api_key") or "",
            timeout=probe_timeout,
        )
        try:
            await self._store.save(
                {
                    **row,
                    "last_test": json.dumps(
                        {
                            "ok": bool(result.get("ok")),
                            "elapsed_ms": int(result.get("elapsed_ms") or 0),
                            "at": datetime.now(timezone.utc).isoformat(),
                            "error": "" if result.get("ok") else str(result.get("message") or ""),
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        except Exception:  # noqa: BLE001 - 回写失败不影响测试结论
            logger.warning("模型测试结果回写失败: %s", name, exc_info=True)
        return result
