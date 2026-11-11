"""Skill 执行沙箱：把声明式 skill 包放在 Harness 治理下执行。

沙箱边界 = Harness 的全部约束（见 `harness/controller.py`）：
白名单（未注册工具直接拒）/ 权限点 / 轮次与工具调用预算 / 超时 / 输出 Schema / 审计埋点；
外加本模块自带的**步数上限**与**答复文本的沙箱扫描**（`harness/sandbox.SandboxCheck`）。

skill 能编排的工具：`execute_query` / `retrieve_knowledge` / `analyze_data` / `mcp_tool`，
以及 `run_skill_script`（执行包内自带脚本）。前四者都是既有能力的复用，不新开旁路；
后者是**受控的例外**——脚本在独立子进程里跑，路径围栏 / 解释器白名单 / 环境剥离 /
rlimit / 超时 / 输出上限见 `script_runner.py`，可用 `DMF_SKILL-SCRIPT_ENABLED=false` 整体关闭。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from dmf_agent.analysis import compute_distribution, compute_stats, format_distribution
from dmf_agent.harness import HarnessContext, HarnessState
from dmf_agent.skills.package import (
    SkillPackage,
    resolve_template,
    unknown_tools,
    validate_inputs,
    with_defaults,
)

logger = logging.getLogger(__name__)

# 兜底白名单（未装配 ToolRegistry 时用于预检；Harness 装配时以其注册表为准）。
# 与 build_default_tool_registry 保持一致（含 run_skill_script）。
_FALLBACK_ALLOWED_TOOLS = [
    "execute_query",
    "retrieve_knowledge",
    "analyze_data",
    "mcp_tool",
    "run_skill_script",
]

_MAX_SUMMARY_LEN = 400


class SkillExecutor:
    """声明式 skill 的受治理执行器（沙箱）。

    `allowed_tools` 缺省取 Harness 注册表；预检不通过时直接返回可读原因，
    不经 Harness 抛异常（两条路径都以"不执行"为结果，预检只是让报错更友好）。
    """

    def __init__(
        self,
        *,
        harness: Any = None,
        metric_engine: Any = None,
        retriever: Any = None,
        plugin_registry: Any = None,
        get_chat_model: Optional[Callable[[], Any]] = None,
        sandbox: Any = None,
        script_runner: Any = None,
        script_enabled: bool = True,
        allowed_tools: Optional[List[str]] = None,
        max_steps: int = 8,
    ) -> None:
        self._harness = harness
        self._metric_engine = metric_engine
        self._retriever = retriever
        self._plugin_registry = plugin_registry
        self._get_chat_model = get_chat_model
        self._sandbox = sandbox
        self._script_runner = script_runner
        self._script_enabled = bool(script_enabled)
        self._max_steps = int(max_steps or 0)
        if allowed_tools is None:
            registry = getattr(harness, "registry", None)
            allowed_tools = (
                registry.names() if registry is not None else list(_FALLBACK_ALLOWED_TOOLS)
            )
        self._allowed_tools = list(allowed_tools)

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    async def run(
        self,
        pkg: SkillPackage,
        inputs: Optional[Dict[str, Any]] = None,
        ctx: Optional[HarnessContext] = None,
        state: Optional[HarnessState] = None,
    ) -> Dict[str, Any]:
        """沙箱内执行一个 skill 包；任何失败都回 `{ok: False, message}`，不冒泡。"""
        missing = validate_inputs(pkg, inputs)
        if missing:
            return {
                "ok": False,
                "skill": pkg.name,
                "message": f"缺少必填输入：{'、'.join(missing)}",
                "steps": [],
            }

        if self._max_steps > 0 and len(pkg.steps) > self._max_steps:
            return {
                "ok": False,
                "skill": pkg.name,
                "message": f"步骤数 {len(pkg.steps)} 超过沙箱上限 {self._max_steps}",
                "steps": [],
            }

        if not pkg.steps:
            return {
                "ok": False,
                "skill": pkg.name,
                "message": "该 skill 未声明 steps，无可执行内容（内置能力由主图直接编排）",
                "steps": [],
            }

        unknown = unknown_tools(pkg, self._allowed_tools)
        if unknown:
            return {
                "ok": False,
                "skill": pkg.name,
                "message": f"使用了未在 Harness 白名单注册的工具：{'、'.join(unknown)}",
                "steps": [],
            }

        resolved_inputs = with_defaults(pkg, inputs)
        ctx = ctx or HarnessContext()
        state = state or HarnessState()
        pkg_dir = pkg.package_dir or ""

        trace: List[Dict[str, Any]] = []
        step_results: List[Any] = []

        for idx, step in enumerate(pkg.steps):
            arguments = resolve_template(step.arguments, resolved_inputs, step_results)
            if not isinstance(arguments, dict):
                arguments = {}
            started = time.time()
            try:
                output = await self._invoke(step.tool, arguments, ctx, state, pkg.body, pkg_dir)
            except Exception as exc:  # noqa: BLE001 - 失败回传原因，沙箱不冒泡
                logger.info("Skill「%s」第 %d 步 %s 失败：%s", pkg.name, idx + 1, step.tool, exc)
                trace.append(
                    {
                        "index": idx,
                        "tool": step.tool,
                        "status": "error",
                        "elapsed_ms": int((time.time() - started) * 1000),
                        "error": str(exc),
                    }
                )
                return {
                    "ok": False,
                    "skill": pkg.name,
                    "message": f"第 {idx + 1} 步「{step.tool}」执行失败：{exc}",
                    "inputs": resolved_inputs,
                    "steps": trace,
                    "harness": state.snapshot(),
                }

            step_results.append(output)
            trace.append(
                {
                    "index": idx,
                    "tool": step.tool,
                    "status": "ok",
                    "elapsed_ms": int((time.time() - started) * 1000),
                    "summary": _summarize(output),
                    "result": _truncate(output),
                }
            )

        answer = await self._compose_answer(pkg, resolved_inputs, step_results, trace)
        guard = self._scan(answer)
        if not guard["ok"]:
            answer = "技能输出命中安全沙箱规则，已拦截（请检查 skill 内容）。"
            logger.warning("Skill「%s」答复命中沙箱规则：%s", pkg.name, guard["hits"])

        return {
            "ok": True,
            "skill": pkg.name,
            "inputs": resolved_inputs,
            "steps": trace,
            "answer": answer,
            "harness": state.snapshot(),
            "sandbox": guard,
        }

    # ------------------------------------------------------------------
    # 工具分派（复用既有实现，不新造能力）
    # ------------------------------------------------------------------

    async def _invoke(
        self,
        tool: str,
        arguments: Dict[str, Any],
        ctx: HarnessContext,
        state: HarnessState,
        skill_body: str = "",
        pkg_dir: str = "",
    ) -> Dict[str, Any]:
        async def _raw() -> Dict[str, Any]:
            return await self._dispatch(tool, arguments, skill_body, pkg_dir)

        if self._harness is None:
            return await _raw()
        return await self._harness.invoke(tool, arguments, ctx, state, _raw)

    async def _dispatch(
        self, tool: str, arguments: Dict[str, Any], skill_body: str = "", pkg_dir: str = ""
    ) -> Dict[str, Any]:
        if tool == "execute_query":
            if self._metric_engine is None:
                raise RuntimeError("语义引擎未装配，无法取数")
            return await self._metric_engine.query(arguments.get("dsl") or {})

        if tool == "retrieve_knowledge":
            if self._retriever is None:
                raise RuntimeError("检索器未装配，无法召回知识")
            docs = await self._retriever.retrieve(str(arguments.get("query") or ""))
            return {"docs": docs, "count": len(docs)}

        if tool == "analyze_data":
            query_result = arguments.get("query_result")
            if not isinstance(query_result, dict):
                raise RuntimeError("analyze_data 需要上一步的 query_result（对象）")
            stats = compute_stats(query_result)
            out: Dict[str, Any] = {"stats": stats}
            # skill 正文提到「分布」时附带确定性分桶（与 FR2 的 analyze 节点同口径）
            if "分布" in (skill_body or "") or arguments.get("distribution"):
                dist = compute_distribution(query_result, arguments.get("metric"))
                if dist:
                    out["distribution"] = dist
            return out

        if tool == "mcp_tool":
            if self._plugin_registry is None:
                raise RuntimeError("plugin_registry 未装配，无法调用 MCP")
            return await self._plugin_registry.call_mcp_tool(
                str(arguments.get("server") or ""),
                str(arguments.get("tool") or ""),
                arguments.get("arguments") or {},
            )

        if tool == "run_skill_script":
            if not self._script_enabled:
                raise RuntimeError("skill 脚本执行已被禁用（DMF_SKILL-SCRIPT_ENABLED=false）")
            if self._script_runner is None:
                raise RuntimeError("脚本执行器未装配，无法执行包内脚本")
            if not pkg_dir:
                raise RuntimeError("该 skill 没有落盘包目录，无法执行包内脚本（请以 ZIP 整包上传）")
            result = await self._script_runner.run(
                str(arguments.get("script") or ""),
                arguments.get("args") or [],
                cwd=pkg_dir,
            )
            if not result.get("ok"):
                raise RuntimeError(result.get("message") or "脚本执行失败")
            return result

        raise RuntimeError(f"未支持的工具：{tool}")

    # ------------------------------------------------------------------
    # 答复
    # ------------------------------------------------------------------

    async def _compose_answer(
        self,
        pkg: SkillPackage,
        inputs: Dict[str, Any],
        step_results: List[Any],
        trace: List[Dict[str, Any]],
    ) -> str:
        llm = self._get_chat_model() if callable(self._get_chat_model) else None
        if llm is not None:
            try:
                from langchain_core.messages import HumanMessage, SystemMessage

                payload = {
                    "inputs": inputs,
                    "steps": [
                        {"tool": t["tool"], "summary": t.get("summary", "")} for t in trace
                    ],
                    "results": [_truncate(r) for r in step_results],
                }
                resp = await llm.ainvoke(
                    [
                        SystemMessage(content=pkg.body or "你是数据分析助手，请基于给定结果作答。"),
                        HumanMessage(
                            content=(
                                f"Skill「{pkg.name}」的执行结果（JSON）：\n"
                                f"{json.dumps(payload, ensure_ascii=False, default=str)}"
                            )
                        ),
                    ]
                )
                content = resp.content if isinstance(resp.content, str) else str(resp.content)
                if content.strip():
                    return content
                logger.warning("Skill「%s」LLM 答复为空，回退模板", pkg.name)
            except Exception:  # noqa: BLE001 - LLM 失败降级模板，不阻断
                logger.warning("Skill「%s」LLM 解读失败，回退模板", pkg.name, exc_info=True)
        return _template_answer(pkg, inputs, step_results, trace)

    def _scan(self, text: str) -> Dict[str, Any]:
        sandbox = self._sandbox
        if sandbox is None:
            from dmf_agent.harness.sandbox import get_default_sandbox

            sandbox = get_default_sandbox()
        if sandbox is None:
            return {"ok": True, "hits": []}
        hits = sandbox.scan_text(text)
        return {"ok": not hits, "hits": hits}


def _truncate(value: Any, limit: int = _MAX_SUMMARY_LEN) -> Any:
    """结果预览：超大结果转成截断字符串，避免把整表塞进响应。"""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) <= limit:
        return value
    return text[:limit] + f"…（已截断，共 {len(text)} 字符）"


def _summarize(result: Any) -> str:
    """确定性的一句话结果摘要（模板答复与步骤轨迹共用）。"""
    if not isinstance(result, dict):
        return _as_text(result)

    if "columns" in result and "rows" in result:
        rows = result.get("rows") or []
        cols = result.get("columns") or []
        if result.get("error"):
            return f"取数失败：{result['error']}"
        return f"取到 {len(rows)} 行 × {len(cols)} 列"

    if "docs" in result:
        return f"召回 {result.get('count', len(result.get('docs') or []))} 篇文档"

    if "exit_code" in result:
        # 包内脚本执行结果：优先给出 stdout 首行，便于步骤轨迹里一眼看到产出
        stdout = (result.get("stdout") or "").strip()
        head = stdout.splitlines()[0] if stdout else ""
        base = f"脚本 {result.get('script', '')} 退出码 {result.get('exit_code')}"
        return f"{base}：{head}" if head else base

    if "stats" in result:
        stats = result.get("stats") or {}
        if not stats:
            return "无数值列可统计"
        parts = []
        for col, s in list(stats.items())[:3]:
            parts.append(f"{col}: 合计 {s.get('total')} / 均值 {s.get('avg')}")
        dist = result.get("distribution")
        if dist:
            parts.append(f"（分布 {len(dist.get('bins') or [])} 个区间）")
        return "；".join(parts)

    if result.get("ok") is False:
        return f"调用失败：{result.get('message') or '未知原因'}"

    if "result" in result:
        return _as_text(result["result"])

    return _as_text(result)


def _as_text(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= _MAX_SUMMARY_LEN else text[:_MAX_SUMMARY_LEN] + "…"


def _template_answer(
    pkg: SkillPackage,
    inputs: Dict[str, Any],
    step_results: List[Any],
    trace: List[Dict[str, Any]],
) -> str:
    """LLM 不可用时的确定性答复（只描述真实执行结果，不编造）。"""
    lines = [f"Skill「{pkg.name}」执行完成（共 {len(trace)} 步）。"]
    if pkg.description:
        lines.append(pkg.description)
    if inputs:
        rendered = "、".join(f"{k}={v}" for k, v in inputs.items())
        lines.append(f"输入：{rendered}")
    for item in trace:
        lines.append(f"{item['index'] + 1}. {item['tool']}：{item.get('summary', '')}")
    for result in step_results:
        if isinstance(result, dict) and result.get("distribution"):
            lines.append(format_distribution(result["distribution"]))
    return "\n".join(lines)
