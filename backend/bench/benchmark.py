"""DmfDataAgent 性能压测台（P2-3）。

目标：在缺乏真实压测脚本/基线的前提下，提供一个可复用、可复现的压测入口，
产出「吞吐（QPS）/ P50·P95·P99 延迟 / 首事件延迟」基线，并对照 PRD 第 10 节
NFR 目标（200 QPS / P95 端到端 ≤8s / 首字 ≤1.5s）给出达标性分析。

设计要点：
- 进程内装配真实 app runtime（create_app + lifespan），复用与线上完全一致的
  路由 / 中间件 / 图 / Harness / 安全层，只替换「外部依赖」：
  * LLM：默认 mock（规则意图 + 模板答复），隔离真实推理模型耗时（实测单轮 ~18.8s）。
  * StarRocks：默认注入内存 Stub Executor，隔离取数网络 + datasource 重试退避
    （~0.6s/次），让压测测的是「框架 + 图编排 + 编译 + 安全/Harness」的真实开销。
- 真实链路验证：提供 --real-llm / --real-db 关闭对应隔离，用于接入真实环境跑分
  （需配置 LLM / StarRocks 连接串）。

用法：
  # 框架天花板（mock LLM + stub DB），8 并发 60 请求
  python bench/benchmark.py --requests 60 --concurrency 8

  # 首事件延迟（SSE 流式视角）
  python bench/benchmark.py --requests 30 --concurrency 4 --stream

  # 真实环境跑分（需 .env 配置 LLM/StarRocks）
  python bench/benchmark.py --requests 10 --concurrency 2 --real-llm --real-db

报告：控制台表格 + bench/results/bench_<ts>.json（便于趋势对比）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---- 依赖隔离：默认强制 mock LLM，保证基线确定、不触达外部推理服务 ----
# （仅在变量未显式设置时注入；--real-llm 时不注入，沿用 .env 真实配置）
if not os.environ.get("DMF_LLM-MODEL") and not os.environ.get("DMF_LLM-BASE_URL"):
    os.environ.setdefault("DMF_LLM-MODEL", "")
    os.environ.setdefault("DMF_LLM-BASE_URL", "")
    os.environ.setdefault("DMF_LLM-API_KEY", "")

# 复用测试环境的存储/鉴权默认（内存库、鉴权关闭 -> 通配权限），
# 不依赖外部 MySQL / 鉴权服务，便于本地基线复现。
os.environ.setdefault("DMF_DB-ENABLED", "false")
os.environ.setdefault("DMF_AUTH-ENABLED", "false")
os.environ.setdefault("DMF_HM-API_BASE", "")
os.environ.setdefault("DMF_RAG-BACKEND", "faiss")
os.environ.setdefault("DMF_STARROCKS-HOST", "127.0.0.1")
os.environ.setdefault("DMF_STARROCKS-PORT", "1")


# --------------------------------------------------------------------------- #
# Stub Executor：内存取数，隔离 StarRocks 网络 + datasource 重试退避
# --------------------------------------------------------------------------- #
class _StubSettings:
    def __init__(self) -> None:
        self.max_rows = 1000
        self.dim_value_recall_limit = 20
        self.query_timeout = 30


class StubExecutor:
    """替代 StarRocksClient，fetch 直接返回确定性内存结果（不触网）。"""

    def __init__(self, columns: Optional[List[str]] = None, rows: int = 3) -> None:
        self.settings = _StubSettings()
        self._columns = columns or ["统计日期", "机构", "指标值"]
        self._rows = rows

    async def fetch(self, sql: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        sample_rows = [
            ["2026-09-01", f"机构{i % 5}", 1000 * (i + 1)] for i in range(self._rows)
        ]
        return {
            "columns": self._columns,
            "rows": sample_rows,
            "row_count": len(sample_rows),
            "elapsed_ms": 1,
            "truncated": False,
        }

    async def ping(self) -> bool:
        return True


def install_stub_executor(runtime: Any) -> bool:
    """把 runtime 的 StarRocks executor 替换为 Stub；成功返回 True。"""
    engine = getattr(runtime, "metric_engine", None)
    if engine is None or not hasattr(engine, "executor"):
        return False
    engine.executor = StubExecutor()
    return True


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #
def _pct(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def compute_stats(records: List[Dict[str, float]], wall_s: float = 0.0) -> Dict[str, Any]:
    """records: 每条含 latency / first_event（秒）。返回聚合统计。"""
    lat = [r["latency"] for r in records]
    fe = [r["first_event"] for r in records if r.get("first_event") is not None]
    ok = sum(1 for r in records if r.get("ok"))
    qps = round(len(records) / wall_s, 2) if wall_s > 0 else 0.0
    return {
        "count": len(records),
        "ok": ok,
        "error": len(records) - ok,
        "wall_s": round(wall_s, 4),
        "qps": qps,
        "latency_mean": round(statistics.fmean(lat), 4) if lat else 0.0,
        "latency_min": round(min(lat), 4) if lat else 0.0,
        "latency_max": round(max(lat), 4) if lat else 0.0,
        "latency_p50": round(_pct(lat, 50), 4),
        "latency_p95": round(_pct(lat, 95), 4),
        "latency_p99": round(_pct(lat, 99), 4),
        "first_event_p50": round(_pct(fe, 50), 4) if fe else None,
        "first_event_p95": round(_pct(fe, 95), 4) if fe else None,
    }


# --------------------------------------------------------------------------- #
# 压测核心
# --------------------------------------------------------------------------- #
SCENARIOS = [
    "各分行零售存款余额排名",
    "零售贷款余额的月度趋势",
    "按客群看存款利息支出构成",
    "不良贷款率环比上升的原因",
    "月日均存款余额是多少",
    "存贷比最近一个季度变化",
]


@dataclass
class BenchConfig:
    requests: int = 60
    concurrency: int = 8
    stream: bool = False
    stub_db: bool = True
    real_llm: bool = False
    warmup: int = 5


async def _one_request(runtime: Any, message: str, use_stream: bool) -> Dict[str, float]:
    session_id = f"bench-{time.time_ns()}-{id(message)}"
    t0 = time.perf_counter()
    first_event: Optional[float] = None
    ok = False
    try:
        if use_stream:
            async for ev in runtime.chat_service.astream_chat(
                session_id=session_id, user_id="bench", message=message,
                user_role="viewer", user_permissions=["*"],
            ):
                if first_event is None and ev.get("type") in ("session", "node", "token"):
                    first_event = time.perf_counter() - t0
                if ev.get("type") in ("final", "error"):
                    ok = ev.get("type") == "final"
                    break
        else:
            res = await runtime.chat_service.chat(
                session_id=session_id, user_id="bench", message=message,
                user_role="viewer", user_permissions=["*"],
            )
            ok = bool(res and res.get("content"))
            first_event = time.perf_counter() - t0  # 阻塞式无首事件概念，取完成时刻
    except Exception:
        ok = False
    t1 = time.perf_counter()
    return {"latency": t1 - t0, "first_event": first_event, "ok": ok}


async def run_benchmark(runtime: Any, cfg: BenchConfig) -> tuple[List[Dict[str, float]], float]:
    messages = [SCENARIOS[i % len(SCENARIOS)] for i in range(cfg.requests)]
    sem = asyncio.Semaphore(cfg.concurrency)

    async def worker(idx: int) -> Dict[str, float]:
        async with sem:
            return await _one_request(runtime, messages[idx], cfg.stream)

    # 预热（不计入统计）
    if cfg.warmup > 0:
        warm = [messages[i % len(messages)] for i in range(cfg.warmup)]
        await asyncio.gather(*[
            _one_request(runtime, m, cfg.stream) for m in warm
        ])
    t_start = time.perf_counter()
    records = list(await asyncio.gather(*[worker(i) for i in range(cfg.requests)]))
    wall_s = time.perf_counter() - t_start
    return records, wall_s


# --------------------------------------------------------------------------- #
# PRD 目标达标性分析
# --------------------------------------------------------------------------- #
PRD_TARGETS = {
    "concurrency_qps": 200,
    "p95_latency_s": 8.0,
    "first_token_s": 1.5,
}


def analyze(stats: Dict[str, Any], cfg: BenchConfig) -> Dict[str, Any]:
    # 实测吞吐：请求数 / 被测总墙钟时长（已剔除预热）
    measured_qps = stats.get("qps", 0.0)
    return {
        "measured_p95_s": stats["latency_p95"],
        "measured_first_event_p95_s": stats.get("first_event_p95"),
        "measured_qps": measured_qps,
        "prd_qps_target": PRD_TARGETS["concurrency_qps"],
        "prd_p95_target_s": PRD_TARGETS["p95_latency_s"],
        "prd_first_token_target_s": PRD_TARGETS["first_token_s"],
        "p95_pass": stats["latency_p95"] <= PRD_TARGETS["p95_latency_s"],
        "first_token_pass": (stats.get("first_event_p95") is not None
                             and stats["first_event_p95"] <= PRD_TARGETS["first_token_s"]),
        "note": (
            "本基线隔离了真实 LLM 与 StarRocks 取数，反映「框架+图编排+编译+安全/Harness」"
            "开销。真实 LLM（推理模型）单轮约 18.8s，是端到端延迟主导项；达标 200 QPS 需"
            "并发≈3765（200×18.8），单模型不可行，建议：① 换非推理小模型（首字/延迟量级下降"
            "1~2 个数量级）；② 开启查询缓存（P2-1）命中重复查询直接返回；③ LLM 网关多模型/"
            "限流与异步流水线。"
        ),
    }


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def _build_runtime():
    from dmf_agent.api.app import create_app

    app = create_app()
    return app


async def amain(cfg: BenchConfig) -> Dict[str, Any]:
    app = _build_runtime()
    # 进入 lifespan 装配 runtime（与线上一致）
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        stub_installed = False
        if cfg.stub_db:
            stub_installed = install_stub_executor(runtime)
        records, wall_s = await run_benchmark(runtime, cfg)
    stats = compute_stats(records, wall_s=wall_s)
    analysis = analyze(stats, cfg)
    report = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "config": asdict(cfg),
        "stub_db_installed": stub_installed,
        "stats": stats,
        "analysis": analysis,
    }
    return report


def _print_report(report: Dict[str, Any]) -> None:
    cfg = report["config"]
    s = report["stats"]
    a = report["analysis"]
    print("=" * 64)
    print(f"性能压测基线  ({report['timestamp']})")
    print(f"  模式: requests={cfg['requests']} concurrency={cfg['concurrency']} "
          f"stream={cfg['stream']} stub_db={report['stub_db_installed']}")
    print("-" * 64)
    print(f"  吞吐(实测 QPS)      : {s['qps']} QPS（PRD 目标 200 QPS）")
    print(f"  延迟 mean/min/max   : {s['latency_mean']}/{s['latency_min']}/{s['latency_max']} s")
    print(f"  延迟 P50/P95/P99    : {s['latency_p50']}/{s['latency_p95']}/{s['latency_p99']} s")
    if s.get("first_event_p95") is not None:
        print(f"  首事件 P50/P95      : {s['first_event_p50']}/{s['first_event_p95']} s")
    print(f"  成功/失败           : {s['ok']}/{s['error']}")
    print("-" * 64)
    print(f"  PRD P95≤8s          : {'PASS' if a['p95_pass'] else 'FAIL'} "
          f"(实测 {a['measured_p95_s']}s)")
    if a['measured_first_event_p95_s'] is not None:
        print(f"  PRD 首字≤1.5s       : {'PASS' if a['first_token_pass'] else 'FAIL'} "
              f"(实测 {a['measured_first_event_p95_s']}s)")
    print(f"  分析: {a['note']}")
    print("=" * 64)


def main() -> None:
    ap = argparse.ArgumentParser(description="DmfDataAgent 性能压测台")
    ap.add_argument("--requests", type=int, default=60)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--stream", action="store_true", help="按 SSE 流式视角测首事件延迟")
    ap.add_argument("--real-db", action="store_true", help="不注入 Stub Executor（需真实 StarRocks）")
    ap.add_argument("--real-llm", action="store_true", help="不强制 mock LLM（需真实 LLM 配置）")
    ap.add_argument("--warmup", type=int, default=5)
    ap.add_argument("--out-dir", type=str, default=None, help="JSON 报告输出目录")
    args = ap.parse_args()

    cfg = BenchConfig(
        requests=args.requests,
        concurrency=args.concurrency,
        stream=args.stream,
        stub_db=not args.real_db,
        real_llm=args.real_llm,
        warmup=args.warmup,
    )
    if args.real_llm:
        # 撤销默认 mock 注入，沿用 .env
        for k in ("DMF_LLM-MODEL", "DMF_LLM-BASE_URL", "DMF_LLM-API_KEY"):
            os.environ.pop(k, None)

    report = asyncio.run(amain(cfg))
    _print_report(report)

    out_dir = Path(args.out_dir) if args.out_dir else Path(__file__).parent / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = out_dir / f"bench_{stamp}.json"
    out_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已写入：{out_file}")


if __name__ == "__main__":
    main()
