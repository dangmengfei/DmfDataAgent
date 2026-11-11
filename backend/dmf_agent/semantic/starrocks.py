"""StarRocks 执行器（FE 的 MySQL 协议直连）—— 主流程唯一取数出口。

职责单一：拿到 **已编译好的只读 SQL**，执行并返回规范化结果集。
不做语义解析、不做 SQL 生成（见 compiler.py），也不做任何 mock 降级。

安全：
- 只读守卫 `assert_read_only` 在执行前拒绝一切非查询语句（语义清单可能来自
  外部导入，过滤值也可能掺入用户输入，必须在出口处兜底）；
- 行数上限 `max_rows` 防大结果集打爆内存，超出即截断并标注 `truncated`。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import date, datetime, time as dt_time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from dmf_agent.config.settings import StarRocksSettings
from dmf_agent.harness.sandbox import get_default_sandbox

logger = logging.getLogger(__name__)

# 允许的语句起始关键字（只读）
_READ_ONLY_PREFIXES = ("select", "with", "show", "desc", "describe", "explain")

# 非只读关键字（剥离注释与字符串字面量后再匹配）
_FORBIDDEN_KEYWORDS = re.compile(
    r"(?i)\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|load|"
    r"replace|merge|call|set|export|install|uninstall|use|kill)\b"
)


class StarRocksError(RuntimeError):
    """StarRocks 执行失败（未配置 / 连接失败 / SQL 被拒 / 查询报错）。"""


def _strip_sql_noise(sql: str) -> str:
    """剥离注释与字符串字面量，得到只含结构关键字的骨架（用于只读判定）。"""
    out: List[str] = []
    i = 0
    length = len(sql)
    while i < length:
        char = sql[i]
        if char == "'" or char == '"':
            quote = char
            i += 1
            while i < length:
                if sql[i] == "\\":
                    i += 2
                    continue
                if sql[i] == quote:
                    if i + 1 < length and sql[i + 1] == quote:  # 转义重复引号
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append(" ? ")
            continue
        if char == "-" and sql.startswith("--", i):
            nl = sql.find("\n", i)
            i = length if nl < 0 else nl + 1
            continue
        if char == "#":
            nl = sql.find("\n", i)
            i = length if nl < 0 else nl + 1
            continue
        if char == "/" and sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = length if end < 0 else end + 2
            continue
        out.append(char)
        i += 1
    return "".join(out)


def assert_read_only(sql: str) -> None:
    """只读守卫：非查询语句直接拒绝。"""
    skeleton = _strip_sql_noise(sql).strip()
    if not skeleton:
        raise StarRocksError("SQL 为空")
    if not skeleton.lower().startswith(_READ_ONLY_PREFIXES):
        raise StarRocksError(
            f"仅允许执行只读查询（SELECT/WITH/SHOW/DESC/EXPLAIN），已拒绝："
            f"{skeleton[:80]}…"
        )
    hit = _FORBIDDEN_KEYWORDS.search(skeleton)
    if hit:
        raise StarRocksError(
            f"SQL 含非只读关键字 [{hit.group(1)}]，已拒绝执行：{skeleton[:80]}…"
        )


def assert_sandbox_sql(sql: str) -> None:
    """FR15 安全沙箱：SQL 层危险模式兜底（数据外泄 / 破坏性 DDL）。

    在 `assert_read_only` 之后调用，专门兜住「SELECT 前缀但外泄/破坏」的缝隙，
    例如 `SELECT ... INTO OUTFILE '/tmp/x'`、`LOAD_FILE()`、`DROP TABLE` 等。
    沙箱关闭（DMF_HARNESS-SANDBOX_ENABLED=false）时直接放行。
    """
    sb = get_default_sandbox()
    if sb is None:
        return
    res = sb.check_sql(sql)
    if not res["ok"]:
        raise StarRocksError(f"SQL 命中安全沙箱规则（FR15）：{res['summary']}")


def _jsonable(value: Any) -> Any:
    """把 DB 驱动返回的类型转成可 JSON 序列化的值（Decimal/日期/字节）。"""
    if value is None or isinstance(value, (int, float, str, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date, dt_time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value)


class StarRocksClient:
    """异步连接池（aiomysql，复用 StarRocks 的 MySQL 协议）。"""

    def __init__(self, settings: StarRocksSettings) -> None:
        self._settings = settings
        self._pool: Optional[Any] = None
        self._lock = asyncio.Lock()

    @property
    def settings(self) -> StarRocksSettings:
        return self._settings

    @property
    def configured(self) -> bool:
        return self._settings.configured

    def _ensure_configured(self) -> None:
        if not self.configured:
            raise StarRocksError(
                "StarRocks 未配置（请设置 DMF_STARROCKS-HOST / PORT / USER / "
                "PASSWORD / DATABASE）"
            )

    async def pool(self) -> Any:
        """惰性创建连接池（首次取数时建立，避免启动期强依赖）。"""
        self._ensure_configured()
        if self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is not None:
                return self._pool
            try:
                import aiomysql  # 局部导入：未安装时不影响其它模块可用性
            except ImportError as exc:  # pragma: no cover - 依赖缺失
                raise StarRocksError("缺少 aiomysql 依赖，无法连接 StarRocks") from exc
            s = self._settings
            # MySQL 方言的时点取值用 GROUP_CONCAT 模拟（见 dialects.MySqlDialect.arg_max），
            # 默认 group_concat_max_len=1024 会被大分组截断并刷 Warning —— 建池时调大。
            init_command = (
                "SET SESSION group_concat_max_len = 4194304"
                if (s.dialect or "").lower() == "mysql"
                else None
            )
            try:
                self._pool = await aiomysql.create_pool(
                    host=s.host,
                    port=s.port,
                    user=s.user,
                    password=s.password,
                    db=s.database or None,
                    charset=s.charset or "utf8mb4",
                    connect_timeout=s.connect_timeout,
                    minsize=s.pool_min_size,
                    maxsize=s.pool_max_size,
                    autocommit=True,
                    init_command=init_command,
                )
            except Exception as exc:  # noqa: BLE001 - 统一转为领域异常
                raise StarRocksError(
                    f"连接 StarRocks 失败（{s.host}:{s.port}）：{exc}"
                ) from exc
            logger.info("StarRocks 连接池已建立：%s:%s/%s", s.host, s.port, s.database)
            return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None

    async def ping(self) -> Dict[str, Any]:
        """连通性探活（供 /health 与管理接口）。"""
        if not self.configured:
            return {"reachable": False, "mode": "disabled", "detail": "未配置"}
        started = time.perf_counter()
        try:
            await self.fetch("SELECT VERSION() AS version, DATABASE() AS db")
            return {
                "reachable": True,
                "mode": "starrocks",
                "detail": f"{self._settings.host}:{self._settings.port}",
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        except StarRocksError as exc:
            return {
                "reachable": False,
                "mode": "starrocks",
                "detail": str(exc),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            }

    async def fetch(self, sql: str, params: Optional[Tuple[Any, ...]] = None) -> Dict[str, Any]:
        """执行只读 SQL，返回 {columns, rows, row_count, truncated, elapsed_ms, sql}。

        两道保护，缺一不可（分别对应两个 settings）：
        - `max_rows`：用 fetchmany 限制拉回内存的行数，超出即截断并标注 `truncated`；
        - `query_timeout`：整体超时。没有它，一条慢查询会**一直占用连接**，而池上限
          只有 `pool_max_size`（默认 5）——5 条慢 SQL 就能把取数整体打挂，且没有任何
          自我保护。超时后连接状态不可信（服务端可能仍在跑），故关闭后归还。
        """
        assert_read_only(sql)
        assert_sandbox_sql(sql)
        pool = await self.pool()
        limit = max(1, int(self._settings.max_rows))
        timeout = max(1, int(getattr(self._settings, "query_timeout", 0) or 0))
        started = time.perf_counter()
        conn = await pool.acquire()
        try:
            async with conn.cursor() as cursor:
                await asyncio.wait_for(cursor.execute(sql, params), timeout)
                columns = [d[0] for d in (cursor.description or [])]
                rows = await asyncio.wait_for(cursor.fetchmany(limit), timeout)
                has_more = await asyncio.wait_for(cursor.fetchmany(1), timeout)
        except asyncio.TimeoutError as exc:
            # aiomysql 的 release() 对已关闭连接只做除名、不会放回空闲池，
            # 所以这里关掉再归还是安全的（不会把脏连接泄漏给下一个请求）。
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响抛出超时
                pass
            raise StarRocksError(
                f"StarRocks 查询超时（>{timeout}s），已终止并丢弃该连接：{sql[:200]}"
            ) from exc
        except StarRocksError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一转为领域异常
            raise StarRocksError(f"StarRocks 查询失败：{exc}\nSQL: {sql[:400]}") from exc
        finally:
            try:
                pool.release(conn)
            except Exception:  # noqa: BLE001 - 归还失败不应掩盖主异常
                logger.debug("归还 StarRocks 连接失败", exc_info=True)

        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        return {
            "columns": columns,
            "rows": [[_jsonable(v) for v in row] for row in rows],
            "row_count": len(rows),
            "truncated": bool(has_more),
            "elapsed_ms": elapsed_ms,
            "sql": sql,
        }
