"""排序 / TopN 解析（FR1 六要素之「排序」）。

把用户口语中的排序与 TopN（"前10 / Top10 / GMV最高的门店 / 按金额从高到低"）
确定性解析为编译器可消费的规范：

- `order_by`：形如 ``"<指标名> desc"`` / ``"<指标名> asc"``（列名必须是语义层
  指标名，正是 semantic/compiler.py::_order_by_sql 认可的目标）；
- `limit`：TopN 的整数 N。

纯函数、零外部依赖，规则降级路径与 LLM 归一化共用。

排名范围（FR1）：
- `global`（默认，全局 TopN）：ORDER BY + LIMIT 在首次取数即可满足；
- `group`（时间序列组内排名，如"各分行**每月**存款余额排名"）：按时间列
  PARTITION 的窗口名次，编译器在 calc 层用 `ROW_NUMBER() OVER (PARTITION BY …)`
  实现，组内 TopN 走"排名列过滤"而非全局 LIMIT（否则只会留下第一个时间点）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

# TopN 数量词（与 analyze_data 历史行为对齐并做了时间词排除）。
# 注意"前7天/第3季度"是时间不是排名：用负向先行断言排除时间/季度量词。
LIMIT_PATTERNS = [
    re.compile(r"top\s*(\d+)", re.IGNORECASE),
    re.compile(r"前\s*(\d+)\s*(?![天日周月年个季])"),
    re.compile(r"第\s*(\d+)\s*(?![季度天日周月年])"),
    re.compile(r"(\d+)\s*名"),
    re.compile(r"(\d+)\s*强"),
    re.compile(r"(\d+)\s*个(?!月)"),  # "最低的5个门店"；排除"近3个月"
]

# 显式方向词（优先级最高）
_DESC_EXPLICIT = ("降序", "倒序", "递减", "从高到低", "由高到低", "高到低", "由多到少", "从多到少")
_ASC_EXPLICIT = ("升序", "正序", "递增", "从低到高", "由低到高", "低到高", "由少到多", "从少到多")
# 极性词（隐含方向）
_MAX_WORDS = ("最高", "最大", "最多", "最强", "最好", "最优", "排名", "排行", "榜首")
_MIN_WORDS = ("最低", "最小", "最少", "最差", "最弱", "末位")
# 任何一个出现都说明"有排序诉求"（即使没有 N / 方向）
_SORT_SIGNAL = ("排序", "顺序", "sort", "order", "前列", "名次")

_DESC_HINT = tuple(s.lower() for s in _MAX_WORDS)
_ASC_HINT = tuple(s.lower() for s in _MIN_WORDS)

# 排名范围（FR1）：全局排名 vs 时间序列组内排名
RANK_SCOPE_GLOBAL = "global"
RANK_SCOPE_GROUP = "group"

# 显式组内表述
_GROUP_RANK_WORDS = ("组内", "分组内", "分组排名", "各组排名", "各组内", "各组分别排名")
# 周期限定 + 名次诉求（"每月排名 / 各月排名前3 / 按季度排名 / 每周排行"）
_GROUP_RANK_PATTERNS = (
    re.compile(r"(每|各|按)(个)?(月|周|季|季度|年|天|日).{0,8}(排名|排行|名次)"),
    re.compile(r"(排名|排行|名次).{0,8}(每|各)(个)?(月|周|季|季度|年|天|日)"),
    # 省略"排名"二字的组内 TopN / 极值："每月前3 / 各月最高"
    re.compile(r"(每|各|按)(个)?(月|周|季|季度|年|天|日)(的)?(前|top|Top|TOP)\s*\d+"),
    re.compile(r"(每|各|按)(个)?(月|周|季|季度|年|天|日)(的)?(最高|最低|最大|最小|最好|最差)"),
)


def detect_rank_scope(text: str) -> str:
    """判定排名范围：`group`（时间序列组内排名）| `global`（全局排名，默认）。

    只有同时出现**周期限定**与**名次诉求**（或显式"组内/分组"）才判为 group；
    "近半年贷款余额月度趋势与环比"这类纯时间序列（无名次诉求）仍是 global，
    避免凭空多出一个名次列。
    """
    if not text:
        return RANK_SCOPE_GLOBAL
    if any(w in text for w in _GROUP_RANK_WORDS):
        return RANK_SCOPE_GROUP
    if any(p.search(text) for p in _GROUP_RANK_PATTERNS):
        return RANK_SCOPE_GROUP
    return RANK_SCOPE_GLOBAL


@dataclass
class SortSpec:
    metric: Optional[str] = None
    direction: str = "desc"
    limit: Optional[int] = None
    # global（全局排名，默认）| group（时间序列组内排名）
    rank_scope: str = RANK_SCOPE_GLOBAL

    @property
    def order_by(self) -> Optional[str]:
        return f"{self.metric} {self.direction}" if self.metric else None


def parse_limit(text: str) -> Optional[int]:
    """解析 TopN 数量（前N / topN / 第N / N名 / N强）；时间表达式不误伤。"""
    if not text:
        return None
    for pattern in LIMIT_PATTERNS:
        m = pattern.search(text)
        if m:
            try:
                n = int(m.group(1))
            except (ValueError, IndexError):
                continue
            if n >= 1:
                return n
    return None


def _direction(text: str, has_limit: bool) -> Optional[str]:
    """判定排序方向；无任何排序线索时返回 None。优先级：显式方向词 > 极性词 > TopN 默认。"""
    lowered = text.lower()
    explicit_desc = any(w in text for w in _DESC_EXPLICIT) or bool(re.search(r"\bdesc\b", lowered))
    explicit_asc = any(w in text for w in _ASC_EXPLICIT) or bool(re.search(r"\basc\b", lowered))
    if explicit_desc and not explicit_asc:
        return "desc"
    if explicit_asc and not explicit_desc:
        return "asc"
    if any(w in text for w in _MIN_WORDS):
        return "asc"
    if any(w in text for w in _MAX_WORDS):
        return "desc"
    if has_limit or any(w in lowered for w in _SORT_SIGNAL):
        # 仅出现"前N/TopN"或泛泛"排序"，默认取大者在前
        return "desc"
    return None


def _pick_metric(text: str, metrics: List[str]) -> Optional[str]:
    """唯一确定排序指标：单指标直接取；多指标取离排序线索最近者，无法判定返回 None。"""
    metrics = [m for m in metrics if m]
    if not metrics:
        return None
    if len(metrics) == 1:
        return metrics[0]
    cue_positions = [
        text.find(w)
        for w in (*_MAX_WORDS, *_MIN_WORDS, "排序", "降序", "升序", "前", "top", "TOP")
        if text.find(w) >= 0
    ]
    best: Optional[str] = None
    best_dist: Optional[int] = None
    for name in metrics:
        pos = text.find(name)
        if pos < 0:
            continue
        if cue_positions:
            dist = min(abs(pos - c) for c in cue_positions)
            if best_dist is None or dist < best_dist:
                best, best_dist = name, dist
        elif best is None:
            best = name
    return best


def parse_sort(
    text: str, metrics: List[str], dimensions: Optional[List[str]] = None
) -> Optional[SortSpec]:
    """从中文问句解析排序/TopN；无排序诉求返回 None。"""
    if not text:
        return None
    limit = parse_limit(text)
    direction = _direction(text, limit is not None)
    if direction is None and limit is None:
        return None
    metric = _pick_metric(text, metrics)
    return SortSpec(
        metric=metric,
        direction=direction or "desc",
        limit=limit,
        rank_scope=detect_rank_scope(text),
    )


def normalize_llm_order(
    raw: Optional[str], metrics: List[str]
) -> Tuple[Optional[str], Optional[int]]:
    """把 LLM 自由文本 order_by 归一为 ("<指标名> asc|desc", limit)。

    匹配不到候选指标时返回 (None, None)，由调用方回退到中文规则解析。
    """
    if not raw:
        return None, None
    text = str(raw)
    target: Optional[str] = None
    for name in sorted(metrics, key=len, reverse=True):
        if name and name in text:
            target = name
            break
    if target is None:
        return None, None
    lowered = text.lower()
    if re.search(r"\basc\b", lowered) or any(w in text for w in (*_ASC_EXPLICIT, *_MIN_WORDS)):
        direction = "asc"
    elif re.search(r"\bdesc\b", lowered) or any(w in text for w in (*_DESC_EXPLICIT, *_MAX_WORDS)):
        direction = "desc"
    else:
        direction = "desc"
    return f"{target} {direction}", parse_limit(text)
