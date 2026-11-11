"""记忆模块测试（mock 模式，无 LLM / 外部依赖）。"""

from dmf_agent.config.settings import Settings
from dmf_agent.db.memory import (
    InMemoryMemorySummaryRepository,
    InMemoryMessageRepository,
    InMemoryUserPreferenceRepository,
)
from dmf_agent.memory.history import HistoryAssembler, pair_rounds
from dmf_agent.schemas.session import MessageData

# 记忆分层改造（FR9）新增：L3 领域知识 + 召回重排 + 管理 API（导入集中在顶部）
import types as _types

from dmf_agent.api.routers import memory as _memory_router
from dmf_agent.schemas.auth import CurrentUser
from dmf_agent.db.memory import (
    InMemoryDomainMemoryRepository as _InMemDom,
    InMemoryFewShotRepository as _InMemFew,
)
from dmf_agent.memory.retriever import MemoryRetriever as _MemoryRetriever
from dmf_agent.schemas.feedback import FewShotData as _FewShotData
from dmf_agent.schemas.memory import DomainMemoryData as _DomainMemoryData


def _make_assembler(settings: Settings | None = None):
    settings = settings or Settings()
    msg_repo = InMemoryMessageRepository()
    sum_repo = InMemoryMemorySummaryRepository()
    assembler = HistoryAssembler(
        message_repo=msg_repo,
        summary_repo=sum_repo,
        settings=settings,
        get_chat_model=lambda: None,  # mock 模式
    )
    return assembler, msg_repo, sum_repo


async def _add_round(msg_repo, session_id: str, i: int) -> None:
    await msg_repo.add(
        MessageData(id=f"u{i}", session_id=session_id, role="user", content=f"问题{i}")
    )
    await msg_repo.add(
        MessageData(id=f"a{i}", session_id=session_id, role="assistant", content=f"回答{i}")
    )


async def test_pair_rounds():
    msgs = [
        MessageData(id="u1", session_id="s", role="user", content="q1", created_at=None),
        MessageData(id="a1", session_id="s", role="assistant", content="a1", created_at=None),
        MessageData(id="u2", session_id="s", role="user", content="q2", created_at=None),
        MessageData(id="a2", session_id="s", role="assistant", content="a2", created_at=None),
    ]
    rounds = pair_rounds(msgs)
    assert len(rounds) == 2
    assert rounds[0].user_query == "q1"
    assert rounds[0].assistant_answer == "a1"
    assert rounds[1].last_message_id == "a2"


async def test_unrelated_query_truncates_history():
    """不相关的新问题 -> 截断历史只带当前轮（老系统 rounds[-1:] 语义）。"""
    assembler, msg_repo, _ = _make_assembler()
    sid = "s1"
    for i in range(5):
        await _add_round(msg_repo, sid, i)
    assembled = await assembler.build(sid, "今天天气怎么样啊")
    assert assembled.related_to_history is False
    assert [m.__class__.__name__ for m in assembled.messages] == ["HumanMessage"]
    assert assembled.messages[0].content == "今天天气怎么样啊"


async def test_related_query_keeps_history():
    """指代词 -> 相关，历史保留并触发压缩（5 轮 >= 2+2）。"""
    assembler, msg_repo, _ = _make_assembler()
    sid = "s2"
    for i in range(5):
        await _add_round(msg_repo, sid, i)
    assembled = await assembler.build(sid, "再看看客单价")
    assert assembled.related_to_history is True
    assert assembled.rewritten_query == "再看看客单价"
    assert assembled.compressed is True
    # SystemMessage(摘要) + 保留 2 轮(4 条) + 当前问题 = 6 条
    assert len(assembled.messages) == 6
    assert assembled.messages[0].__class__.__name__ == "SystemMessage"
    assert assembled.messages[-1].content == "再看看客单价"


async def test_compression_persists_and_progresses():
    """摘要落库 + 增量压缩（不重复压缩已覆盖轮次）。"""
    settings = Settings()
    settings.memory.rounds_to_compress = 2
    settings.memory.rounds_to_retain = 2
    assembler, msg_repo, sum_repo = _make_assembler(settings)
    sid = "s3"

    # 6 轮历史 -> 压缩 4 轮保留 2 轮
    for i in range(6):
        await _add_round(msg_repo, sid, i)
    assembled = await assembler.build(sid, "再看看问题6的内容")
    assert assembled.compressed is True
    summary = await sum_repo.get(sid)
    assert summary is not None
    assert summary.upto_message_id == "a3"
    assert "问题0" in summary.summary and "问题3" in summary.summary

    # 再加 1 轮（未压缩 3 轮 < 4）-> 不再压缩
    await _add_round(msg_repo, sid, 6)
    assembled2 = await assembler.build(sid, "再看看问题7的内容")
    assert assembled2.compressed is False
    summary2 = await sum_repo.get(sid)
    assert summary2.upto_message_id == "a3"

    # 再加 1 轮（未压缩 4 轮 >= 4）-> 增量压缩 2 轮，摘要融合
    await _add_round(msg_repo, sid, 7)
    assembled3 = await assembler.build(sid, "再看看问题8的内容")
    assert assembled3.compressed is True
    summary3 = await sum_repo.get(sid)
    assert summary3.upto_message_id == "a5"
    # 旧摘要内容保留 + 新轮次融入
    assert "问题0" in summary3.summary and "问题4" in summary3.summary


