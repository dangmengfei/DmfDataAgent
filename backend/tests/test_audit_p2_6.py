"""P2-6 审计补口测试：记忆操作 / 兜底创建 / token 并入审计明细。

覆盖：
- AuditLogger 三个新语义方法产出的事件形状（event_type / 关键字段）；
- 集成：记忆开关、L2 偏好、L3 知识写删经 HTTP 后在审计日志可查；
- 集成：首次启动兜底创建（默认租户/管理员）写 system/bootstrap。
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from dmf_agent.api.app import app
from dmf_agent.security.auditor import AuditLogger


class _MemSink:
    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    async def add(self, event: Dict[str, Any]) -> int:
        self.events.append(event)
        return len(self.events)


# ---------------- 单元：事件形状 ----------------


@pytest.mark.asyncio
async def test_memory_operation_event_shape():
    sink = _MemSink()
    await AuditLogger(sink=sink).memory_operation(
        operation="delete", layer="L3", user_id="u1", tenant_id="t1", target="7"
    )
    ev = sink.events[0]
    assert ev["event_type"] == "memory/operation"
    assert ev["level"] == "info"
    assert ev["user_id"] == "u1"
    assert ev["tenant_id"] == "t1"
    assert ev["detail_json"]["operation"] == "delete"
    assert ev["detail_json"]["layer"] == "L3"
    assert ev["detail_json"]["target"] == "7"


@pytest.mark.asyncio
async def test_system_bootstrap_is_warning():
    sink = _MemSink()
    await AuditLogger(sink=sink).system_bootstrap(
        action="default_admin", tenant_id="t1", user_id="u1", target="admin"
    )
    ev = sink.events[0]
    assert ev["event_type"] == "system/bootstrap"
    assert ev["level"] == "warning"
    assert ev["detail_json"]["action"] == "default_admin"
    assert ev["tenant_id"] == "t1"


@pytest.mark.asyncio
async def test_token_usage_event_shape():
    sink = _MemSink()
    await AuditLogger(sink=sink).token_usage(
        session_id="s1",
        user_id="u1",
        tenant_id="t1",
        round_id="r1",
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        model="gpt-test",
        cost=0.012,
        intent="metric_query",
    )
    ev = sink.events[0]
    assert ev["event_type"] == "token_usage"
    d = ev["detail_json"]
    assert d["prompt_tokens"] == 100
    assert d["completion_tokens"] == 50
    assert d["total_tokens"] == 150
    assert d["model"] == "gpt-test"
    assert d["cost"] == 0.012
    assert d["intent"] == "metric_query"
    assert ev["round_id"] == "r1"


@pytest.mark.asyncio
async def test_metric_creation_event_shape():
    sink = _MemSink()
    await AuditLogger(sink=sink).metric_creation(
        action="llm_draft",
        metric_key="metric_key_x",
        name="月活客户数",
        user_id="u1",
        tenant_id="t1",
        status="draft",
        version="v1",
    )
    ev = sink.events[0]
    assert ev["event_type"] == "metric/creation"
    assert ev["level"] == "info"
    d = ev["detail_json"]
    assert d["action"] == "llm_draft"
    assert d["metric_key"] == "metric_key_x"
    assert d["name"] == "月活客户数"
    assert d["status"] == "draft"


@pytest.mark.asyncio
async def test_disabled_auditor_writes_nothing():
    sink = _MemSink()
    n = await AuditLogger(sink=sink, enabled=False).system_bootstrap(action="x")
    assert n == 0
    assert sink.events == []


# ---------------- 集成：HTTP 触发记忆操作审计 ----------------


def _audit_events(client: TestClient, event_type: str) -> List[Dict[str, Any]]:
    r = client.get(
        "/api/v1/audit/logs", params={"event_type": event_type, "page_size": 200}
    )
    assert r.status_code == 200
    return r.json()["items"]


def test_memory_disable_toggle_audited():
    with TestClient(app) as client:
        r = client.post("/api/v1/memory/disable", json={"disabled": True})
        assert r.status_code == 200
        events = _audit_events(client, "memory/operation")
        ops = [e["detail_json"]["operation"] for e in events]
        assert "disable" in ops
        # 再开启
        r = client.post("/api/v1/memory/disable", json={"disabled": False})
        assert r.status_code == 200
        events = _audit_events(client, "memory/operation")
        assert "enable" in [e["detail_json"]["operation"] for e in events]


def test_l2_preference_upsert_and_delete_audited():
    with TestClient(app) as client:
        r = client.put(
            "/api/v1/memory/preferences",
            json={"key": "currency", "value": "CNY", "source": "user"},
        )
        assert r.status_code == 200
        events = _audit_events(client, "memory/operation")
        upserts = [e for e in events if e["detail_json"].get("operation") == "upsert"]
        assert upserts and upserts[0]["detail_json"]["target"] == "currency"

        r = client.delete("/api/v1/memory/preferences/currency")
        assert r.status_code == 200
        events = _audit_events(client, "memory/operation")
        deletes = [e for e in events if e["detail_json"].get("operation") == "delete"]
        assert deletes and deletes[0]["detail_json"]["target"] == "currency"


def test_l3_create_and_delete_audited():
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/memory/l3",
            json={
                "domain": "bank",
                "type": "rule",
                "question": "什么是活期",
                "answer": "活期存款",
                "status": "approved",
            },
        )
        assert r.status_code == 200
        item_id = r.json()["id"]
        events = _audit_events(client, "memory/operation")
        creates = [e for e in events if e["detail_json"].get("operation") == "create"]
        assert creates and creates[0]["detail_json"].get("layer") == "L3"

        r = client.delete(f"/api/v1/memory/l3/{item_id}")
        assert r.status_code == 200
        events = _audit_events(client, "memory/operation")
        l3del = [
            e
            for e in events
            if e["detail_json"].get("operation") == "delete"
            and e["detail_json"].get("layer") == "L3"
        ]
        assert l3del


def test_system_bootstrap_audited_on_fresh_start():
    """每次 lifespan 都用全新内存库（无用户）→ 必触发默认租户/管理员兜底创建审计。"""
    with TestClient(app) as client:
        events = _audit_events(client, "system/bootstrap")
        actions = [e["detail_json"]["action"] for e in events]
        assert "default_tenant" in actions
        assert "default_admin" in actions
        # 默认管理员事件应带 username
        admin_ev = next(e for e in events if e["detail_json"]["action"] == "default_admin")
        assert admin_ev["detail_json"]["target"]
