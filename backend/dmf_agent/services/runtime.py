"""应用运行时容器：在 FastAPI lifespan 中装配，注入到各路由。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from langgraph.graph.state import CompiledStateGraph

from dmf_agent.config.settings import Settings
from dmf_agent.db.factory import DatabaseBundle
from dmf_agent.rag.retriever import KnowledgeRetriever
from dmf_agent.semantic.engine import MetricFlowEngine
from dmf_agent.semantic.layer import SemanticLayer
from dmf_agent.services.chat_service import ChatService
from dmf_agent.services.plugins import PluginRegistry
from dmf_agent.services.prompts import PromptManager


@dataclass
class AppRuntime:
    settings: Settings
    db: DatabaseBundle
    graph: CompiledStateGraph
    chat_service: ChatService
    retriever: Optional[KnowledgeRetriever]
    rag_backend: str  # "hybrid"（BM25 + 向量）| "bm25"（向量库/Embedding 不可用降级）
    # FR7：重排是否可用（模型就位 = True）。未就绪时检索链路降级为纯 RRF 顺序。
    rerank_available: bool = False
    semantic_layer: Optional[SemanticLayer] = None
    # MetricFlow 语义引擎（编译 SQL + StarRocks 取数）；未装配时取数接口返回 503
    metric_engine: Optional[MetricFlowEngine] = None
    # 语义层溯源信息（ResolvedLayer）：请求源 / 实际生效源 / 是否回落 / 生效版本链。
    # 管理面「语义层来源」面板据此展示，避免"以为改的是 YAML、实际读的是 DB 快照"。
    semantic_resolution: Optional[Any] = None
    # 请求级语义层解析与缓存（SemanticScopeService）：按租户返回「全局链 + 租户 overlay」
    # 生效层，供 chat 请求绑定 ContextVar 与语义读接口按租户取层。
    semantic_scope: Optional[Any] = None
    # StarRocks 客户端：热更新重建 MetricFlowEngine 时复用同一连接池
    starrocks_client: Optional[Any] = None
    # 语义层热更新锁（lifespan 内创建，绑定事件循环）
    semantic_refresh_lock: Optional[Any] = None
    # AgentDeps 引用：图节点通过 deps.semantic_layer **在调用时**读取语义层，
    # 故热更新就地改写该属性即可立即生效（无需重建 graph）。
    agent_deps: Optional[Any] = None
    embedding_status: dict = field(
        default_factory=dict
    )  # 嵌入服务探活状态（openai 后端由看门狗写入；含 reachable/mode/detail/model）
    # 数据源深度探针最近结果（P0-8）：由启动自检与 GET /semantic/health 写入；
    # 含 reachable/latency_ms/version/data_as_of/tables/summary/stale，前端徽标据此降级。
    datasource_status: dict = field(default_factory=dict)
    # 管理端服务（插件注册 / 系统提示词版本管理）
    plugin_registry: Optional[PluginRegistry] = None
    # 模型管理注册表：对话 LLM 配置 CRUD + 连接测试 + 运行时生效覆盖
    # （is_default 条目覆盖环境变量 DMF_LLM-*；见 services/models.py）
    model_registry: Optional[Any] = None
    # Skill 执行沙箱（FR14）：把声明式 skill 包放在 Harness 治理下执行，
    # 只编排已注册工具，不执行任意代码（见 skills/executor.py）。
    skill_executor: Optional[Any] = None
    prompt_manager: Optional[PromptManager] = None
    # 三级黑话词典解析器（读路径 Redis 优先；管理面「在线试改写」直接复用它，
    # 保证「试改写所见」与「线上改写所得」同源同口径）。
    slang_resolver: Optional[Any] = None
    # Harness 强约束壳（PRD FR15）
    harness: Optional[Any] = None
    # 合规安全层（PRD FR13）
    security: Optional[Any] = None
    # 错误样本池仓储（PRD FR6）
    error_sample_repo: Optional[Any] = None
    # 记忆注入一键关闭开关（按用户维度；PRD FR9 用户可一键关闭记忆）。内存态，
    # 重启失效；持久化留待后续。键为 user_id，值为 True 表示关闭记忆注入。
    memory_disabled: dict = field(default_factory=dict)
