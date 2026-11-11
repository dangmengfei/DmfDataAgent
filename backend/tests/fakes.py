"""测试替身（主流程脱离指标平台后）。

主流程取数链路为 语义层(manifest.yml) -> MetricFlowSqlCompiler -> StarRocks，
单测不能依赖真实 StarRocks，因此用一个与 `MetricFlowEngine` 同契约的
`FakeMetricEngine` 替身（compile / query / dimension_values / health / describe）：

- 结果确定、可断言（时间序列 + 维度组合笛卡尔积 + 单调递增的指标值）；
- `mock` 字段恒为 False —— 主流程已无 mock 分支，取数失败一律以 error 抛出；
- `fail_with` 用于模拟 StarRocks 未配置 / 查询失败，供 execute_query 节点降级路径测试。

`make_deps()` 组装一个无 LLM、无外部依赖的 AgentDeps（规则降级 + 假引擎）。
"""

from __future__ import annotations

import itertools
from typing import Any, Dict, List, Optional, Sequence

from dmf_agent.agents.metric_agent.deps import AgentDeps
from dmf_agent.config.settings import Settings
from dmf_agent.recall import DimensionRecaller, MetricRecaller
from dmf_agent.semantic.layer import SemanticLayer

# 默认时间点（>=2 个，保证 metric_change 可算；单调递增保证统计量 > 0）
DEFAULT_DATES = ("2026-08-01", "2026-08-04", "2026-08-07")
# 默认各维度的维值池（按 dsl.dimensions 取用）
DEFAULT_DIM_VALUE_SETS: Dict[str, List[str]] = {
    "门店": ["A店", "B店"],
    "渠道": ["线上", "门店"],
    "地区": ["华东", "华南"],
    "品类": ["家电", "数码"],
    "平台": ["APP", "小程序"],
}


class FakeMetricEngine:
    """`MetricFlowEngine` 测试替身：不编译 SQL、不连 StarRocks。"""

    def __init__(
        self,
        layer: Optional[SemanticLayer] = None,
        dates: Sequence[str] = DEFAULT_DATES,
        dim_value_sets: Optional[Dict[str, List[str]]] = None,
        base_value: float = 100.0,
        step_value: float = 20.0,
        metric_offset: float = 5.0,
        fail_with: Optional[str] = None,
        empty: bool = False,
        data_as_of_date: Optional[str] = None,
    ) -> None:
        self.layer = layer
        self.dates = list(dates)
        self.dim_value_sets = {**DEFAULT_DIM_VALUE_SETS, **(dim_value_sets or {})}
        self.base_value = base_value
        self.step_value = step_value
        self.metric_offset = metric_offset
        self.fail_with = fail_with
        # 空数据场景：empty=True 时，时间窗结束日晚于 data_as_of_date 的查询返回 0 行
        # （模拟「查了未来日期」）；窗口已落在截至日及之前时仍正常返回，用于验证
        # 「确认改查最新数据」后续跑能取到数。data_as_of_date 缺省则一律空。
        self.empty = empty
        self.data_as_of_date = data_as_of_date
        # 记录 query 收到的 dsl，便于断言"二次取数确实走了语义引擎"
        self.queries: List[Dict[str, Any]] = []

    # ---------------- 编译（只回放，不产 SQL） ----------------

    def compile(self, source: Any) -> Dict[str, Any]:
        return {"sql": "-- fake metricflow sql", "columns": [], "models": []}

    def _is_beyond_as_of(self, source: Dict[str, Any]) -> bool:
        """该 DSL 的时间窗结束日是否晚于数据截至日（无截至配置时视为越界）。"""
        if not self.empty:
            return False
        if not self.data_as_of_date:
            return True
        tr = source.get("time_range") or {}
        end = str(tr.get("end") or "")[:10]
        return not end or end > self.data_as_of_date

    # ---------------- 取数 ----------------

    async def query(
        self, source: Dict[str, Any], row_scopes: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        self.queries.append(source)

        metrics = [m.get("name") for m in (source.get("metrics") or []) if m.get("name")]
        time_dim = self.layer.time_dimension_name() if self.layer is not None else "统计日期"
        dims = [d for d in (source.get("dimensions") or []) if d and d != time_dim]
        columns = [time_dim or "统计日期", *dims, *metrics]

        if self._is_beyond_as_of(source):
            # 真实 StarRocks 行为：无 GROUP BY 的纯聚合在时间窗无匹配行时仍返回
            # **一行全 NULL**（SUM(空)=NULL，row_count=1），而不是 0 行。
            # 带维度时才有 GROUP BY，才真是 0 行。旧版 fake 一律给 0 行，
            # 掩盖了 row_count==1/rows=[[None]] 会让空数据回溯（FR5 卡片）不触发的缺陷。
            rows: List[List[Any]] = [] if dims else [[None] * len(metrics)]
            return {
                "columns": columns,
                "rows": rows,
                "sql": "-- fake metricflow sql (empty: beyond data_as_of)",
                "engine": "metricflow",
                "models": [],
                "group_columns": columns[: len(columns) - len(metrics)],
                "metric_columns": metrics,
                "row_count": len(rows),
                "truncated": False,
                "elapsed_ms": 0.3,
                "mock": False,
                "data_as_of": self.data_as_of_date,
            }

        pools = [self.dim_value_sets.get(d, [d]) for d in dims]
        combos = list(itertools.product(*pools)) if pools else [()]
        rows: List[List[Any]] = []
        for t_idx, day in enumerate(self.dates):
            for combo in combos:
                values = [
                    round(self.base_value + self.step_value * t_idx + self.metric_offset * i, 2)
                    for i in range(len(metrics))
                ]
                rows.append([day, *combo, *values])

        return {
            "columns": columns,
            "rows": rows,
            "sql": "-- fake metricflow sql",
            "engine": "metricflow",
            "models": [],
            "group_columns": columns[: len(columns) - len(metrics)],
            "metric_columns": metrics,
            "row_count": len(rows),
            "truncated": False,
            "elapsed_ms": 0.5,
            "mock": False,
        }

    async def dimension_values(self, dim_name: str, limit: Optional[int] = None) -> List[str]:
        return list(self.dim_value_sets.get(dim_name, []))

    async def data_as_of(self, model_names: List[str]) -> Optional[str]:
        return self.data_as_of_date

    async def health(self) -> Dict[str, Any]:
        return {"ok": True, "engine": "metricflow", "fake": True}

    def describe(self) -> Dict[str, Any]:
        return {"engine": "metricflow", "fake": True}


def make_deps(
    layer: Optional[SemanticLayer] = None,
    engine: Optional[FakeMetricEngine] = None,
    **overrides: Any,
) -> AgentDeps:
    """组装无 LLM / 无外部依赖的 AgentDeps（规则降级 + 假语义引擎）。"""
    layer = layer or SemanticLayer()
    engine = engine if engine is not None else FakeMetricEngine(layer)
    kwargs: Dict[str, Any] = {
        "settings": Settings(),
        "get_chat_model": lambda: None,  # None -> 节点自动降级规则/模板
        "semantic_layer": layer,
        "metric_engine": engine,
        "recaller": MetricRecaller(),
        "dim_recaller": DimensionRecaller(),
        "conv_state_repo": None,  # 默认不接对话状态仓储；反问澄清节点降级为"不限次追问"
        "semantic_registry_repo": None,  # 默认不接注册中心；create 节点降级为"需 LLM/未就绪"
    }
    kwargs.update(overrides)
    return AgentDeps(**kwargs)
