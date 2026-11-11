"""会话 trace 文件留痕（observability.archive）测试。

- 单元：落盘布局 / 字段 / 时间戳 / 原子写无残留 / 路径穿越防御 / 按配置装配
- 端到端：开启留痕跑一轮对话，校验 {archive_dir}/{session_id}/{round_id}.json 生成
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from dmf_agent.config.settings import Settings, SessionSettings
from dmf_agent.observability import SessionTraceArchiver, build_trace_archiver


def _id() -> str:
    return uuid4().hex


async def test_save_writes_expected_layout(tmp_path):
    sid, rid = _id(), _id()
    archiver = SessionTraceArchiver(tmp_path)
    path = await archiver.save(sid, rid, {"intent": "metric_query", "final_answer": "ok"})

    assert path == tmp_path / sid / f"{rid}.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["session_id"] == sid
    assert data["round_id"] == rid
    assert data["trace"] == {"intent": "metric_query", "final_answer": "ok"}
    assert isinstance(data["saved_at"], str) and data["saved_at"]
    # 原子写：最终目录不应残留临时文件
    assert list((tmp_path / sid).glob("*.tmp")) == []


async def test_save_uses_explicit_saved_at(tmp_path):
    ts = datetime(2026, 9, 9, 7, 22, 2, tzinfo=timezone.utc)
    path = await SessionTraceArchiver(tmp_path).save(_id(), _id(), {}, saved_at=ts)
    assert json.loads(path.read_text(encoding="utf-8"))["saved_at"] == ts.isoformat()


@pytest.mark.parametrize("bad", ["", "xyz", "../etc", "a/b", ".."])
async def test_save_rejects_unsafe_id(tmp_path, bad):
    archiver = SessionTraceArchiver(tmp_path)
    with pytest.raises(ValueError):
        await archiver.save(bad, _id(), {})
    with pytest.raises(ValueError):
        await archiver.save(_id(), bad, {})
    # 被拦截后不应创建任何目录 / 文件
    assert list(tmp_path.rglob("*.json")) == []


def test_build_archiver_disabled_or_blank_returns_none(tmp_path):
    assert (
        build_trace_archiver(Settings(session=SessionSettings(archive_enabled=False)))
        is None
    )
    assert (
        build_trace_archiver(Settings(session=SessionSettings(archive_dir="   ")))
        is None
    )


def test_build_archiver_enabled(tmp_path):
    archiver = build_trace_archiver(
        Settings(session=SessionSettings(archive_dir=str(tmp_path)))
    )
    assert archiver is not None
    assert archiver.base_dir == tmp_path


def test_archive_file_after_chat_round(tmp_path, monkeypatch):
    """端到端：开启留痕后，/api/v1/chat 一轮应在配置目录生成会话留痕文件。"""
    from fastapi.testclient import TestClient

    from dmf_agent.api.app import create_app
    from dmf_agent.config.settings import get_settings

    monkeypatch.setenv("DMF_SESSION-ARCHIVE_ENABLED", "true")
    monkeypatch.setenv("DMF_SESSION-ARCHIVE_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            r = client.post(
                "/api/v1/chat",
                json={"message": "查一下近7天的GMV趋势", "user_id": "u_arch_t"},
            )
            assert r.status_code == 200
            body = r.json()
            sid, rid = body["session_id"], body["message_id"]

        archived = tmp_path / sid / f"{rid}.json"
        assert archived.exists()
        data = json.loads(archived.read_text(encoding="utf-8"))
        assert data["session_id"] == sid
        assert data["round_id"] == rid
        assert data["saved_at"]
        assert data["trace"]["final_answer"]
    finally:
        # 复位缓存，避免带开关的 settings 泄漏给其它用例
        get_settings.cache_clear()
