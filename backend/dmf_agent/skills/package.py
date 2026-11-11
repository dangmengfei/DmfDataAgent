"""Skill 包（声明式 SKILL.md）：解析、校验与模板解析。

Skill 是**声明式**的——frontmatter 声明可用工具 / 输入 / 步骤，正文是给 LLM 的指令。
本模块只做解析与校验，**不执行任何代码**；执行见 `executor.py`，全程经 Harness 治理
（白名单 / 权限 / 预算 / 超时 / 输出 Schema）。

上传形态是**一个 ZIP 整包**（`skills/archive.py` 负责安全解压与落盘）：包根含 `SKILL.md`，
可附带脚本、文档、数据等任意文件（脚本的执行见 `script_runner.py` 与 `run_skill_script` 工具）。

`SKILL.md` 格式::

    ---
    name: deposit_branch_analysis
    description: 按分行做存款专题归因分析
    version: 1
    triggers: [存款专题, 分行存款]
    tools: [execute_query, analyze_data]
    inputs:
      - name: metric
        description: 指标名，如 存款余额
        required: true
    steps:
      - tool: execute_query
        arguments: {dsl: {metrics: [{name: "${metric}"}], dimensions: ["分行"]}}
      - tool: run_skill_script
        arguments: {script: "scripts/render.py", args: ["--top", "5"]}
      - tool: analyze_data
        arguments: {query_result: "${steps.0.result}"}
    ---
    # 存款专题分析
    结合上一步取数结果，按分行归因存款变化并给出结论。

`triggers` 是**确定性兜底触发词**（见 `match_package`）：主图 `execute_skill` 节点
默认走**模型驱动**（LLM 从 skill 清单里选），LLM 不可用或未选中时才回落到 triggers 匹配。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dmf_agent.skills.archive import SKILL_MD_NAME

# 允许上传的 skill 包后缀（整包上传，包内可含脚本与文档）
ALLOWED_SUFFIXES = (".zip",)

_FRONTMATTER_RE = re.compile(r"\A\s*---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?(.*)\Z", re.DOTALL)
_PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")


class SkillParseError(ValueError):
    """Skill 包解析/校验失败（原因面向使用者，中文）。"""


@dataclass
class SkillInput:
    name: str
    description: str = ""
    required: bool = False
    default: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "required": self.required,
            "default": self.default,
        }


@dataclass
class SkillStep:
    tool: str
    arguments: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"tool": self.tool, "arguments": self.arguments}


@dataclass
class SkillPackage:
    name: str
    description: str = ""
    version: str = "1"
    tools: List[str] = field(default_factory=list)
    inputs: List[SkillInput] = field(default_factory=list)
    steps: List[SkillStep] = field(default_factory=list)
    body: str = ""
    raw: str = ""
    filename: str = ""
    size: int = 0
    sha256: str = ""
    # 对话触发词（FR14）：命中即由主图 execute_skill 节点选中该 skill，见 match_package
    triggers: List[str] = field(default_factory=list)
    # 整包上传（zip）：包内文件清单（相对包根的 POSIX 路径 + 大小 + sha256 + 类别），
    # 不含 SKILL.md 自身；文件内容落在磁盘，见 skills/archive.py
    files: List[Dict[str, Any]] = field(default_factory=list)
    # 包落盘目录（相对路径，如 data/skills/<name>）；脚本执行以它为工作目录
    package_dir: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """落库用的完整表示（存进 `PluginModel.config`，无需新增表/列）。"""
        return {
            "format": "skill.md",
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "tools": list(self.tools),
            "inputs": [i.to_dict() for i in self.inputs],
            "steps": [s.to_dict() for s in self.steps],
            "body": self.body,
            "raw": self.raw,
            "filename": self.filename,
            "size": self.size,
            "sha256": self.sha256,
            "triggers": list(self.triggers),
            "files": [dict(f) for f in self.files],
            "package_dir": self.package_dir,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SkillPackage":
        """与 `to_dict` 对称的解码（读库路径）；字段缺失时按空值兜底，不抛错。"""
        data = data or {}

        def _inputs() -> List[SkillInput]:
            out: List[SkillInput] = []
            for item in data.get("inputs") or []:
                if isinstance(item, str):
                    out.append(SkillInput(name=item))
                elif isinstance(item, dict) and item.get("name"):
                    out.append(
                        SkillInput(
                            name=str(item["name"]),
                            description=str(item.get("description") or ""),
                            required=bool(item.get("required")),
                            default=item.get("default"),
                        )
                    )
            return out

        def _steps() -> List[SkillStep]:
            out: List[SkillStep] = []
            for item in data.get("steps") or []:
                if isinstance(item, dict) and item.get("tool"):
                    args = item.get("arguments")
                    out.append(
                        SkillStep(tool=str(item["tool"]), arguments=args if isinstance(args, dict) else {})
                    )
            return out

        return cls(
            name=str(data.get("name") or ""),
            description=str(data.get("description") or ""),
            version=str(data.get("version") or "1"),
            tools=[str(t) for t in (data.get("tools") or [])],
            inputs=_inputs(),
            steps=_steps(),
            body=str(data.get("body") or ""),
            raw=str(data.get("raw") or ""),
            filename=str(data.get("filename") or ""),
            size=int(data.get("size") or 0),
            sha256=str(data.get("sha256") or ""),
            triggers=_as_trigger_list(data.get("triggers")),
            files=[dict(f) for f in (data.get("files") or []) if isinstance(f, dict)],
            package_dir=str(data.get("package_dir") or ""),
        )

    def required_inputs(self) -> List[str]:
        return [i.name for i in self.inputs if i.required]

    def step_tools(self) -> List[str]:
        """实际会被调用的工具（取自 steps；未声明 steps 时回落到 frontmatter 的 tools）。"""
        return [s.tool for s in self.steps] or list(self.tools)


def _as_str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v is not None]
    return [str(value)]


def _as_trigger_list(value: Any) -> List[str]:
    """触发词：列表直接用；字符串按中英文逗号/顿号切分（作者常写成一行）。"""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else re.split(r"[,，、]", str(value))
    return [str(v).strip() for v in items if str(v).strip()]


def parse_skill_package(text: str, filename: str = "") -> SkillPackage:
    """解析 SKILL.md 文本为 `SkillPackage`；不合法时抛 `SkillParseError`（中文原因）。"""
    if text is None:
        raise SkillParseError("文件内容为空")
    text = text.lstrip("﻿")
    if not text.strip():
        raise SkillParseError("文件内容为空")

    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise SkillParseError("缺少 YAML frontmatter：文件需以 `---` 开头，以 `---` 结束元信息块")

    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - 依赖缺失
        raise SkillParseError("解析 frontmatter 需要 PyYAML，请先安装") from exc

    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise SkillParseError(f"frontmatter YAML 解析失败：{exc}") from exc

    if not isinstance(meta, dict):
        raise SkillParseError("frontmatter 需为键值映射（YAML 对象）")

    name = str(meta.get("name") or "").strip()
    if not name:
        raise SkillParseError("frontmatter 缺少必填字段 `name`")

    raw_steps = meta.get("steps")
    if raw_steps is not None and not isinstance(raw_steps, list):
        raise SkillParseError("`steps` 需为列表")

    steps: List[SkillStep] = []
    for idx, item in enumerate(raw_steps or []):
        if not isinstance(item, dict) or not item.get("tool"):
            raise SkillParseError(f"第 {idx + 1} 个 step 缺少 `tool` 字段")
        args = item.get("arguments")
        if args is not None and not isinstance(args, dict):
            raise SkillParseError(f"第 {idx + 1} 个 step 的 `arguments` 需为对象")
        steps.append(SkillStep(tool=str(item["tool"]).strip(), arguments=args or {}))

    inputs: List[SkillInput] = []
    raw_inputs = meta.get("inputs")
    if raw_inputs is not None and not isinstance(raw_inputs, list):
        raise SkillParseError("`inputs` 需为列表")
    for idx, item in enumerate(raw_inputs or []):
        if isinstance(item, str):
            inputs.append(SkillInput(name=item.strip()))
        elif isinstance(item, dict) and item.get("name"):
            inputs.append(
                SkillInput(
                    name=str(item["name"]).strip(),
                    description=str(item.get("description") or ""),
                    required=bool(item.get("required")),
                    default=item.get("default"),
                )
            )
        else:
            raise SkillParseError(f"第 {idx + 1} 个 input 缺少 `name` 字段")

    declared_tools = _as_str_list(meta.get("tools"))
    tools = declared_tools or [s.tool for s in steps]

    body = (match.group(2) or "").strip()
    encoded = text.encode("utf-8")

    return SkillPackage(
        name=name,
        description=str(meta.get("description") or "").strip(),
        version=str(meta.get("version") or "1"),
        tools=tools,
        inputs=inputs,
        steps=steps,
        body=body,
        raw=text,
        filename=filename,
        size=len(encoded),
        sha256=hashlib.sha256(encoded).hexdigest(),
        triggers=_as_trigger_list(meta.get("triggers") or meta.get("keywords")),
    )


def parse_skill_package_dir(
    root: Path, package_dir: str = "", filename: str = ""
) -> SkillPackage:
    """从解压后的包目录解析 skill：读 `<root>/SKILL.md`，并回填包位置。

    `package_dir` 是包在磁盘上的位置，脚本执行以它为工作目录；留空则记录 `root` 本身。
    `filename` 记录**上传时的压缩包名**（展示用）。包内文件清单由调用方
    （`skills/archive.py` 的 `extract_skill_zip`）扫描后回填到 `pkg.files`。
    """
    skill_md = Path(root) / SKILL_MD_NAME
    if not skill_md.is_file():
        raise SkillParseError(f"包内未找到 {SKILL_MD_NAME}")
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        raise SkillParseError(f"读取 {SKILL_MD_NAME} 失败：{exc}") from exc

    pkg = parse_skill_package(text, filename=filename or SKILL_MD_NAME)
    pkg.package_dir = package_dir or str(root)
    return pkg


def match_package(query: str, packages: List[SkillPackage]) -> Optional[SkillPackage]:
    """按 triggers 确定性命中 skill：命中词最多的胜出，同分按传入顺序（= 注册顺序）。

    skill 名本身算一个隐式 trigger（用户直接点名时也能触发）。无命中返回 None。
    纯字符串包含匹配——无 LLM 也能工作，守住降级矩阵。
    """
    text = (query or "").strip()
    if not text:
        return None
    best: Optional[SkillPackage] = None
    best_score = 0
    for pkg in packages or []:
        hits = [t for t in list(pkg.triggers) + [pkg.name] if t and t in text]
        if len(hits) > best_score:
            best, best_score = pkg, len(hits)
    return best


def validate_inputs(pkg: SkillPackage, inputs: Optional[Dict[str, Any]]) -> List[str]:
    """返回缺失的必填输入名（空列表 = 通过）；未提供的可选输入按 default 补齐由调用方处理。"""
    provided = inputs or {}
    return [name for name in pkg.required_inputs() if provided.get(name) in (None, "")]


def with_defaults(pkg: SkillPackage, inputs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把未提供的可选输入的 default 补齐（default 为 None 的仍不注入）。"""
    merged: Dict[str, Any] = dict(inputs or {})
    for spec in pkg.inputs:
        if spec.name not in merged and spec.default is not None:
            merged[spec.name] = spec.default
    return merged


