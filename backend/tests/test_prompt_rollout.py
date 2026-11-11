"""FR12 提示词 5 层落库 + 灰度路由 + 评测门禁 + A/B 对比测试。

全部基于内存态 PromptVersionStore + PromptManager（与运行环境一致：MySQL 不可用时降级内存），
因此无需真实 MySQL 即可验证 FR12 机制闭环。
"""

from __future__ import annotations

import pytest

from dmf_agent.agents import prompt_loader
from dmf_agent.agents.prompt_rollout import (
    RolloutContext,
    build_resolver,
    bucket,
    choose_canary,
    reset_rollout_context,
    set_rollout_context,
)
from dmf_agent.db.stores import PromptVersionStore
from dmf_agent.services.prompts import PromptManager


def _store() -> PromptVersionStore:
    return PromptVersionStore()  # 内存态


@pytest.fixture(autouse=True)
def _clean_overrides():
    yield
    prompt_loader.clear_overrides()
    prompt_loader.clear_rollout_resolver()
    reset_rollout_context()


async def _seed(manager: PromptManager, name: str, content: str, **kw):
    return await manager.update(name, content, description="seed", **kw)


async def test_layer_roundtrip_and_rollout_fields():
    store = _store()
    mgr = PromptManager(store)
    res = await _seed(mgr, "intent", "你是指引", layer="task", rollout_status="full", gray_ratio=0.2, eval_passed=True)
    assert res["ok"]
    rows = await store.load_by_name("intent")
    assert rows[0]["layer"] == "task"
    assert rows[0]["rollout_status"] == "full"
    assert abs(rows[0]["gray_ratio"] - 0.2) < 1e-9
    assert rows[0]["eval_passed"] is True
    # 列表也应携带 layer
    lst = await mgr.list()
    item = next(i for i in lst["items"] if i["name"] == "intent")
    assert item["layer"] == "task"
    assert item["gray_ratio"] == 0.2


def test_bucket_boundaries():
    assert bucket("intent", "u1", 1.0) is True
    assert bucket("intent", "u1", 0.0) is False
    # 确定性：同一 (name,user) 始终一致
    assert bucket("intent", "alice", 0.3) == bucket("intent", "alice", 0.3)
    # 不同 user 至少可能不同（不强等）
    assert isinstance(bucket("intent", "bob", 0.5), bool)


def test_choose_canary_picks_latest():
    versions = [
        {"version": "1.0.1", "rollout_status": "full"},
        {"version": "1.0.2", "rollout_status": "canary"},
        {"version": "1.0.3", "rollout_status": "canary"},
        {"version": "1.0.4", "rollout_status": "full"},
    ]
    canary = choose_canary(versions)
    assert canary["version"] == "1.0.3"


def test_resolver_returns_canary_for_bucketed_user():
    registry = {"intent": {"system": "金丝雀内容", "human": "", "gray_ratio": 1.0}}
    resolver = build_resolver(registry, gray_enabled=True)
    set_rollout_context(RolloutContext(user_id="u1"))
    out = resolver("intent")
    assert out == {"system": "金丝雀内容", "human": ""}


def test_resolver_none_when_no_context():
    registry = {"intent": {"system": "金丝雀内容", "human": "", "gray_ratio": 1.0}}
    resolver = build_resolver(registry, gray_enabled=True)
    reset_rollout_context()
    assert resolver("intent") is None


def test_resolver_none_when_gray_disabled():
    registry = {"intent": {"system": "金丝雀内容", "human": "", "gray_ratio": 1.0}}
    resolver = build_resolver(registry, gray_enabled=False)
    set_rollout_context(RolloutContext(user_id="u1"))
    assert resolver("intent") is None


async def test_load_prompt_uses_canary_via_resolver():
    store = _store()
    mgr = PromptManager(store)
    # 全量版本
    await _seed(mgr, "intent", "全量内容", layer="task")
    # 金丝雀版本（同名更高 patch）
    await _seed(mgr, "intent", "金丝雀内容", layer="task", rollout_status="canary", gray_ratio=1.0)
    await mgr.reload()
    # 命中金丝雀的用户上下文
    set_rollout_context(RolloutContext(user_id="uX"))
    loaded = prompt_loader.load_prompt("intent")
    assert loaded["system"] == "金丝雀内容"
    # 无上下文回落全量
    reset_rollout_context()
    loaded2 = prompt_loader.load_prompt("intent")
    assert loaded2["system"] == "全量内容"


async def test_eval_gate_blocks_promotion():
    store = _store()
    mgr = PromptManager(store)
    await _seed(mgr, "intent", "全量", layer="task")
    res = await _seed(mgr, "intent", "金丝雀", layer="task", rollout_status="canary", gray_ratio=0.1, eval_passed=False)
    canary_version = res["version"]
    # 未通过评测 -> 晋级被门禁拦下
    blocked = await mgr.promote("intent", canary_version)
    assert blocked["ok"] is False
    assert blocked["code"] == "EVAL_GATE"
    # 记录评测通过 -> 晋级成功
    ev = await mgr.mark_eval("intent", canary_version, passed=True)
    assert ev["ok"]
    ok = await mgr.promote("intent", canary_version)
    assert ok["ok"] is True


async def test_ab_compare_returns_both():
    store = _store()
    mgr = PromptManager(store)
    a = await _seed(mgr, "intent", "版本A内容", layer="task")
    b = await _seed(mgr, "intent", "版本B内容", layer="task")
    cmp = await mgr.ab_compare("intent", a["version"], b["version"])
    assert cmp["ok"]
    assert cmp["a"]["system"] == "版本A内容"
    assert cmp["b"]["system"] == "版本B内容"
