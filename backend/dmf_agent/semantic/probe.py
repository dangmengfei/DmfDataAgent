"""数据源连通性深度探针（PRD FR7）。

在 ``executor.ping()`` 的"心跳"之上补三件事：
- **latency_ms**：一次轻量 ``SELECT 1`` 的往返延迟（区别于取数耗时）；
- **version**：数据源版本与当前库（``SELECT VERSION()/DATABASE()``）；
- **表存量**：对语义层每个模型对应的物理表，探 ``COUNT(*)`` 与时间维度最新值，
  用于发现"数据源在线、但某张底表空了 / 数据陈旧"这种心跳发现不了的故障。

设计原则：
- **逐项容错**：任一表探测失败只在该项标注 error，绝不阻断整体探测；
- **总超时兜底**：调用方可给 ``timeout``，整体包一层 ``asyncio.wait_for``，
  避免探针被慢连接拖住（``/health`` 类端点不能被拖死）；
- **只读**：全部为 SELECT，经执行器既有的只读守卫与沙箱；
- **不抛错**：任何异常都收敛为结构化结果，便于直接 JSON 返回 / 前端徽标消费。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 单张表存量探测的语句超时（秒）；COUNT(*) 在大表上可能较慢，单独受限。
DEFAULT_TABLE_TIMEOUT_S = 5
# 整个探针的总超时（秒），防止探测端点被慢连接拖住。
DEFAULT_TOTAL_TIMEOUT_S = 15
# 数据"陈旧"判定阈值（天）：data_as_of 早于当前时间超过该值即标 stale。
DEFAULT_STALE_DAYS = 3


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


async def _safe_fetch(
    executor: Any, sql: str, timeout: float
) -> Dict[str, Any]:
    """执行单条探查 SQL，带独立超时；返回 {ok, rows, error}。"""
    try:
        res = await asyncio.wait_for(executor.fetch(sql), timeout)
        return {"ok": True, "rows": res.get("rows") or [], "error": None}
    except asyncio.TimeoutError:
        return {"ok": False, "rows": [], "error": f"超时（>{timeout}s）"}
    except Exception as exc:  # noqa: BLE001 - 探查收敛错误，不抛出
        return {"ok": False, "rows": [], "error": str(exc)[:200]}


async def _probe_one_table(
    executor: Any, model: Any, timeout: float
) -> Dict[str, Any]:
    """探一张物理表：存量 COUNT(*) + 时间维度最新值（若有）。"""
    item: Dict[str, Any] = {
        "model": model.name,
        "table": model.table_ref,
        "row_count": None,
        "latest_at": None,
        "ok": False,
        "error": None,
    }
    count_res = await _safe_fetch(
        executor, f"SELECT COUNT(*) FROM {model.table_ref}", timeout
    )
    if not count_res["ok"]:
        item["error"] = count_res["error"]
        return item
    rows = count_res["rows"]
    if rows and rows[0] is not None:
        try:
            item["row_count"] = int(rows[0][0])
        except (TypeError, ValueError, IndexError):
            item["row_count"] = None

    td = model.time_dimension()
    if td is not None:
        latest_res = await _safe_fetch(
            executor,
            f"SELECT MAX({td.expr}) FROM {model.table_ref}",
            timeout,
        )
        if latest_res["ok"]:
            lrows = latest_res["rows"]
            if lrows and lrows[0] and lrows[0][0] is not None:
                item["latest_at"] = str(lrows[0][0])
        # latest 失败不改变表的整体 ok（存量已探到）

    item["ok"] = True
    return item


def _is_stale(latest_at: Optional[str], stale_days: int) -> Optional[bool]:
    """根据最新时间与当前时间粗略判定是否陈旧；无法解析时返回 None。"""
    if not latest_at:
        return None
    raw = latest_at.replace("Z", "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            ts = datetime.strptime(raw, fmt)
            return (datetime.now() - ts).days > stale_days
        except ValueError:
            continue
    # 退一步：ISO 解析
    try:
        ts = datetime.fromisoformat(raw)
        return (datetime.now() - ts).days > stale_days
    except ValueError:
        return None


async def probe_datasource(
    engine: Any,
    *,
    table_timeout: float = DEFAULT_TABLE_TIMEOUT_S,
    total_timeout: float = DEFAULT_TOTAL_TIMEOUT_S,
    stale_days: int = DEFAULT_STALE_DAYS,
) -> Dict[str, Any]:
    """对数据源做一次深度探针，返回结构化结果（不抛错）。

    返回字段：
    - reachable/mode/detail/elapsed_ms：与旧 ``ping()`` 结果对齐（向后兼容）；
    - latency_ms：SELECT 1 往返延迟；version/db：数据源版本与当前库；
    - data_as_of：所有模型时间维度最新值中的最新者；
    - tables：每张物理表 {model,table,row_count,latest_at,ok,error,stale}；
    - summary：{total, reachable_tables, empty_tables, stale_tables, failed_tables}；
    - checked_at：探测时刻；stale：整体是否存在陈旧/空表/失败表。
    """
    started = time.perf_counter()

    async def _run() -> Dict[str, Any]:
        executor = engine.executor
        base = await executor.ping()
        result: Dict[str, Any] = {
            "reachable": bool(base.get("reachable")),
            "mode": base.get("mode", "unknown"),
            "detail": base.get("detail", ""),
            "elapsed_ms": base.get("elapsed_ms"),
            "latency_ms": None,
            "version": None,
            "db": None,
            "data_as_of": None,
            "tables": [],
            "summary": {},
            "checked_at": _now_iso(),
            "stale": False,
        }
        if not result["reachable"]:
            result["summary"] = {
                "total": 0,
                "reachable_tables": 0,
                "empty_tables": 0,
                "stale_tables": 0,
                "failed_tables": 0,
            }
            return result

        # 延迟 + 版本（一次轻量查询）
        t0 = time.perf_counter()
        meta = await _safe_fetch(
            executor,
            "SELECT 1 AS ok, VERSION() AS version, DATABASE() AS db",
            table_timeout,
        )
        result["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        if meta["ok"] and meta["rows"]:
            mrow = meta["rows"][0]
            try:
                result["version"] = str(mrow[1]) if len(mrow) > 1 else None
                result["db"] = str(mrow[2]) if len(mrow) > 2 else None
            except (TypeError, IndexError):
                pass

        # 各物理表存量
        models = list(
            getattr(getattr(engine.layer, "manifest", None), "models", [])
        )
        latest_values: List[str] = []
        for model in models:
            item = await _probe_one_table(executor, model, table_timeout)
            item["stale"] = _is_stale(item.get("latest_at"), stale_days)
            if item.get("latest_at"):
                latest_values.append(item["latest_at"])
            result["tables"].append(item)

        result["data_as_of"] = max(latest_values) if latest_values else None

        total = len(result["tables"])
        empty = sum(
            1 for t in result["tables"] if t["ok"] and t["row_count"] == 0
        )
        failed = sum(1 for t in result["tables"] if not t["ok"])
        stale = sum(1 for t in result["tables"] if t.get("stale"))
        result["summary"] = {
            "total": total,
            "reachable_tables": total - failed,
            "empty_tables": empty,
            "stale_tables": stale,
            "failed_tables": failed,
        }
        result["stale"] = bool(empty or failed or stale)
        return result

    try:
        out = await asyncio.wait_for(_run(), total_timeout)
    except asyncio.TimeoutError:
        logger.warning("数据源探针整体超时（>%ss）", total_timeout)
        out = {
            "reachable": False,
            "mode": "unknown",
            "detail": f"探针整体超时（>{total_timeout}s）",
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "latency_ms": None,
            "version": None,
            "db": None,
            "data_as_of": None,
            "tables": [],
            "summary": {
                "total": 0,
                "reachable_tables": 0,
                "empty_tables": 0,
                "stale_tables": 0,
                "failed_tables": 0,
            },
            "checked_at": _now_iso(),
            "stale": True,
        }
    except Exception as exc:  # noqa: BLE001 - 探针绝不抛出
        logger.warning("数据源探针异常：%s", exc)
        out = {
            "reachable": False,
            "mode": "unknown",
            "detail": f"探针异常：{exc}"[:200],
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "latency_ms": None,
            "version": None,
            "db": None,
            "data_as_of": None,
            "tables": [],
            "summary": {
                "total": 0,
                "reachable_tables": 0,
                "empty_tables": 0,
                "stale_tables": 0,
                "failed_tables": 0,
            },
            "checked_at": _now_iso(),
            "stale": True,
        }
    return out


def _describe(status: Dict[str, Any]) -> str:
    """把一次探针结果浓缩成一行可读描述，供日志使用。"""
    summ = status.get("summary") or {}
    return (
        f"{status.get('mode')} {status.get('detail')} | latency={status.get('latency_ms')}ms"
        f" | 底表 可探{summ.get('reachable_tables', 0)}/{summ.get('total', 0)}"
        f" 空{summ.get('empty_tables', 0)} 陈旧{summ.get('stale_tables', 0)}"
        f" 失败{summ.get('failed_tables', 0)} | data_as_of={status.get('data_as_of')}"
    )


async def datasource_watchdog(runtime: Any, interval: Optional[int] = None) -> None:
    """后台周期看门狗：周期性深度探针，状态翻转即告警/恢复。

    与 ``embedding_watchdog`` 同一范式：
    - 每轮调 ``runtime.metric_engine.probe()``，结果持续写入
      ``runtime.datasource_status``，供 ``/semantic/health`` 与前端徽标消费；
    - 仅在**状态翻转**时打日志（可达 <-> 不可达；健康 <-> 空表/陈旧/失败），
      避免周期刷屏；首轮（启动）做一次基线播报；
    - 探针本身绝不抛错（见 ``probe_datasource``），看门狗只在被取消时退出。

    ``interval`` 缺省取 ``settings.effective_starrocks.probe_interval``；
    显式传 ``<=0`` 则不启动（保留启动自检 + 按需探测）。
    """
    settings = getattr(runtime, "settings", None)
    if interval is None:
        interval = (
            int(settings.effective_starrocks.probe_interval) if settings is not None else 60
        )
    if interval is not None and interval <= 0:
        logger.info("数据源看门狗已关闭（probe_interval<=0），仅启动自检 + 按需探测")
        return

    last_reachable: Optional[bool] = None
    last_stale: Optional[bool] = None
    try:
        while True:
            engine = getattr(runtime, "metric_engine", None)
            if engine is None:
                status = {
                    "reachable": False,
                    "mode": "unknown",
                    "detail": "runtime 无 metric_engine",
                    "stale": True,
                    "summary": {},
                }
            else:
                status = await engine.probe()
            runtime.datasource_status = status
            reachable = bool(status.get("reachable"))
            stale = bool(status.get("stale"))

            if last_reachable is None:
                # 首轮基线播报（与启动自检互补：这里是周期任务视角的第一条）
                if reachable and not stale:
                    logger.info("数据源看门狗基线正常：%s", _describe(status))
                elif reachable:
                    logger.warning("⚠️ 数据源看门狗基线：在线但有 空表/陈旧/失败：%s",
                                   _describe(status))
                else:
                    logger.error("🚨 数据源看门狗基线：数据源不可达，取数将报错：%s",
                                 _describe(status))
            else:
                if reachable != last_reachable:
                    if reachable:
                        logger.warning("✅ 数据源已恢复连通：%s", _describe(status))
                    else:
                        logger.error("🚨 告警：数据源不可达，取数将报错：%s",
                                     _describe(status))
                elif reachable and stale != last_stale:
                    if stale:
                        logger.warning(
                            "⚠️ 数据源在线但出现 空表/陈旧/失败表：%s", _describe(status)
                        )
                    else:
                        logger.info("✅ 数据源底表已全部恢复正常：%s", _describe(status))

            last_reachable = reachable
            last_stale = stale
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("数据源看门狗已停止")
