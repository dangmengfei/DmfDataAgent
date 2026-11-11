"""FR15 输出约束（output_schema）测试：声明式 Schema 强制解析 + 重试 + 降级 + 审计。"""

from __future__ import annotations

from typing import Any, Dict

import pytest
from pydantic import BaseModel

from dmf_agent.config.settings import HarnessSettings
from dmf_agent.harness import (
    HarnessContext,
    HarnessController,
    HarnessState,
    HarnessViolationError,
    enforce_output_schema,
)
from dmf_agent.harness.registry import ToolRegistry


class EchoOut(BaseModel):
    text: str


class NumOut(BaseModel):
    value: int


def make_controller(settings=None, registry=None, auditor=None) -> HarnessController:
    return HarnessController(settings or HarnessSettings(), registry or ToolRegistry(), auditor)


# ---- registry 层声明与校验 ----

async def test_registry_output_schema_declaration():
    reg = ToolRegistry()
    reg.register("a", "desc", output_schema=EchoOut, output_retries=2)
    spec = reg.get("a")
    assert spec.output_schema is EchoOut
    assert spec.output_retries == 2
    assert reg.validate_output("a", {"text": "x"})["ok"] is True
    assert reg.validate_output("a", {"text": 1})["ok"] is False
    out = reg.coerce_output("a", {"text": "y"})
    assert isinstance(out, EchoOut) and out.text == "y"


# ---- controller.invoke 强制解析 / 重试 / 降级 ----

async def test_invoke_enforces_and_coerces():
    reg = ToolRegistry()
    reg.register("echo", "echo", output_schema=EchoOut)
    calls = []

    async def fn() -> Dict[str, Any]:
        calls.append(1)
        return {"text": "hi"}

    c = make_controller(registry=reg)
    res = await c.invoke("echo", {}, HarnessContext(), HarnessState(), fn)
    assert res == {"text": "hi"}
    assert len(calls) == 1


async def test_invoke_retries_transient_invalid():
    reg = ToolRegistry()
    reg.register("num", "num", output_schema=NumOut, output_retries=1)
    seq = [{"value": "bad"}, {"value": 5}]

    async def fn() -> Dict[str, Any]:
        return seq.pop(0)

    c = make_controller(registry=reg)
    res = await c.invoke("num", {}, HarnessContext(), HarnessState(), fn)
    assert res == {"value": 5}


async def test_invoke_degrades_after_retries():
    reg = ToolRegistry()
    reg.register("num", "num", output_schema=NumOut, output_retries=1)

    async def fn() -> Dict[str, Any]:
        return {"value": "stillbad"}

    c = make_controller(registry=reg)
    with pytest.raises(HarnessViolationError) as ei:
        await c.invoke("num", {}, HarnessContext(), HarnessState(), fn)
    assert ei.value.code == "HARNESS_OUTPUT_SCHEMA"


async def test_invoke_no_schema_unchanged():
    reg = ToolRegistry()
    reg.register("raw", "raw")

    async def fn() -> Dict[str, Any]:
        return {"anything": 123, "extra": True}

    c = make_controller(registry=reg)
    res = await c.invoke("raw", {}, HarnessContext(), HarnessState(), fn)
    assert res == {"anything": 123, "extra": True}


async def test_disabled_harness_skips_enforcement():
    reg = ToolRegistry()
    reg.register("num", "num", output_schema=NumOut)

    async def fn() -> Dict[str, Any]:
        return {"value": "notint"}

    c = make_controller(settings=HarnessSettings(enabled=False), registry=reg)
    res = await c.invoke("num", {}, HarnessContext(), HarnessState(), fn)
    assert res == {"value": "notint"}


# ---- 独立 helper：enforce_output_schema ----

async def test_enforce_output_schema_standalone_retry():
    # 初始 payload 非法；重试时 fn 返回合法 payload
    seq = [{"value": 7}]

    async def fn() -> Dict[str, Any]:
        return seq.pop(0)

    out = await enforce_output_schema(NumOut, {"value": "x"}, retries=1, fn=fn)
    assert isinstance(out, NumOut) and out.value == 7


async def test_enforce_output_schema_standalone_fail():
    async def fn() -> Dict[str, Any]:
        return {"value": "bad"}

    with pytest.raises(HarnessViolationError):
        await enforce_output_schema(NumOut, {"value": "bad"}, retries=1, fn=fn)


# ---- 审计 ----

async def test_auditor_receives_schema_error():
    events: list = []

    async def aud(e: Dict[str, Any]) -> None:
        events.append(e)

    reg = ToolRegistry()
    reg.register("num", "num", output_schema=NumOut, output_retries=0)

    async def fn() -> Dict[str, Any]:
        return {"value": "bad"}

    c = make_controller(registry=reg, auditor=aud)
    with pytest.raises(HarnessViolationError):
        await c.invoke("num", {}, HarnessContext(), HarnessState(), fn)
    assert any(e.get("status") == "output_schema_error" for e in events)
