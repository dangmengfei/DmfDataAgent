"""FastAPI 依赖注入。"""

from __future__ import annotations

from fastapi import Request

from dmf_agent.services.runtime import AppRuntime


def get_runtime(request: Request) -> AppRuntime:
    return request.app.state.runtime
