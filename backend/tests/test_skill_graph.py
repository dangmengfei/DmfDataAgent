"""声明式 Skill 接入主图的集成测试（FR14，无 LLM / 无外部依赖）。

用内存 `PluginStore` 装一个真 `PluginRegistry` + 真 `SkillExecutor`（配 `FakeMetricEngine`），
走完整 LangGraph：意图 -> 要素 -> 校验 -> **execute_skill** -> 答复。

回归护栏是首要目标：**未注册任何 skill 时主链路必须逐字不变**。
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage

from dmf_agent.agents.metric_agent import build_metric_agent_graph
from dmf_agent.config.settings import Settings
from dmf_agent.db.stores import PluginStore
from dmf_agent.services.plugins import PluginRegistry
from dmf_agent.skills.executor import SkillExecutor
from dmf_agent.skills.package import SkillPackage, parse_skill_package
from fakes import FakeMetricEngine, make_deps

# 触发词 = 存款专题；无必填输入；两步：取数 -> 确定性统计
SKILL_MD = """---
name: deposit_topic
description: 存款专题分析：先取数再做确定性统计
triggers: [存款专题]
tools: [execute_query, analyze_data]
steps:
  - tool: execute_query
    arguments:
      dsl:
        metrics:
          - name: GMV
  - tool: analyze_data
    arguments:
      query_result: "${steps.0.result}"
