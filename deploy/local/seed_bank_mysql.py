#!/usr/bin/env python3
"""银行零售（存款 / 贷款）底表造数 —— 本地 MySQL 版。

与 `deploy/starrocks/seed_bank.py` 的差别只有一处：DDL 用
`deploy/local/bank_schema_mysql.sql`（InnoDB + 复合主键 + 索引），
而不是 StarRocks 的 ENGINE=OLAP / AGGREGATE KEY。

**数据生成逻辑完全复用** seed_bank.build_rows —— 同 seed 同输出，
因此本地 MySQL 与线上 StarRocks 跑出来的是同一份明细数据，回归可比对。

用法（在仓库根，使用 backend 的虚拟环境；cwd 需在 backend 下才能读到 .env）：

    backend/.venv/bin/python deploy/starrocks/seed_bank.py --execute --schema mysql   # 等价
    cd backend && ./.venv/bin/python ../deploy/local/seed_bank_mysql.py --execute     # 建表 + 灌数
    cd backend && ./.venv/bin/python ../deploy/local/seed_bank_mysql.py --verify      # 仅校验
    cd backend && ./.venv/bin/python ../deploy/local/seed_bank_mysql.py --emit-sql /tmp/bank_seed_mysql.sql
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY_STARROCKS = _REPO_ROOT / "deploy" / "starrocks"
if str(_DEPLOY_STARROCKS) not in sys.path:
    sys.path.insert(0, str(_DEPLOY_STARROCKS))

import seed_bank  # type: ignore[import-not-found]  # noqa: E402


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--schema" not in args:
        args += ["--schema", "mysql"]
        print("[local] DDL 变体: mysql（deploy/local/bank_schema_mysql.sql）")
    return seed_bank.main(args)


if __name__ == "__main__":
    raise SystemExit(main())