async def test_memory_disabled_keeps_legacy_behavior():
    """memory.enabled=False -> 全量历史 + 原始输入（兼容开关）。"""
    settings = Settings()
    settings.memory.enabled = False
    assembler, msg_repo, _ = _make_assembler(settings)
    sid = "s4"
    for i in range(3):
        await _add_round(msg_repo, sid, i)
    assembled = await assembler.build(sid, "查一下GMV")
    # 3 轮(6 条) + 当前 = 7 条，无摘要 SystemMessage
    assert len(assembled.messages) == 7
    assert assembled.rewritten_query == "查一下GMV"
    assert not any(m.__class__.__name__ == "SystemMessage" for m in assembled.messages)


# ===========================================================================
# 记忆分层改造（FR9）新增：L3 领域知识 + 召回重排 + 管理 API
# ===========================================================================

async def test_domain_memory_repo_crud_and_filter():
    repo = _InMemDom()
    a = await repo.add(
        _DomainMemoryData(
            id=0, domain="metric_query", type="few_shot",
            question="查一下近7天的GMV趋势", answer="按日聚合", intent="metric_query",
            weight=1.0, status="approved", created_by="s1",
        )
    )
    await repo.add(
        _DomainMemoryData(
            id=0, domain="metric_query", type="rule",
            question="季度末要复核口径", answer="", intent=None,
            weight=2.0, status="pending", created_by="admin",
        )
    )
    assert a.id == 1

    all_items = await repo.list_all()
    assert len(all_items) == 2

    # list_approved 仅返回 approved
    approved = await repo.list_approved()
    assert len(approved) == 1 and approved[0].id == 1

    approved_domain = await repo.list_approved(domain="metric_query", type="few_shot")
    assert len(approved_domain) == 1

    got = await repo.get_by_id(1)
    assert got is not None and got.question.startswith("查一下近7天")

    await repo.delete(1)
    assert await repo.get_by_id(1) is None


async def test_recall_l3_merges_few_shot_and_domain():
    few = _InMemFew()
    await few.add(
        _FewShotData(
            id=0, session_id="s1", message_id="m1", intent="metric_query",
            category="intention", question="查一下近7天的GMV趋势", answer="按日聚合", score=2,
        )
    )
    dom = _InMemDom()
    await dom.add(
        _DomainMemoryData(
            id=0, domain="metric_query", type="rule",
            question="各分行存款余额排名怎么算", answer="", intent="metric_query",
            weight=1.0, status="approved", created_by="admin",
        )
    )
    retriever = _MemoryRetriever(few, dom, top_k=3)
    out = await retriever.recall_l3("查一下近7天的GMV趋势")
    assert "GMV趋势" in out, out
    assert "识别意图：metric_query" in out, out


async def test_recall_l3_empty_when_repos_none():
    retriever = _MemoryRetriever(None, None, top_k=3)
    assert await retriever.recall_l3("任意问题") == ""


def _fake_runtime():
    db = _types.SimpleNamespace(
        memory_repo=InMemoryMemorySummaryRepository(),
        domain_memory_repo=_InMemDom(),
        preference_repo=InMemoryUserPreferenceRepository(),
    )
    # 鉴权关闭：清会话不做归属校验，user_id 走显式传参（保持本地调试语义）
    settings = _types.SimpleNamespace(auth=_types.SimpleNamespace(enabled=False))
    return _types.SimpleNamespace(db=db, settings=settings, memory_disabled={})


def _anon_user():
    """鉴权关闭时的合成用户（id=anonymous）。"""
    return CurrentUser(id="anonymous", username="anonymous", tenant_id="", role="admin")


async def test_memory_router_clear_l1_and_disable_and_l3():
    rt = _fake_runtime()

    await rt.db.domain_memory_repo.add(
        _DomainMemoryData(
            id=0, domain="metric_query", type="few_shot",
            question="查一下近7天的GMV趋势", answer="按日聚合", intent="metric_query",
            weight=1.0, status="approved", created_by="s1",
        )
    )
    # 直接调用路由函数会绕过 DI：`x = Query(None)` 的默认值是 Query 对象本身（真值），
    # 漏传会被当成过滤器 -> 命中 0 条。带 Query 参数的路由必须显式传全。
    l3 = await _memory_router.list_l3(
        domain=None, type=None, status=None, keyword="", include_few_shot=True,
        limit=50, offset=0, runtime=rt,
    )
    assert len(l3) == 1

    await _memory_router.set_memory_disabled(
        {"user_id": "u1", "disabled": True}, current=_anon_user(), runtime=rt
    )
    st = await _memory_router.get_memory_disabled(user_id="u1", current=_anon_user(), runtime=rt)
    assert st["disabled"] is True

    await _memory_router.set_memory_disabled(
        {"user_id": "u1", "disabled": False}, current=_anon_user(), runtime=rt
    )
    st2 = await _memory_router.get_memory_disabled(user_id="u1", current=_anon_user(), runtime=rt)
    assert st2["disabled"] is False

    res = await _memory_router.clear_session_memory("sess-x", current=_anon_user(), runtime=rt)
    assert res["ok"] is True and res["session_id"] == "sess-x"
