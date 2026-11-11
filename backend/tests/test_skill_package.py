"""Skill 包解析（`skills/package.py`）：frontmatter 解析、校验与 `${...}` 模板解析。

纯函数测试，无外部依赖。
"""

from __future__ import annotations

import pytest

from dmf_agent.skills.package import (
    SkillInput,
    SkillPackage,
    SkillParseError,
    SkillStep,
    match_package,
    parse_skill_package,
    resolve_template,
    unknown_tools,
    validate_inputs,
    with_defaults,
)

VALID = """---
name: deposit_branch_analysis
description: 按分行做存款专题归因分析
version: 2
tools: [execute_query, analyze_data]
inputs:
  - name: metric
    description: 指标名
    required: true
  - name: top_n
    default: 10
steps:
  - tool: execute_query
    arguments:
      dsl:
        metrics: ["${metric}"]
        dimensions: ["分行"]
  - tool: analyze_data
    arguments:
      query_result: "${steps.0.result}"
---
# 存款专题分析
结合上一步取数结果，按分行归因存款变化并给出结论。
"""


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def test_parse_valid_package():
    pkg = parse_skill_package(VALID, filename="SKILL.md")
    assert pkg.name == "deposit_branch_analysis"
    assert pkg.description == "按分行做存款专题归因分析"
    assert pkg.version == "2"
    assert pkg.tools == ["execute_query", "analyze_data"]
    assert [i.name for i in pkg.inputs] == ["metric", "top_n"]
    assert pkg.inputs[0].required is True
    assert pkg.inputs[1].default == 10
    assert [s.tool for s in pkg.steps] == ["execute_query", "analyze_data"]
    assert pkg.body.startswith("# 存款专题分析")
    assert pkg.filename == "SKILL.md"
    assert pkg.size == len(VALID.encode("utf-8"))
    assert len(pkg.sha256) == 64


def test_parse_missing_frontmatter_raises():
    with pytest.raises(SkillParseError, match="frontmatter"):
        parse_skill_package("# 只有正文，没有元信息块\n")


def test_parse_missing_name_raises():
    with pytest.raises(SkillParseError, match="name"):
        parse_skill_package("---\ndescription: 无名字\n---\n正文\n")


def test_parse_steps_must_be_list():
    with pytest.raises(SkillParseError, match="steps"):
        parse_skill_package("---\nname: x\nsteps: {tool: a}\n---\n正文\n")


def test_parse_step_missing_tool_raises():
    with pytest.raises(SkillParseError, match="tool"):
        parse_skill_package("---\nname: x\nsteps:\n  - arguments: {}\n---\n正文\n")


def test_parse_inputs_as_bare_strings():
    pkg = parse_skill_package("---\nname: x\ninputs: [metric, region]\n---\n正文\n")
    assert [i.name for i in pkg.inputs] == ["metric", "region"]
    assert all(not i.required for i in pkg.inputs)


def test_parse_tools_default_from_steps():
    pkg = parse_skill_package(
        "---\nname: x\nsteps:\n  - tool: retrieve_knowledge\n---\n正文\n"
    )
    assert pkg.tools == ["retrieve_knowledge"]
    assert pkg.step_tools() == ["retrieve_knowledge"]


def test_parse_invalid_yaml_raises():
    with pytest.raises(SkillParseError, match="YAML"):
        parse_skill_package("---\nname: [unclosed\n---\n正文\n")


def test_parse_empty_raises():
    with pytest.raises(SkillParseError, match="为空"):
        parse_skill_package("   \n")


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------
def test_validate_inputs_reports_missing_required():
    pkg = parse_skill_package(VALID)
    assert validate_inputs(pkg, {}) == ["metric"]
    assert validate_inputs(pkg, {"metric": ""}) == ["metric"]
    assert validate_inputs(pkg, {"metric": "存款余额"}) == []


def test_with_defaults_fills_optional():
    pkg = parse_skill_package(VALID)
    merged = with_defaults(pkg, {"metric": "GMV"})
    assert merged == {"metric": "GMV", "top_n": 10}


def test_with_defaults_keeps_explicit_value():
    pkg = parse_skill_package(VALID)
    assert with_defaults(pkg, {"metric": "GMV", "top_n": 3})["top_n"] == 3


def test_unknown_tools():
    pkg = parse_skill_package(VALID)
    assert unknown_tools(pkg, ["execute_query", "analyze_data"]) == []
    assert unknown_tools(pkg, ["execute_query"]) == ["analyze_data"]


# ---------------------------------------------------------------------------
# 模板解析
# ---------------------------------------------------------------------------
def test_resolve_whole_placeholder_keeps_object_type():
    """整串是一个占位符时返回原对象，便于把上一步结果直接喂给下一步。"""
    qr = {"columns": ["a"], "rows": [[1]]}
    assert resolve_template("${steps.0.result}", {}, [qr]) is qr


def test_resolve_nested_and_inputs_prefix():
    out = resolve_template(
        {"dsl": {"metrics": ["${metric}"]}, "k": "${inputs.metric}"},
        {"metric": "GMV"},
        [],
    )
    assert out == {"dsl": {"metrics": ["GMV"]}, "k": "GMV"}


def test_resolve_step_path():
    results = [{"columns": ["a"], "rows": [[1], [2]]}]
    assert resolve_template("${steps.0.result.rows}", {}, results) == [[1], [2]]
    assert resolve_template("${steps.0.result.columns.0}", {}, results) == "a"


