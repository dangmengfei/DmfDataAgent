# DmfDataAgent 后端

指标问答 Agent 后端（FastAPI + LangGraph + LangChain + MySQL + RAG + OpenTelemetry 观测）。
前端见 `../frontend/`（前后端分离，通过 `/api/v1` HTTP 接口交互）。

## 快速开始

```bash
# 依赖（dev venv 位于项目根 ../.venv）
../.venv/bin/pip install -e ".[rag,dev]"

# 启动
../.venv/bin/uvicorn dmf_agent.api.app:app --host 0.0.0.0 --port 8000

# 测试
../.venv/bin/python -m pytest

# 评测
../.venv/bin/python -m dmf_agent.eval
```

## 目录

- `dmf_agent/`  后端包（api / agents / semantic / integrations / analysis / rag / recall / time / db / memory / observability / experience / eval / diagnosis）
- `tests/`          pytest 用例
- `migrations/`     alembic 迁移（`alembic upgrade head`）
- `doc/`            变更记录等见 `../doc/`

## 观测（OpenTelemetry）

观测遵循 OTel 语义（TraceId / SpanId / 父子 span / SpanKind / StatusCode / events），**本地落库**、
不依赖任何外部观测平台：

- `trace_records`：轮级汇总（每轮一条，`trace` JSON 内含 `otel` 元信息）；
- `trace_spans`：span 明细（root `chat_round` + 各节点 `node.<name>`，含真实起止时间）；
- 接口：`/api/v1/obs/{summary,traces,rounds,spans,sessions/{id}}`，导出 `/api/v1/obs/export`（OTLP/JSON，能力保留）；
- 关闭 span 采集：`DMF_OBSERVABILITY-ENABLED=false`（不影响对话主链路）。

## 配置

环境变量前缀 `DMF_`（如 `DMF_DB-HOST`），完整键见 `dmf_agent/config/settings.py` 与 `.env.example`。
未配置组件自动降级（mock / 内存 / 离线），零依赖可跑通全链路。
