"""Few-Shot 样本管理（仓储 + 管理路由 + 试召回）单测。

不依赖完整 app 启动（milvus 等重资源），直接对仓储层与路由函数做
内存模式集成测试（与 tests/test_feedback.py 同一套手法）。

**调用路由函数时的坑**：直接调用会绕过 FastAPI 依赖注入，形如
`x: Optional[str] = Query(None)` 的参数默认值就是那个 `Query` 对象（真值！），
若漏传就会当成过滤器命中 0 条 —— 因此调用带 Query 参数的路由函数必须
**显式传全每个参数**（下文的 list_* / list_l3 调用都按此写）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from dmf_agent.api.routers import fewshots as fs_router
from dmf_agent.db.factory import DatabaseBundle
from dmf_agent.schemas.feedback import (
    FewShotData,
    FewShotImportRequest,
    FewShotPatch,
    FewShotRecallRequest,
    FewShotUpsert,
)


def _runtime() -> SimpleNamespace:
    return SimpleNamespace(db=DatabaseBundle(mode="memory"))


def _manual(question: str, answer: str = "A", intent: str = "metric_query", **kw) -> FewShotData:
    return FewShotData(
        id=0, session_id="", message_id="", intent=intent, category="intention",
        question=question, answer=answer, score=1, source="manual", **kw,
    )


# ---------- 仓储层 ----------


async def test_repo_query_filters_and_paging():
    db = DatabaseBundle(mode="memory")
    await db.few_shot_repo.add(_manual("查一下近7天的GMV趋势"))
    await db.few_shot_repo.add(_manual("各分行存款排名", intent="dimension_query"))
    await db.few_shot_repo.add(
        FewShotData(
            id=0, session_id="s1", message_id="m1", intent="diagnosis",
            category="intention", question="为什么贷款余额下降", answer="B",
            score=1, source="like",
        )
    )

    items, total = await db.few_shot_repo.query(limit=10)
    assert total == 3 and len(items) == 3

    only_manual, mt = await db.few_shot_repo.query(source="manual", limit=10)
    assert mt == 2 and all(i.source == "manual" for i in only_manual)

    only_like, lt = await db.few_shot_repo.query(source="like", limit=10)
    assert lt == 1 and only_like[0].intent == "diagnosis"

    hit, ht = await db.few_shot_repo.query(keyword="存款", limit=10)
    assert ht == 1 and "存款" in hit[0].question

    # 分页
    page2, _ = await db.few_shot_repo.query(limit=1, offset=1)
    assert len(page2) == 1

    assert await db.few_shot_repo.count() == 3
    assert await db.few_shot_repo.count(source="manual") == 2


async def test_repo_update_delete_and_find_by_question():
    db = DatabaseBundle(mode="memory")
    ex = await db.few_shot_repo.add(_manual("各分行存款排名"))
    got = await db.few_shot_repo.get_by_id(ex.id)
    assert got is not None and got.id == ex.id

    # 部分更新：只改 status，其余不动
    upd = await db.few_shot_repo.update(ex.id, status="disabled")
    assert upd is not None and upd.status == "disabled" and upd.question == "各分行存款排名"

    assert (await db.few_shot_repo.find_by_question("各分行存款排名", "intention")) is not None
    assert (await db.few_shot_repo.find_by_question("不存在的问题")) is None

    assert await db.few_shot_repo.delete(ex.id) is True
    assert await db.few_shot_repo.delete(ex.id) is False
    assert await db.few_shot_repo.get_by_id(ex.id) is None


async def test_repo_like_dedupe_by_message_keeps_single_row():
    """同一条消息重复点赞仍只占一行（score 累加），不因新增 source 字段退化。"""
    db = DatabaseBundle(mode="memory")
    for _ in range(3):
        await db.few_shot_repo.add(
            FewShotData(
                id=0, session_id="s1", message_id="m1", intent="metric_query",
                question="查一下GMV", answer="A",
            )
        )
    items = await db.few_shot_repo.list_all()
    assert len(items) == 1 and items[0].score == 3


async def test_repo_disabled_excluded_from_recall():
    """停用的样本不参与召回，但仍在列表里（可恢复）。"""
    db = DatabaseBundle(mode="memory")
    keep = await db.few_shot_repo.add(_manual("查一下近7天的GMV趋势"))
    off = await db.few_shot_repo.add(_manual("查一下近7天的GMV趋势是多少"))
    await db.few_shot_repo.update(off.id, status="disabled")

    forced = SimpleNamespace(db=db)
    res = await fs_router.recall_few_shots(
        FewShotRecallRequest(query="查一下近7天GMV趋势是多少", top_k=5), forced
    )
    ids = {h.id for h in res.hits}
    assert keep.id in ids
    assert off.id not in ids
    # 列表口径仍能看到停用样本
    _items, total = await db.few_shot_repo.query(limit=10)
    assert total == 2


async def test_recall_dedupes_few_shot_and_domain_mirror():
    """同一问答对在 few_shot_examples 与 domain_memories 各存一份时只召回一次。

    点赞会双写两表（见 feedback 路由），若不去重，同一问题会占掉 top_k 里两个名额。
    """
    from dmf_agent.memory.retriever import MemoryRetriever
    from dmf_agent.schemas.memory import DomainMemoryData

    db = DatabaseBundle(mode="memory")
    await db.few_shot_repo.add(_manual("查一下近7天的GMV趋势", answer="A"))
    await db.domain_memory_repo.add(
        DomainMemoryData(
            id=0, domain="metric_query", type="few_shot",
            question="查一下近7天的GMV趋势", answer="A", intent="metric_query",
            weight=1.0, status="approved", created_by="s1",
        )
    )
    retriever = MemoryRetriever(db.few_shot_repo, db.domain_memory_repo, top_k=5)
    outcome = await retriever.recall("查一下近7天的GMV趋势", top_k=5)
    assert outcome.candidates == 1  # 去重后只剩一条候选
    questions = [h.question for h in outcome.hits]
    assert questions.count("查一下近7天的GMV趋势") == 1


# ---------- 路由层 ----------


async def test_router_crud_roundtrip():
    rt = _runtime()
    created = await fs_router.create_few_shot(
        FewShotUpsert(question="各分行存款余额排名", answer="SELECT ...", intent="metric_query",
                      tags="存款,排名", weight=2.0),
        rt,
    )
    assert created.id >= 1 and created.source == "manual" and created.status == "active"
    assert created.tags == "存款,排名" and created.weight == 2.0

    # 同类目同一问题重复新增 -> 409
    with pytest.raises(HTTPException) as ei:
        await fs_router.create_few_shot(
            FewShotUpsert(question="各分行存款余额排名", answer="X"), rt
        )
    assert ei.value.status_code == 409

    listed = await fs_router.list_few_shots(
        category=None, intent=None, source=None, status=None,
        keyword="", limit=50, offset=0, runtime=rt,
    )
    assert listed["total"] == 1 and listed["items"][0]["question"] == "各分行存款余额排名"

    detail = await fs_router.get_few_shot(created.id, rt)
    assert detail.id == created.id

    updated = await fs_router.update_few_shot(
        created.id, FewShotPatch(status="disabled", tags="存款"), rt
    )
    assert updated.status == "disabled" and updated.tags == "存款"

    out = await fs_router.delete_few_shot(created.id, rt)
    assert out["deleted"] is True
    with pytest.raises(HTTPException):
        await fs_router.get_few_shot(created.id, rt)


async def test_router_recall_preview_returns_injected_text():
    rt = _runtime()
    await fs_router.create_few_shot(
        FewShotUpsert(question="查一下近7天的GMV趋势", answer="A", intent="metric_query"), rt
    )
    res = await fs_router.recall_few_shots(
        FewShotRecallRequest(query="查一下近7天GMV趋势是多少", top_k=3), rt
    )
    assert res.candidates == 1
    assert len(res.hits) == 1
    assert res.hits[0].intent == "metric_query"
    assert res.hits[0].bm25 > 0 and res.hits[0].rerank > 0
    # 注入文本与线上意图识别同源（含标题行与"识别意图"）
    assert "已验证样本" in res.injected_text
    assert "查一下近7天的GMV趋势" in res.injected_text
    assert "识别意图：metric_query" in res.injected_text


async def test_router_recall_fail_on_empty():
    rt = _runtime()
    with pytest.raises(HTTPException) as ei:
        await fs_router.recall_few_shots(
            FewShotRecallRequest(query="完全无关的问题", top_k=3, fail_on_empty=True), rt
        )
    assert ei.value.status_code == 404


async def test_router_import_compatible_and_overwrite():
    """导入兼容两种字段命名，重复默认跳过、overwrite=true 才覆盖。"""
    rt = _runtime()
    res = await fs_router.import_few_shots(
        FewShotImportRequest(
            items=[
                {"question": "各分行存款排名", "answer": "A", "intent": "metric_query"},
                # 旧 experience JSON 的 query/content 命名
                {"query": "画个图看趋势", "content": "B"},
                {"answer": "缺少问题字段，应被跳过"},
                "不是对象",
            ],
            category="intention",
        ),
        rt,
    )
    assert res.created == 2 and res.skipped == 0 and len(res.errors) == 2

    # 重复导入：默认跳过
    again = await fs_router.import_few_shots(
        FewShotImportRequest(items=[{"question": "各分行存款排名", "answer": "NEW"}]), rt
    )
    assert again.skipped == 1 and again.created == 0
    items, _ = await rt.db.few_shot_repo.query(keyword="各分行存款排名", limit=5)
    assert items[0].answer == "A"

    # overwrite=true：覆盖
    forced = await fs_router.import_few_shots(
        FewShotImportRequest(
            items=[{"question": "各分行存款排名", "answer": "NEW"}], overwrite=True
        ),
        rt,
    )
    assert forced.updated == 1
    items2, _ = await rt.db.few_shot_repo.query(keyword="各分行存款排名", limit=5)
    assert items2[0].answer == "NEW"


async def test_router_export_shape():
    from dmf_agent.schemas.auth import CurrentUser

    rt = _runtime()
    await fs_router.create_few_shot(
        FewShotUpsert(question="各分行存款排名", answer="A", intent="metric_query", tags="存款"), rt
    )
    admin = CurrentUser(
        id="1", username="admin", tenant_id="t1", role="admin", permissions=["*"]
    )
    payload = await fs_router.export_few_shots(status=None, current=admin, runtime=rt)
    assert payload["version"] == fs_router.EXPORT_VERSION
    assert payload["count"] == 1
    row = payload["items"][0]
    # 导出字段与导入解析字段对齐（question/answer 可直接回灌）
    for key in ("question", "answer", "intent", "category", "tags", "weight", "status", "source"):
        assert key in row
    assert row["source"] == "manual"
    # FR13 治理：默认开水印（导出人/行数随文件流出），默认不开审批闸门
    assert payload["watermark"]["exported_by"] == "admin"
    assert payload["watermark"]["tenant_id"] == "t1"
    assert payload["watermark"]["rows"] == 1
    assert payload["watermark"]["truncated"] is False


async def test_router_stats_and_facets():
    rt = _runtime()
    await fs_router.create_few_shot(
        FewShotUpsert(question="查一下GMV", intent="metric_query", category="intention", tags="gmv"), rt
    )
    await rt.db.few_shot_repo.add(
        FewShotData(
            id=0, session_id="s1", message_id="m1", intent="diagnosis",
            category="intention", question="为什么下降", answer="B",
        )
    )
    stats = await fs_router.few_shot_stats(rt)
    assert stats["total"] == 2 and stats["manual"] == 1 and stats["like"] == 1
    assert stats["active"] == 2 and stats["disabled"] == 0

    facets = await fs_router.few_shot_facets(rt)
    assert facets["categories"] == ["intention"]
    assert sorted(facets["intents"]) == ["diagnosis", "metric_query"]
    assert facets["tags"] == ["gmv"]
