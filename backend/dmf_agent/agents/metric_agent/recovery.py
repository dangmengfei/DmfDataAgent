"""限次自动修复重试（PRD FR6 报错分析处理的核心自愈机制）。

两类修复策略：
- `reparse_rule`：解析类错误（LLM 结构化失败）-> 退化为规则兜底抽取，最多重试
  `max_retries` 次（默认 2 次：第 1 次 LLM，第 2 次规则兜底）。
- `retry_query`：数据源瞬时错误（连接/查询超时）-> 短暂退避后重查，最多重试
  `datasource_max_retries` 次（默认 2 次）。

无论是否自愈成功，都通过 `on_recovery` 回调把这次错误（含是否恢复、尝试次数）
回写到「观测样本池」，供后续分析自愈成功率。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from dmf_agent.agents.metric_agent.errors import (
    AgentError,
    ParseError,
    build_error_sample,
    classify_exception,
)
from dmf_agent.config.settings import RecoverySettings

logger = logging.getLogger(__name__)

# on_recovery(error, recovered, attempts) -> 上报到样本池（best-effort）
RecoveryHook = Optional[Callable[[AgentError, bool, int], Awaitable[None]]]


async def recover_parse(
    node: str,
    primary: Callable[[], Awaitable[dict]],
    fallback: Callable[[], Awaitable[dict]],
    settings: Optional[RecoverySettings] = None,
    *,
    on_recovery: RecoveryHook = None,
) -> dict:
    settings = settings or RecoverySettings()
    """解析类节点的限次自愈：先 `primary`（LLM），失败分类为 ParseError，
    再按策略重试 `fallback`（规则兜底）。

    返回最终节点输出 dict；`fallback` 也失败则抛出 ParseError（供上层采样/上报）。
    """
    try:
        return await primary()
    except Exception as exc:  # noqa: BLE001 - LLM 失败，进入自愈
        error = ParseError(str(exc) or "LLM 解析失败", code="E1001", node=node, detail=repr(exc))

    attempts = 1
    while attempts <= settings.max_retries:
        attempts += 1
        try:
            result = await fallback()
            if on_recovery is not None:
                try:
                    await on_recovery(error, True, attempts)
                except Exception:  # noqa: BLE001 - 采样失败不影响主链路
                    logger.warning("错误样本记录失败（忽略）", exc_info=True)
            return result
        except Exception as exc2:  # noqa: BLE001 - 兜底也失败
            error = ParseError(
                str(exc2) or "规则兜底仍失败", code="E1001", node=node, detail=repr(exc2)
            )

    if on_recovery is not None:
        try:
            await on_recovery(error, False, attempts)
        except Exception:  # noqa: BLE001
            logger.warning("错误样本记录失败（忽略）", exc_info=True)
    raise error


async def recover_datasource(
    query_fn: Callable[[], Awaitable[dict]],
    settings: Optional[RecoverySettings] = None,
    *,
    node: str = "execute_query",
    on_recovery: RecoveryHook = None,
) -> tuple[Optional[dict], Optional[AgentError], int]:
    settings = settings or RecoverySettings()
    """数据源瞬时错误的限次自愈：失败分类为 DataSourceError（可重试），
    退避后重查最多 `datasource_max_retries` 次。

    返回 (result, error, attempts)：
    - 成功时 result 非空、error 为 None（首次成功则 attempts=1，自愈成功则 >1）；
    - 耗尽时 result 为 None、error 为最终的 DataSourceError；
    - 调用方据此构造友好错误结果，并只在「最终有错误」时采样一次（自愈成功/失败各一条）。
    """
    attempts = 0
    last_error: Optional[AgentError] = None
    while attempts <= settings.datasource_max_retries:
        attempts += 1
        try:
            return await query_fn(), None, attempts
        except Exception as exc:  # noqa: BLE001
            error = classify_exception(exc, node=node)
            last_error = error
            if not error.retryable or attempts > settings.datasource_max_retries:
                if on_recovery is not None:
                    try:
                        await on_recovery(error, False, attempts)
                    except Exception:  # noqa: BLE001
                        logger.warning("错误样本记录失败（忽略）", exc_info=True)
                return None, error, attempts
            logger.warning(
                "数据源查询失败（第 %d/%d 次），%.2fs 后重试：%s",
                attempts,
                settings.datasource_max_retries,
                settings.backoff_seconds,
                error.code,
            )
            await asyncio.sleep(settings.backoff_seconds)

    return None, last_error, attempts


def make_recovery_hook(
    repo: Optional[Any], ctx: dict, *, only_recovered: bool = True
) -> RecoveryHook:
    """构造把错误写进样本池的回调。

    - repo 为 None 时返回 None，不采样；
    - only_recovered=True（默认）：只回写「自愈成功」的样本，避免与服务层
      （未捕获异常的统一采样）重复计数；解析类节点用此模式。
    - only_recovered=False：成功与失败都回写（数据源节点自行决定最终只采样一次时用）。
    """
    if repo is None:
        return None

    async def _hook(error: AgentError, recovered: bool, attempts: int) -> None:
        if only_recovered and not recovered:
            return
        await repo.add(build_error_sample(error, recovered=recovered, attempts=attempts, ctx=ctx))

    return _hook
