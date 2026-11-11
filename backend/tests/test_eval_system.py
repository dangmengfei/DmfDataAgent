"""P1-7 评测体系测试：LLM-as-judge / 数据集版本化 / 聚合指标 / badcase 回流 / FR12 门禁联动。

全部基于内存态（无 LLM / 无 MySQL），与运行环境降级路径一致。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dmf_agent.eval.badcase import backflow_from_feedback, badcase_store
from dmf_agent.eval.dataset import (
    EvalCase,
    EvalDataset,
    add_cases_to_dataset,
    get_or_seed_default,
    list_datasets,
    load_dataset,
    save_dataset,
    snapshot_dataset,
)
from dmf_agent.eval.judge import JudgeResult, JudgeRubric, deterministic_judge, judge_answer
from dmf_agent.eval.metrics import EvalCaseResult, compute_metrics
from dmf_agent.eval.runner import run_suite_with_judge


# ---------------------------------------------------------------------------
# 1. judge：确定性兜底（无 LLM）
# ---------------------------------------------------------------------------

def test_deterministic_judge_no_reference_passes():
    j = deterministic_judge("查一下GMV", "", "GMV 近 7 天上升")
    assert j.source == "deterministic"
    assert j.hallucination is False
    assert 0.0 <= j.faithfulness <= 1.0


def test_deterministic_judge_detects_hallucination():
    # 参考与回答几乎无重叠 -> 疑似幻觉
    j = deterministic_judge(
        "各分行存款排名",
        "上海分行存款 120 亿居首，北京 98 亿次之",
        "今天天气真好，适合开会",
    )
    assert j.hallucination is True
    assert j.faithfulness < 0.15


def test_deterministic_judge_high_overlap_no_hallucination():
    j = deterministic_judge(
        "存款趋势",
        "近 7 天存款稳步上升，环比 +3%",
        "近 7 天存款稳步上升，环比 +3%",
    )
    assert j.hallucination is False
    assert j.faithfulness > 0.8


@pytest.mark.asyncio
async def test_judge_with_injected_llm():
    class _FakeStructured:
        def __init__(self, result):
            self._result = result

        async def ainvoke(self, messages, **kwargs):
            return self._result

    class _FakeLLM:
        def __init__(self, result):
            self._result = result

        def with_structured_output(self, schema, method=None):
            return _FakeStructured(self._result)

    rubric = JudgeRubric(faithfulness=0.9, hallucination=False, accuracy=0.85,
                         rationale="测试")
    res = await judge_answer(_FakeLLM(rubric), "q", "ref", "ans")
    assert res.source == "llm"
    assert res.faithfulness == 0.9
    assert res.hallucination is False


# ---------------------------------------------------------------------------
# 2. 数据集版本化
# ---------------------------------------------------------------------------

def test_dataset_seed_and_versioning(tmp_path):
    ds = get_or_seed_default()
    assert ds.version == "1.0.0"
    assert len(ds.cases) >= 6
    # 再次获取应命中已播种（不重复建）
    assert load_dataset("1.0.0") is not None
    # 快照派生新版本
    snap = snapshot_dataset("1.0.0", "1.1.0", note="test-snap")
    assert snap.version == "1.1.0"
    assert len(snap.cases) == len(ds.cases)
    versions = {d["version"] for d in list_datasets()}
    assert "1.1.0" in versions


def test_dataset_add_cases_dedup():
    # 用独立版本避免共享 data/eval 文件被多次运行污染
    ver = "dedup-test-1.0.0"
    save_dataset(EvalDataset(version=ver, cases=[
        EvalCase(name="base", query="q", expected_intent="metric_query"),
    ]))
    before = len(load_dataset(ver).cases)
    add_cases_to_dataset(
        ver,
        [EvalCase(name="extra-1", query="q1", category="rank", expected_intent="metric_query")],
    )
    after = load_dataset(ver).cases
    assert len(after) == before + 1
    # 同名去重
    add_cases_to_dataset(
        ver,
        [EvalCase(name="extra-1", query="q1", category="rank", expected_intent="metric_query")],
    )
    assert len(load_dataset(ver).cases) == before + 1


# ---------------------------------------------------------------------------
# 3. 聚合指标
# ---------------------------------------------------------------------------

def test_compute_metrics_aggregates():
    results = [
        EvalCaseResult(name="a", query="q", intent_ok=True, metrics_ok=True,
                       skills_ok=True, judge=JudgeResult(faithfulness=1.0, hallucination=False, accuracy=1.0)),
        EvalCaseResult(name="b", query="q", intent_ok=False, metrics_ok=True,
                       skills_ok=True, judge=JudgeResult(faithfulness=0.5, hallucination=True, accuracy=0.5)),
        EvalCaseResult(name="c", query="q", category="attribution", is_attribution=True,
                       intent_ok=True, metrics_ok=True, skills_ok=True, attribution_ok=False,
                       judge=JudgeResult(faithfulness=0.8, hallucination=False, accuracy=0.8)),
    ]
    rep = compute_metrics(results)
    assert rep.total == 3
    assert rep.passed == 1  # 仅 a 全过（c 归因失败）
    assert rep.tcr == pytest.approx(1 / 3, abs=1e-3)
    assert rep.six_element_accuracy == pytest.approx(2 / 3, abs=1e-3)
    assert rep.hallucination_rate == pytest.approx(1 / 3, abs=1e-3)  # 3 条有 judge，1 条幻觉
    assert rep.attribution_accuracy == 0.0  # 唯一归因用例失败


# ---------------------------------------------------------------------------
# 4. runner：可注入 answer_provider（不依赖全图）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_suite_with_judge_stub_provider():
    cases = [
        EvalCase(name="m1", query="查GMV", expected_intent="metric_query",
                 expected_metrics=["GMV"], reference_answer="GMV 上升"),
        EvalCase(name="m2", query="归因", category="attribution", expected_intent="diagnosis",
                 reference_answer="华东下滑"),
    ]

    def provider(case):
        if case.name == "m1":
            return {"answer": "GMV 上升", "intent": "metric_query",
                    "metrics": ["GMV"], "skills": []}
        return {"answer": "华东下滑导致下降", "intent": "diagnosis",
                "metrics": [], "skills": [], "attribution_ok": True, "category": "attribution"}

    rep = await run_suite_with_judge(cases, provider, judge_fn=deterministic_judge)
    assert rep.total == 2
    assert rep.tcr == 1.0
    assert rep.passed == 2


# ---------------------------------------------------------------------------
# 5. badcase 回流（接已有点踩源）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_badcase_backflow_from_feedback():
    store = badcase_store()
    store.clear()
    fake_repo = SimpleNamespace(
        list_recent=lambda limit=500: [
            SimpleNamespace(session_id="s1", message_id="m1", intent="metric_query",
                           question="各分行存款排名", answer="错误答案", reason="不准"),
        ]
    )
    res = backflow_from_feedback(fake_repo, dataset_version=None)
    assert res["imported"] == 1
    assert store.count() == 1
    # 应能在库中查到
    assert store.list_all()[0].message_id == "m1"
    store.clear()


# ---------------------------------------------------------------------------
# 6. FR12 门禁联动：eval_by_dataset 设 eval_passed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_eval_by_dataset_gate_pass_and_block():
    from dmf_agent.db.stores import PromptVersionStore
    from dmf_agent.services.prompts import PromptManager

    mgr = PromptManager(PromptVersionStore())
    await mgr.update("intent", "你是指引意图", layer="task")
    rows = await mgr.history("intent")
    version = rows["current"]

    # 全过 -> 门禁通过
    good = [{"name": "c1", "intent_ok": True, "metrics_ok": True, "skills_ok": True}]
    r1 = await mgr.eval_by_dataset("intent", version, good, min_tcr=0.8)
    assert r1["eval_passed"] is True
    det = await mgr.detail("intent", version)
    assert det["eval_passed"] is True

    # 不达标 -> 门禁不通过（promote 仍会被 EVAL_GATE 拦）
    bad = [{"name": "c1", "intent_ok": False, "metrics_ok": True, "skills_ok": True}]
    r2 = await mgr.eval_by_dataset("intent", version, bad, min_tcr=0.8)
    assert r2["eval_passed"] is False
    det2 = await mgr.detail("intent", version)
    assert det2["eval_passed"] is False


# ---------------------------------------------------------------------------
# 7. 路由集成（TestClient，内存模式 -> judge 走确定性兜底）
# ---------------------------------------------------------------------------

def test_eval_router_datasets_and_judge():
    from dmf_agent.api.app import app
    from fastapi.testclient import TestClient

    get_or_seed_default()  # 确保有默认数据集
    with TestClient(app) as client:
        r = client.get("/api/v1/eval/datasets")
        assert r.status_code == 200
        assert any(d["version"] == "1.0.0" for d in r.json()["datasets"])

        j = client.post("/api/v1/eval/judge", json={
            "query": "查GMV", "reference": "GMV 上升", "answer": "GMV 上升",
        })
        assert j.status_code == 200
        body = j.json()
        assert body["hallucination"] is False
        assert body["source"] == "deterministic"


def test_eval_router_eval_dataset_gate():
    from dmf_agent.api.app import app
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        upd = client.post("/api/v1/prompts/update", json={
            "name": "intent", "content": "你是指引意图", "layer": "task",
        })
        assert upd.status_code == 200
        version = upd.json()["version"]

        r = client.post("/api/v1/prompts/eval-dataset", json={
            "name": "intent",
            "version": version,
            "answers": [{"name": "c1", "intent_ok": True, "metrics_ok": True, "skills_ok": True}],
            "min_tcr": 0.8,
        })
        assert r.status_code == 200
        assert r.json()["eval_passed"] is True

        det = client.get("/api/v1/prompts/detail", params={"name": "intent", "version": version})
        assert det.json()["eval_passed"] is True
