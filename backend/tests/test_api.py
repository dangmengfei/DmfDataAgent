"""FastAPI 接口集成测试（mock 模式，无外部依赖）。"""

from fastapi.testclient import TestClient

from dmf_agent.api.app import app


def test_health():
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["components"]["database"] == "memory"
        assert data["components"]["llm"] == "mock"


def test_api_v1_health():
    with TestClient(app) as client:
        r = client.get("/api/v1/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_chat_metric_mock():
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/chat", json={"message": "查一下近7天的GMV趋势", "user_id": "u1"}
        )
        assert r.status_code == 200
        data = r.json()
        assert data["intent"] == "metric_query"
        assert data["mock_mode"] is True
        assert "GMV" in data["content"]
        assert data["session_id"]

        # 同一会话再次提问，应带上下文且历史可查
        r2 = client.post(
            "/api/v1/chat",
            json={"message": "那再看看客单价", "user_id": "u1", "session_id": data["session_id"]},
        )
        assert r2.status_code == 200

        r3 = client.get(f"/api/v1/sessions/{data['session_id']}/messages")
        assert r3.status_code == 200
        messages = r3.json()
        roles = [m["role"] for m in messages]
        assert roles == ["user", "assistant", "user", "assistant"]
        assert messages[0]["node_trace"] == {}


def test_chat_stream_sse():
    with TestClient(app) as client:
        with client.stream(
            "POST", "/api/v1/chat/stream", json={"message": "你好呀"}
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            body = "".join(chunk for chunk in resp.iter_text())
        assert "event: session" in body
        assert "event: final" in body


def test_sessions_crud():
    with TestClient(app) as client:
        r = client.post("/api/v1/sessions", json={"user_id": "u2", "title": "测试会话"})
        assert r.status_code == 200
        sid = r.json()["id"]

        r = client.get("/api/v1/sessions", params={"user_id": "u2"})
        assert any(s["id"] == sid for s in r.json())

        r = client.patch(f"/api/v1/sessions/{sid}", json={"title": "改名"})
        assert r.json()["title"] == "改名"

        r = client.delete(f"/api/v1/sessions/{sid}")
        assert r.json()["deleted"] is True

        r = client.get(f"/api/v1/sessions/{sid}")
        assert r.status_code == 404


def test_metrics_search():
    """指标目录来自语义层（manifest.yml），不再读指标平台元数据。"""
    with TestClient(app) as client:
        r = client.get("/api/v1/metrics", params={"keyword": "GMV"})
        assert r.status_code == 200
        items = r.json()
        gmv = next(m for m in items if m["code"] == "GMV")
        assert gmv["unit"] == "元"
        assert "门店" in gmv["dimensions"]

        # 别名命中（销售额 -> GMV）
        r = client.get("/api/v1/metrics", params={"keyword": "销售额"})
        assert any(m["code"] == "GMV" for m in r.json())

        # 详情：按标准名 / 别名解析
        r = client.get("/api/v1/metrics/客单价")
        assert r.status_code == 200
        assert r.json()["category"] == "derived"

        r = client.get("/api/v1/metrics/不存在的指标")
        assert r.status_code == 404


def test_knowledge_documents_list_empty():
    with TestClient(app) as client:
        r = client.get("/api/v1/knowledge/documents")
        assert r.status_code == 200
        assert r.json() == []


def test_chat_multi_turn_memory():
    """多轮对话记忆：相关性判断与 rewritten_query 落 trace。"""
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/chat", json={"message": "查一下近7天的GMV趋势", "user_id": "u9"}
        )
        assert r.status_code == 200
        data = r.json()
        sid = data["session_id"]
        # 首轮无历史
        assert data["rewritten_query"] == "查一下近7天的GMV趋势"

        # 第二轮带指代词 -> 相关，改写记录进 trace
        r2 = client.post(
            "/api/v1/chat",
            json={"message": "那再看看客单价", "user_id": "u9", "session_id": sid},
        )
        assert r2.status_code == 200
        assert r2.json()["rewritten_query"] == "那再看看客单价"

        messages = client.get(f"/api/v1/sessions/{sid}/messages").json()
        assistants = [m for m in messages if m["role"] == "assistant"]
        assert len(assistants) == 2
        trace = assistants[-1]["node_trace"]
        assert trace.get("related_to_history") is True
        assert "rewritten_query" in trace
