"""语义注册中心治理服务（P2：指标/维度版本与口径审批）。

对应 doc/语义层注册中心与指标权限治理方案.md §3.3（六道校验门禁）/ §3.4（状态机/版本/回滚）。

两大职责：
1. **校验门禁** `validate_metric_definition` / `validate_dimension_definition`
   —— 发布前必须全过（无 problem 级错误）才允许 `publish`：
   - 指标六道闸：① Schema ② 引用完整性 ③ DAG 无环 ④ SQL dry-run
     ⑤ 物理列存在性（深度，best-effort）⑥ 试跑（深度，best-effort）。
   - 维度：① Schema。
   - ⑤⑥ 依赖数据源连接，不可用时**降级为 warning**（不阻断发布），
     由 `deep=True` 显式开启（见 `/metrics/{key}/validate?deep=true`）。
2. **状态机** `METRIC_TRANSITIONS` / `can_transition`
   —— draft → review → published → deprecated（废弃后可重新发布）。
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from dmf_agent.semantic.layer import SemanticLayer
from dmf_agent.semantic.manifest import MEASURE_AGGS
from dmf_agent.semantic.query import MetricQuery

logger = logging.getLogger(__name__)

# 物理列抽取时排除的 SQL 关键字/函数名（避免把 SUM/CASE 等误判为列标识）
_FUNC_WORDS = set(MEASURE_AGGS) | {
    "sum", "avg", "max", "min", "count", "distinct", "case", "when", "then",
    "else", "end", "cast", "abs", "round", "coalesce", "nullif", "if", "div",
    "mod", "and", "or", "not", "null", "as", "between", "like", "in", "is",
    "true", "false", "interval", "on", "by", "group", "order", "having",
    "where", "from", "select", "from", "asc", "desc",
}
_COL_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _extract_columns(expr: str) -> List[str]:
    """从 measure 表达式抽取可能的物理列标识（best-effort，过滤函数/关键字）。"""
    out: List[str] = []
    for tok in _COL_TOKEN_RE.findall(expr or ""):
        if tok.lower() in _FUNC_WORDS:
            continue
        out.append(tok)
    return out


def _split_table_ref(table: str):
    """把 `db.table` 拆成 (schema, table)；纯 `table` 则 (None, table)。"""
    parts = (table or "").split(".", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return None, parts[0]


def _with_limit1(sql: str) -> str:
    """给试跑 SQL 加/改 LIMIT 1，避免大结果集打爆内存。"""
    s = sql.rstrip().rstrip(";").strip()
    if re.search(r"\blimit\s+\d+\s*$", s, re.IGNORECASE):
        return re.sub(r"\blimit\s+\d+\s*$", "LIMIT 1", s, flags=re.IGNORECASE)
    return s + " LIMIT 1"

# ---------------- 状态机 ----------------

DRAFT = "draft"
REVIEW = "review"
PUBLISHED = "published"
DEPRECATED = "deprecated"

METRIC_STATUSES: Set[str] = {DRAFT, REVIEW, PUBLISHED, DEPRECATED}

# 合法转移（口径审批：起草→提交审核→审批发布→废弃）：
#   draft   仅可 提交审核(review) 或 废弃(deprecated，放弃草稿)；
#   review  可由 admin 审批发布(published)、驳回退回草稿(draft)、或废弃；
#   published 仅可废弃(deprecated)；
#   deprecated 可重新发布(published) 或 复活为草稿(draft)；
# 注意：草稿**不能**直接发布，必须经过 review 这一审批闸口（见 publish_metric）。
METRIC_TRANSITIONS: Dict[str, Set[str]] = {
    DRAFT: {REVIEW, DEPRECATED},
    REVIEW: {PUBLISHED, DRAFT, DEPRECATED},
    PUBLISHED: {DEPRECATED},
    DEPRECATED: {PUBLISHED, DRAFT},
}

# 指标类型枚举（口径定义）
METRIC_TYPES: Set[str] = {"simple", "ratio", "derived"}
# 维度类型枚举
DIMENSION_TYPES: Set[str] = {"categorical", "time", "boolean", "number"}


def can_transition(from_status: str, to_status: str) -> bool:
    """状态机守卫：是否允许 from -> to 转移。"""
    if from_status == to_status:
        return True
    return to_status in METRIC_TRANSITIONS.get(from_status or DRAFT, set())


# ---------------- 校验结果 ----------------


@dataclass
class GateResult:
    """单道校验闸结果。"""

    gate: str
    # "ok" | "problem" | "warning"
    status: str
    detail: str


@dataclass
class ValidationResult:
    """指标/维度定义校验汇总。"""

    passed: bool
    problems: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    gates: List[GateResult] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "problems": self.problems,
            "warnings": self.warnings,
            "gates": [
                {"gate": g.gate, "status": g.status, "detail": g.detail}
                for g in self.gates
            ],
        }


# ---------------- 核心定义抽取 ----------------


def metric_core_defn(brief: Dict[str, Any]) -> Dict[str, Any]:
    """从指标 brief/行 抽取「口径」核心字段（用于校验与版本快照）。"""
    return {
        "metric_key": brief.get("metric_key", ""),
        "name": brief.get("name", ""),
        "metric_type": brief.get("metric_type", "simple"),
        "type_params": dict(brief.get("type_params") or {}),
        "aliases": list(brief.get("aliases") or []),
        "unit": brief.get("unit", ""),
        "description": brief.get("description", ""),
        "scale": float(brief.get("scale", 1.0) or 1.0),
        "round": int(brief.get("round", 4) or 4),
        "granularities": list(brief.get("granularities") or []),
        "pop_types": list(brief.get("pop_types") or []),
        "owner": brief.get("owner", ""),
    }


def dimension_core_defn(brief: Dict[str, Any]) -> Dict[str, Any]:
    """从维度 brief/行 抽取「口径」核心字段。"""
    return {
        "dimension_key": brief.get("dimension_key", ""),
        "name": brief.get("name", ""),
        "dim_type": brief.get("dim_type", "categorical"),
        "expr": brief.get("expr", ""),
        "time_granularity": brief.get("time_granularity", "day"),
        "description": brief.get("description", ""),
        "model_key": brief.get("model_key", ""),
    }


# ---------------- 稳定 key 派生 ----------------


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    return _SLUG_RE.sub("_", (text or "").lower()).strip("_") or "x"


def derive_metric_key(defn: Dict[str, Any]) -> str:
    """从定义派生不可变 metric_key（与 manifest.yml 推导规则对齐）。

    - simple: 度量名
    - ratio:  {分子}__{分母}_ratio
    - derived: derived_{短哈希}
    """
    mtype = defn.get("metric_type", "simple")
    tp = defn.get("type_params") or {}
    if mtype == "ratio":
        num = slugify(str(tp.get("numerator") or ""))
        den = slugify(str(tp.get("denominator") or ""))
        return f"{num}__{den}_ratio"
    if mtype == "derived":
        seed = str(defn.get("name", "")) + repr(sorted((tp.get("metrics") or [])))
        return "derived_" + hashlib.md5(seed.encode("utf-8")).hexdigest()[:8]
    # simple / 兜底
    return slugify(str(tp.get("measure") or defn.get("name") or "metric"))


def derive_dimension_key(defn: Dict[str, Any]) -> str:
    return slugify(str(defn.get("name") or defn.get("expr") or "dimension"))


# ---------------- 引用收集 / DAG ----------------


def _collect_metric_refs(defn: Dict[str, Any]) -> Set[str]:
    """收集一个指标定义引用的其它指标 key（用于引用完整性 + DAG）。"""
    mtype = defn.get("metric_type", "simple")
    tp = defn.get("type_params") or {}
    refs: Set[str] = set()
    if mtype == "simple":
        m = tp.get("measure")
        if m:
            refs.add(str(m))
    elif mtype == "ratio":
        for k in (tp.get("numerator"), tp.get("denominator")):
            if k:
                refs.add(str(k))
    elif mtype == "derived":
        for k in tp.get("metrics") or []:
            if k:
                refs.add(str(k))
    return refs


def _detect_cycle(start: str, graph: Dict[str, Set[str]]) -> Optional[List[str]]:
    """从 start 出发的依赖图里检测环（start 经依赖回到自身即环）。

    返回环路径（含首尾 start），无环返回 None。
    """
    GRAY, BLACK = 1, 2
    color: Dict[str, int] = {}
    parent: Dict[str, Optional[str]] = {}

    def dfs(node: str) -> Optional[List[str]]:
        color[node] = GRAY
        for nxt in graph.get(node, set()):
            if nxt not in color and nxt in graph:
                parent[nxt] = node
                cyc = dfs(nxt)
                if cyc is not None:
                    return cyc
            elif color.get(nxt) == GRAY:
                # 找到环：从 nxt 回溯到 node
                path = [nxt, node]
                cur = node
                while parent.get(cur) is not None and parent[cur] != nxt:
                    cur = parent[cur]  # type: ignore[assignment]
                    path.append(cur)  # type: ignore[arg-type]
                path.reverse()
                path.append(nxt)
                return path
        color[node] = BLACK
        return None

    parent[start] = None
    return dfs(start)


# ---------------- 指标校验（六道闸） ----------------


async def validate_metric_definition(
    defn: Dict[str, Any],
    *,
    metric_key: str,
    all_metric_defs: Dict[str, Dict[str, Any]],
    measure_keys: Set[str],
    layer: Optional[SemanticLayer] = None,
    engine: Any = None,
    deep: bool = False,
) -> ValidationResult:
    """校验一个指标定义。

    `all_metric_defs`：全量指标定义（key -> 核心定义），用于引用完整性 + DAG；
    校验草稿时由调用方把本定义并入（以 `metric_key` 为键）。
    `measure_keys`：全量度量 key 集合（引用完整性用）。
    `layer`/`engine`：SQL dry-run 用；缺失则降级 warning。
    `deep`：是否执行 ⑤物理列 / ⑥试跑（依赖数据源，best-effort）。
    """
    gates: List[GateResult] = []
    problems: List[str] = []
    warnings: List[str] = []

    # ① Schema
    try:
        name = str(defn.get("name") or "").strip()
        mtype = str(defn.get("metric_type") or "simple")
        tp = defn.get("type_params") or {}
        if not name:
            problems.append("①Schema：name 必填且非空")
        if mtype not in METRIC_TYPES:
            problems.append(f"①Schema：metric_type 必须是 {sorted(METRIC_TYPES)}，收到 {mtype!r}")
        if not isinstance(tp, dict):
            problems.append("①Schema：type_params 必须是对象")
        else:
            if mtype == "simple" and not str(tp.get("measure") or "").strip():
                problems.append("①Schema：simple 指标必须指定 type_params.measure")
            if mtype == "ratio":
                if not str(tp.get("numerator") or "").strip():
                    problems.append("①Schema：ratio 指标必须指定 type_params.numerator")
                if not str(tp.get("denominator") or "").strip():
                    problems.append("①Schema：ratio 指标必须指定 type_params.denominator")
            if mtype == "derived":
                if not str(tp.get("expr") or "").strip():
                    problems.append("①Schema：derived 指标必须指定 type_params.expr")
                if not (tp.get("metrics") or []):
                    problems.append("①Schema：derived 指标必须指定 type_params.metrics[]")
        gates.append(
            GateResult(
                "schema",
                "ok" if not any(g.startswith("①") for g in problems) else "problem",
                "字段类型/必填/枚举校验通过" if not any(g.startswith("①") for g in problems)
                else "存在字段级错误",
            )
        )
    except Exception as exc:  # noqa: BLE001
        problems.append(f"①Schema：校验异常 {exc}")
        gates.append(GateResult("schema", "problem", str(exc)))

    # ② 引用完整性（依赖 ① 解析出的 mtype/tp）
    try:
        missing: List[str] = []
        if mtype == "simple":
            if str(tp.get("measure") or "") not in measure_keys:
                missing.append(f"measure={tp.get('measure')}")
        elif mtype == "ratio":
            for k in (tp.get("numerator"), tp.get("denominator")):
                if str(k or "") not in all_metric_defs:
                    missing.append(f"{k}")
        elif mtype == "derived":
            for k in tp.get("metrics") or []:
                if str(k or "") not in all_metric_defs:
                    missing.append(f"{k}")
        if missing:
            problems.append(f"②引用完整性：引用不存在 -> {', '.join(missing)}")
            gates.append(GateResult("reference", "problem", f"缺失引用：{missing}"))
        else:
            gates.append(GateResult("reference", "ok", "引用目标均存在"))
    except Exception as exc:  # noqa: BLE001
        problems.append(f"②引用完整性：校验异常 {exc}")
        gates.append(GateResult("reference", "problem", str(exc)))

    # ③ DAG 无环
    try:
        graph: Dict[str, Set[str]] = {}
        for k, d in all_metric_defs.items():
            graph[k] = _collect_metric_refs(d)
        cycle = _detect_cycle(metric_key, graph)
        if cycle:
            problems.append(f"③DAG 无环：检测到循环依赖 -> {' -> '.join(cycle)}")
            gates.append(
                GateResult("dag", "problem", "循环依赖：" + " -> ".join(cycle))
            )
        else:
            gates.append(GateResult("dag", "ok", "无环（拓扑可排序）"))
    except Exception as exc:  # noqa: BLE001
        problems.append(f"③DAG 无环：校验异常 {exc}")
        gates.append(GateResult("dag", "problem", str(exc)))

    # ④ SQL dry-run（best-effort）
    if engine is not None and layer is not None:
        name = str(defn.get("name") or "").strip()
        metric = layer.resolve_metric(name) if name else None
        if metric is None:
            warnings.append(
                "④SQL dry-run 跳过：指标未在运行态语义层注册"
                "（定义尚未进入语义层；P3 热更新后可全量校验）"
            )
            gates.append(GateResult("sql_dryrun", "warning", "指标未在语义层，跳过编译"))
        else:
            try:
                td = layer.time_dimension_name()
                dsl = {
                    "metrics": [name],
                    "group_by": [td] if td else [],
                    "time_range": {"granularity": "day"},
                }
                q = MetricQuery.from_dsl(dsl, layer)
                compiled = engine.compile(q)
                gates.append(
                    GateResult(
                        "sql_dryrun", "ok", f"编译通过：{compiled.sql[:200]}"
                    )
                )
            except Exception as exc:  # noqa: BLE001
                problems.append(f"④SQL dry-run：编译失败 {exc}")
                gates.append(GateResult("sql_dryrun", "problem", f"编译失败：{exc}"))
    else:
        warnings.append("④SQL dry-run 跳过：未提供引擎/语义层")
        gates.append(GateResult("sql_dryrun", "warning", "无引擎，跳过编译"))

    # ⑤ 物理列存在性（深度，best-effort）：通过 information_schema 校验指标依赖的
    # 度量物理列是否真实存在；缺列报 problem（阻断发布），探查失败/无资源降级 warning。
    if deep and engine is not None:
        try:
            if layer is None:
                raise RuntimeError("未提供语义层，无法定位物理表/列")
            m = layer.resolve_metric(name) if name else None
            if m is None:
                warnings.append("⑤物理列存在性：指标未注册到（合并）语义层，无法定位物理表/列")
                gates.append(GateResult("physical_columns", "warning", "指标未注册，跳过"))
            else:
                required: Dict[str, set] = {}
                for measure in layer.base_measures(name):
                    owner = layer.manifest.measure_owner(measure)
                    if owner is None:
                        continue
                    mexpr = owner.measure(measure).expr if owner.measure(measure) else measure
                    for col in _extract_columns(mexpr):
                        required.setdefault(owner.table, set()).add(col)
                if not required:
                    warnings.append("⑤物理列存在性：未能从指标定位到度量/物理列（跳过）")
                    gates.append(GateResult("physical_columns", "warning", "无可用物理列，跳过"))
                else:
                    missing_all: List[str] = []
                    for table, cols in required.items():
                        schema, tname = _split_table_ref(table)
                        sql = (
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = %s"
                        )
                        params: List[Any] = [tname]
                        if schema:
                            sql += " AND table_schema = %s"
                            params.append(schema)
                        res = await engine.executor.fetch(sql, tuple(params))
                        have = {str(r[0]).lower() for r in res["rows"]}
                        missing = [c for c in cols if c.lower() not in have]
                        for c in missing:
                            missing_all.append(f"{table}.{c}")
                    if missing_all:
                        problems.append(
                            "⑤物理列存在性：以下列在物理表中不存在 -> " + "; ".join(missing_all)
                        )
                        gates.append(
                            GateResult(
                                "physical_columns", "problem",
                                "缺失列：" + "; ".join(missing_all),
                            )
                        )
                    else:
                        gates.append(GateResult("physical_columns", "ok", "物理列均存在"))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"⑤物理列存在性：探查失败，降级为 warning：{exc}")
            gates.append(GateResult("physical_columns", "warning", f"探查失败：{exc}"))
    else:
        gates.append(GateResult("physical_columns", "warning", "非深度模式，跳过"))

    # ⑥ 试跑（深度，best-effort）：编译并真实执行 LIMIT 1 查询，验证端到端可取数；
    # 执行失败报 problem（阻断发布），未注册/无资源降级 warning。依赖 ④ 编译，若 ④ 已失败则跳过。
    sql_gate = next((g for g in gates if g.gate == "sql_dryrun"), None)
    if deep and engine is not None and not (sql_gate is not None and sql_gate.status == "problem"):
        try:
            if layer is None:
                raise RuntimeError("未提供语义层，无法编译试跑")
            tm = layer.resolve_metric(name) if name else None
            if tm is None:
                warnings.append("⑥试跑跳过：指标未在（合并）语义层注册")
                gates.append(GateResult("trial_run", "warning", "指标未注册，跳过"))
            else:
                td = layer.time_dimension_name()
                dsl = {
                    "metrics": [name],
                    "group_by": [td] if td else [],
                    "time_range": {"granularity": "day"},
                }
                q = MetricQuery.from_dsl(dsl, layer)
                compiled = engine.compile(q)
                bounded = _with_limit1(compiled.sql)
                res = await engine.executor.fetch(bounded)
                gates.append(
                    GateResult(
                        "trial_run", "ok",
                        f"试跑成功：返回 {res['row_count']} 行（样本截断）",
                    )
                )
        except Exception as exc:  # noqa: BLE001
            problems.append(f"⑥试跑失败：{exc}")
            gates.append(GateResult("trial_run", "problem", f"试跑失败：{exc}"))
    elif deep and engine is not None and sql_gate is not None and sql_gate.status == "problem":
        warnings.append("⑥试跑跳过：依赖的 ④ SQL dry-run 已失败")
        gates.append(GateResult("trial_run", "warning", "依赖 ④ 编译失败，跳过"))
    else:
        gates.append(GateResult("trial_run", "warning", "非深度模式，跳过"))

    return ValidationResult(
        passed=len(problems) == 0,
        problems=problems,
        warnings=warnings,
        gates=gates,
    )


# ---------------- 维度校验（Schema） ----------------


def validate_dimension_definition(defn: Dict[str, Any]) -> ValidationResult:
    """校验一个维度定义（当前为 Schema 闸；维度不独立编译 SQL）。"""
    gates: List[GateResult] = []
    problems: List[str] = []
    warnings: List[str] = []

    name = str(defn.get("name") or "").strip()
    dim_type = str(defn.get("dim_type") or "categorical")
    expr = str(defn.get("expr") or "").strip()
    tg = str(defn.get("time_granularity") or "").strip()

    if not name:
        problems.append("①Schema：name 必填且非空")
    if dim_type not in DIMENSION_TYPES:
        problems.append(f"①Schema：dim_type 必须是 {sorted(DIMENSION_TYPES)}，收到 {dim_type!r}")
    if not expr:
        problems.append("①Schema：expr（物理列表达式）必填")
    if not tg:
        problems.append("①Schema：time_granularity 必填")

    gates.append(
        GateResult(
            "schema",
            "ok" if not problems else "problem",
            "字段校验通过" if not problems else "存在字段级错误",
        )
    )
    return ValidationResult(
        passed=len(problems) == 0,
        problems=problems,
        warnings=warnings,
        gates=gates,
    )
