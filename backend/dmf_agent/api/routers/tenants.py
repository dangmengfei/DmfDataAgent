"""租户管理接口（仅管理员）。"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException

from dmf_agent.api.deps import get_runtime
from dmf_agent.auth.dependencies import require_role
from dmf_agent.schemas.auth import TenantCreate, TenantData, TenantUpdate
from dmf_agent.services.runtime import AppRuntime

router = APIRouter(
    prefix="/tenants", tags=["tenants"], dependencies=[Depends(require_role("admin"))]
)


@router.get("", response_model=List[TenantData], summary="租户列表")
async def list_tenants(runtime: AppRuntime = Depends(get_runtime)) -> List[TenantData]:
    return await runtime.db.tenant_repo.list_all()


@router.post("", response_model=TenantData, summary="新建租户")
async def create_tenant(
    body: TenantCreate, runtime: AppRuntime = Depends(get_runtime)
) -> TenantData:
    if await runtime.db.tenant_repo.get_by_code(body.code):
        raise HTTPException(status_code=409, detail=f"租户编码已存在：{body.code}")
    return await runtime.db.tenant_repo.add(
        TenantData(id="", code=body.code, name=body.name)
    )


@router.patch("/{tenant_id}", response_model=TenantData, summary="更新租户")
async def update_tenant(
    tenant_id: str, body: TenantUpdate, runtime: AppRuntime = Depends(get_runtime)
) -> TenantData:
    tenant = await runtime.db.tenant_repo.update(
        tenant_id, name=body.name, status=body.status
    )
    if tenant is None:
        raise HTTPException(status_code=404, detail="租户不存在")
    return tenant


@router.delete("/{tenant_id}", summary="删除租户（有用户时禁止）")
async def delete_tenant(tenant_id: str, runtime: AppRuntime = Depends(get_runtime)) -> dict:
    users = await runtime.db.user_repo.list_all(tenant_id=tenant_id)
    if users:
        raise HTTPException(status_code=409, detail=f"该租户下仍有 {len(users)} 个用户，无法删除")
    tenant = await runtime.db.tenant_repo.delete(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="租户不存在")
    return {"deleted": True}