---
# 存款专题分析
结合上一步取数结果，给出结论。
"""

# 同上但声明了一个绑不到的必填输入 region（query_elements 里没有这个要素）
SKILL_MD_REQUIRED = SKILL_MD.replace(
    "triggers: [存款专题]",
    "triggers: [存款专题]\ninputs:\n  - name: region\n    required: true",
)

# 触发词不命中常见问句
SKILL_MD_NO_HIT = SKILL_MD.replace("triggers: [存款专题]", "triggers: [对公贷款专项]")

# 命中即触发，但取数步骤会失败（StarRocks 未配置）
HIT_QUERY = "帮我做存款专题分析，看下GMV"
PLAIN_QUERY = "查一下近7天的GMV趋势"


def _deps_with_skills(
    text: str,
    engine: FakeMetricEngine | None = None,
    get_chat_model=None,
    **settings_override,
):
    """组装：内存 PluginRegistry（已注册 `text`）+ 真 SkillExecutor + 假语义引擎。"""
    registry = PluginRegistry(PluginStore())
    pkg = parse_skill_package(text)
    assert pkg.name
    settings = Settings()
    for key, value in settings_override.items():
        setattr(settings.skill, key, value)
    engine = engine if engine is not None else FakeMetricEngine()
    executor = SkillExecutor(
        harness=None,  # 沙箱白名单走兜底表（execute_query/analyze_data 均在）
        metric_engine=engine,
        retriever=None,
        plugin_registry=registry,
        get_chat_model=get_chat_model or (lambda: None),  # 默认无 LLM：triggers 是唯一触发途径
        max_steps=8,
    )
    deps = make_deps(
        engine=engine,
        plugin_registry=registry,
        skill_executor=executor,
        settings=settings,
        get_chat_model=get_chat_model or (lambda: None),
    )
    return deps, registry, pkg


async def _register(registry: PluginRegistry, pkg: SkillPackage) -> None:
    res = await registry.register_skill_package(pkg)
    assert res["ok"] is True


async def _run(deps, query: str):
    graph = build_metric_agent_graph(deps)
    return await graph.ainvoke(
        {"messages": [HumanMessage(content=query)], "session_id": "s1", "user_id": "tester"}
    )


# ---------------------------------------------------------------------------
# 回归护栏
# ---------------------------------------------------------------------------
async def test_no_skill_registered_chain_unchanged():
    """未注册任何 skill：execute_skill 静默透传，主链路与接线前逐字一致。"""
    deps = make_deps()  # 无 plugin_registry / skill_executor
    result = await _run(deps, PLAIN_QUERY)

    assert "active_skill" not in result
    assert result["intent"] == "metric_query"
    assert result["query_elements"]["metrics"] == ["GMV"]
    assert result["validation"]["ok"] is True
    assert result["dsl"]["metrics"][0]["name"] == "GMV"
    assert result["query_result"]["row_count"] == 3
    assert result["analysis"]["stats"]["GMV"]["avg"] > 0
    assert "GMV" in result["final_answer"]


async def test_registered_skill_but_no_trigger_still_unchanged():
    """注册了 skill 但问句不含 triggers：同样透传回原取数链路。"""
    deps, registry, pkg = _deps_with_skills(SKILL_MD_NO_HIT)
    await _register(registry, pkg)
    result = await _run(deps, PLAIN_QUERY)

    assert "active_skill" not in result
    assert result["dsl"]["metrics"][0]["name"] == "GMV"
    assert result["query_result"]["row_count"] == 3


# ---------------------------------------------------------------------------
# 触发与执行
# ---------------------------------------------------------------------------
async def test_trigger_hit_executes_skill_and_answers():
    """triggers 命中：skill 在沙箱内跑完，直接进答复（不再走常规取数）。"""
    deps, registry, pkg = _deps_with_skills(SKILL_MD)
    await _register(registry, pkg)
    result = await _run(deps, HIT_QUERY)

    assert result["active_skill"] == "deposit_topic"
    sk = result["skill_result"]
    assert sk["ok"] is True
    assert [s["tool"] for s in sk["steps"]] == ["execute_query", "analyze_data"]
    assert all(s["status"] == "ok" for s in sk["steps"])
    # 沙箱产出经 synthesize_answer 渲染（模板路径）
    assert "deposit_topic" in result["final_answer"]
    # 命中即不再走常规取数链路
    assert "dsl" not in result


async def test_skill_name_is_implicit_trigger():
    """用户直接点名 skill：名本身算隐式 trigger。"""
    deps, registry, pkg = _deps_with_skills(SKILL_MD_NO_HIT)
    await _register(registry, pkg)
    result = await _run(deps, "跑一下 deposit_topic，指标用GMV")

    assert result["active_skill"] == "deposit_topic"
    assert result["skill_result"]["ok"] is True


# ---------------------------------------------------------------------------
# 失败与边界
# ---------------------------------------------------------------------------
async def test_required_input_unbound_reports_missing():
    """必填输入绑不到：如实报「缺少必填输入」，不编造、不抛异常。"""
    deps, registry, pkg = _deps_with_skills(SKILL_MD_REQUIRED)
    await _register(registry, pkg)
    result = await _run(deps, HIT_QUERY)

    sk = result["skill_result"]
    assert sk["ok"] is False
    assert "region" in sk["message"]
    assert "未完成" in result["final_answer"]


async def test_skill_step_failure_reported_not_raised():
    """skill 步骤失败（StarRocks 未配置）：ok=False + 可读原因，不冒泡。"""
    engine = FakeMetricEngine(fail_with="StarRocks 未配置")
    deps, registry, pkg = _deps_with_skills(SKILL_MD, engine=engine)
    await _register(registry, pkg)
    result = await _run(deps, HIT_QUERY)

    sk = result["skill_result"]
    assert sk["ok"] is False
    assert "StarRocks 未配置" in sk["message"]
    assert result["final_answer"]  # 有答复（如实说明失败），不是异常
    assert "deposit_topic" in result["final_answer"]


async def test_wire_into_graph_false_never_executes():
    """wire_into_graph=False：节点不接入拓扑，即使 triggers 命中也不触发。"""
    deps, registry, pkg = _deps_with_skills(SKILL_MD, wire_into_graph=False)
    await _register(registry, pkg)
    result = await _run(deps, HIT_QUERY)

    assert "active_skill" not in result
    assert result["dsl"]["metrics"][0]["name"] == "GMV"


async def test_skill_enabled_false_never_executes():
    """enabled=False：节点接入了但整体关停，命中也不触发。"""
    deps, registry, pkg = _deps_with_skills(SKILL_MD, enabled=False)
    await _register(registry, pkg)
    result = await _run(deps, HIT_QUERY)

    assert "active_skill" not in result
    assert result["dsl"]["metrics"][0]["name"] == "GMV"


# ---------------------------------------------------------------------------
# 模型驱动触发（对齐 Claude Code / Codex 机制）
# ---------------------------------------------------------------------------
class _PickLLM:
    """`get_chat_model` 替身：经 `with_structured_output` 通道返回预置的 `_SkillPick`。

    `execute_skill._llm_pick` 走 `llm.structured.astructured_invoke`，其首选通道是
    `llm.with_structured_output(schema).ainvoke(...)` —— 替身只需实现这两个方法，
    且返回值必须是 **pydantic 实例**（`_coerce` 不认普通命名空间类）。
    """

    class _Runnable:
        def __init__(self, pick) -> None:
            self._pick = pick

        async def ainvoke(self, messages):  # noqa: ARG002
            return self._pick

    def __init__(self, picked: str) -> None:
        self._picked = picked
        self.pick_calls = 0  # 只数 _SkillPick 的路由调用（意图/时间解析等其它 schema 不算）

    def with_structured_output(self, schema, **kwargs):  # noqa: ARG002
        from dmf_agent.agents.metric_agent.nodes.execute_skill import _SkillPick

        if schema is _SkillPick:
            self.pick_calls += 1
        return self._Runnable(_SkillPick(name=self._picked))


async def test_model_driven_pick_takes_priority():
    """模型驱动：LLM 选中即触发——即使问句不含 triggers 也命中。"""
    llm = _PickLLM("deposit_topic")
    deps, registry, pkg = _deps_with_skills(SKILL_MD_NO_HIT, model_driven=True, get_chat_model=lambda: llm)
    await _register(registry, pkg)
    result = await _run(deps, PLAIN_QUERY)  # 问句不含「存款专题」，靠模型选中

    assert result["active_skill"] == "deposit_topic"
    assert result["skill_result"]["ok"] is True


async def test_model_driven_none_falls_back_to_triggers():
    """模型返回 none：回落 triggers 确定性匹配（triggers 命中则触发）。"""
    llm = _PickLLM("none")
    deps, registry, pkg = _deps_with_skills(SKILL_MD, model_driven=True, get_chat_model=lambda: llm)
    await _register(registry, pkg)
    result = await _run(deps, HIT_QUERY)

    assert result["active_skill"] == "deposit_topic"


async def test_model_driven_none_and_no_trigger_passes_through():
    """模型返回 none 且 triggers 不命中：透传回原取数链路。"""
    llm = _PickLLM("none")
    deps, registry, pkg = _deps_with_skills(SKILL_MD_NO_HIT, model_driven=True, get_chat_model=lambda: llm)
    await _register(registry, pkg)
    result = await _run(deps, PLAIN_QUERY)

    assert "active_skill" not in result
    assert result["dsl"]["metrics"][0]["name"] == "GMV"


async def test_model_driven_off_keeps_legacy_order():
    """model_driven=False：回到旧顺序——triggers 命中即触发，不消耗模型路由调用。

    （问句含 triggers 时 `execute_skill` 不应再调 LLM——这里用「只数 _SkillPick 调用」
    的替身验证：triggers 命中走的是 match_package，模型路由计数为 0。）
    """
    llm = _PickLLM("deposit_topic")
    deps, registry, pkg = _deps_with_skills(SKILL_MD, model_driven=False, get_chat_model=lambda: llm)
    await _register(registry, pkg)
    result = await _run(deps, HIT_QUERY)

    assert result["active_skill"] == "deposit_topic"
    assert llm.pick_calls == 0  # 旧模式 triggers 优先，未兜底前不该问模型


async def test_model_driven_off_llm_fallback_after_miss():
    """model_driven=False + match_llm_fallback=true：triggers 未命中再由模型兜底选中。"""
    llm = _PickLLM("deposit_topic")
    deps, registry, pkg = _deps_with_skills(
        SKILL_MD_NO_HIT, model_driven=False, get_chat_model=lambda: llm
    )
    await _register(registry, pkg)
    result = await _run(deps, PLAIN_QUERY)

    assert result["active_skill"] == "deposit_topic"
    assert llm.pick_calls == 1


# ---------------------------------------------------------------------------
# 包内脚本步骤（run_skill_script）
# ---------------------------------------------------------------------------
SCRIPT_SKILL_MD = """---
name: script_topic
description: 带脚本的存款专题：取数后跑包内脚本加工
triggers: [存款专题]
steps:
  - tool: run_skill_script
    arguments:
      script: "scripts/render.py"
      args: ["--mode", "summary"]
