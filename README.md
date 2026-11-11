# DmfDataAgent

指标问答 Agent 的重构版本，基于 **LLM + Harness** 架构，前后端分离：

- **后端 `backend/`**：FastAPI + LangGraph + LangChain + MySQL + RAG（BM25 + FAISS/Milvus 混合）+ OpenTelemetry 观测
- **前端 `frontend/`**：Vue3 + Vite + TypeScript SPA（参考 metriccopilot-mvp/frontend 样式），通过 `/api/v1` HTTP（REST + SSE）调用后端

## 技术栈

- **FastAPI**：Web 服务壳（REST / SSE / 生命周期管理）
- **LangGraph**：Agent 编排引擎（状态图即业务 Harness：意图路由、要素抽取、取数、分析、归因、答复）
- **LangChain**：LLM 统一抽象 / RAG
- **语义层 `semantic/`**：内置 MetricFlow 规范引擎——`manifest.d/`（多层目录：语义模型 + 指标 + 维度/物理列映射）
  → `compiler.py` 编译 StarRocks SQL → `starrocks.py` 直连取数（`/api/v1/semantic/*`）；
  主流程不依赖指标平台，SQL 生成引擎抽象为可替换 provider。
  **定义来源为混合方案**：YAML 基座 或 DB 发布快照（`DMF_SEMANTIC-SOURCE=yaml|db`），
  快照走 `draft→review→published` 审批、支持租户级 `overlay` 局部覆盖，审批通过**热更新**生效（`/api/v1/semantic/registry/*`）。
  **多租户请求级装配**：按登录租户解析「全局链 + 租户覆盖」并用链指纹缓存（`services/semantic_scope.py`），
  图执行期经 ContextVar 绑定 —— 节点读 `deps.semantic_layer` 即得本租户口径，并发请求互不串台
  （`semantic/scope.py`；`/semantic/registry/runtime` 的 `scope_cache` 可见各租户生效链）；
  全局基座另有**「拉」式自愈**（`DMF_SEMANTIC-SCOPE_BASE_PROBE_TTL_S`，默认 1s 节流探测全局链指纹），
  多实例免广播即可各自收敛。
  **治理 CRUD 已按租户隔离**：读走可见性（全局 ∪ 本租户，全局只读）、写走归属（非 admin 恒为本租户，
  越权 403）；引用校验取值域同样是「全局 ∪ 本租户」。
  **overlay 编辑面**：`/semantic/registry/base-manifest` 提供整条条目源（overlay 按 name 整条替换，
  不做字段级 merge），管理面板「租户覆盖」抽屉可勾选条目、改字段、看差异后发 overlay 草稿。
- **OpenTelemetry**：观测遵循 OTel 语义（TraceId/SpanId/父子 span/SpanKind/StatusCode），
  span 与轮级 trace **本地落库**（`trace_spans` / `trace_records`），前端专页展示汇总与明细；
  导出能力保留（`/api/v1/obs/export`，OTLP/JSON），**不接 agent-insight 等外部平台**
- **MySQL**：会话、消息、知识库、指标元数据、黑话词典持久化（SQLAlchemy 2.0 async + Alembic）
- **向量库**：Milvus 默认（HNSW/L2，pymilvus 内嵌库免 Docker，生产指向 server）/ FAISS 零依赖可选，与 BM25 做 RRF 混合检索

## 目录结构

```
DmfDataAgent/
├── backend/                    # 后端（见 backend/README.md）
│   ├── dmf_agent/              # 后端包
│   │   ├── api/                # FastAPI 路由（chat/sessions/knowledge/metrics/semantic/obs/health）
│   │   ├── agents/             # LangGraph 主图与节点（prompts/*.yaml + prompt_loader）
│   │   ├── semantic/           # 语义层（manifest.d/ + layer + compiler + starrocks + engine + registry + overlay + scope + runtime + CLI）
│   │   ├── integrations/       # 外部系统接入（metric_platform：指标平台离线元信息/历史取数，非主流程）
│   │   ├── analysis/           # 结果集统计与描述（与取数来源解耦）
│   │   ├── rag/                # BM25 + 向量混合检索（faiss/milvus）
│   │   ├── recall/             # 候选召回（指标/维度/过滤/黑话）
│   │   ├── diagnosis/          # 归因核心（主指标 + 维度归因）
│   │   ├── db/  memory/  observability/  experience/  eval/  time/
│   │   └── config/settings.py  # 统一配置（DMF_ 前缀 + __ 分层）
│   ├── tests/                  # pytest（零外部依赖；语义引擎用 tests/fakes.py 替身）
│   ├── migrations/             # alembic 迁移
│   └── pyproject.toml
├── deploy/starrocks/           # 示例指标底表 DDL + 确定性造数脚本（跑通全链路）
├── frontend/                   # 前端 SPA（见 frontend/README.md / nginx.conf.example）
├── doc/                        # 改造记录.md / 待办清单.md / PRD
├── docker-compose.yml          # MySQL（+ 可选 milvus / langfuse profile）
└── MIGRATION.md
```

## 快速开始（后端）

```bash
cd backend

# 1. 依赖（venv 在项目根 ../.venv）
../.venv/bin/pip install -e ".[rag,dev]"

# 2.（可选）启动 MySQL；不启动也能跑，自动降级内存模式
docker compose -f ../docker-compose.yml up -d mysql

# 3. 配置环境变量
cp .env.example .env   # 按需填 LLM Key；不填则以 mock 模式跑通全链路

# 4. 启动
../.venv/bin/uvicorn dmf_agent.api.app:app --reload --port 8000
```

