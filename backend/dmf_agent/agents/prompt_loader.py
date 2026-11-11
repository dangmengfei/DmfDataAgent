"""Prompt 模板加载器 + 运行时覆盖（供「系统提示词管理」接口使用）。

- 基线：agents/prompts/*.yaml（代码仓库内，只读基线，版本 1.0.0）；
- 运行时覆盖：PromptManager 从 DB / 内存注入，set_override 后立即生效，无需重启；
- 版本规则：基线 1.0.0，每次保存 / 回滚按 patch 递增（1.0.1、1.0.2 …）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).parent / "prompts"

# 内置基线版本号
BASELINE_VERSION = "1.0.0"

# 模板说明（管理端展示）
PROMPT_DESCRIPTIONS: Dict[str, str] = {
    "intent": "意图识别：判定 metric_query / diagnosis / knowledge_qa 等处理链路与追加技能",
    "elements": "语义解析：抽取指标、维度、时间、过滤、同环比等查询要素",
    "time_llm": "时间解析：相对时间 / 节假日 → 绝对时间区间",
    "analysis": "数据分析：查询结果的确定性统计解读",
    "analysis_adv": "深度分析：趋势 / 结构 / 归因的进阶解读",
    "report": "回答生成：组织查询与分析结果为最终回答",
    "relevance": "记忆相关性：判断当前问题是否依赖历史上下文",
    "compression": "历史压缩：超长会话的滚动摘要",
}

# 运行时覆盖：name -> {version, system, human, source, updated_at}
_OVERRIDES: Dict[str, Dict[str, Any]] = {}

# FR12 灰度 resolver：由 PromptManager.reload 注册；load_prompt 命中金丝雀时覆盖内容
_ROLLOUT_RESOLVER: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None

def set_rollout_resolver(fn: Optional[Callable[[str], Optional[Dict[str, Any]]]]) -> None:
    global _ROLLOUT_RESOLVER
    _ROLLOUT_RESOLVER = fn

def clear_rollout_resolver() -> None:
    global _ROLLOUT_RESOLVER
    _ROLLOUT_RESOLVER = None


def prompt_names() -> List[str]:
    """扫描目录得到全部模板名。"""
    if not _PROMPT_DIR.exists():
        return []
    return sorted(p.stem for p in _PROMPT_DIR.glob("*.yaml") if not p.name.startswith("_"))


def _baseline(name: str) -> Dict[str, Any]:
    file = _PROMPT_DIR / f"{name}.yaml"
    if not file.exists():
        raise FileNotFoundError(f"Prompt 模板不存在：{file}")
    with open(file, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_prompt(name: str) -> Dict[str, Any]:
    """读取 prompt：优先运行时覆盖，否则回落到本地 YAML 基线；FR12 灰度时段按请求上下文注入金丝雀。返回 {"system", "human"}（对 .format 调用方透明）。"""
    override = _OVERRIDES.get(name)
    if override and override.get("system"):
        result = {"system": override["system"], "human": override.get("human", "")}
    else:
        base = _baseline(name)
        result = {"system": base.get("system", ""), "human": base.get("human", "")}
    if _ROLLOUT_RESOLVER is not None:
        canary = _ROLLOUT_RESOLVER(name)
        if canary:
            result = {"system": canary.get("system", ""), "human": canary.get("human", "")}
    return result


def baseline_spec(name: str) -> Dict[str, Any]:
    """内置基线规格（版本 / 内容 / 描述）。"""
    base = _baseline(name)
    return {
        "name": name,
        "version": BASELINE_VERSION,
        "system": base.get("system", ""),
        "human": base.get("human", ""),
        "description": PROMPT_DESCRIPTIONS.get(name, ""),
        "source": "builtin",
        "source_version": "",
        "created_at": 0,
        "updated_at": 0,
    }


def set_override(
    name: str,
    version: str,
    system: str,
    human: str = "",
    source: str = "memory",
    updated_at: int = 0,
) -> None:
    """注入 / 更新运行时覆盖（立即生效）。"""
    _OVERRIDES[name] = {
        "version": version,
        "system": system,
        "human": human,
        "source": source,
        "updated_at": updated_at,
    }


def clear_overrides() -> None:
    _OVERRIDES.clear()


def active_meta() -> Dict[str, Dict[str, Any]]:
    """各模板当前生效版本信息（未覆盖的回落基线）。"""
    meta: Dict[str, Dict[str, Any]] = {}
    for name in prompt_names():
        override = _OVERRIDES.get(name)
        if override:
            meta[name] = {
                "version": override["version"],
                "source": override.get("source", "memory"),
                "updated_at": override.get("updated_at", 0),
            }
        else:
            meta[name] = {"version": BASELINE_VERSION, "source": "builtin", "updated_at": 0}
    return meta


def version_key(v: str) -> tuple:
    """x.y.z → 数值排序键。"""
    try:
        parts = (v or "").split(".")
        return (int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)
    except (ValueError, IndexError):
        return (0, 0, 0)


def next_patch_version(versions: List[str]) -> str:
    """在给定版本集合（含基线）基础上取最大版本并 patch +1。"""
    all_versions = list(versions) + [BASELINE_VERSION]
    latest = max(all_versions, key=version_key)
    major, minor, patch = version_key(latest)
    return f"{major}.{minor}.{patch + 1}"