---
# 带脚本的存款专题
结合脚本输出给出结论。
"""


async def test_script_step_executes_in_sandbox(tmp_path):
    """脚本步骤：包内 .py 在沙箱真实执行，stdout 进步骤轨迹。"""
    from dmf_agent.skills import ScriptRunner

    script = tmp_path / "scripts" / "render.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "import sys\nprint('render done mode=' + sys.argv[sys.argv.index('--mode') + 1])\n",
        encoding="utf-8",
    )

    deps, registry, pkg = _deps_with_skills(SKILL_MD)  # 先占位用不到的包
    registry2 = PluginRegistry(PluginStore())
    pkg2 = parse_skill_package(SCRIPT_SKILL_MD)
    pkg2.package_dir = str(tmp_path)
    assert (await registry2.register_skill_package(pkg2))["ok"] is True

    engine = FakeMetricEngine()
    executor = SkillExecutor(
        harness=None,
        metric_engine=engine,
        retriever=None,
        plugin_registry=registry2,
        get_chat_model=lambda: None,
        script_runner=ScriptRunner(timeout_seconds=10),
        max_steps=8,
    )
    settings = Settings()
    deps = make_deps(
        engine=engine, plugin_registry=registry2, skill_executor=executor, settings=settings
    )

    result = await _run(deps, HIT_QUERY)

    assert result["active_skill"] == "script_topic"
    sk = result["skill_result"]
    assert sk["ok"] is True, sk
    assert sk["steps"][0]["tool"] == "run_skill_script"
    assert "render done mode=summary" in sk["steps"][0]["result"]["stdout"]


async def test_script_disabled_reports_error():
    """script_enabled=False：脚本步骤明确报错（不静默跳过）。"""
    from dmf_agent.skills import ScriptRunner

    registry = PluginRegistry(PluginStore())
    pkg = parse_skill_package(SCRIPT_SKILL_MD)
    pkg.package_dir = "."  # 有目录但脚本不存在也无妨：禁用检查在前
    assert (await registry.register_skill_package(pkg))["ok"] is True

    engine = FakeMetricEngine()
    executor = SkillExecutor(
        harness=None,
        metric_engine=engine,
        retriever=None,
        plugin_registry=registry,
        get_chat_model=lambda: None,
        script_runner=ScriptRunner(timeout_seconds=10),
        script_enabled=False,
        max_steps=8,
    )
    settings = Settings()
    deps = make_deps(
        engine=engine, plugin_registry=registry, skill_executor=executor, settings=settings
    )
    result = await _run(deps, HIT_QUERY)

    sk = result["skill_result"]
    assert sk["ok"] is False
    assert "禁用" in sk["message"]
