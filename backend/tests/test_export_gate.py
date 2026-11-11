"""T198g：PRD FR13 导出治理——审批闸门 + 大结果集截断 + 下载水印 + 审计。

覆盖 `security/export_gate.py` 与三个导出端点的接线：
- `export_policy` 在缺省 / 缺 settings / 缺 security 时的安全默认值；
- `assert_export_allowed`：闸门关闭放行；开启后 admin(`*`) 与持 `export:approve` 者放行、
  其余 403（且**先于取数**拒绝）；
- `cap_rows` / `apply_export_governance`：上限截断 + 水印构造 + 关水印返回 None；
- `audit_export`：调用协调器写审计；协调器缺失或抛错时**不阻断导出**；
- 端点接线：`/fewshots/export` 在三态下的行为（默认 / 开水印 / 开闸门被拒）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from dmf_agent.api.routers import fewshots as fs_router
from dmf_agent.auth.roles import PERM_EXPORT_APPROVE
from dmf_agent.db.factory import DatabaseBundle
from dmf_agent.schemas.auth import CurrentUser
from dmf_agent.schemas.feedback import FewShotUpsert
from dmf_agent.security.export_gate import (
    apply_export_governance,
    assert_export_allowed,
    audit_export,
    build_watermark,
    cap_rows,
    export_policy,
)


def _runtime(*, require_approval: bool = False, max_rows: int = 5000, watermark: bool = True):
    return SimpleNamespace(
        settings=SimpleNamespace(
            security=SimpleNamespace(
                export_require_approval=require_approval,
                export_max_rows=max_rows,
                export_watermark_enabled=watermark,
            )
        )
    )


def _user(perms=(), role="analyst", username="u1"):
    return CurrentUser(
        id="u1", username=username, tenant_id="t1", role=role, permissions=list(perms)
    )


# ---------------- 配置读取 / 安全默认 ----------------

async def test_export_policy_defaults_when_settings_missing():
    """运行时不带 settings / security（轻量部署）→ 安全默认：不强制审批、5000 行、开水印。"""
    p = export_policy(SimpleNamespace())
    assert p == {"require_approval": False, "max_rows": 5000, "watermark": True}


async def test_export_policy_reads_settings():
    p = export_policy(_runtime(require_approval=True, max_rows=3, watermark=False))
    assert p == {"require_approval": True, "max_rows": 3, "watermark": False}


async def test_export_policy_max_rows_zero_falls_back():
    """max_rows=0 属配置错误 → 回落 5000，避免"导出 0 行"这种静默事故。"""
    assert export_policy(_runtime(max_rows=0))["max_rows"] == 5000


# ---------------- 审批闸门 ----------------

async def test_gate_off_allows_everyone():
    rt = _runtime(require_approval=False)
    assert_export_allowed(rt, _user(), kind="slang")  # 不抛即通过


async def test_gate_on_blocks_user_without_permission():
    rt = _runtime(require_approval=True)
    with pytest.raises(HTTPException) as ei:
        assert_export_allowed(rt, _user(), kind="slang")
    assert ei.value.status_code == 403
    assert "导出审批" in ei.value.detail
    assert PERM_EXPORT_APPROVE in ei.value.detail


async def test_gate_on_allows_admin_wildcard():
    rt = _runtime(require_approval=True)
    assert_export_allowed(rt, _user(perms=["*"], role="admin"), kind="obs")


async def test_gate_on_allows_explicit_export_approve():
    rt = _runtime(require_approval=True)
    assert_export_allowed(rt, _user(perms=[PERM_EXPORT_APPROVE]), kind="fewshot")


async def test_gate_on_allows_admin_role_even_without_wildcard():
    rt = _runtime(require_approval=True)
    assert_export_allowed(rt, _user(perms=[], role="admin"), kind="obs")


# ---------------- 大结果集截断 ----------------

async def test_cap_rows_under_limit_untouched():
    rows = list(range(3))
    capped, truncated = cap_rows(rows, runtime=_runtime(max_rows=10))
    assert capped == rows
    assert truncated is False


async def test_cap_rows_over_limit_truncates():
    capped, truncated = cap_rows(list(range(10)), runtime=_runtime(max_rows=4))
    assert capped == [0, 1, 2, 3]
    assert truncated is True


async def test_cap_rows_non_positive_cap_means_no_cap():
    capped, truncated = cap_rows(list(range(10)), runtime=_runtime(max_rows=-1))
    assert len(capped) == 10
    assert truncated is False


# ---------------- 水印 ----------------

async def test_build_watermark_fields():
    wm = build_watermark(
        _runtime(max_rows=100), _user(username="zhang"), kind="slang", rows=7, truncated=True
    )
    assert wm["kind"] == "slang"
    assert wm["exported_by"] == "zhang"
    assert wm["tenant_id"] == "t1"
    assert wm["rows"] == 7
    assert wm["truncated"] is True
    assert wm["max_rows"] == 100
    assert wm["exported_at"]  # ISO 时间戳非空
    assert "水印" in wm["notice"]


async def test_build_watermark_falls_back_to_id_for_anonymous():
    wm = build_watermark(
        _runtime(), _user(username=""), kind="obs", rows=0, truncated=False
    )
    assert wm["exported_by"] == "u1"  # username 空 → 回落 id


async def test_apply_governance_returns_none_when_watermark_disabled():
    capped, wm = apply_export_governance(
        _runtime(watermark=False), _user(), kind="obs", rows=list(range(9))
    )
    assert len(capped) == 9
    assert wm is None


async def test_apply_governance_caps_and_watermarks():
    capped, wm = apply_export_governance(
        _runtime(max_rows=2), _user(), kind="obs", rows=list(range(9))
    )
    assert len(capped) == 2
    assert wm is not None
    assert wm["rows"] == 2
    assert wm["truncated"] is True


# ---------------- 审计（best-effort） ----------------

class _SpyCoord:
    def __init__(self, boom: bool = False) -> None:
        self.calls = []
        self.boom = boom

    async def audit_security(self, **kw):
        if self.boom:
            raise RuntimeError("audit sink down")
        self.calls.append(kw)
        return 1


async def test_audit_export_writes_event():
    rt = _runtime()
    rt.security = _SpyCoord()
    await audit_export(rt, _user(), kind="slang", rows=5, truncated=True)
    assert len(rt.security.calls) == 1
    call = rt.security.calls[0]
    assert call["event_type"] == "security/export"
    assert call["detail"] == {"kind": "slang", "rows": 5, "truncated": True}
    assert "截断" in call["summary"]


async def test_audit_export_without_security_layer_is_noop():
    await audit_export(_runtime(), _user(), kind="obs", rows=1, truncated=False)  # 不抛即通过


async def test_audit_export_swallows_sink_failure():
    """审计写入失败绝不能阻断导出（变更已发生，只是留痕失败）。"""
    rt = _runtime()
    rt.security = _SpyCoord(boom=True)
    await audit_export(rt, _user(), kind="obs", rows=1, truncated=False)  # 不抛即通过


# ---------------- 端点接线 ----------------

async def _rt_with_items():
    rt = SimpleNamespace(db=DatabaseBundle(mode="memory"))
    await fs_router.create_few_shot(
        FewShotUpsert(question="各分行存款排名", answer="A", intent="metric_query"), rt
    )
    return rt


async def test_endpoint_default_no_gate_with_watermark():
    rt = await _rt_with_items()
    out = await fs_router.export_few_shots(
        status=None, current=_user(), runtime=rt
    )
    assert out["count"] == 1
    assert out["watermark"]["kind"] == "fewshot"
    assert out["watermark"]["exported_by"] == "u1"


async def test_endpoint_gate_blocks_before_querying():
    """开闸门 + 无权用户 → 403，且**不应**返回 items（越权时不查库/不导出）。"""
    rt = await _rt_with_items()
    rt.settings = SimpleNamespace(
        security=SimpleNamespace(
            export_require_approval=True, export_max_rows=5000, export_watermark_enabled=True
        )
    )
    with pytest.raises(HTTPException) as ei:
        await fs_router.export_few_shots(status=None, current=_user(), runtime=rt)
    assert ei.value.status_code == 403


async def test_endpoint_gate_allows_approved_user():
    rt = await _rt_with_items()
    rt.settings = SimpleNamespace(
        security=SimpleNamespace(
            export_require_approval=True, export_max_rows=5000, export_watermark_enabled=True
        )
    )
    out = await fs_router.export_few_shots(
        status=None, current=_user(perms=[PERM_EXPORT_APPROVE]), runtime=rt
    )
    assert out["count"] == 1


async def test_endpoint_truncates_to_max_rows():
    rt = SimpleNamespace(db=DatabaseBundle(mode="memory"))
    for i in range(5):
        await fs_router.create_few_shot(
            FewShotUpsert(question=f"q{i}", answer="A", intent="metric_query"), rt
        )
    rt.settings = SimpleNamespace(
        security=SimpleNamespace(
            export_require_approval=False, export_max_rows=2, export_watermark_enabled=True
        )
    )
    out = await fs_router.export_few_shots(status=None, current=_user(), runtime=rt)
    assert out["count"] == 2
    assert out["watermark"]["truncated"] is True
    assert out["watermark"]["max_rows"] == 2