def test_resolve_inline_substitution_in_string():
    out = resolve_template("指标：${metric}（近 7 天）", {"metric": "GMV"}, [])
    assert out == "指标：GMV（近 7 天）"


def test_resolve_unresolved_placeholder_kept():
    assert resolve_template("${nope}", {}, []) == "${nope}"
    assert resolve_template("${steps.9.result}", {}, []) == "${steps.9.result}"


def test_resolve_recurses_lists_and_non_strings():
    out = resolve_template([1, "${metric}", {"a": ["${metric}"]}], {"metric": "GMV"}, [])
    assert out == [1, "GMV", {"a": ["GMV"]}]


# ---------------------------------------------------------------------------
# 编解码往返
# ---------------------------------------------------------------------------
def test_to_dict_from_dict_roundtrip():
    pkg = parse_skill_package(VALID, filename="SKILL.md")
    restored = SkillPackage.from_dict(pkg.to_dict())
    assert restored.name == pkg.name
    assert restored.version == pkg.version
    assert restored.tools == pkg.tools
    assert [i.name for i in restored.inputs] == [i.name for i in pkg.inputs]
    assert [s.tool for s in restored.steps] == [s.tool for s in pkg.steps]
    assert restored.body == pkg.body
    assert restored.raw == pkg.raw
    assert restored.sha256 == pkg.sha256


def test_from_dict_tolerates_missing_fields():
    pkg = SkillPackage.from_dict({})
    assert pkg.name == ""
    assert pkg.steps == []
    assert pkg.inputs == []
    assert pkg.version == "1"


def test_dataclass_defaults():
    pkg = SkillPackage(name="x", inputs=[SkillInput(name="a")], steps=[SkillStep(tool="t")])
    assert pkg.description == ""
    assert pkg.step_tools() == ["t"]


# ---------------------------------------------------------------------------
# triggers 解析（FR14：对话内触发词）与 match_package
# ---------------------------------------------------------------------------
def test_parse_triggers_list():
    text = VALID.replace(
        "tools: [execute_query, analyze_data]",
        "triggers: [存款专题, 分行存款]\ntools: [execute_query, analyze_data]",
    )
    pkg = parse_skill_package(text)
    assert pkg.triggers == ["存款专题", "分行存款"]


def test_parse_triggers_string_split_by_separators():
    """triggers 写成一串字符串时按中英文逗号/顿号切分；空格不是分隔符，整串保留。"""
    cases = {
        "存款专题, 分行存款": ["存款专题", "分行存款"],
        "存款专题，分行存款、对公": ["存款专题", "分行存款", "对公"],
        "存款专题 分行存款": ["存款专题 分行存款"],
    }
    for raw, expected in cases.items():
        text = VALID.replace(
            "tools: [execute_query, analyze_data]",
            f'triggers: "{raw}"\ntools: [execute_query, analyze_data]',
        )
        assert parse_skill_package(text).triggers == expected


def test_parse_triggers_keywords_alias():
    """keywords 作为 triggers 的别名同样生效。"""
    text = VALID.replace(
        "tools: [execute_query, analyze_data]",
        "keywords: 存款专题\ntools: [execute_query, analyze_data]",
    )
    pkg = parse_skill_package(text)
    assert pkg.triggers == ["存款专题"]


def test_parse_no_triggers_defaults_empty():
    assert parse_skill_package(VALID).triggers == []


def test_triggers_roundtrip():
    text = VALID.replace(
        "tools: [execute_query, analyze_data]",
        "triggers: [存款专题, 分行存款]\ntools: [execute_query, analyze_data]",
    )
    pkg = parse_skill_package(text)
    assert SkillPackage.from_dict(pkg.to_dict()).triggers == ["存款专题", "分行存款"]


def _pkg(name: str, triggers):
    return SkillPackage(name=name, description=f"{name} 描述", triggers=list(triggers))


def test_match_package_hits_trigger():
    pkgs = [_pkg("a", ["存款专题"]), _pkg("b", ["对公"])]
    assert match_package("帮我做存款专题分析", pkgs).name == "a"


def test_match_package_higher_hit_count_wins():
    """命中词更多的 skill 胜出（而非先出现的）。"""
    more = _pkg("more", ["存款专题", "归因"])
    less = _pkg("less", ["存款专题"])
    assert match_package("存款专题归因分析", [less, more]).name == "more"


def test_match_package_ties_prefer_first():
    a = _pkg("a", ["存款专题"])
    b = _pkg("b", ["存款专题"])
    assert match_package("存款专题分析", [a, b]).name == "a"


def test_match_package_skill_name_implicit_trigger():
    """skill 名本身是隐式 trigger（用户直接点名）。"""
    pkg = _pkg("deposit_analysis", ["对公"])
    assert match_package("跑一下 deposit_analysis", [pkg]).name == "deposit_analysis"


def test_match_package_no_hit_returns_none():
    pkgs = [_pkg("a", ["存款专题"])]
    assert match_package("GMV 为什么下降了", pkgs) is None


def test_match_package_empty_query_and_empty_list():
    assert match_package("", [_pkg("a", ["x"])]) is None
    assert match_package("任意内容", []) is None
    assert match_package("", []) is None
