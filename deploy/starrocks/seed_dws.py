#!/usr/bin/env python3
"""DmfDataAgent 示例底表造数脚本（StarRocks）。

按 `backend/dmf_agent/semantic/manifest.yml` 的语义模型设计，生成两张 DWS 预聚合
宽表（dws_trade_day / dws_user_day）的**确定性**测试数据，用于跑通
"语义层 -> MetricFlow 编译 -> StarRocks 取数"全链路。

用法（在仓库根目录，使用 backend 的虚拟环境）：

    # 1) 只生成 SQL（不连库，可人工审查后手动执行）
    backend/.venv/bin/python deploy/starrocks/seed_dws.py --emit-sql deploy/starrocks/dws_seed.sql

    # 2) 直接连库建表 + 灌数（读取 DMF_STARROCKS-* 配置，见 backend/.env）
    backend/.venv/bin/python deploy/starrocks/seed_dws.py --execute --create-database

    # 3) 只做连通性与数据校验
    backend/.venv/bin/python deploy/starrocks/seed_dws.py --verify

数据设计要点：
- 覆盖"近 N 天"（默认 42 天，够算环比）与"去年同期 M 天"（默认 11 天，够算同比）；
- 交易域与用户域共享 (统计日期, 渠道, 平台)，因此
  付费转化率 = 付费用户数 / 日活跃用户数 这类跨模型 ratio 指标可算；
- 付费/复购用户按渠道×平台先定量，再分摊到 (地区, 品类, 门店) 组合，
  保证按渠道汇总时 付费用户数 < 日活跃用户数（转化率落在合理区间）；
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

# ---- 让脚本能直接以 dmf_agent 的配置读取 StarRocks 连接信息 ----
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_DIR = _REPO_ROOT / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

SCHEMA_FILE = Path(__file__).with_name("dws_schema.sql")
DEFAULT_SEED = 20260911

# ---------------------------------------------------------------- 维度取值域
CHANNELS = ("线上", "门店", "小程序")
REGIONS = ("华东", "华南", "华北", "西南")
CATEGORIES = ("家电", "数码", "服饰")
PLATFORMS = ("APP", "小程序", "H5")
STORES: Dict[str, Tuple[str, ...]] = {
    "华东": ("上海静安店", "杭州西湖店"),
    "华南": ("广州天河店", "深圳南山店"),
    "华北": ("北京朝阳店", "天津和平店"),
    "西南": ("成都武侯店", "重庆渝中店"),
}

# ---------------------------------------------------------------- 业务参数
# 日活基数与渠道/平台分布
DAU_BASE = 200_000
CHANNEL_DAU_W = {"线上": 0.45, "门店": 0.35, "小程序": 0.20}
PLATFORM_W = {"APP": 0.50, "小程序": 0.30, "H5": 0.20}
# 渠道特征：新增率 / 付费率 / 复购率 / 人均订单数
NEW_RATE = {"线上": 0.12, "门店": 0.05, "小程序": 0.18}
PAY_RATE = {"线上": 0.30, "门店": 0.22, "小程序": 0.35}
REPURCHASE_RATE = {"线上": 0.28, "门店": 0.40, "小程序": 0.22}
ORDER_PER_PAID = {"线上": 1.45, "门店": 1.20, "小程序": 1.30}
# 品类客单价（元）
AOV = {"家电": 1200.0, "数码": 800.0, "服饰": 260.0}

TRADE_COLUMNS = (
    "(stat_date, channel_name, region_name, category_name, platform_name, "
    "store_name, gmv, order_cnt, paid_user_cnt, repurchase_user_cnt)"
)
USER_COLUMNS = "(stat_date, channel_name, platform_name, active_user_cnt, new_user_cnt)"


# ---------------------------------------------------------------- 数据生成
def _seasonal(day: date, base_day: date) -> float:
    """周末抬升 + 温和增长趋势，让趋势/同环比查询有肉眼可见的形状。"""
    weekend = 1.18 if day.weekday() >= 5 else 1.0
    trend = 1.0 + 0.004 * (day - base_day).days
    return weekend * trend


def _combos(channel: str) -> List[Tuple[str, str, str]]:
    """该渠道的交易明细组合 (地区, 品类, 门店)；非门店渠道无门店归属 -> ''。"""
    if channel == "门店":
        return [
            (region, category, store)
            for region in REGIONS
            for category in CATEGORIES
            for store in STORES[region]
        ]
    return [(region, category, "") for region in REGIONS for category in CATEGORIES]


def _split(rng: random.Random, total: int, n: int) -> List[int]:
    """把 total 按随机权重拆成 n 个非负整数（和恰好为 total）。"""
    if n <= 0:
        return []
    if total <= 0:
        return [0] * n
    weights = [rng.random() + 0.5 for _ in range(n)]
    weight_sum = sum(weights)
    parts = [int(total * w / weight_sum) for w in weights]
    parts[0] += total - sum(parts)
    return parts


def build_rows(
    start: date, end: date, *, seed: int = DEFAULT_SEED
) -> Tuple[List[tuple], List[tuple]]:
    """生成 (交易域行, 用户域行)；确定性：同一 seed + 日期范围 -> 同一份数据。"""
    rng = random.Random(seed)
    trade: List[tuple] = []
    users: List[tuple] = []

    day = start
    while day <= end:
        factor = _seasonal(day, start)
        iso = day.isoformat()
        for channel in CHANNELS:
            for platform in PLATFORMS:
                active = int(
                    DAU_BASE
                    * CHANNEL_DAU_W[channel]
                    * PLATFORM_W[platform]
                    * factor
                    * rng.uniform(0.92, 1.08)
                )
                new_user = int(active * NEW_RATE[channel] * rng.uniform(0.90, 1.10))
                paid_total = int(active * PAY_RATE[channel] * rng.uniform(0.95, 1.05))
                repurchase_total = int(
                    paid_total * REPURCHASE_RATE[channel] * rng.uniform(0.90, 1.10)
                )
                users.append((iso, channel, platform, active, new_user))

                combos = _combos(channel)
                paid_parts = _split(rng, paid_total, len(combos))
                repurchase_parts = _split(rng, repurchase_total, len(combos))
                for (region, category, store), paid, repurchase in zip(
                    combos, paid_parts, repurchase_parts
                ):
                    orders = int(paid * ORDER_PER_PAID[channel] * rng.uniform(0.85, 1.15))
                    aov = AOV[category] * rng.uniform(0.90, 1.10)
                    gmv = round(orders * aov, 2)
                    trade.append(
                        (iso, channel, region, category, platform, store, gmv, orders, paid, repurchase)
                    )
        day += timedelta(days=1)
    return trade, users


# ---------------------------------------------------------------- SQL 拼装
def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def build_sql(trade_rows: Sequence[tuple], user_rows: Sequence[tuple], *, batch: int = 200) -> str:
    """DDL + TRUNCATE + 分批 INSERT 拼成一份可直接执行的 SQL 脚本。"""
    parts: List[str] = [
        SCHEMA_FILE.read_text(encoding="utf-8").rstrip(),
        "",
        "-- ---------------------------------------------------------------",
        "-- 幂等灌数：先清空再写入（StarRocks AGGREGATE KEY 表 TRUNCATE 直接清空全部数据）",
        "-- ---------------------------------------------------------------",
        "TRUNCATE TABLE dws_trade_day;",
        "TRUNCATE TABLE dws_user_day;",
        "",
    ]
    for table, columns, rows in (
        ("dws_trade_day", TRADE_COLUMNS, trade_rows),
        ("dws_user_day", USER_COLUMNS, user_rows),
    ):
        for offset in range(0, len(rows), batch):
            chunk = rows[offset : offset + batch]
            values = ",\n  ".join(
                "(" + ", ".join(_literal(v) for v in row) + ")" for row in chunk
            )
            parts.append(f"INSERT INTO {table} {columns} VALUES\n  {values};")
            parts.append("")
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
async def _run_sql(sql_text: str, *, create_database: bool = False) -> Dict[str, int]:
    import aiomysql

    from dmf_agent.config.settings import Settings  # noqa: PLC0415 - 允许无配置时仅生成 SQL

    settings = Settings()
    sr = settings.starrocks
    if not sr.host:
        raise SystemExit(
            "未配置 StarRocks 连接：请设置 DMF_STARROCKS-HOST / PORT / USER / "
            "PASSWORD / DATABASE（可写入 backend/.env，或直接用 --emit-sql 生成 SQL）"
        )

    conn = await aiomysql.connect(
        host=sr.host,
        port=sr.port,
        user=sr.user,
        password=sr.password,
        db=None if create_database else (sr.database or None),
        charset=sr.charset or "utf8mb4",
        connect_timeout=sr.connect_timeout,
        autocommit=True,
    )
    executed = 0
    try:
        async with conn.cursor() as cursor:
            if create_database and sr.database:
                await cursor.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{sr.database}`"
                )
                await cursor.execute(f"USE `{sr.database}`")
            for statement in iter_statements(sql_text):
                await cursor.execute(statement)
                executed += 1
    finally:
        conn.close()
    return {"executed": executed}


async def _verify() -> int:
    from dmf_agent.config.settings import Settings
    from dmf_agent.semantic.starrocks import StarRocksClient, StarRocksError

    client = StarRocksClient(Settings().starrocks)
    if not client.configured:
        print("[verify] StarRocks 未配置，跳过校验")
        return 1

    checks = [
        ("dws_trade_day 行数", "SELECT COUNT(*) FROM dws_trade_day"),
        ("dws_user_day  行数", "SELECT COUNT(*) FROM dws_user_day"),
        (
            "日期范围",
            "SELECT MIN(stat_date), MAX(stat_date) FROM dws_trade_day",
        ),
        (
            "近 7 天 GMV（按日期）",
            "SELECT stat_date, SUM(gmv) AS gmv FROM dws_trade_day "
            "GROUP BY stat_date ORDER BY stat_date DESC LIMIT 7",
        ),
        (
            "付费转化率（渠道维度）",
            "SELECT t.channel_name, ROUND(SUM(t.paid_user_cnt) * 100.0 / SUM(u.active_user_cnt), 2) "
            "AS 付费转化率 FROM dws_trade_day t JOIN dws_user_day u "
            "ON t.stat_date = u.stat_date AND t.channel_name = u.channel_name "
            "AND t.platform_name = u.platform_name GROUP BY t.channel_name ORDER BY 1",
        ),
    ]
    failed = 0
    for label, sql in checks:
        try:
            result = await client.fetch(sql)
        except StarRocksError as exc:
            print(f"[verify] ✗ {label}：{exc}")
            failed += 1
            continue
        preview = result["rows"][:7]
        print(f"[verify] ✓ {label}：{preview if result['row_count'] <= 7 else str(preview) + ' …'}")
    await client.close()
    return 1 if failed else 0


# ---------------------------------------------------------------- CLI
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成/写入 StarRocks 示例底表数据")
    parser.add_argument("--emit-sql", metavar="PATH", help="把生成结果写成 SQL 文件（不连库）")
    parser.add_argument("--execute", action="store_true", help="连库执行建表 + 灌数")
    parser.add_argument("--create-database", action="store_true", help="执行前先 CREATE DATABASE IF NOT EXISTS")
    parser.add_argument("--verify", action="store_true", help="只做连通性与数据校验")
    parser.add_argument("--days", type=int, default=42, help="当期窗口天数（默认 42，覆盖环比）")
    parser.add_argument("--yoy-days", type=int, default=11, help="去年同期天数（默认 11，覆盖同比）")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="随机种子（保证确定性）")
    parser.add_argument("--end", default=date.today().isoformat(), help="当期窗口结束日期（默认今天）")
    parser.add_argument("--batch", type=int, default=200, help="单条 INSERT 的行数上限")
    args = parser.parse_args(argv)

    if args.verify and not (args.execute or args.emit_sql):
        return asyncio.run(_verify())

    end = date.fromisoformat(args.end)
    cur_start = end - timedelta(days=max(1, args.days) - 1)
    yoy_end = end.replace(year=end.year - 1)
    yoy_start = yoy_end - timedelta(days=max(1, args.yoy_days) - 1)

    trade, users = build_rows(cur_start, end, seed=args.seed)
    trade_yoy, users_yoy = build_rows(yoy_start, yoy_end, seed=args.seed + 1)
    trade, users = trade + trade_yoy, users + users_yoy

    print(
        f"生成数据：当期 {cur_start}~{end}、去年同期 {yoy_start}~{yoy_end}；"
        f"dws_trade_day {len(trade)} 行、dws_user_day {len(users)} 行"
    )

    sql_text = build_sql(trade, users, batch=max(1, args.batch))

    if args.emit_sql:
        out = Path(args.emit_sql)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(sql_text, encoding="utf-8")
        print(f"SQL 已写出：{out}（{len(sql_text) / 1024:.0f} KB）")

    if args.execute:
        result = asyncio.run(_run_sql(sql_text, create_database=args.create_database))
        print(f"执行完成：{result['executed']} 条语句")

    if not args.emit_sql and not args.execute:
        print("未指定 --emit-sql / --execute，仅生成数据未落盘。示例：")
        print("  --emit-sql deploy/starrocks/dws_seed.sql")
        print(sql_text[:400] + "\n…")

    if args.verify:
        return asyncio.run(_verify())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
