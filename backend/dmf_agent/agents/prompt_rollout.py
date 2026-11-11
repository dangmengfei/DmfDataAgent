"""FR12 提示词灰度路由：请求上下文分桶 + 金丝雀选择。

- RolloutContext：一次请求的用户 / 租户 / 部门 / 会话标识，经 ContextVar 注入，
  对 load_prompt 调用方透明（由 prompt_loader 的 resolver 钩子消费）。
- bucket：基于 (name, user_id) 的确定性哈希，保证同一用户始终落入同一桶（稳定可复现）。
- choose_canary：从某模板全量版本中挑出「最新金丝雀」版本供灰度命中时返回。
- build_resolver：构造注入 prompt_loader 的 resolver，命中金丝雀时返回其内容，否则回落默认。
- EvalGateError：金丝雀晋级 full 前的评测门禁异常（也可经 manager.promote 返回 ok=False）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from contextvars import ContextVar


@dataclass
class RolloutContext:
    user_id: str = ""
    tenant_id: str = ""
    department: str = ""
    session_id: str = ""


_ROLL_CTX: ContextVar[Optional[RolloutContext]] = ContextVar(
    "dmf_prompt_rollout_ctx", default=None
)


def set_rollout_context(ctx: Optional[RolloutContext]) -> None:
    _ROLL_CTX.set(ctx)


def get_rollout_context() -> Optional[RolloutContext]:
    return _ROLL_CTX.get()


def reset_rollout_context() -> None:
    _ROLL_CTX.set(None)


def bucket(name: str, user_id: str, gray_ratio: float) -> bool:
    """返回 True 表示该 (name, user) 落入金丝雀桶。

    gray_ratio=1.0 全量命中，0.0 永不命中；其余按 md5 哈希稳定分桶。
    """
    if gray_ratio >= 1.0:
        return True
    if gray_ratio <= 0.0:
        return False
    h = hashlib.md5(f"{name}:{user_id}".encode("utf-8")).hexdigest()
    frac = int(h[:8], 16) / 0xFFFFFFFF
    return frac < gray_ratio


def choose_canary(versions: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """从某模板的版本列表中挑出最新的 rollout_status=='canary' 版本（无则 None）。"""
    import dmf_agent.agents.prompt_loader as pl

    canaries = [v for v in versions if v.get("rollout_status") == "canary"]
    if not canaries:
        return None
    canaries.sort(key=lambda v: pl.version_key(v.get("version", "0.0.0")))
    return canaries[-1]


def build_resolver(
    canary_registry: Dict[str, Dict[str, Any]],
    gray_enabled: bool = True,
) -> Callable[[str], Optional[Dict[str, Any]]]:
    """构造注入 prompt_loader 的 resolver：命中金丝雀返回其内容，否则 None（回落默认覆盖）。"""

    def resolver(name: str) -> Optional[Dict[str, Any]]:
        if not gray_enabled:
            return None
        entry = canary_registry.get(name)
        if not entry:
            return None
        ctx = get_rollout_context()
        if ctx is None or not ctx.user_id:
            return None
        ratio = float(entry.get("gray_ratio", 1.0))
        if bucket(name, ctx.user_id, ratio):
            return {"system": entry.get("system", ""), "human": entry.get("human", "")}
        return None

    return resolver


class EvalGateError(Exception):
    """评测门禁：金丝雀晋级 full 前必须通过评测。"""
