"""HM 元数据 API 客户端（异步 httpx）。

迁移自老系统 metadata/hm_meta/hm_api_base.py::HmApiBase 与
hm_metric.py::HmApiMeta 的元数据取数：
- 指标元数据：POST v1/standard-api/sa/metaInfo/metrics -> data.list
- 维度元数据：POST v1/standard-api/sa/metaInfo/dimension -> data.list
- 租户鉴权（老系统两种模式）：
  - X_tenant：tenantId/userAccount/userName base64 编码（内部直连）
  - Access-Token：nginx 转发模式
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from dmf_agent.config.settings import Settings
from dmf_agent.integrations.metric_platform.endpoints import hm_path

logger = logging.getLogger(__name__)


def tenant_encode(tenant_id: str, user_account: str, user_name: str) -> str:
    """生成 X_tenant 头（迁移自老 metadata/user_meta_convert.py::tenant_encode）。"""
    payload = {
        "userAccount": user_account,
        "userName": user_name,
        "tenantId": tenant_id,
    }
    return base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("utf-8")


def _dedup_cant_domains(data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """公司字典(dm)与个人字典(dc)去重：同名黑话个人字典优先（老 HmApiMeta.get_cant）。"""
    dm = [c for c in data if c.get("cantDomain") == "dm"]
    dc = [c for c in data if c.get("cantDomain") == "dc"]
    if dm and dc:
        dc_slangs = {c.get("cantCnName") for c in dc}
        dm = [c for c in dm if c.get("cantCnName") not in dc_slangs]
        return dm + dc
    return data


def build_hm_headers(settings: Settings) -> Dict[str, str]:
    """HM 通用鉴权头（元数据 / 取数接口共用，迁移自老 HmApiBase.get_header_url）。"""
    headers = {"Content-Type": "application/json"}
    if settings.hm.access_token:
        headers["Access-Token"] = settings.hm.access_token
    elif settings.hm.tenant_id:
        headers["X_tenant"] = tenant_encode(
            settings.hm.tenant_id, settings.hm.user_account, settings.hm.user_name
        )
    return headers


class HmMetaClient:
    """HM 元数据接口客户端；api_base 未配置或请求失败时返回空列表（不阻塞主链路）。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base = (settings.hm.api_base or "").rstrip("/")
        self._timeout = settings.hm.timeout

    @property
    def configured(self) -> bool:
        return bool(self._base)

    def build_headers(self) -> Dict[str, str]:
        return build_hm_headers(self._settings)

    async def _post_json(self, path: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """POST 并返回 data 段；未配置 / 失败 / 异常码时返回 None（不阻断主链路）。"""
        if not self.configured:
            return None
        url = f"{self._base}/{path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=body, headers=self.build_headers())
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:  # noqa: BLE001 - 元数据失败不应阻断主链路
            logger.warning("HM 元数据接口请求失败 %s：%s", path, exc)
            return None
        if payload.get("code") != 200:
            logger.warning("HM 元数据接口返回异常 %s：%s", path, payload.get("code"))
            return None
        return payload.get("data") or {}

    async def _post_list(self, path: str, body: Dict[str, Any]) -> List[Dict[str, Any]]:
        data = await self._post_json(path, body)
        if not data:
            return []
        return data.get("list") or []

    async def fetch_metrics(
        self, keywords: str = "", page_size: int = 1000
    ) -> List[Dict[str, Any]]:
        return await self._post_list(
            hm_path("metrics_meta"),
            {
                "keywords": keywords,
                "keywordsType": 1,
                "pageNum": 1,
                "pageSize": page_size,
                "enablePermission": False,
            },
        )

    async def fetch_dimensions(
        self, keywords: str = "", page_size: int = 1000
    ) -> List[Dict[str, Any]]:
        return await self._post_list(
            hm_path("dimension_meta"),
            {
                "keywords": keywords,
                "keywordsType": 1,
                "pageNum": 1,
                "pageSize": page_size,
            },
        )

    async def fetch_dim_values(
        self,
        query: str,
        dim_ids: Optional[List[int]] = None,
        metric_id: Optional[int] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """维值反查（迁移自老 metadata/hm_meta/hm_dim_by_value.py::HmDimByValue）。

        POST v1/standard-api/sa/dim/getByValue -> data.exactList + data.chatList
        （精准 / 模糊两路合并返回）。行字段：dimId/dimCnName/dimValue/match。
        dim_ids / metric_id 二选一或都不传，用于缩小反查范围；未配置 HM
        或请求失败返回空列表（过滤召回优雅降级为空）。
        """
        body: Dict[str, Any] = {"dimQry": query, "limit": limit}
        if dim_ids:
            body["dimIds"] = dim_ids
        if metric_id is not None:
            body["metricId"] = metric_id
        data = await self._post_json(hm_path("dim_by_value"), body)
        if not data:
            return []
        exact = data.get("exactList") or []
        chat = data.get("chatList") or []
        return [*exact, *chat]

    async def fetch_cants(
        self,
        cant_types: Optional[List[str]] = None,
        cant_status: int = 1,
    ) -> List[Dict[str, Any]]:
        """黑话字典（迁移自老 metadata/hm_meta/hm_metric.py::HmApiMeta.get_cant）。

        POST v1/aigcCantName/listBySa -> data 为黑话行列表，字段：
        cnName/cantCnName/cantType/cantDomain（dm 公司 / dc 个人字典）/
        dimValue（cantType=dim_value 时）。个人字典优先：与公司字典存在同名黑话
        冲突时剔除公司条目。未配置 HM / 请求失败返回空列表。
        """
        body: Dict[str, Any] = {"cantType": cant_types, "cantStatus": cant_status}
        data = await self._post_json(hm_path("cant_list"), body)
        if not isinstance(data, list):
            return []
        return _dedup_cant_domains(data)
