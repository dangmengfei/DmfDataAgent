"""语义层 CLI：python -m dmf_agent.semantic <命令>

用法：
  check                                  校验语义清单（结构 + 引用完整性）
  list-metrics                           列出指标（含别名/支持维度/基础 measure）
  list-dimensions                        列出维度（含物理列/归属语义模型）
  dim-values 渠道 [--limit 20]           打印某维度的维值候选 SQL 并执行（需 StarRocks）
  sql  "metric:GMV group_by:统计日期,渠道 time:2026-09-01~2026-09-07:day"
                                         只编译，打印 StarRocks SQL
  query "metric:GMV ... [pop:yoy]"       编译并执行，打印结果表

查询表达式语法（空格分隔的段）：metric:/metrics: group_by:/groupby: filters:/time:/pop:
  filters 形如  filters:渠道 in APP,小程序
  time    形如  time:2026-09-01~2026-09-07:day
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from typing import Any, Dict, List

from dmf_agent.config.settings import get_settings
from dmf_agent.semantic.compiler import CompileError
from dmf_agent.semantic.engine import MetricFlowEngine
from dmf_agent.semantic.layer import SemanticLayer
from dmf_agent.semantic.starrocks import StarRocksClient, StarRocksError

_OPERATORS = "in|not in|=|!=|>|<|>=|<="

# 段解析：`key:值` 直到下一个 ` 词:` 之前（值里允许空格，如 filters:渠道 in APP,小程序）
_SEGMENT_RE = re.compile(r"(\S+?):(.*?)(?=\s+\S+?:\S|$)", re.S)


def parse_query(text: str) -> Dict[str, Any]:
    """解析命令行查询表达式 -> 语义查询 dict。"""
    query: Dict[str, Any] = {
        "metrics": [],
        "group_by": [],
        "filters": [],
        "time_range": None,
        "period_comparison": False,
        "period_comparison_type": None,
        "period_comparison_types": [],
        "rank_scope": "global",
    }
    for key, raw_value in _SEGMENT_RE.findall(text):
        value = raw_value.strip()
        if key in ("metric", "metrics"):
            query["metrics"] = [v.strip() for v in value.split(",") if v.strip()]
        elif key in ("group_by", "groupby", "group", "dimensions"):
            query["group_by"] = [v.strip() for v in value.split(",") if v.strip()]
        elif key in ("filters", "filter"):
            m = re.match(rf"(\S+)\s+({_OPERATORS})\s+(.+)", value)
            if m:
                query["filters"].append(
                    {
                        "field": m.group(1),
                        "operator": m.group(2),
                        "values": [v.strip() for v in m.group(3).split(",") if v.strip()],
                    }
                )
        elif key == "time":
            # 形如 time:2026-09-01~2026-09-30:month（粒度可省略，默认 day）
            span = [part.strip() for part in value.split("~")]
            start = span[0] if span else ""
            end = span[1] if len(span) > 1 else ""
            granularity = "day"
            if ":" in end:
                end, granularity = end.rsplit(":", 1)
            query["time_range"] = {
                "start": start or None,
                "end": end.strip() or None,
                "granularity": granularity.strip() or "day",
            }
        elif key == "pop":
            # pop:yoy 或 pop:yoy,mom（FR1 支持同比 + 环比并存）
            types = [v.strip().lower() for v in value.split(",") if v.strip()]
            types = types or ["yoy"]
            query["period_comparison"] = True
            query["period_comparison_types"] = types
            query["period_comparison_type"] = types[0]
        elif key in ("rank", "rank_scope"):
            # rank:group（时间序列组内排名）| rank:global（默认）
            query["rank_scope"] = value.strip().lower() or "global"
    return query


def _print_table(columns: List[str], rows: List[List[Any]]) -> None:
    if not columns:
        print("（无结果）")
        return
    widths = [len(str(c)) for c in columns]
    for row in rows:
        for i, value in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(str(value)))
    print(" | ".join(str(c).ljust(widths[i]) for i, c in enumerate(columns)))
    print("-+-".join("-" * w for w in widths))
    for row in rows:
        print(" | ".join(str(v).ljust(widths[i]) for i, v in enumerate(row)))


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DmfDataAgent 语义层 CLI（MetricFlow 规范）")
    parser.add_argument(
        "command",
        nargs="?",
        default="check",
        help="check | list-metrics | list-dimensions | dim-values | sql | query",
    )
    parser.add_argument("args", nargs="*", help="命令参数（查询表达式 / 维度名）")
    parser.add_argument("--limit", type=int, default=20, help="dim-values 的条数上限")
    args = parser.parse_args(argv)

    settings = get_settings()
    layer = SemanticLayer()

    if args.command == "check":
        problems = layer.manifest.validate() + layer.manifest.reference_problems()
        print(f"语义清单：{layer.manifest.path}")
        if problems:
            print("校验失败：")
            for p in problems:
                print(f"  - {p}")
            return 1
        print(
            f"校验通过：{len(layer.manifest.models)} 个语义模型 / "
            f"{len(layer.list_metrics())} 个指标 / {len(layer.list_dimensions())} 个维度"
        )
        return 0

    if args.command == "list-metrics":
        for m in layer.list_metrics():
            print(
                f"- {m.name}（type={m.type} 单位={m.unit or '-'} "
                f"别名={m.aliases} 维度={'/'.join(layer.supported_dimensions(m))} "
                f"measure={'/'.join(layer.base_measures(m.name))}）"
            )
        return 0

    if args.command == "list-dimensions":
        for d in layer.list_dimensions():
            owners = "/".join(x.name for x in layer.manifest.dimension_owners(d.name))
            print(f"- {d.name}（{d.type} 列={d.expr} 模型={owners}）")
        return 0

    engine = MetricFlowEngine(layer, StarRocksClient(settings.effective_starrocks))

    if args.command == "dim-values":
        dim_name = args.args[0] if args.args else (layer.time_dimension_name() or "")
        try:
            sql = engine.compiler.compile_dimension_values(dim_name, args.limit)
            print(sql)
            result = asyncio.run(engine.executor.fetch(sql))
        except (CompileError, StarRocksError) as exc:
            print(f"失败：{exc}")
            return 1
        _print_table(result["columns"], result["rows"])
        return 0

    if args.command in ("sql", "query"):
        text = " ".join(args.args) if args.args else "metric:GMV"
        query = parse_query(text)
        if args.command == "sql":
            try:
                compiled = engine.compile(query)
            except CompileError as exc:
                print(f"编译失败：{exc}")
                return 1
            print(compiled.sql)
            print(f"\n-- 输出列：{compiled.columns}")
            print(f"-- 语义模型：{compiled.models}")
            return 0
        try:
            result = asyncio.run(engine.query(query))
        except (CompileError, StarRocksError) as exc:
            print(f"查询失败：{exc}")
            return 1
        print(f"-- 语义模型：{result['models']} | 行数：{result['row_count']} | "
              f"耗时：{result['elapsed_ms']} ms")
        _print_table(result["columns"], result["rows"])
        return 0

    print(f"未知命令：{args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
