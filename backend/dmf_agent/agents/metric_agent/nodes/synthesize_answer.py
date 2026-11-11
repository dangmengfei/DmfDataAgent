"""最终答复生成节点（所有意图汇聚点，SSE token 流式输出的来源）。"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from langchain_core.messages import HumanMessage, SystemMessage

from dmf_agent.agents.metric_agent.deps import AgentDeps
from dmf_agent.agents.metric_agent.empty_trace import is_effectively_empty
from dmf_agent.harness.sandbox import get_default_sandbox
from dmf_agent.agents.metric_agent.guidance import (
    build_llm_guidance_instruction,
    resolve_create_guidance,
)
from dmf_agent.agents.prompt_loader import load_prompt
from dmf_agent.core.state import AgentState, user_query
from dmf_agent.analysis import describe_data
from dmf_agent.semantic.engine import caliber_context_text, caliber_markdown

logger = logging.getLogger(__name__)

# 报告用数据明细最大行数（迁移自老 df_analysis_report.MAX_LENGHT）
_MAX_DATA_ROWS = 50


def _format_prior_dialogue(state: AgentState, max_messages: int = 8) -> str:
    """把当前问题之前的对话（含滚动摘要 SystemMessage）格式化为上下文文本。"""
    msgs = state.get("messages", [])
    if not msgs:
        return ""
    # 最后一条 HumanMessage 为当前问题
    last_human_idx = -1
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].__class__.__name__ == "HumanMessage":
            last_human_idx = i
            break
    prior = msgs[:last_human_idx] if last_human_idx > 0 else []
    prior = [m for m in prior if m.__class__.__name__ in ("HumanMessage", "AIMessage", "SystemMessage")]
    prior = prior[-max_messages:]
    lines = []
    for m in prior:
        cls = m.__class__.__name__
        if cls == "SystemMessage":
            lines.append(f"[历史摘要] {m.content}")
        elif cls == "HumanMessage":
            lines.append(f"用户：{m.content}")
        else:
            content = m.content if isinstance(m.content, str) else str(m.content)
            lines.append(f"助手：{content[:200]}…")
    return "\n".join(lines)


def _build_context(state: AgentState) -> str:
    sections = []
    # L2 用户偏好：置顶，让模型优先遵守个人口径/呈现习惯（记忆关闭时该字段为空）
    prefs = state.get("preferences") or []
    if prefs:
        pref_lines = [
            f"- {p.get('key')}：{p.get('value')}"
            + ("（系统推断，仅供参考）" if p.get("source") == "inferred" else "")
            for p in prefs
            if p.get("value")
        ]
        if pref_lines:
            sections.append("用户偏好（请优先遵从其口径与呈现习惯）：\n" + "\n".join(pref_lines))
    if state.get("intent"):
        sections.append(f"意图：{state['intent']}")
    history_text = _format_prior_dialogue(state)
    if history_text:
        sections.append(f"最近对话：\n{history_text}")
    if state.get("query_elements"):
        sections.append(f"查询要素：{json.dumps(state['query_elements'], ensure_ascii=False)}")
    # FR8：未命中（指标名已提到但语义层未收录）-> 下达"引导创建而非代劳"的硬约束
    missed = (state.get("validation") or {}).get("missed_metrics") or []
    guidance_instruction = build_llm_guidance_instruction(missed)
    if guidance_instruction:
        sections.append(guidance_instruction)
    if state.get("dsl"):
        sections.append(f"DSL：{json.dumps(state['dsl'], ensure_ascii=False)}")
    # FR12 指标计算可解释性：把**确定性口径说明**作为事实交给模型引用。
    # 口径由语义层渲染（semantic/engine.py::build_caliber），模型只复述、不改写——
    # 避免 LLM 对公式/同环比定义/过滤条件的复述产生口径漂移。
    caliber_text = caliber_context_text((state.get("query_result") or {}).get("caliber") or {})
    if caliber_text:
        sections.append(
            "本次结果口径（**事实**，来自语义层定义；若结论中说明口径，必须与此逐字一致，"
            "不得改写公式或同环比定义）：\n" + caliber_text
        )
    analysis = state.get("analysis") or {}
    if analysis.get("summary"):
        sections.append(f"分析结论：{analysis['summary']}")
    # FR10 Sub-Agent 编排：多子任务并行聚合的上下文标注，引导 LLM 分来源组织结论
    if analysis.get("subagent"):
        counts = analysis.get("subagent_counts") or {}
        c_parts = [
            f"成功 {counts.get('ok', 0)}",
            f"失败 {counts.get('failed', 0)}",
            f"超时 {counts.get('timed_out', 0)}",
            f"跳过 {counts.get('skipped', 0)}",
        ]
        sections.append(
            "本次查询由多个子任务并行编排聚合而成（Sub-Agent 编排，FR10）："
            + "、".join(c_parts)
            + f"，共 {counts.get('total', 0)} 个子任务。"
            + "请按各子任务分别组织结论，并明确标注每个结论对应的来源指标/子问题。"
        )
    # 分布分桶（FR2）：Python 确定性统计结果，LLM 仅组织语言（先算后说）
    dist = analysis.get("distribution")
    if dist and dist.get("bins"):
        dist_lines = "\n".join(f"- {b['label']}：{b['count']} 个" for b in dist["bins"])
        sections.append(
            f"分布分桶（{dist['metric']}，共 {dist['total_count']} 个取值，"
            f"范围 {dist['min']} ~ {dist['max']}）：\n{dist_lines}\n"
            "请基于上述分桶结果描述分布特征（集中区间/长尾），不要编造分桶外的结论。"
        )
    if analysis.get("stats"):
        sections.append(f"统计量：{json.dumps(analysis['stats'], ensure_ascii=False)}")
    diag = state.get("diagnosis") or {}
    if diag.get("ok"):
        change = diag.get("change") or {}
        primary_dim = diag.get("primary_dim")
        primary_list = (diag.get("dimensions") or {}).get(primary_dim) if primary_dim else []
        same = diag.get("same_contribution_list") or []
        opp = diag.get("opposite_contribution_list") or []
        same_n = "上升" if change.get("change_value", 0) > 0 else "下降"
        diag_lines = [
            "归因分析（以下均为基于真实取数结果的确定性计算，请据此生成自然语言解读，"
            "不要编造数据之外的事实或维度）：",
            f"- 主指标：【{diag.get('metric')}】，{change.get('begin_time')}→{change.get('end_time')}，"
            f"从 {change.get('metrics_value')} 变为 {change.get('compare_metrics_value')}，"
            f"变化 {change.get('change_value')}（{same_n}），变化率 {change.get('change_rate')}",
            f"- 维度归因（{primary_dim or '无'}，按 |变化量| 降序，仅列前8）：",
        ]
        for d in (primary_list or [])[:8]:
            diag_lines.append(
                f"  · {d.get('dim_name')}={d.get('dim_value')}：{d.get('change_value')}"
                f"（影响指数 {d.get('effect_index')}，影响占比 {d.get('effect_rate')}）"
            )
        diag_lines.append(f"- 同向贡献TOP：{json.dumps(same, ensure_ascii=False)}")
        diag_lines.append(f"- 反向贡献TOP：{json.dumps(opp, ensure_ascii=False)}")
        hints = diag.get("action_hints") or []
        if hints:
            diag_lines.append("行动建议线索（基于上述贡献TOP，供你组织建议）：\n- " + "\n- ".join(hints))
        diag_lines.append(
            f"规则总结参考：{diag.get('global_summary', '')} {diag.get('smart_summary', '')}"
        )
        diag_lines.append("请在解读末尾标注『基于当前数据推断』。")
        sections.append("\n".join(diag_lines))
    # 数据明细 + 描述性统计（迁移自老 df_analysis_report 的报告输入）
    qr = state.get("query_result") or {}
    if qr.get("error"):
        code = qr.get("error_code")
        sections.append(
            f"取数失败：{qr['error']}" + (f"（错误码 {code}）" if code else "")
        )
    # 空数据回溯诊断（PRD FR5）：让 LLM 在答复中解释原因与数据截至时间
    empty_diag = (state.get("analysis") or {}).get("empty_diagnosis")
    if empty_diag and empty_diag.get("is_empty"):
        as_of = empty_diag.get("data_as_of") or "未知"
        cause_lines = "\n".join(
            f"- [{c.get('level')}] {c.get('label')}：{c.get('detail')}（建议：{c.get('suggestion')}）"
            for c in empty_diag.get("causes", [])
        )
        sections.append(
            f"空数据回溯诊断（数据截至 {as_of}）：\n{cause_lines}\n"
            "注意：不要静默重试，向用户说明可能原因并询问是否要按建议放宽条件后重查。"
        )
    # 指标创建结果（FR8）：create 意图由 create_metric 节点产出，供 LLM 据此向用户说明
    cr = state.get("create_result")
    if cr:
        if cr.get("ok"):
            sections.append(
                f"指标创建结果（草稿已落库）：指标【{cr.get('name')}】，key={cr.get('metric_key')}，"
                f"状态 {cr.get('status')}。请告知用户草稿需经管理员『提交审核 -> 发布』后方可查询，"
                "并简要复述指标口径。"
            )
        else:
            problems = cr.get("problems") or []
            sections.append(
                "指标创建未能完成：" + (cr.get("message") or "校验未通过") +
                ("\n问题清单：\n- " + "\n- ".join(problems) if problems else "") +
                "\n请向用户说明原因并引导其补充/修正口径信息。"
            )
    # Skill 执行结果（FR14）：execute_skill 节点产出；沙箱内已按 skill 正文合成好 answer
    sk = state.get("skill_result")
    if sk:
        if sk.get("ok"):
            step_lines = "\n".join(
                f"- {s.get('tool')}：{s.get('summary') or s.get('status')}" for s in sk.get("steps") or []
            )
            sections.append(
                f"本次由技能【{sk.get('skill')}】完成（步骤轨迹：\n{step_lines}\n）。\n"
                "技能沙箱产出的答复如下，请据此整理为面向用户的最终答复（可精简格式，不要改动数据与结论）：\n"
                + str(sk.get("answer") or "")
            )
        else:
            sections.append(
                "技能【" + str(sk.get("skill") or "") + "】执行未完成：" + (sk.get("message") or "未知原因") +
                "\n请向用户如实说明失败原因，不要编造数据。"
            )
    if qr.get("rows"):
        cols = qr.get("columns") or []
        table = ["| " + " | ".join(str(c) for c in cols) + " |", "|" + "---|" * len(cols)]
        for row in qr.get("rows", [])[:_MAX_DATA_ROWS]:
            table.append("| " + " | ".join(str(v) for v in row) + " |")
        sections.append("数据明细（前%d条）：\n%s" % (min(len(qr.get("rows", [])), _MAX_DATA_ROWS), "\n".join(table)))
        desc = describe_data(qr)
        if desc:
            sections.append(f"描述性统计：{json.dumps(desc, ensure_ascii=False)}")
    docs = state.get("retrieved_docs") or []
    if docs:
        docs_text = "\n\n".join(
            f"【{d.get('source') or '知识库'}】{d.get('content', '')[:800]}" for d in docs
        )
        sections.append(f"检索到的知识片段：\n{docs_text}")
    return "\n".join(sections) if sections else "（无可用上下文）"


def _render_subagent_block(state: AgentState) -> str:
    """FR10 Sub-Agent：把逐子任务结果渲染为多段可读的 mock 答复（无 LLM 时仍可端到端验证）。"""
    results = state.get("subagent_results") or []
    if not results:
        return ""
    lines = [
        "> **多子任务并行编排结果（Sub-Agent，FR10）**：本次问题被拆分为 "
        f"{len(results)} 个子任务并行执行，以下按子任务聚合呈现。\n",
    ]
    ok = 0
    for i, r in enumerate(results, 1):
        status = (r.get("status") or "ok").upper()
        q = r.get("question") or f"子任务{i}"
        line = [f"### 子任务 {i}：{q}  「{status}」"]
        if status == "OK":
            ok += 1
            a = r.get("analysis") or {}
            if a.get("summary"):
                line.append(a["summary"])
            qr = r.get("query_result") or {}
            if qr.get("error"):
                line.append(f"\n**取数错误**：{qr['error']}"
                            + (f"（错误码 {qr.get('error_code')}）" if qr.get("error_code") else ""))
            elif qr.get("rows") and not is_effectively_empty(qr):
                cols = qr.get("columns") or []
                table = ["| " + " | ".join(str(c) for c in cols) + " |",
                         "|" + "---|" * len(cols)]
                for row in qr.get("rows", [])[:_MAX_DATA_ROWS]:
                    table.append("| " + " | ".join(str(v) for v in row) + " |")
                line.append("\n" + "\n".join(table))
                line.append(f"\n*来源：{qr.get('engine', 'metricflow')} -> StarRocks，"
                            f"{qr.get('row_count', 0)} 行，{qr.get('elapsed_ms', 0)} ms*")
        elif r.get("error"):
            e = r["error"]
            line.append(f"\n子任务失败：{e.get('message')}（{e.get('code')}）")
        lines.append("\n".join(line))
    lines.append(f"\n_已成功聚合 {ok} / {len(results)} 个子任务结果。_")
    return "\n\n".join(lines)


def _render_skill_block(sk: Dict[str, Any]) -> str:
    """FR14 声明式 Skill：渲染沙箱执行结果（有/无 LLM 共用；无 LLM 时 answer 为模板产物）。"""
    name = sk.get("skill") or ""
    if sk.get("ok"):
        step_lines = "\n".join(
            f"- {s.get('tool')}（{s.get('status')}"
            + (f"，{s.get('elapsed_ms')} ms" if s.get("elapsed_ms") is not None else "")
            + f"）：{s.get('summary') or ''}"
            for s in sk.get("steps") or []
        )
        return (
            f"**本次由技能【{name}】完成**（FR14 声明式 Skill，沙箱内受治理执行）\n\n"
            f"{sk.get('answer') or '（无答复）'}\n\n"
            f"步骤轨迹：\n{step_lines}"
        )
    return (
        f"**技能【{name}】执行未完成**（FR14）\n\n"
        f"{sk.get('message') or '未知原因'}\n\n"
        "_提示：请检查 skill 步骤配置与输入要素；缺必填项时会先反问澄清。_"
    )


def _template_answer(state: AgentState) -> str:
    """mock 模式下的模板答复，保证链路可端到端验证。"""
    intent = state.get("intent", "chitchat")

    # 查询后追加技能（迁移自老系统插件 B/C/E），标注迁移状态
    skill_names = {
        "chart_visualization": "图表可视化",
        "data_interpretation": "数据解读",
        "complex_calculation": "复杂计算",
    }
    skills_line = ""
    skills = state.get("skills") or []
    if skills:
        names = "、".join(skill_names.get(s, s) for s in skills)
        skills_line = f"**识别到追加技能**：{names}（对应老系统插件，迁移排期见 MIGRATION.md）\n\n"

    multi_query_line = ""
    if state.get("multi_query"):
        multi_query_line = "**多次取数标记**：本次查询涉及多个问题/时间点，需拆分多次取数（Phase 3 迁移）\n\n"

    # Skill 执行结果（FR14）：命中即优先于意图模板 —— 沙箱内已按 skill 正文产出确定性答复
    sk = state.get("skill_result")
    if sk:
        return _render_skill_block(sk)

    if intent == "metric_query":
        analysis = state.get("analysis") or {}
        # FR10 Sub-Agent：多子任务聚合已有逐子任务明细，直接渲染，不再用单表兜底
        if analysis.get("subagent"):
            return (
                "> 当前未启用 LLM，以下为基于**真实取数结果**的多子任务聚合摘要。\n\n"
                f"{_render_subagent_block(state)}\n\n"
                "_提示：配置 DMF_LLM-* 后可获得跨子任务的自然语言综合解读；"
                "子任务并行编排见 PRD FR10。_"
            )
        stats = analysis.get("stats") or {}
        qr = state.get("query_result") or {}
        stats_lines = "\n".join(
            f"| {name} | {s['total']} | {s['avg']} | {s['max']} | {s['min']} |"
            for name, s in stats.items()
        )
        stats_table = (
            f"| 指标 | 合计 | 均值 | 最大 | 最小 |\n|---|---|---|---|---|\n{stats_lines}"
            if stats_lines
            else "（无数据）"
        )
        code = qr.get("error_code")
        error_line = (
            f"\n**取数错误**：{qr['error']}"
            + (f"（错误码 {code}）" if code else "")
            + "\n"
            if qr.get("error")
            else ""
        )
        source_line = (
            f"**取数来源**：{qr.get('engine', 'metricflow')} -> StarRocks"
            f"，返回 {qr.get('row_count', 0)} 行，耗时 {qr.get('elapsed_ms', 0)} ms\n\n"
        )
        # 空数据回溯（PRD FR5）：呈现原因 + 数据截至时间 + 确认后再重查（不静默重试）
        empty_block = ""
        empty_diag = analysis.get("empty_diagnosis")
        # 纯聚合在时间窗无数据时 StarRocks 仍返回一行全 NULL（row_count=1），
        # 这里按"实质为空"表述，避免出现"返回 1 行"与"未返回数据"并存的矛盾文案。
        if is_effectively_empty(qr):
            source_line = (
                f"**取数来源**：{qr.get('engine', 'metricflow')} -> StarRocks"
                f"，未返回数据，耗时 {qr.get('elapsed_ms', 0)} ms\n\n"
            )
        if empty_diag and empty_diag.get("is_empty"):
            as_of = empty_diag.get("data_as_of") or "未知"
            cause_md = "\n".join(
                f"- **{c.get('label')}**（{c.get('level')}）：{c.get('detail')}\n"
                f"  - 建议：{c.get('suggestion')}"
                for c in empty_diag.get("causes", [])
            )
            empty_block = (
                "\n**⚠️ 查询成功但未返回数据（数据截至 " + as_of + "）**，可能原因：\n\n"
                f"{cause_md}\n\n"
                "> 我不会自动重试。请确认是否要我按上述建议（如放宽时间窗 / 去掉过滤条件）重新查询？\n\n"
            )
        # 分布分桶表（FR2）：确定性统计结果，直方图由前端据 trace 绘制
        dist_block = ""
        dist = analysis.get("distribution")
        if dist and dist.get("bins"):
            dist_rows = "\n".join(
                f"| {b['label']} | {b['count']} |" for b in dist["bins"]
            )
            dist_block = (
                f"\n**{dist['metric']} 分布**（共 {dist['total_count']} 个取值，"
                f"范围 {dist['min']} ~ {dist['max']}）：\n\n"
                f"| 区间 | 数量 |\n|---|---|\n{dist_rows}\n\n"
            )
        create_guidance = resolve_create_guidance(state.get("validation"))
        # FR12：模板路径没有 LLM 去复述口径，因此直接附确定性口径块（可追溯）
        caliber_block = caliber_markdown(qr.get("caliber") or {})
        return (
            "> 当前未启用 LLM，以下为基于**真实取数结果**的统计摘要。\n\n"
            f"**查询要素**：{json.dumps(state.get('query_elements') or {}, ensure_ascii=False)}\n\n"
            f"{source_line}{error_line}{empty_block}{dist_block}"
            f"**数据统计**：\n\n{stats_table}\n\n"
            f"{analysis.get('summary', '')}\n\n{skills_line}{multi_query_line}"
            + (f"{create_guidance}\n\n" if create_guidance else "")
            + caliber_block
            + "_提示：配置 DMF_LLM-* 后可获得自然语言解读；取数链路为语义层 -> MetricFlow 编译 -> StarRocks。_"
        )
    if intent == "diagnosis":
        analysis = state.get("analysis") or {}
        diag = state.get("diagnosis") or {}
        if diag.get("ok"):
            diag_lines = [diag.get("global_summary", ""), diag.get("smart_summary", "")]
            for item in diag.get("same_contribution_list") or []:
                diag_lines.append(
                    f"- 【{item.get('dim_name')}-{item.get('dim_value')}】影响该指标 {item.get('change_value')}"
                )
            for item in diag.get("opposite_contribution_list") or []:
                diag_lines.append(
                    f"- 【{item.get('dim_name')}-{item.get('dim_value')}】（反向）影响该指标 {item.get('change_value')}"
                )
            hints = diag.get("action_hints") or []
            if hints:
                diag_lines.append("\n**行动建议线索（基于当前数据推断）**：")
                diag_lines.extend(f"- {h}" for h in hints)
            return "**归因分析（基于真实取数结果）**\n\n" + "\n".join(diag_lines)
        return (
            "> 已识别意图：**指标归因**。\n\n"
            f"{diag.get('message', analysis.get('summary', '暂无可分析数据。'))}"
            "\n\n_提示：完整归因（因子归因 / 指标树 / 行动建议）见 MIGRATION Phase 4c 遗留。_"
        )
    if intent == "create":
        cr = state.get("create_result") or {}
        if cr.get("ok"):
            return (
                f"**指标草稿已创建**（FR8）\n\n"
                f"- 指标名：**{cr.get('name')}**\n"
                f"- 草稿 key：`{cr.get('metric_key')}`\n"
                f"- 状态：`{cr.get('status')}`（v{cr.get('version')}）\n\n"
                "> 草稿已写入语义注册中心，需经管理员 **提交审核 → 发布** 后方可用于查询。"
            )
        problems = cr.get("problems") or []
        problem_md = "\n".join(f"- {p}" for p in problems) if problems else ""
        return (
            "**指标创建未能完成**（FR8）\n\n"
            f"{cr.get('message', '校验未通过，请修正后重试。')}\n\n"
            + (f"问题清单：\n{problem_md}\n\n" if problem_md else "")
            + "_提示：请用自然语言说明指标名称与口径，例如『月活客户数，按交易日去重统计客户数』。_"
        )
    if intent == "dimension_query":
        return (
            "维度查询依赖维度元数据（Phase 2 迁移中），当前请改用指标查询，"
            "例如：\"按地区看GMV\"。"
        )
    if intent == "knowledge_qa":
        docs = state.get("retrieved_docs") or []
        if docs:
            top = docs[0]
            return f"根据知识库《{top.get('source') or '文档'}》：\n\n{top.get('content', '')[:1000]}"
        return (
            "知识库尚未配置或未检索到相关内容。\n\n"
            "_提示：配置 Embedding（DMF_LLM-EMBEDDING_*）并入库文档后即可回答知识类问题。_"
        )
    if intent == "help":
        return _HELP_ANSWER
    if intent == "exit":
        return _EXIT_ANSWER
    return (
        "你好！我是 DmfDataAgent 指标分析助手（当前为 mock 模式）。\n\n"
        "可以试着问我：\"查一下近 7 天的 GMV 趋势\"、\"GMV 为什么下降了\"、\"GMV 环比怎么样\"。"
    )


_HELP_ANSWER = (
    "你好！我是 DmfDataAgent 指标问答助手，可以用自然语言帮你查指标、做分析。\n\n"
    "你可以这样问我：\n"
    "- 指标查询：\"查一下近 7 天的 GMV 趋势\"、\"各渠道 GMV 前 10 的门店\"\n"
    "- 同环比：\"GMV 同比 / 环比怎么样\"\n"
    "- 归因分析：\"GMV 为什么下降了\"\n"
    "- 知识问答：\"什么是零售存款日均余额\"\n\n"
    "支持排序 / TopN、过滤、占比、均值等分析；口径以语义层注册的指标为准。"
)

_EXIT_ANSWER = "好的，本次就到这里。需要查数或分析时随时叫我，再见！"


def _control_answer(state: AgentState) -> str | None:
    """help / exit 控制类意图的确定性答复（不走 LLM）。"""
    intent = state.get("intent")
    if intent == "help":
        return _HELP_ANSWER
    if intent == "exit":
        return _EXIT_ANSWER
    return None


def build_synthesize_answer_node(deps: AgentDeps):
    async def synthesize_answer(state: AgentState) -> Dict[str, Any]:
        # 控制类意图（帮助/退出）直接确定性回复，免 LLM 往返
        control = _control_answer(state)
        if control is not None:
            return await _finalize_answer(deps, state, control)

        llm = deps.get_chat_model()
        if llm is not None:
            try:
                resp = await llm.ainvoke(
                    [
                        SystemMessage(content=load_prompt("report")["system"]),
                        HumanMessage(
                            content=(
                                f"上下文信息：\n{_build_context(state)}\n\n"
                                f"用户问题：{user_query(state)}"
                            )
                        ),
                    ]
                )
                content = (
                    resp.content if isinstance(resp.content, str) else str(resp.content)
                )
                # 空内容兜底：LLM 返回空/纯空白时回退确定性模板，避免产出空答复
                if not content.strip():
                    logger.warning("LLM 答复内容为空，回退模板")
                    return await _finalize_answer(
                        deps, state, _template_answer(state), mock_mode=True
                    )
                # FR3：归因解读必须标注"基于当前数据推断"（LLM 未标注时兜底补上）
                if state.get("intent") == "diagnosis":
                    content = _ensure_inference_label(content)
                return await _finalize_answer(deps, state, content)
            except Exception as exc:  # noqa: BLE001 - LLM 失败降级模板
                logger.warning("LLM 答复生成失败，回退模板：%s", exc)

        return await _finalize_answer(deps, state, _template_answer(state), mock_mode=True)

    return synthesize_answer


def _ensure_inference_label(text: str) -> str:
    """FR3：归因解读必须显式标注『基于当前数据推断』。

    LLM 已在 prompt 中收到标注指令，此处做确定性兜底：若输出遗漏则补在末尾，
    已含则不重复追加，保证用户始终能看到数据推断的边界声明。
    """
    if not text:
        return text
    if "基于当前数据推断" in text:
        return text
    return f"{text.rstrip()}\n\n（基于当前数据推断）"


async def _finalize_answer(
    deps: AgentDeps, state: AgentState, text: str, mock_mode: bool = False
) -> Dict[str, Any]:
    """P0-2 输出安全过滤：命中 PII / 危险模式时脱敏或替换为安全提示。"""
    # FR15 安全沙箱：先扫最终答复文本（文本层，刻意不含 drop/delete 等 SQL 关键字，
    # 避免"请删除该过滤条件"这类正常中文答复被误伤）。命中则直接替换为安全提示并审计。
    security = getattr(deps, "security", None)
    sandbox = get_default_sandbox()
    if sandbox is not None:
        sb_res = sandbox.check_text(text)
        if not sb_res["ok"]:
            safe_text = (
                "（系统检测到输出内容含潜在危险模式（FR15 安全沙箱），已做安全处理。"
                "请联系管理员排查。）"
            )
            if security is not None:
                await security.audit_security(
                    event_type="security/sandbox_output",
                    level="high",
                    summary="输出命中 FR15 安全沙箱规则，已拦截",
                    session_id=state.get("session_id") or "",
                    user_id=state.get("user_id") or "",
                    tenant_id=state.get("tenant_id") or "",
                    round_id=state.get("round_id") or "",
                    detail={"hits": sb_res.get("hits", [])},
                )
            return {
                "final_answer": safe_text,
                "mock_mode": mock_mode,
                "output_guarded": True,
                "sandbox_blocked": True,
            }

    if security is not None:
        out_check = security.check_output(text)
        if not out_check.get("safe"):
            safe_text = (
                "（系统检测到输出内容含潜在安全风险或敏感信息，已做安全处理。"
                "请联系管理员排查。）"
            )
            await security.audit_security(
                event_type="security/output_guard",
                level="high",
                summary="输出内容命中安全规则，已拦截/替换",
                session_id=state.get("session_id") or "",
                user_id=state.get("user_id") or "",
                tenant_id=state.get("tenant_id") or "",
                round_id=state.get("round_id") or "",
                detail={"hits": out_check.get("hits", [])},
            )
            return {"final_answer": safe_text, "mock_mode": mock_mode, "output_guarded": True}
        # 否则做 PII 脱敏（保留语义，仅替换敏感值）
        text = out_check.get("sanitized_text") or text
    result: Dict[str, Any] = {"final_answer": text}
    if mock_mode:
        result["mock_mode"] = True
    return result