打开 <http://localhost:8000/docs> 查看接口文档。

## 快速开始（前端）

```bash
cd frontend
npm install
npm run dev          # 开发：Vite dev server，/api 代理到 localhost:8000
npm run build        # 生产：产物 dist/，独立部署
```

生产部署采用前后端分离：前端静态资源放任意静态服务器（nginx 等），把 `/api` 反代到后端
（见 [frontend/nginx.conf.example](frontend/nginx.conf.example)）；跨域部署时构建设置
`VITE_API_BASE=<后端根地址>`（见 [frontend/.env.example](frontend/.env.example)）。

## 降级矩阵（未配置时的行为）

| 组件 | 未配置时 | 配置项 |
|---|---|---|
| LLM | 规则/模板降级路径：规则意图识别 + 模板答复，链路照常走通 | `DMF_LLM-MODEL/BASE_URL/API_KEY` |
| StarRocks | 指标取数**明确报错**（不返回假数据），答复中如实说明原因 | `DMF_STARROCKS-HOST/PORT/USER/PASSWORD/DATABASE` |
| MySQL | 内存存储（重启丢失，日志有警告） | `DMF_DB-ENABLED/HOST/...` |
| Embedding/向量 | BM25 纯关键词检索（知识问答离线可用） | `DMF_LLM-EMBEDDING_*`；`DMF_RAG-BACKEND=milvus`（默认）/`faiss` |
| 观测 | 仍按 OTel 采集并落库到内存（重启丢失），仅 trace 文件留痕关闭 | `DMF_OBSERVABILITY-ENABLED`（span 采集开关）、`DMF_SESSION-ARCHIVE_ENABLED` |

健康检查可实时查看各组件状态：`curl localhost:8000/health`

## 配置说明

环境变量前缀 `DMF_`，连字符分层（如 `DMF_LLM-MODEL` -> `llm.model`），完整清单见 `backend/.env.example`。

LLM 统一走 OpenAI 兼容协议（替代老系统十余个自研厂商适配）：

| 服务商 | base_url |
|---|---|
| 阿里 qwen | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| 智谱 | `https://open.bigmodel.cn/api/paas/v4` |
| DeepSeek | `https://api.deepseek.com/v1` |
| vLLM / 自建 | `http://<host>:<port>/v1` |
| Ollama | `http://localhost:11434/v1` |

## 对话接口

```bash
# 阻塞式
curl -X POST localhost:8000/api/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "查一下近7天的GMV趋势", "user_id": "u1"}'

# SSE 流式（curl -N）
curl -N -X POST localhost:8000/api/v1/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"message": "查一下近7天的GMV趋势", "user_id": "u1"}'
```

SSE 事件协议（对齐老系统 SSE 语义，映射表见 [MIGRATION.md](MIGRATION.md)）：

| event | data | 说明 |
|---|---|---|
| `session` | `{session_id, title}` | 会话创建/复用（首轮下发） |
| `node_start` | `{node, started_at}` | 图节点开始执行（前端显示“运行中”+秒表） |
| `node_done` | `{node, status, elapsed_ms?}` | 图节点执行完成（status: ok/error，过程可见） |
| `token` | `{content}` | 最终答复的增量 token 流（仅 synthesize_answer 节点） |
| `final` | `{content, intent, node_trace, mock_mode, ...}` | 终态结果（node_trace 含 query_result 供前端绘图） |
| `error` | `{content, error_info, ...}` | 结构化异常（错误码/分类/可读解释/可重试） |

## 语义层

```bash
# CLI（对齐 MetricFlow 心智）
python -m dmf_agent.semantic list-metrics
python -m dmf_agent.semantic query "metric:GMV group_by:门店 time:2026-08-01~2026-08-07:day"

# API
curl -X POST localhost:8000/api/v1/semantic/query \
  -H 'Content-Type: application/json' \
  -d '{"metrics": ["GMV"], "group_by": ["门店"], "time_range": {"granularity": "day"}}'
```

## 知识库入库

```bash
# CLI（本地文件）
python -m dmf_agent.rag --file docs/product.md --title 产品说明

# API（文本 / 文件上传）
curl -X POST localhost:8000/api/v1/knowledge/documents \
  -H 'Content-Type: application/json' \
  -d '{"title": "口径说明", "text": "GMV 口径为……"}'
```

## 评测

```bash
python -m dmf_agent.eval --verbose      # 跑默认用例集，输出准确率
python -m dmf_agent.eval --json         # 评测报告写 JSON
```

## 数据库迁移

开发环境默认启动时自动建表（`DMF_DB-AUTO_CREATE_TABLES=true`）。
生产 / 正式演进请使用 Alembic（在 `backend/` 下执行）：

```bash
alembic revision --autogenerate -m "init"   # 模型变更后生成迁移
alembic upgrade head
```

## 测试

```bash
cd backend && ../.venv/bin/python -m pytest -v   # 无需任何外部依赖（LLM/MySQL/RAG 均降级）
```

## 文档

- **[doc/改造记录.md](doc/改造记录.md)**：改造说明文档（按日期记录每次调整与新增需求改造，持续更新）
- **[doc/待办清单.md](doc/待办清单.md)**：下一步待办（前端二期 / 接口规整 / skill 管理）
- **[MIGRATION.md](MIGRATION.md)**：老系统模块 -> v2 模块完整映射表与 Phase 1~7 分阶段计划
