"""Prompt 加载器与要素抽取 prompt 迁移测试。"""

import pytest

from dmf_agent.agents.prompt_loader import load_prompt


def test_load_prompt_local_yaml():
    prompt = load_prompt("elements")
    assert prompt["system"].strip().startswith("你是取数语义的信息解析助手")


def test_load_prompt_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_prompt("不存在的模板")


def test_elements_prompt_migrated_directives():
    system = load_prompt("elements")["system"]
    # 老 prompt_element.py 的核心约束均已迁移
    assert "只能来自候选集" in system
    assert "不要把问题中的时间实体数字或比较数值" in system
    assert "以指标为第一优先级" in system
    assert "优先从返回指标的候选维度中选择" in system
    # v2 QueryElements 字段说明（field/operator/values + time_range/order_by/period_comparison）
    assert '"field"' in system and '"operator"' in system and '"values"' in system
    assert "time_range" in system and "order_by" in system and "period_comparison" in system
    # 时间由独立 time 模块解析，不在本 prompt 中输出
    assert "一律返回 null" in system


def test_format_candidates_old_filter_keys_mapped():
    from dmf_agent.agents.metric_agent.nodes.extract_elements import format_candidates

    human = format_candidates(
        "看看四川公司线下的销售额",
        metrics=["销售额"],
        dimensions=[],
        filters=[{"dimension": "分公司", "dimension_value": ["四川公司"], "filter_operator": "in"}],
    )
    assert "候选指标集: ['销售额']" in human
    assert "候选维度集: []" in human
    assert "候选过滤条件集: [{'field': '分公司', 'values': ['四川公司']}]" in human
    assert "用户输入的问题: 看看四川公司线下的销售额" in human
