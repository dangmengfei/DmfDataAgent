"""Skill 包：声明式 SKILL.md 的解析（`package`）、ZIP 整包解压（`archive`）、
输入绑定（`binding`）与沙箱执行（`executor` / `script_runner`）。

Skill 的声明式部分只编排已注册工具与 LLM 提示；包内自带脚本由 `run_skill_script`
白名单工具在沙箱内执行（路径围栏 / 解释器白名单 / 环境剥离 / rlimit / 超时 / 输出上限），
全部调用经 Harness 治理（白名单 / 权限 / 预算 / 超时 / 审计）。
"""

from dmf_agent.skills.archive import (
    ArchiveError,
    ExtractResult,
    cleanup_stale_temp,
    commit,
    discard,
    extract_skill_zip,
    kind_of,
    read_package_file,
    remove_package,
)
from dmf_agent.skills.binding import bind_inputs
from dmf_agent.skills.executor import SkillExecutor
from dmf_agent.skills.package import (
    ALLOWED_SUFFIXES,
    SkillInput,
    SkillPackage,
    SkillParseError,
    SkillStep,
    match_package,
    parse_skill_package,
    parse_skill_package_dir,
    resolve_template,
    unknown_tools,
    validate_inputs,
    with_defaults,
)
from dmf_agent.skills.script_runner import ScriptRunner

__all__ = [
    "ALLOWED_SUFFIXES",
    "ArchiveError",
    "ExtractResult",
    "ScriptRunner",
    "SkillExecutor",
    "SkillInput",
    "SkillPackage",
    "SkillParseError",
    "SkillStep",
    "bind_inputs",
    "cleanup_stale_temp",
    "commit",
    "discard",
    "extract_skill_zip",
    "kind_of",
    "match_package",
    "parse_skill_package",
    "parse_skill_package_dir",
    "read_package_file",
    "remove_package",
    "resolve_template",
    "unknown_tools",
    "validate_inputs",
    "with_defaults",
]