def unknown_tools(pkg: SkillPackage, allowed: List[str]) -> List[str]:
    """包内声明/使用但不在白名单的工具名（去重保序）。"""
    allowed_set = set(allowed or [])
    seen: List[str] = []
    for tool in pkg.step_tools():
        if tool not in allowed_set and tool not in seen:
            seen.append(tool)
    return seen


def _resolve_expr(
    expr: str, inputs: Dict[str, Any], step_results: List[Any]
) -> Tuple[Any, bool]:
    """解析 `${...}` 表达式；返回 (值, 是否解析成功)。

    支持：`${metric}` / `${inputs.metric}`（输入）、`${steps.0.result}`（第 0 步结果）、
    `${steps.0.result.rows}`（结果内的点号路径）。
    """
    parts = [p for p in expr.strip().split(".") if p]
    if not parts:
        return None, False

    if parts[0] == "steps":
        if len(parts) < 2 or not parts[1].isdigit():
            return None, False
        idx = int(parts[1])
        if idx >= len(step_results):
            return None, False
        cur = step_results[idx]
        rest = parts[2:]
        if rest and rest[0] == "result":
            rest = rest[1:]
        for key in rest:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            elif isinstance(cur, list) and key.isdigit() and int(key) < len(cur):
                cur = cur[int(key)]
            else:
                return None, False
        return cur, True

    if parts[0] == "inputs":
        parts = parts[1:]
    if len(parts) == 1 and parts[0] in (inputs or {}):
        return inputs[parts[0]], True
    return None, False


def resolve_template(
    value: Any, inputs: Dict[str, Any], step_results: List[Any]
) -> Any:
    """递归解析参数模板里的 `${...}` 占位符。

    整串恰为一个占位符时返回**原对象**（保持 dict/list 类型，便于把上一步的
    `query_result` 直接喂给 `analyze_data`）；解析失败的占位符**原样保留**，
    交由下游的参数 Schema 校验报错，不静默吞掉。
    """
    if isinstance(value, str):
        whole = _PLACEHOLDER_RE.fullmatch(value.strip())
        if whole is not None:
            resolved, ok = _resolve_expr(whole.group(1), inputs, step_results)
            return resolved if ok else value

        def _sub(m: re.Match[str]) -> str:
            resolved, ok = _resolve_expr(m.group(1), inputs, step_results)
            return str(resolved) if ok else m.group(0)

        return _PLACEHOLDER_RE.sub(_sub, value)

    if isinstance(value, dict):
        return {k: resolve_template(v, inputs, step_results) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_template(v, inputs, step_results) for v in value]
    return value
