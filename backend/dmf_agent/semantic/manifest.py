"""语义清单结构定义与加载（MetricFlow / dbt 语义层规范 + StarRocks 物理映射）。

对齐 dbt-semantic-interfaces 的核心心智：
- SemanticModel：一张物理表（或 dbt model）上的 entities / dimensions / measures
- Metric：对外可查询的指标，分 simple / ratio / derived 三类
  后两者引用其它指标，编译期展开到 measure 层，公式在 JOIN 后的最外层计算

本模块只做"结构 + 校验"，不做 SQL 生成（见 compiler.py）。
物理映射字段（table / expr）是相对 MetricFlow 原规范的扩展——因为本项目
不经过 dbt 工程，需要直接从清单定位 StarRocks 的库表列。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

_DEFAULT_MANIFEST = Path(__file__).parent / "manifest.d"

# 时间粒度（StarRocks date_trunc 的单位，与 compiler 对齐）
TIME_GRANULARITIES = ["day", "week", "month", "quarter", "year"]

# 同环比类型白名单
POP_TYPES = ["wow", "mom", "qoq", "yoy", "pop"]

# measure 支持的聚合方式 -> SQL 模板（{e} 为被聚合表达式）
MEASURE_AGGS: Dict[str, str] = {
    "sum": "SUM({e})",
    "avg": "AVG({e})",
    "max": "MAX({e})",
    "min": "MIN({e})",
    "count": "COUNT({e})",
    "count_distinct": "COUNT(DISTINCT {e})",
    "median": "PERCENTILE_APPROX({e}, 0.5)",
}

# 时间相对聚合：取某时间维度上最值对应的度量值（期间末/初值）
# - last  = 期间最后一时刻的值（如"月末时点余额"），StarRocks 用 ARG_MAX / MySQL 用 MAX_BY
# - first = 期间最初一时刻的值，StarRocks 用 ARG_MIN / MySQL 用 MIN_BY
# 这两个聚合依赖模型的时间维度，必须在有 time 维度的模型上使用（validate 已校验）。
_TIME_RELATIVE_AGGS = ("last", "first")

# 全部受支持的 measure 聚合方式（含时间相对聚合）
SUPPORTED_AGGS: frozenset = frozenset(MEASURE_AGGS) | frozenset(_TIME_RELATIVE_AGGS)

# MetricFlow 的 metric 类型
METRIC_TYPES = ["simple", "ratio", "derived"]


class ManifestError(ValueError):
    """语义清单结构非法（缺字段 / 引用不存在的 measure 或 metric）。"""


@dataclass
class Entity:
    name: str
    type: str  # primary | unique | foreign
    expr: str


@dataclass
class Dimension:
    name: str
    type: str  # time | categorical
    expr: str
    time_granularity: str = "day"
    description: str = ""

    @property
    def is_time(self) -> bool:
        return self.type == "time"


@dataclass
class Measure:
    name: str
    agg: str
    expr: str
    agg_time_dimension: Optional[str] = None
    unit: str = ""
    description: str = ""


@dataclass
class SemanticModel:
    name: str
    table: str
    description: str = ""
    agg_time_dimension: Optional[str] = None
    entities: List[Entity] = field(default_factory=list)
    dimensions: List[Dimension] = field(default_factory=list)
    measures: List[Measure] = field(default_factory=list)

    # ---- 查询辅助 ----

    def dimension(self, name: str) -> Optional[Dimension]:
        return next((d for d in self.dimensions if d.name == name), None)

    def measure(self, name: str) -> Optional[Measure]:
        return next((m for m in self.measures if m.name == name), None)

    def time_dimension(self) -> Optional[Dimension]:
        """聚合时间维度：优先 agg_time_dimension 指定，否则首个 time 维度。"""
        if self.agg_time_dimension:
            dim = self.dimension(self.agg_time_dimension)
            if dim is not None:
                return dim
        return next((d for d in self.dimensions if d.is_time), None)

    @property
    def table_ref(self) -> str:
        """物理表引用（带库名时原样返回，不做方言转义——由 compiler 处理标识符引号）。"""
        return self.table


@dataclass
class Metric:
    name: str
    type: str  # simple | ratio | derived
    type_params: Dict[str, Any] = field(default_factory=dict)
    aliases: List[str] = field(default_factory=list)
    unit: str = ""
    description: str = ""
    # 输出换算：SQL 计算结果 * scale，保留 round 位小数（ratio 常配 scale=100 输出百分比）
    scale: float = 1.0
    round: int = 4
    granularities: List[str] = field(default_factory=lambda: list(TIME_GRANULARITIES))
    pop_types: List[str] = field(default_factory=lambda: list(POP_TYPES))
    # 两阶段聚合（仅 simple 指标）：先按天聚合 inner，再在目标粒度上聚合 outer。
    # 例：月日均存款余额 = 先按天 SUM 余额、再按月 AVG 每日余额。
    # dict 形如 {"inner": "sum", "outer": "avg"}；inner 缺省取 measure.agg，outer 缺省 "avg"。
    two_stage: Optional[Dict[str, str]] = None
    # 因子公式（P3 因子归因，可选）：把指标拆解为因子算式。
    # 形如 {"expr": "uv * cvr * arpu", "factors": {"uv": "访客数", "cvr": "转化率", "arpu": "客单价"}}，
    # factors 的值为因子名（可引用其它指标名）；expr 仅支持因子键与 * ( ) 组合。
    # 校验：expr 中出现的因子键必须在 factors 中声明；factors 引用的指标须真实存在。
    factors: Optional[Dict[str, Any]] = None

    def match(self, name: str) -> bool:
        """业务名或别名命中。"""
        return bool(name) and (name == self.name or name in self.aliases)

    @property
    def measure(self) -> Optional[str]:
        if self.type == "simple":
            return self.type_params.get("measure")
        return None

    @property
    def numerator(self) -> Optional[str]:
        return self.type_params.get("numerator") if self.type == "ratio" else None

    @property
    def denominator(self) -> Optional[str]:
        return self.type_params.get("denominator") if self.type == "ratio" else None

    @property
    def input_metrics(self) -> List[str]:
        """ratio / derived 引用的指标名（simple 为空）。"""
        if self.type == "ratio":
            return [x for x in (self.numerator, self.denominator) if x]
        if self.type == "derived":
            return list(self.type_params.get("metrics") or [])
        return []


class SemanticManifest:
    """语义清单：加载 + 校验 + 索引。"""

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self._path = Path(path) if path else _DEFAULT_MANIFEST
        self._raw: Dict[str, Any] = {}
        self.models: List[SemanticModel] = []
        self.metrics: List[Metric] = []
        # 指标树（P3 指标树归因，可选）：父子聚合关系列表。
        # 每节点形如 {"parent": "GMV", "child": "订单数", "weight": 1.0} 或
        # {"node": "GMV", "children": ["订单数", "客单价"]} 的规范化展开；
        # 缺失/为空时指标树归因整体跳过（不报错）。
        self.metrics_tree: List[Dict[str, Any]] = []
        self._model_by_name: Dict[str, SemanticModel] = {}
        self._measure_owner: Dict[str, SemanticModel] = {}
        self._metric_by_name: Dict[str, Metric] = {}
        self.load()

    # ---- 加载 ----

    def load(self) -> None:
        self._raw = self._load_raw()
        self._parse_and_index()

    def _parse_and_index(self) -> None:
        self.models = [self._parse_model(m) for m in self._raw.get("semantic_models") or []]
        self.metrics = [self._parse_metric(m) for m in self._raw.get("metrics") or []]
        self.metrics_tree = self._parse_metrics_tree(self._raw.get("metrics_tree"))
        # 索引（重复 measure / 指标名会在此暴露）——须在引用校验前完成
        self._reindex()
        problems = self.validate() + self.reference_problems()
        if problems:
            raise ManifestError("语义清单校验失败：\n- " + "\n- ".join(problems))

    @staticmethod
    def _parse_metrics_tree(raw: Any) -> List[Dict[str, Any]]:
        """解析 metrics_tree：统一展开为 {parent, child, weight} 边列表。

        接受两种写法：
        - 边写法：[{"parent": "GMV", "child": "订单数", "weight": 1.0}, ...]
        - 子树写法：[{"node": "GMV", "children": ["订单数", "客单价"]}, ...]
          （weight 缺省 1.0；两种写法可混用）
        结构异常的节点静默跳过（P3 归因依赖方自行判断空树），不在加载期报错——
        树是可选能力，坏配置不应阻断整个清单加载。
        """
        if not isinstance(raw, list):
            return []
        edges: List[Dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            if item.get("parent") and item.get("child"):
                edges.append(
                    {
                        "parent": str(item["parent"]),
                        "child": str(item["child"]),
                        "weight": float(item.get("weight") or 1.0),
                    }
                )
                continue
            node = item.get("node")
            children = item.get("children")
            if node and isinstance(children, list):
                for child in children:
                    if isinstance(child, dict) and child.get("child"):
                        edges.append(
                            {
                                "parent": str(node),
                                "child": str(child["child"]),
                                "weight": float(child.get("weight") or 1.0),
                            }
                        )
                    elif isinstance(child, str):
                        edges.append(
                            {"parent": str(node), "child": child, "weight": 1.0}
                        )
        return edges

    def _load_raw(self) -> Dict[str, Any]:
        """加载原始清单字典。

        - 单文件：直接解析；
        - 目录：递归 glob ``*.yml``/``*.yaml``，按相对路径排序后合并
          ``semantic_models`` / ``metrics`` 列表，并做跨文件重名检测
          （model / metric / measure 任一重复即抛 ``ManifestError``）。
        """
        p = self._path
        if p.is_dir():
            files = sorted(
                (f for f in p.rglob("*") if f.is_file() and f.suffix in (".yml", ".yaml")),
                key=lambda f: str(f.relative_to(p)),
            )
            if not files:
                raise ManifestError(f"语义清单目录为空：{p}")
            merged: Dict[str, Any] = {}
            model_names: Dict[str, str] = {}
            metric_names: Dict[str, str] = {}
            measure_names: Dict[str, str] = {}
            for f in files:
                raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                self._merge_raw(merged, raw)
                for m in raw.get("semantic_models") or []:
                    nm = m.get("name")
                    if not nm:
                        continue
                    if nm in model_names:
                        raise ManifestError(
                            f"semantic_model 名跨文件重复：{nm}（{model_names[nm]} 与 {f}）"
                        )
                    model_names[nm] = str(f)
                    for me in m.get("measures") or []:
                        mn = me.get("name")
                        if not mn:
                            continue
                        if mn in measure_names:
                            raise ManifestError(
                                f"measure 名跨文件重复：{mn}（{measure_names[mn]} 与 {f}）"
                            )
                        measure_names[mn] = str(f)
                for mt in raw.get("metrics") or []:
                    mtn = mt.get("name")
                    if not mtn:
                        continue
                    if mtn in metric_names:
                        raise ManifestError(
                            f"metric 名跨文件重复：{mtn}（{metric_names[mtn]} 与 {f}）"
                        )
                    metric_names[mtn] = str(f)
            return merged
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    @staticmethod
    def _merge_raw(acc: Dict[str, Any], raw: Dict[str, Any]) -> None:
        """把单文件字典合并进累加器：列表类键做 extend，标量键后者覆盖。"""
        for k, v in raw.items():
            if isinstance(v, list):
                acc.setdefault(k, [])
                acc[k].extend(v)
            else:
                acc[k] = v

    @classmethod
    def from_dict(cls, raw: Dict[str, Any], path: Optional[str | Path] = None) -> "SemanticManifest":
        """从已合并的字典构造（用于 DB 发布快照重建，避免重复文件 IO）。

        等价于 ``load()`` 之后拿到的结果，但输入来自内存字典而非文件。
        """
        inst = cls.__new__(cls)
        inst._path = Path(path) if path else _DEFAULT_MANIFEST
        inst._raw = dict(raw)
        inst.models = []
        inst.metrics = []
        inst.metrics_tree = []
        inst._model_by_name = {}
        inst._measure_owner = {}
        inst._metric_by_name = {}
        inst._parse_and_index()
        return inst

    def to_dict(self) -> Dict[str, Any]:
        """导出已合并的原始字典（用于发布快照持久化，无损往返）。"""
        import copy

        return copy.deepcopy(self._raw)

    def _reindex(self) -> None:
        self._model_by_name = {m.name: m for m in self.models}
        self._measure_owner = {}
        for model in self.models:
            for measure in model.measures:
                if measure.name in self._measure_owner:
                    raise ManifestError(f"measure 名重复：{measure.name}")
                self._measure_owner[measure.name] = model
        self._metric_by_name = {m.name: m for m in self.metrics}

    @staticmethod
    def _parse_model(raw: Dict[str, Any]) -> SemanticModel:
        return SemanticModel(
            name=raw["name"],
            table=raw.get("table") or raw["name"],
            description=raw.get("description") or "",
            agg_time_dimension=raw.get("agg_time_dimension"),
            entities=[
                Entity(name=e["name"], type=e.get("type") or "foreign", expr=e.get("expr") or e["name"])
                for e in raw.get("entities") or []
            ],
            dimensions=[
                Dimension(
                    name=d["name"],
                    type=d.get("type") or "categorical",
                    expr=d.get("expr") or d["name"],
                    time_granularity=(d.get("type_params") or {}).get("time_granularity") or "day",
                    description=d.get("description") or "",
                )
                for d in raw.get("dimensions") or []
            ],
            measures=[
                Measure(
                    name=m["name"],
                    agg=m.get("agg") or "sum",
                    expr=m.get("expr") or m["name"],
                    agg_time_dimension=m.get("agg_time_dimension"),
                    unit=m.get("unit") or "",
                    description=m.get("description") or "",
                )
                for m in raw.get("measures") or []
            ],
        )

    @staticmethod
    def _parse_metric(raw: Dict[str, Any]) -> Metric:
        return Metric(
            name=raw["name"],
            type=raw.get("type") or "simple",
            type_params=dict(raw.get("type_params") or {}),
            aliases=list(raw.get("aliases") or []),
            unit=raw.get("unit") or "",
            description=raw.get("description") or "",
            scale=float(raw.get("scale") or 1.0),
            round=int(raw.get("round") or 4),
            granularities=list(raw.get("granularities") or TIME_GRANULARITIES),
            pop_types=list(raw.get("pop_types") or POP_TYPES),
            two_stage=raw.get("two_stage"),
            factors=raw.get("factors"),
        )

    # ---- 校验 ----

    def validate(self) -> List[str]:
        """结构校验：返回问题列表（空表示通过）。"""
        problems: List[str] = []
        if not self.models:
            problems.append("semantic_models 为空")
        if not self.metrics:
            problems.append("metrics 为空")

        seen_models: set[str] = set()
        for model in self.models:
            if model.name in seen_models:
                problems.append(f"semantic_model 名重复：{model.name}")
            seen_models.add(model.name)
            if not model.table:
                problems.append(f"semantic_model[{model.name}] 缺 table")
            if model.time_dimension() is None:
                problems.append(f"semantic_model[{model.name}] 缺时间维度（type=time）")
            for measure in model.measures:
                if measure.agg not in SUPPORTED_AGGS:
                    problems.append(
                        f"measure[{measure.name}] 聚合方式不支持：{measure.agg}"
                        f"（支持 {'/'.join(sorted(SUPPORTED_AGGS))}）"
                    )
                if not measure.expr:
                    problems.append(f"measure[{measure.name}] 缺 expr")
        return problems

    def reference_problems(self) -> List[str]:
        """引用完整性校验（指标 -> measure/指标 的引用链）。"""
        problems: List[str] = []
        for metric in self.metrics:
            if metric.two_stage is not None:
                if metric.type != "simple":
                    problems.append(
                        f"metric[{metric.name}] two_stage 仅支持 simple 类型指标（当前 {metric.type}）"
                    )
                else:
                    ts = metric.two_stage or {}
                    inner = ts.get("inner")
                    outer = ts.get("outer")
                    if inner is not None and inner not in SUPPORTED_AGGS:
                        problems.append(
                            f"metric[{metric.name}] two_stage.inner 聚合不支持：{inner}"
                            f"（支持 {'/'.join(sorted(SUPPORTED_AGGS))}）"
                        )
                    if outer is not None and outer not in SUPPORTED_AGGS:
                        problems.append(
                            f"metric[{metric.name}] two_stage.outer 聚合不支持：{outer}"
                            f"（支持 {'/'.join(sorted(SUPPORTED_AGGS))}）"
                        )
            if metric.type not in METRIC_TYPES:
                problems.append(f"metric[{metric.name}] 类型不支持：{metric.type}")
                continue
            if metric.type == "simple":
                measure = metric.measure
                if not measure:
                    problems.append(f"metric[{metric.name}] simple 类型缺 type_params.measure")
                elif measure not in self._measure_owner:
                    problems.append(f"metric[{metric.name}] 引用了不存在的 measure：{measure}")
            elif metric.type == "ratio":
                for role in ("numerator", "denominator"):
                    ref = metric.type_params.get(role)
                    if not ref:
                        problems.append(f"metric[{metric.name}] ratio 类型缺 type_params.{role}")
                    elif ref not in self._metric_by_name and ref not in self._measure_owner:
                        problems.append(f"metric[{metric.name}] 的 {role}【{ref}】未定义")
            elif metric.type == "derived":
                if not metric.type_params.get("expr"):
                    problems.append(f"metric[{metric.name}] derived 类型缺 type_params.expr")
                refs = metric.input_metrics
                if not refs:
                    problems.append(f"metric[{metric.name}] derived 类型缺 type_params.metrics")
                for ref in refs:
                    if ref not in self._metric_by_name:
                        problems.append(f"metric[{metric.name}] 引用了不存在的指标：{ref}")
            # P3 因子归因：factors 声明校验（可选能力，配错即报出，不静默吞）
            if metric.factors:
                factor_names = metric.factors.get("factors") or {}
                expr = str(metric.factors.get("expr") or "")
                if not factor_names or not expr:
                    problems.append(
                        f"metric[{metric.name}] factors 需同时声明 expr 与 factors"
                    )
                else:
                    import re as _re

                    used = set(_re.findall(r"[A-Za-z_一-鿿][\w一-鿿]*", expr))
                    undeclared = used - set(factor_names)
                    if undeclared:
                        problems.append(
                            f"metric[{metric.name}] factors.expr 使用了未声明的因子："
                            f"{sorted(undeclared)}（factors 键：{sorted(factor_names)}）"
                        )
                    for fkey, ref in factor_names.items():
                        if ref not in self._metric_by_name and ref not in self._measure_owner:
                            problems.append(
                                f"metric[{metric.name}] 的因子[{fkey}]【{ref}】未定义"
                                "（须为已收录指标或 measure）"
                            )
        # P3 指标树：节点引用校验（树为空跳过；引用不存在指标即报出）
        for edge in self.metrics_tree:
            for role in ("parent", "child"):
                name = edge.get(role)
                if name and name not in self._metric_by_name:
                    problems.append(f"metrics_tree 边引用了不存在的指标：{name}")
        # 指标名与别名唯一性
        alias_seen: Dict[str, str] = {}
        for metric in self.metrics:
            for key in [metric.name, *metric.aliases]:
                if key in alias_seen and alias_seen[key] != metric.name:
                    problems.append(f"指标名/别名冲突：{key}（{alias_seen[key]} 与 {metric.name}）")
                alias_seen[key] = metric.name
        return problems

    # ---- 查询 ----

    @property
    def path(self) -> Path:
        return self._path

    def model(self, name: str) -> Optional[SemanticModel]:
        return self._model_by_name.get(name)

    def measure_owner(self, measure: str) -> Optional[SemanticModel]:
        return self._measure_owner.get(measure)

    def metric(self, name: str) -> Optional[Metric]:
        return self._metric_by_name.get(name)

    def resolve_metric(self, name: str) -> Optional[Metric]:
        """按业务名或别名解析指标。"""
        if not name:
            return None
        metric = self._metric_by_name.get(name)
        if metric is not None:
            return metric
        return next((m for m in self.metrics if m.match(name)), None)

    def dimensions(self) -> List[Dimension]:
        """全部维度（跨模型去重，同名列合并为一条）。"""
        seen: Dict[str, Dimension] = {}
        for model in self.models:
            for dim in model.dimensions:
                seen.setdefault(dim.name, dim)
        return list(seen.values())

    def dimension(self, name: str) -> Optional[Dimension]:
        return next((d for d in self.dimensions() if d.name == name), None)

    def time_dimension(self) -> Optional[Dimension]:
        """全局时间维度（各模型应使用同名时间维度；取首个模型的时间维度）。"""
        for model in self.models:
            dim = model.time_dimension()
            if dim is not None:
                return dim
        return None

    def dimension_owners(self, dim_name: str) -> List[SemanticModel]:
        """提供某维度的语义模型列表。"""
        return [m for m in self.models if m.dimension(dim_name) is not None]
