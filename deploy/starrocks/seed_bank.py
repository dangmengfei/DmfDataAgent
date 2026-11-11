#!/usr/bin/env python3
"""银行零售（存款 / 贷款）示例底表造数脚本。

按 `backend/dmf_agent/semantic/manifest.yml` 的 `retail_deposit_day` /
`retail_loan_day` 两个语义模型设计，生成两张 DWS 预聚合宽表的**确定性**明细数据，
用于跑通「语义层 -> MetricFlow 编译 -> StarRocks/MySQL 取数」全链路，并支撑
银行零售 10 个指标（原子 / 衍生 / 派生）的手工测试。

用法（在仓库根目录，使用 backend 的虚拟环境）：

    # 1) 只生成 SQL（不连库，可人工审查后手动执行）
    backend/.venv/bin/python deploy/starrocks/seed_bank.py --emit-sql /tmp/bank_seed.sql

    # 2) 直接连库建表 + 灌数（读取 DMF_STARROCKS-* 配置）
    backend/.venv/bin/python deploy/starrocks/seed_bank.py --execute

    # 3) 只做连通性与数据校验（余额规模 / 存贷比 / 不良率 / 加权利率）
    backend/.venv/bin/python deploy/starrocks/seed_bank.py --verify

数据设计要点：
- 覆盖"近 N 天"（默认 42 天，够算环比）与"去年同期 M 天"（默认 42 天，够算同比）；
- 两表维度**完全一致**（统计日期 × 机构 × 零售渠道 × 产品类型 × 客户类型 × 客户等级），
  因此跨表指标（净利息收入 / 存贷比）能按任意维度分组；
- 规模：零售存款约 1200 亿元、零售贷款约 900 亿元 -> 存贷比 ≈ 75%；
- 结构：存款加权付息率 ≈ 1.48%，贷款加权收息率 ≈ 4.87%，不良率 ≈ 0.84%；
- 季节特征：存款月末 3 天冲量 +6%、周末回落 -1.5%；贷款月初 3 天投放 +0.8%；
- 去年同期整体按 0.94 折算（模拟同比 +6% 左右的自然增长），让 yoy 有真实形状；
- 随机数由固定 seed 驱动，同一 (seed, 日期范围) 必得同一份数据，便于回归比对。
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

# ---- 让脚本能直接以 dmf_agent 的配置读取 StarRocks / MySQL 连接信息 ----
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_DIR = _REPO_ROOT / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

_SCHEMA_VARIANTS = {
    "starrocks": _REPO_ROOT / "deploy" / "starrocks" / "bank_schema.sql",
    "mysql": _REPO_ROOT / "deploy" / "local" / "bank_schema_mysql.sql",
}
DEFAULT_SEED = 20260913

# ---------------------------------------------------------------- 维度取值域
ORGS = ("上海分行", "北京分行", "深圳分行", "成都分行")
ORG_W: Dict[str, float] = {"上海分行": 1.00, "北京分行": 0.90, "深圳分行": 0.80, "成都分行": 0.60}

CHANNELS = ("手机银行", "网上银行", "柜面")
CHANNEL_W: Dict[str, float] = {"手机银行": 0.50, "网上银行": 0.20, "柜面": 0.30}

# 存款产品：(结构占比, 年化付息率)
DEPOSIT_PRODUCTS: Dict[str, Tuple[float, float]] = {
    "活期存款": (0.35, 0.0025),
    "定期存款": (0.30, 0.0180),
    "大额存单": (0.20, 0.0230),
    "结构性存款": (0.15, 0.0260),
}
# 贷款产品：(结构占比, 年化收息率, 不良率)
LOAN_PRODUCTS: Dict[str, Tuple[float, float, float]] = {
    "个人住房贷款": (0.55, 0.0395, 0.0035),
    "个人消费贷款": (0.20, 0.0460, 0.0105),
    "个人经营贷款": (0.15, 0.0420, 0.0160),
    "信用卡分期": (0.10, 0.1150, 0.0195),
}

CUSTOMER_TYPES: Dict[str, float] = {"个人": 0.85, "个人经营": 0.15}
CUSTOMER_LEVELS: Dict[str, float] = {"普通": 0.75, "贵宾": 0.25}

# ---------------------------------------------------------------- 业务规模参数
DEPOSIT_TOTAL = 1.2e11  # 零售存款余额约 1200 亿元（元）
LOAN_TOTAL = 9.0e10  # 零售贷款余额约 900 亿元（元）
DEPOSIT_PER_CUSTOMER: Dict[str, float] = {"普通": 80_000.0, "贵宾": 1_200_000.0}
LOAN_PER_CUSTOMER: Dict[str, float] = {
    "个人住房贷款": 1_200_000.0,
    "个人消费贷款": 150_000.0,
    "个人经营贷款": 500_000.0,
    "信用卡分期": 50_000.0,
}

_TRADE_COLS = (
    "stat_date, org_name, channel_name, product_type, customer_type, customer_level"
)
DEPOSIT_COLUMNS = f"({_TRADE_COLS}, deposit_balance, deposit_interest, deposit_customer_cnt)"
LOAN_COLUMNS = (
    f"({_TRADE_COLS}, loan_balance, loan_interest, npl_balance, loan_customer_cnt)"
)

_ORG_W_SUM = sum(ORG_W.values())


# ---------------------------------------------------------------- 组合与季节
def _combos() -> Tuple[List[tuple], List[tuple]]:
    """预计算两组维度组合：(org, channel, product, ctype, clevel, share, rate[, npl_rate])。

    share 为该组合在全行的结构占比（各分类权重已归一化，合计 ≈ 1）。
    """
    dep: List[tuple] = []
    for org in ORGS:
        for channel in CHANNELS:
            for product, (pw, rate) in DEPOSIT_PRODUCTS.items():
                for ctype, tw in CUSTOMER_TYPES.items():
                    for clevel, lw in CUSTOMER_LEVELS.items():
                        share = (ORG_W[org] / _ORG_W_SUM) * CHANNEL_W[channel] * pw * tw * lw
                        dep.append((org, channel, product, ctype, clevel, share, rate))
    loan: List[tuple] = []
    for org in ORGS:
        for channel in CHANNELS:
            for product, (pw, rate, npl) in LOAN_PRODUCTS.items():
                for ctype, tw in CUSTOMER_TYPES.items():
                    for clevel, lw in CUSTOMER_LEVELS.items():
                        share = (ORG_W[org] / _ORG_W_SUM) * CHANNEL_W[channel] * pw * tw * lw
                        loan.append((org, channel, product, ctype, clevel, share, rate, npl))
    return dep, loan


DEPOSIT_COMBOS, LOAN_COMBOS = _combos()


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - date(year, month, 1)).days


def _deposit_factor(day: date, base: date) -> float:
    """存款：月末 3 天冲量 + 周末回落 + 温和增长。"""
    month_end = 1.06 if _days_in_month(day.year, day.month) - day.day < 3 else 1.0
    weekend = 0.985 if day.weekday() >= 5 else 1.0
    trend = 1.0 + 0.0006 * (day - base).days
    return month_end * weekend * trend


def _loan_factor(day: date, base: date) -> float:
    """贷款：月初 3 天集中投放 + 稳定增长（余额周末不动）。"""
    month_head = 1.008 if day.day <= 3 else 1.0
    trend = 1.0 + 0.0009 * (day - base).days
    return month_head * trend


# ---------------------------------------------------------------- 数据生成
def build_rows(
    start: date,
    end: date,
    *,
    seed: int = DEFAULT_SEED,
    scale: float = 1.0,
) -> Tuple[List[tuple], List[tuple]]:
    """生成 (存款行, 贷款行)；确定性：同一 seed + 日期范围 -> 同一份数据。

    scale 用于整体缩放（去年同期传 <1，模拟自然增长，让同比有真实形状）。
    """
    rng = random.Random(seed)
    deposit: List[tuple] = []
    loan: List[tuple] = []

    day = start
    while day <= end:
        df = _deposit_factor(day, start)
        lf = _loan_factor(day, start)
        iso = day.isoformat()
        for org, channel, product, ctype, clevel, share, rate in DEPOSIT_COMBOS:
            balance = DEPOSIT_TOTAL * share * df * scale * rng.uniform(0.97, 1.03)
            interest = balance * rate / 365.0 * rng.uniform(0.98, 1.02)
            customers = max(
                1, int(balance / DEPOSIT_PER_CUSTOMER[clevel] * rng.uniform(0.98, 1.02))
            )
            deposit.append(
                (
                    iso, org, channel, product, ctype, clevel,
                    round(balance, 2), round(interest, 2), customers,
                )
            )
        for org, channel, product, ctype, clevel, share, rate, npl_rate in LOAN_COMBOS:
            balance = LOAN_TOTAL * share * lf * scale * rng.uniform(0.97, 1.03)
            interest = balance * rate / 365.0 * rng.uniform(0.98, 1.02)
            npl = balance * npl_rate * rng.uniform(0.90, 1.10)
            customers = max(
                1, int(balance / LOAN_PER_CUSTOMER[product] * rng.uniform(0.98, 1.02))
            )
            loan.append(
                (
                    iso, org, channel, product, ctype, clevel,
                    round(balance, 2), round(interest, 2), round(npl, 2), customers,
                )
            )
        day += timedelta(days=1)
    return deposit, loan


# ---------------------------------------------------------------- SQL 拼装
def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def _inserts(table: str, columns: str, rows: Sequence[tuple], batch: int) -> List[str]:
    parts: List[str] = []
    for offset in range(0, len(rows), batch):
        chunk = rows[offset : offset + batch]
        values = ",\n  ".join("(" + ", ".join(_literal(v) for v in row) + ")" for row in chunk)
        parts.append(f"INSERT INTO {table} {columns} VALUES\n  {values};")
    return parts


def build_sql(
    deposit_rows: Sequence[tuple],
    loan_rows: Sequence[tuple],
    *,
    schema_variant: str = "starrocks",
    batch: int = 500,
    truncate: bool = True,
) -> str:
    """DDL + TRUNCATE + 分批 INSERT 拼成一份可直接执行的 SQL 脚本。"""
    schema_file = _SCHEMA_VARIANTS[schema_variant]
    parts: List[str] = [schema_file.read_text(encoding="utf-8").rstrip(), ""]
    if truncate:
        parts += [
            "-- ---------------------------------------------------------------",
            "-- 幂等灌数：先清空再写入（两表均按 stat_date 全量重灌）",
            "-- ---------------------------------------------------------------",
            "TRUNCATE TABLE dws_retail_deposit_day;",
            "TRUNCATE TABLE dws_retail_loan_day;",
            "",
        ]
    parts += _inserts("dws_retail_deposit_day", DEPOSIT_COLUMNS, deposit_rows, batch)
    parts += _inserts("dws_retail_loan_day", LOAN_COLUMNS, loan_rows, batch)
    return "\n".join(parts).rstrip() + "\n"


def iter_statements(sql_text: str) -> Iterable[str]:
    """按 ';' 切分语句（跳过整行注释与空行），供逐条执行。"""
    buffer: List[str] = []
    for raw in sql_text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buffer.append(raw)
        if stripped.endswith(";"):
            statement = "\n".join(buffer).strip().rstrip(";").strip()
            if statement:
                yield statement
            buffer = []
    tail = "\n".join(buffer).strip()
    if tail:
        yield tail


# ---------------------------------------------------------------- 连库执行
def _dsn() -> Dict[str, Any]:
    from dmf_agent.config.settings import Settings  # noqa: PLC0415

    sr = Settings().starrocks
    if not sr.host:
        raise SystemExit(
            "未配置 StarRocks/MySQL 连接：请设置 DMF_STARROCKS-HOST / PORT / USER / "
            "PASSWORD / DATABASE（可写入 backend/.env，或直接用 --emit-sql 生成 SQL）"
        )
    return {
        "host": sr.host,
        "port": int(sr.port),
        "user": sr.user,
        "password": sr.password or "",
        "db": sr.database,
        "charset": sr.charset or "utf8mb4",
        "connect_timeout": int(sr.connect_timeout or 10),
        "autocommit": True,
    }


async def _run_sql(sql_text: str) -> Dict[str, int]:
    import aiomysql

    conn = await aiomysql.connect(**_dsn())
    kinds = {"DDL": 0, "TRUNCATE": 0, "INSERT": 0, "OTHER": 0}
    try:
        async with conn.cursor() as cursor:
            for statement in iter_statements(sql_text):
                head = statement.lstrip().split(None, 1)[0].upper()
                if head in ("CREATE", "ALTER", "DROP"):
                    kinds["DDL"] += 1
                elif head == "TRUNCATE":
                    kinds["TRUNCATE"] += 1
                elif head == "INSERT":
                    kinds["INSERT"] += 1
                else:
                    kinds["OTHER"] += 1
                await cursor.execute(statement)
    finally:
        conn.close()
    return kinds


async def _verify() -> int:
    """数据体检：规模 / 日期范围 / 存贷比 / 不良率 / 加权利率。"""
    import aiomysql

    conn = await aiomysql.connect(**_dsn())
    failed = 0

    async def one(cursor, label: str, sql: str) -> None:
        nonlocal failed
        try:
            await cursor.execute(sql)
            row = await cursor.fetchone()
        except Exception as exc:  # noqa: BLE001 - 校验脚本，失败即报告
            print(f"[verify] x {label}：{exc}")
            failed += 1
            return
        print(f"[verify] v {label}：{row}")

    try:
        async with conn.cursor() as cursor:
            await one(
                cursor, "存款表(行数/起/止)",
                "SELECT COUNT(*), MIN(stat_date), MAX(stat_date) FROM dws_retail_deposit_day",
            )
            await one(
                cursor, "贷款表(行数/起/止)",
                "SELECT COUNT(*), MIN(stat_date), MAX(stat_date) FROM dws_retail_loan_day",
            )
            await one(
                cursor, "某日 存款余额 / 贷款余额 / 存贷比%",
                "SELECT ROUND(d.bal/1e8,2) AS 存款亿, ROUND(l.bal/1e8,2) AS 贷款亿,"
                " ROUND(l.bal/d.bal*100,2) AS 存贷比"
                " FROM (SELECT SUM(deposit_balance) bal FROM dws_retail_deposit_day"
                "       WHERE stat_date=(SELECT MAX(stat_date) FROM dws_retail_deposit_day)) d,"
                "      (SELECT SUM(loan_balance) bal FROM dws_retail_loan_day"
                "       WHERE stat_date=(SELECT MAX(stat_date) FROM dws_retail_loan_day)) l",
            )
            # 注意：别名不可用 int（MySQL 保留字），否则 1064 语法错误
            await one(
                cursor, "不良贷款率% / 存款加权付息率% / 贷款加权收息率%",
                "SELECT ROUND(l.npl/l.bal*100,4) AS 不良率,"
                " ROUND(d.dint/d.bal*365*100,4) AS 付息率,"
                " ROUND(l.lint/l.bal*365*100,4) AS 收息率"
                " FROM (SELECT SUM(deposit_balance) bal, SUM(deposit_interest) dint"
                "       FROM dws_retail_deposit_day) d,"
                "      (SELECT SUM(loan_balance) bal, SUM(loan_interest) lint, SUM(npl_balance) npl"
                "       FROM dws_retail_loan_day) l",
            )
            await one(
                cursor, "最新日 按机构存款余额(亿)",
                "SELECT org_name, ROUND(SUM(deposit_balance)/1e8,2)"
                " FROM dws_retail_deposit_day"
                " WHERE stat_date=(SELECT MAX(stat_date) FROM dws_retail_deposit_day)"
                " GROUP BY org_name ORDER BY 2 DESC",
            )
            await one(
                cursor, "全期 按产品贷款不良率%",
                "SELECT product_type, ROUND(SUM(npl_balance)/SUM(loan_balance)*100,4)"
                " FROM dws_retail_loan_day GROUP BY product_type ORDER BY 2 DESC",
            )
            # 去年同期（同比取数）是否存在
            await one(
                cursor, "存款表去年同期天数",
                "SELECT COUNT(DISTINCT stat_date) FROM dws_retail_deposit_day"
                " WHERE stat_date < DATE_SUB(CURDATE(), INTERVAL 300 DAY)",
            )
    finally:
        conn.close()
    return 1 if failed else 0


# ---------------------------------------------------------------- CLI
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成/写入银行零售示例底表数据")
    parser.add_argument("--emit-sql", metavar="PATH", help="把生成结果写成 SQL 文件（不连库）")
    parser.add_argument("--execute", action="store_true", help="连库执行建表 + 灌数")
    parser.add_argument("--verify", action="store_true", help="只做连通性与数据校验")
    parser.add_argument("--days", type=int, default=42, help="当期窗口天数（默认 42，覆盖环比）")
    parser.add_argument("--yoy-days", type=int, default=42, help="去年同期天数（默认 42，覆盖同比）")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="随机种子（保证确定性）")
    parser.add_argument("--end", default=date.today().isoformat(), help="当期窗口结束日期（默认今天）")
    parser.add_argument("--batch", type=int, default=500, help="单条 INSERT 的行数上限")
    parser.add_argument(
        "--schema", choices=sorted(_SCHEMA_VARIANTS), default="starrocks",
        help="DDL 变体（starrocks 默认；本机 MySQL 跑链路时用 mysql）",
    )
    parser.add_argument("--yoy-scale", type=float, default=0.94, help="去年同期整体折算系数")
    args = parser.parse_args(argv)

    if args.verify and not (args.execute or args.emit_sql):
        return asyncio.run(_verify())

    end = date.fromisoformat(args.end)
    cur_start = end - timedelta(days=max(1, args.days) - 1)
    yoy_end = end.replace(year=end.year - 1)
    yoy_start = yoy_end - timedelta(days=max(1, args.yoy_days) - 1)

    dep1, loan1 = build_rows(cur_start, end, seed=args.seed)
    dep2, loan2 = build_rows(yoy_start, yoy_end, seed=args.seed + 1, scale=args.yoy_scale)
    dep, loan = dep1 + dep2, loan1 + loan2

    print(
        f"生成数据：当期 {cur_start}~{end}、去年同期 {yoy_start}~{yoy_end}；"
        f"dws_retail_deposit_day {len(dep)} 行、dws_retail_loan_day {len(loan)} 行"
    )

    sql_text = build_sql(
        dep, loan, schema_variant=args.schema, batch=max(1, args.batch)
    )

    if args.emit_sql:
        out = Path(args.emit_sql)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(sql_text, encoding="utf-8")
        print(f"SQL 已写出：{out}（{len(sql_text) / 1024:.0f} KB）")

    if args.execute:
        result = asyncio.run(_run_sql(sql_text))
        print(f"执行完成：{result}")

    if not args.emit_sql and not args.execute:
        print("未指定 --emit-sql / --execute，仅生成数据未落盘。示例：")
        print("  --emit-sql /tmp/bank_seed.sql --schema mysql")

    if args.verify:
        return asyncio.run(_verify())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
