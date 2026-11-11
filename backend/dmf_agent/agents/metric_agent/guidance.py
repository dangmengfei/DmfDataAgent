"""用户引导文案（跨节点共享，FR8）。

背景：查询时若用户提到的指标名**未收录在语义层**（"未命中"），系统不应静默失败，
也不应**自动**替用户创建指标（口径必须由人确认，否则会污染语义层）。正确做法是
**引导**用户显式发起创建：告知其可直接说「创建一个指标，名叫 X，口径为 …」，
由 `create` 意图起草草稿，再经管理员「提交审核 -> 发布」后生效。

本模块只产出文案，供 `ask_clarification`（未命中 -> 追问分支）与 `synthesize_answer`
（LLM 上下文 / 模板答复）共用，避免节点间互相 import。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

# 明确"不自动创建、需用户确认"的边界声明，避免用户误以为系统已代为落库
_NO_AUTO_CREATE_NOTE = "（我不会自动创建指标——口径需要你确认后才会起草。）"

# validate_elements 在"完全没识别出指标"时写入的固定问题文案（用于触发通用引导）
NO_METRIC_PROBLEM = "未能从问题中识别出任何业务指标"


def create_guidance_lines(missed_metrics: Iterable[Any]) -> List[str]:
    """生成"未命中 -> 引导创建"的 markdown 行（无未命中时返回空列表）。"""
    names = [str(n).strip() for n in (missed_metrics or []) if str(n).strip()]
    if not names:
        return []
    listing = "、".join(f"**{n}**" for n in names)
    example = names[0]
    return [
        f"⚠️ 以下指标尚未收录在语义层：{listing}",
        "",
        f"如需新增，请直接对我说：**「创建一个指标，名叫{example}，口径为……（如按交易日去重统计客户数）」**，"
        "我会为你起草指标定义草稿，经管理员『提交审核 → 发布』后即可用于查询。",
        _NO_AUTO_CREATE_NOTE,
    ]


def build_create_guidance(missed_metrics: Iterable[Any]) -> str:
    """生成"未命中 -> 引导创建"文案块（无未命中时返回空串，便于直接拼接）。"""
    lines = create_guidance_lines(missed_metrics)
    return "\n".join(lines) if lines else ""


def build_llm_guidance_instruction(missed_metrics: Iterable[Any]) -> str:
    """给 LLM 的引导指令（下达"引导而非代劳"的硬约束）。"""
    names = [str(n).strip() for n in (missed_metrics or []) if str(n).strip()]
    if not names:
        return ""
    return (
        f"未收录指标：{'、'.join(names)}。请在答复中明确告知用户这些指标尚未登记，"
        "并引导其用「创建一个指标，名叫X，口径为……」的方式显式发起创建；"
        "严禁声称已自动创建，也不要编造其口径或数值。"
    )


def build_generic_create_guidance() -> str:
    """通用引导（未捕捉到具体指标名时）：不点名，只给创建入口。"""
    return (
        "💡 若你要查询的指标尚未登记在语义层，可以直接对我说："
        "**「创建一个指标，名叫X，口径为……」**，我会为你起草指标定义草稿，"
        "经管理员『提交审核 → 发布』后即可查询。\n"
        + _NO_AUTO_CREATE_NOTE
    )


def resolve_create_guidance(validation: Optional[Dict[str, Any]]) -> str:
    """由 validation 结果决定"是否 / 如何"引导创建（空串 = 不引导）。

    三级判定，避免误引导：
    1. `missed_metrics` 非空（用户提到了指标名，语义层没有）-> **精准**引导（带名字）；
    2. 完全没识别出指标 -> **通用**引导（"若尚未登记可创建"）；
    3. 其它（维度未收录、低置信澄清等，指标本身存在）-> **不引导**，
       否则会让本可正常查询/澄清的用户被误导去建指标。
    """
    v = validation or {}
    missed = [str(n).strip() for n in (v.get("missed_metrics") or []) if str(n).strip()]
    if missed:
        return build_create_guidance(missed)
    problems = v.get("problems") or []
    joined = "；".join(str(p) for p in problems)
    if NO_METRIC_PROBLEM in joined:
        return build_generic_create_guidance()
    return ""
