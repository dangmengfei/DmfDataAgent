"""SQL 方言适配层。

编译器的方言差异集中在这一个模块，避免散落在编译逻辑里各写 `if dialect == ...`
（历史上 MySQL 分支曾把"取时点值"写成 `MAX_BY`，而 MySQL 根本没有该函数，
直到真机执行才暴露 —— 方言差异必须显式抽象、可单测）。

当前支持四种：

| 方言 | 标识符引用 | 时间粒度截断 | 月份平移 | 时点取值（arg_max） | FULL OUTER JOIN |
|---|---|---|---|---|---|
| `starrocks`（默认） | 反引号 | `date_trunc('unit', x)` | `add_months(x, n)` | `ARG_MAX(v, o)` | 支持 |
| `mysql` | 反引号 | `DATE`/`DATE_FORMAT`/`MAKEDATE` 组合 | `DATE_ADD(x, INTERVAL n MONTH)` | `SUBSTRING_INDEX(GROUP_CONCAT(...))` 模拟 | **不支持**，降级 LEFT JOIN |
| `postgresql` | 双引号 | `date_trunc('unit', x)` | `x + INTERVAL 'n months'` | `(ARRAY_AGG(v ORDER BY o DESC))[1]` | 支持 |
| `oracle` | 双引号 | `TRUNC(x, 'DD'/'IW'/'MM'/'Q'/'YYYY')` | `ADD_MONTHS(x, n)` | `MAX(v) KEEP (DENSE_RANK LAST ORDER BY o)` | 支持 |

**arg_max/arg_min 的语义**：取 `order_by` 列取最值（或最末/最先）那一行对应的 `value`，
即"月末时点余额"这类指标。四种方言的写法见上表。
"""

from __future__ import annotations

from typing import Dict

# 时间粒度 -> 通用 date_trunc 单位（StarRocks / PostgreSQL 共用）
_TRUNC_UNIT = {
    "day": "day",
    "week": "week",
    "month": "month",
    "quarter": "quarter",
    "year": "year",
}

# 时间粒度 -> Oracle TRUNC 的格式符
_ORACLE_TRUNC_FMT = {
    "day": "DD",
    "week": "IW",   # ISO 周（与 StarRocks date_trunc('week') 的周一对齐）
    "month": "MM",
    "quarter": "Q",
    "year": "YYYY",
}


class SqlDialect:
    """方言基类：默认按 StarRocks 语义实现子类覆盖。"""

    name = "starrocks"
    quote = "`"
    supports_full_outer_join = True

    # ---------------- 标识符 ----------------

    def ident(self, name: str) -> str:
        """引用标识符（内部引号双写转义）。"""
        return f"{self.quote}{str(name).replace(self.quote, self.quote * 2)}{self.quote}"

    def table_ref(self, table: str) -> str:
        """`db.table` / `table` -> 逐段引用。"""
        return ".".join(self.ident(p) for p in str(table).split(".") if p)

    # ---------------- 时间 ----------------

    def date_trunc(self, granularity: str, base: str) -> str:
        """把时间表达式截断到指定粒度（day/week/month/quarter/year）。"""
        return f"date_trunc('{_TRUNC_UNIT.get(granularity, 'day')}', {base})"

    def day_trunc(self, base: str) -> str:
        """截断到"天"（两阶段聚合的日内时间桶）。"""
        return self.date_trunc("day", base)

    def shift_time(self, expr: str, delta: Dict[str, int]) -> str:
        """按 delta（months/days）整体平移时间表达式（同环比对比期对齐）。"""
        months = delta.get("months")
        if months:
            return f"add_months({expr}, {months})"
        days = delta.get("days")
        if days:
            return f"DATE_ADD({expr}, INTERVAL {days} DAY)"
        return expr

    # ---------------- 时点聚合（last / first） ----------------

    def arg_max(self, value: str, order_by: str) -> str:
        """取 order_by 最大那一行的 value（"期末时点值"）。"""
        return f"ARG_MAX({value}, {order_by})"

    def arg_min(self, value: str, order_by: str) -> str:
        """取 order_by 最小那一行的 value（"期初时点值"）。"""
        return f"ARG_MIN({value}, {order_by})"

    def arg_value(self, agg: str, value: str, order_by: str) -> str:
        """agg in {last, first} -> 对应时点取值表达式。"""
        return (
            self.arg_max(value, order_by)
            if agg == "last"
            else self.arg_min(value, order_by)
        )


class StarRocksDialect(SqlDialect):
    name = "starrocks"


