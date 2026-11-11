#!/usr/bin/env python3
"""DmfDataAgent 本地 MySQL 示例底表造数脚本。

场景：没有真实 StarRocks 时，让 semantic/starrocks.py（实际走 aiomysql）指向本地
MySQL，跑通「语义层编译 -> 执行 -> 真实数据返回」的全链路。

设计：
- 数据**完全复用** deploy/starrocks/seed_dws.py::build_rows + build_sql —— 同 seed 同输出。
- DDL 用本目录的 dws_schema_mysql.sql（InnoDB + 复合主键 + 索引），不是 StarRocks OLAP。
- DSN 直接读 backend 的 Settings：把 `DMF_STARROCKS-*` 指向本地 MySQL 即可，
  semantic 引擎读这组 env，本脚本亦同。

用法（在仓库根，使用 backend 的虚拟环境）：

    # 1) 建表 + 灌数（一把梭）
    backend/.venv/bin/python deploy/local/seed_mysql.py --execute

    # 2) 仅校验数据
    backend/.venv/bin/python deploy/local/seed_mysql.py --verify

    # 3) 仅生成 SQL（不连库）
    backend/.venv/bin/python deploy/local/seed_mysql.py --emit-sql /tmp/dws_seed_mysql.sql

要求：
- `DMF_STARROCKS-*` 必须已配置（host/port/user/password/database），
  缺失会立即报错并打印提示。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict

# ---- 复用 deploy/starrocks/seed_dws.py 的 build_rows/build_sql ----
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_DIR = _REPO_ROOT / "backend"
_DEPLOY_STARROCKS = _REPO_ROOT / "deploy" / "starrocks"
for p in (str(_BACKEND_DIR), str(_DEPLOY_STARROCKS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import seed_dws as _starrocks_seed  # type: ignore[import-not-found]  # noqa: E402

_LOCAL_DIR = Path(__file__).resolve().parent
SCHEMA_FILE = _LOCAL_DIR / "dws_schema_mysql.sql"

DEFAULT_END = date(2026, 9, 11)
DEFAULT_DAYS_PAST = 42
# 去年同期造数范围（覆盖去年同 N 天，便于同比 query 取到非 NULL 前期值）
DEFAULT_DAYS_LAST_YEAR = 42


# ---------------------------------------------------------------- DSN / aiomysql
def _require_dsn() -> Dict[str, str]:
    """从 backend Settings 抽取 StarRocksSettings；缺一即报错。"""
    from dmf_agent.config.settings import Settings  # noqa: WPS433

    s = Settings()
    sr = s.starrocks
    missing = [
        k for k in ("host", "port", "user", "database")
        if not getattr(sr, k, None)
    ]
    if missing:
        raise SystemExit(
            "[!] DMF_STARROCKS-"
            + "/".join(missing).upper()
            + " 未配置；请在 backend/.env 设置后再跑。"
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
    """用 aiomysql 直连 DSN 跑 SQL；返回各类语句数。"""
    import aiomysql

    dsn = _require_dsn()
    conn = await aiomysql.connect(**dsn)
    cur: aiomysql.Cursor = await conn.cursor()
    try:
        kinds = {"DDL": 0, "TRUNCATE": 0, "INSERT": 0, "OTHER": 0}
        for stmt in _starrocks_seed.iter_statements(sql_text):
            head = stmt.lstrip().split(None, 1)[0].upper()
            if head in ("CREATE", "ALTER", "DROP"):
                kinds["DDL"] += 1
            elif head == "TRUNCATE":
                kinds["TRUNCATE"] += 1
            elif head == "INSERT":
                kinds["INSERT"] += 1
            else:
                kinds["OTHER"] += 1
            await cur.execute(stmt)
        await conn.commit()
        return kinds
    finally:
        await cur.close()
        conn.close()


async def _verify() -> Dict[str, int]:
    """返回两张表的行数与一次跨模型 ratio 的真实计算结果。"""
    import aiomysql

    dsn = _require_dsn()
    conn = await aiomysql.connect(**dsn)
    try:
        cur = await conn.cursor()
        summary = {}
        for t in ("dws_trade_day", "dws_user_day"):
            await cur.execute(f"SELECT COUNT(*), MIN(stat_date), MAX(stat_date) FROM {t}")
            cnt, mn, mx = await cur.fetchone()
            summary[t] = {"rows": cnt, "min_date": str(mn), "max_date": str(mx)}
        # 跨模型 ratio 实算：付费转化率 = 付费用户数 / 日活跃用户数 (按天全量)
        await cur.execute(
            """
            SELECT
              u.stat_date, u.channel_name,
              COALESCE(p.paid, 0)   AS paid,
              u.active              AS active,
              ROUND(COALESCE(p.paid,0)*100.0 / u.active, 2) AS rate_pct
            FROM (
              SELECT stat_date, channel_name, SUM(active_user_cnt) AS active
              FROM dws_user_day GROUP BY stat_date, channel_name
            ) u
            LEFT JOIN (
              SELECT stat_date, channel_name, SUM(paid_user_cnt) AS paid
              FROM dws_trade_day GROUP BY stat_date, channel_name
            ) p USING (stat_date, channel_name)
            WHERE u.active > 0
            ORDER BY u.stat_date DESC, u.channel_name
            LIMIT 5
            """
        )
        rows = await cur.fetchall()
        summary["ratio_sample"] = [
            {"date": str(r[0]), "channel": r[1],
             "paid": int(r[2]), "active": int(r[3]), "rate_pct": float(r[4])}
            for r in rows
        ]
        return summary
    finally:
        conn.close()


# ---------------------------------------------------------------- 入口
def _build_full_sql(start: date, end: date, seed: int) -> str:
    trade, users = _starrocks_seed.build_rows(start, end, seed=seed)
    # 直接拼 MySQL DDL + 清表 + INSERT（复用 build_sql，但 SCHEMA_FILE 替换）
    parts = [
        SCHEMA_FILE.read_text(encoding="utf-8").rstrip(),
        "",
        "-- ---------------------------------------------------------------",
        "-- 幂等灌数：先清空再写入（MySQL 主键表 REPLACE INTO 也可）",
        "-- ---------------------------------------------------------------",
        "TRUNCATE TABLE dws_trade_day;",
        "TRUNCATE TABLE dws_user_day;",
        "",
    ]
    batch = 200
    for table, columns, rows in (
        ("dws_trade_day", _starrocks_seed.TRADE_COLUMNS, trade),
        ("dws_user_day", _starrocks_seed.USER_COLUMNS, users),
    ):
        for offset in range(0, len(rows), batch):
            chunk = rows[offset: offset + batch]
            values = ",\n  ".join(
                "(" + ", ".join(_starrocks_seed._literal(v) for v in row) + ")"
                for row in chunk
            )
            parts.append(f"INSERT INTO {table} {columns} VALUES\n  {values};")
            parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    today = DEFAULT_END
    p.add_argument(
        "--end", type=str, default=today.isoformat(),
        help=f"结束日期（含），默认 {today.isoformat()}",
    )
    p.add_argument(
        "--past-days", type=int, default=DEFAULT_DAYS_PAST,
        help=f"近 N 天，默认 {DEFAULT_DAYS_PAST}",
    )
    p.add_argument(
        "--last-year-days", type=int, default=DEFAULT_DAYS_LAST_YEAR,
        help=f"去年同期 M 天（用于同比），默认 {DEFAULT_DAYS_LAST_YEAR}",
    )
    p.add_argument("--seed", type=int, default=_starrocks_seed.DEFAULT_SEED)
    p.add_argument("--emit-sql", type=str, default=None,
                   help="只生成 SQL 到指定文件，不连库")
    p.add_argument("--execute", action="store_true", help="建表 + 灌数")
    p.add_argument("--verify", action="store_true", help="校验数据 + ratio 抽样")
    args = p.parse_args(argv)

    end = date.fromisoformat(args.end)
    start = end - timedelta(days=args.past_days - 1)
    ly_end = end.replace(year=end.year - 1)
    ly_start = ly_end - timedelta(days=args.last_year_days - 1)
    # 用 build_rows 两次：本期 + 去年同期
    trade1, users1 = _starrocks_seed.build_rows(start, end, seed=args.seed)
    trade2, users2 = _starrocks_seed.build_rows(ly_start, ly_end, seed=args.seed + 1)

    print(f"[plan] 本期 {start} ~ {end}  去年 {ly_start} ~ {ly_end}")

    sql = _build_full_sql(start, end, args.seed)
    # 追加去年同期的插入（拼接 build_sql 后的 INSERT，DDL/TRUNCATE 不重复）
    extra = []
    batch = 200
    for table, columns, rows in (
        ("dws_trade_day", _starrocks_seed.TRADE_COLUMNS, trade2),
        ("dws_user_day", _starrocks_seed.USER_COLUMNS, users2),
    ):
        for offset in range(0, len(rows), batch):
            chunk = rows[offset: offset + batch]
            values = ",\n  ".join(
                "(" + ", ".join(_starrocks_seed._literal(v) for v in row) + ")"
                for row in chunk
            )
            extra.append(f"INSERT INTO {table} {columns} VALUES\n  {values};")
            extra.append("")
    sql = sql + "\n-- 去年同期同写\n" + "\n".join(extra)

    if args.emit_sql:
        Path(args.emit_sql).write_text(sql, encoding="utf-8")
        print(f"[emit] SQL 已写入 {args.emit_sql} (bytes={len(sql)})")
        return 0

    if args.execute:
        from dmf_agent.config.settings import Settings  # noqa: WPS433
        sr = Settings().starrocks
        print(f"[dsn] {sr.host}:{sr.port} db={sr.database} user={sr.user}")
        kinds = asyncio.run(_run_sql(sql))
        print(f"[done] {kinds}")
        return 0

    if args.verify:
        summary = asyncio.run(_verify())
        for k, v in summary.items():
            print(f"  {k}: {v}")
        return 0

    p.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
