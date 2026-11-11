"""Phase 7 测试：few-shot 经验库 + 评测体系（mock 模式）。"""

from dmf_agent.experience import (
    DEFAULT_EXAMPLES,
    ExampleStore,
    default_example_store,
    export_examples_to_json,
    format_dynamic_examples,
)


# ---- ExampleStore（BM25 召回） ----


def test_example_store_recall_by_similarity():
    store = default_example_store()
    examples = store.get_examples("查一下GMV同比增长", "intention", top_k=1)
    assert examples
    # 相似度最高应为"销售额同比"那条
    assert "同比" in examples[0].query


def test_example_store_unknown_category_empty():
    store = default_example_store()
    assert store.get_examples("任意", "不存在的类别", top_k=2) == []


def test_example_store_empty_query():
    store = default_example_store()
    assert store.get_examples("", "intention", top_k=1) == []


def test_example_store_from_json(tmp_path):
    import json

    path = tmp_path / "examples.json"
    path.write_text(
        json.dumps(
            {
                "intention": [
                    {"query": "查GMV", "content": '{"intent": "metric_query"}'},
                ]
            }
        ),
        encoding="utf-8",
    )
    store = ExampleStore.from_json(str(path))
    assert store.get_examples("查一下GMV", "intention", top_k=1)


def test_format_dynamic_examples():
    store = default_example_store()
    text = format_dynamic_examples(store.get_examples("查GMV同比", "intention", top_k=1))
    assert text.startswith("用户输入: ")
    assert "\n输出: " in text


def test_example_store_categories():
    assert set(default_example_store().categories()) == {"intention", "element", "time"}


# ---- 经验库本地 JSON 导出 ----


def test_export_examples_to_json(tmp_path):
    path = str(tmp_path / "examples.json")
    n = export_examples_to_json(DEFAULT_EXAMPLES, path)
    assert n == sum(len(v) for v in DEFAULT_EXAMPLES.values())
    # 导出可被 ExampleStore 重新加载
    store = ExampleStore.from_json(path)
    assert store.get_examples("查GMV同比", "intention", top_k=1)


# ---- 评测执行器 ----


async def test_run_suite_mock_accuracy():
    from dmf_agent.eval.runner import run_suite

    report = await run_suite()
    assert report["total"] == 6
    assert report["passed"] == report["total"]
    assert report["accuracy"] == 1.0
    for r in report["results"]:
        assert r.actual_intent == r.case.expected_intent


async def test_run_case_pass_and_fail():
    from dmf_agent.agents.metric_agent import build_metric_agent_graph
    from dmf_agent.eval.cases import EvalCase
    from dmf_agent.eval.runner import build_eval_deps, run_case

    graph = build_metric_agent_graph(build_eval_deps())
    ok = await run_case(graph, EvalCase(name="x", query="查一下近7天的GMV趋势",
                                        expected_intent="metric_query", expected_metrics=["GMV"]))
    assert ok.passed is True
    bad = await run_case(graph, EvalCase(name="y", query="查一下近7天的GMV趋势",
                                         expected_intent="chitchat"))
    assert bad.passed is False
    assert bad.actual_intent == "metric_query"


# ---- 评测 CLI ----


def test_eval_cli_exit_zero():
    import asyncio

    from dmf_agent.eval.__main__ import main

    assert asyncio.run(main(["--verbose"])) == 0