class MySqlDialect(SqlDialect):
    """MySQL：无 date_trunc / add_months / ARG_MAX，且不支持 FULL OUTER JOIN。"""

    name = "mysql"
    supports_full_outer_join = False

    def date_trunc(self, granularity: str, base: str) -> str:
        if granularity == "day":
            return f"DATE({base})"
        if granularity == "week":
            # 周一为周首日：回退到本周一
            return f"DATE_SUB(DATE({base}), INTERVAL WEEKDAY({base}) DAY)"
        if granularity == "month":
            return f"STR_TO_DATE(DATE_FORMAT({base}, '%Y-%m-01'), '%Y-%m-%d')"
        if granularity == "quarter":
            return f"DATE_ADD(MAKEDATE(YEAR({base}), 1), INTERVAL (QUARTER({base}) - 1) QUARTER)"
        if granularity == "year":
            return f"STR_TO_DATE(DATE_FORMAT({base}, '%Y-01-01'), '%Y-%m-%d')"
        return f"DATE({base})"

    def shift_time(self, expr: str, delta: Dict[str, int]) -> str:
        months = delta.get("months")
        if months:
            return f"DATE_ADD({expr}, INTERVAL {months} MONTH)"
        days = delta.get("days")
        if days:
            return f"DATE_ADD({expr}, INTERVAL {days} DAY)"
        return expr

    def arg_max(self, value: str, order_by: str) -> str:
        # MySQL 无 ARG_MAX/MAX_BY：用「按 order_by 倒序拼接后取首元素」模拟。
        # GROUP_CONCAT 截断发生在序列尾端，而这里只取首元素，顺序不受 group_concat_max_len 影响。
        return (
            f"CAST(SUBSTRING_INDEX(GROUP_CONCAT({value} ORDER BY {order_by} DESC), ',', 1)"
            " AS DECIMAL(65,10))"
        )

    def arg_min(self, value: str, order_by: str) -> str:
        return (
            f"CAST(SUBSTRING_INDEX(GROUP_CONCAT({value} ORDER BY {order_by} ASC), ',', 1)"
            " AS DECIMAL(65,10))"
        )


class PostgreSqlDialect(SqlDialect):
    """PostgreSQL：date_trunc 通用形式一致；时点取值用 ARRAY_AGG 取首元素。"""

    name = "postgresql"
    quote = '"'

    def shift_time(self, expr: str, delta: Dict[str, int]) -> str:
        months = delta.get("months")
        if months:
            return f"({expr} + INTERVAL '{months} months')"
        days = delta.get("days")
        if days:
            return f"({expr} + INTERVAL '{days} days')"
        return expr

    def arg_max(self, value: str, order_by: str) -> str:
        # PG 无 arg_max：数组按 order_by 倒序聚合后取第一个（PG 数组下标从 1 开始）
        return f"(ARRAY_AGG({value} ORDER BY {order_by} DESC))[1]"

    def arg_min(self, value: str, order_by: str) -> str:
        return f"(ARRAY_AGG({value} ORDER BY {order_by} ASC))[1]"


class OracleDialect(SqlDialect):
    """Oracle：TRUNC 取粒度、ADD_MONTHS 平移、KEEP (DENSE_RANK ...) 取时点值。"""

    name = "oracle"
    quote = '"'

    def date_trunc(self, granularity: str, base: str) -> str:
        return f"TRUNC({base}, '{_ORACLE_TRUNC_FMT.get(granularity, 'DD')}')"

    def shift_time(self, expr: str, delta: Dict[str, int]) -> str:
        months = delta.get("months")
        if months:
            return f"ADD_MONTHS({expr}, {months})"
        days = delta.get("days")
        if days:
            return f"({expr} + {days})"
        return expr

    def arg_max(self, value: str, order_by: str) -> str:
        return f"MAX({value}) KEEP (DENSE_RANK LAST ORDER BY {order_by})"

    def arg_min(self, value: str, order_by: str) -> str:
        return f"MIN({value}) KEEP (DENSE_RANK FIRST ORDER BY {order_by})"


_DIALECTS = {
    d.name: d
    for d in (
        StarRocksDialect(),
        MySqlDialect(),
        PostgreSqlDialect(),
        OracleDialect(),
    )
}

SUPPORTED_DIALECTS = tuple(_DIALECTS)


def get_dialect(name: str | None) -> SqlDialect:
    """按名字取方言实例；空值按 StarRocks（历史默认），未知名字显式报错。

    未知方言直接报错而不是静默回落：方言写错会让编译产物在真库执行时才炸，
    启动期报错更容易定位。
    """
    key = (name or "starrocks").strip().lower()
    dialect = _DIALECTS.get(key)
    if dialect is None:
        raise ValueError(
            f"不支持的 SQL 方言：{name!r}；可选：{'、'.join(SUPPORTED_DIALECTS)}"
        )
    return dialect


__all__ = [
    "SqlDialect",
    "StarRocksDialect",
    "MySqlDialect",
    "PostgreSqlDialect",
    "OracleDialect",
    "SUPPORTED_DIALECTS",
    "get_dialect",
]