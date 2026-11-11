"""会话 trace 文件留痕（合规审计）：trace 的第三个出口。

与 trace 落库（TraceRepository）、OTel 导出并列：每轮对话额外把完整 trace 以
JSON 归档到本地"可配置目录"，布局与字段严格对齐历史导出产物 / 单轮详情接口：

    {archive_dir}/{session_id}/{round_id}.json
    {"session_id", "round_id", "saved_at", "trace"}

未启用（archive_enabled=false 或 archive_dir 为空）时 build_trace_archiver 返回
None，ChatService 优雅跳过；写盘失败仅告警，不影响主对话链路（与落库 / OTel 一致）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from dmf_agent.config.settings import Settings

logger = logging.getLogger(__name__)

# session_id / round_id 均由 uuid4().hex 生成（32 位十六进制）。
# 严格校验白名单，避免外部可控字符串拼进留痕路径造成目录穿越（合规留痕不收任意串）。
_ID_RE = re.compile(r"[0-9a-fA-F]{32}")


def _validate_id(value: str, name: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ValueError(f"非法 {name}（应为 32 位十六进制）：{value!r}")
    return value


class SessionTraceArchiver:
    """把每轮 trace 原子写入 {base_dir}/{session_id}/{round_id}.json。"""

    def __init__(self, base_dir: str | Path) -> None:
        self._base = Path(base_dir)

    @property
    def base_dir(self) -> Path:
        return self._base

    def path_for(self, session_id: str, round_id: str) -> Path:
        _validate_id(session_id, "session_id")
        _validate_id(round_id, "round_id")
        return self._base / session_id / f"{round_id}.json"

    async def save(
        self,
        session_id: str,
        round_id: str,
        trace: Dict[str, Any],
        *,
        saved_at: Optional[datetime] = None,
    ) -> Path:
        """归档单轮 trace；返回落盘路径。阻塞 IO 放到线程池，避免卡住事件循环。"""
        path = self.path_for(session_id, round_id)
        timestamp = saved_at or datetime.now(timezone.utc)
        payload = {
            "session_id": session_id,
            "round_id": round_id,
            "saved_at": timestamp.isoformat(),
            "trace": trace,
        }
        await asyncio.to_thread(self._write_atomic, path, payload)
        return path

    @staticmethod
    def _write_atomic(path: Path, payload: Dict[str, Any]) -> None:
        # 同目录临时文件 + os.replace：崩溃 / 并发下不会留下半截 JSON
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)


def build_trace_archiver(settings: Settings) -> Optional[SessionTraceArchiver]:
    """按配置装配留痕器；未启用或目录为空时返回 None（优雅降级，风格同 build_round_tracer）。"""
    cfg = settings.session
    if not cfg.archive_enabled or not cfg.archive_dir.strip():
        return None
    return SessionTraceArchiver(cfg.archive_dir.strip())
