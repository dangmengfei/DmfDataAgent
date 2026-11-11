"""FastAPI 应用装配（纯 API 服务，前后端分离：前端为独立 SPA 通过 HTTP 调用）。

启动时在 lifespan 中完成依赖装配（DB 连接 -> Agent 图构建 -> 服务装配），
组件装配失败自动降级（见各模块说明），保证任何环境都能拉起服务。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from dmf_agent import __version__
from dmf_agent.agents.metric_agent import AgentDeps, build_metric_agent_graph
from dmf_agent.api.routers import (
    audit,
    auth as auth_router,
    chat,
    eval as eval_router,
    feedback,
    fewshots,
    health,
    knowledge,
    memory,
    metrics,
    models as models_router,
    obs,
    permission,
    plugins,
    prompts,
    roles as roles_router,
    semantic,
    semantic_registry,
    sessions,
    slang as slang_router,
    tenants as tenants_router,
    users as users_router,
)
from dmf_agent.auth.dependencies import get_current_user
from dmf_agent.auth.service import AuthService
from dmf_agent.config.settings import get_settings
from dmf_agent.db.factory import build_database
from dmf_agent.harness import (
    HarnessController,
    ToolRegistry,
    build_default_tool_registry,
)
from dmf_agent.security import SecurityCoordinator
from dmf_agent.llm.providers import get_chat_model
from dmf_agent.memory import HistoryAssembler
from dmf_agent.observability import build_trace_archiver
from dmf_agent.rag.reranker import build_reranker
from dmf_agent.rag.retriever import HybridKnowledgeRetriever
from dmf_agent.rag.vector_store import build_vector_store
from dmf_agent.rag.embedding_health import embedding_watchdog
from dmf_agent.semantic.probe import datasource_watchdog
from dmf_agent.rag.scheduler import DictionaryRefreshScheduler
from dmf_agent.recall import (
    CantDictionary,
    DEFAULT_CANT_DICT,
    DimensionRecaller,
    FilterRecaller,
    MetricRecaller,
)
from dmf_agent.recall.slang import build_slang_resolver
from dmf_agent.recall.slang_seed import build_seed_entries, collect_guard_sources
from dmf_agent.schemas.slang import SCOPE_INDUSTRY, SCOPE_TENANT, SCOPE_USER
from dmf_agent.semantic import MetricFlowEngine, StarRocksClient
from dmf_agent.semantic.registry import SemanticRegistry
from dmf_agent.semantic.runtime import resolve_layer
from dmf_agent.services.chat_service import ChatService
from dmf_agent.services.models import ModelRegistry
from dmf_agent.services.plugins import PluginRegistry
from dmf_agent.services.prompts import PromptManager
from dmf_agent.services.runtime import AppRuntime
from dmf_agent.services.semantic_layer import apply_resolved
from dmf_agent.services.session_lock import SessionLock
from dmf_agent.skills import ScriptRunner, SkillExecutor, cleanup_stale_temp
from dmf_agent.services.semantic_scope import SemanticScopeService

logger = logging.getLogger(__name__)


async def _auto_seed_dictionary_if_needed(settings, db, vector_store, semantic_layer) -> None:
    """启动自动播种指标口径字典（R4 内容兜底）。

    RAG 与语义层就绪后，若目标租户下无 authoritative 文档则自动灌入口径字典，
    保证 RAG 一上线即有内容可召回。鉴权关闭时 tenant_id=""；鉴权开启时按各租户分别补齐。
    """
    if vector_store is None or semantic_layer is None:
        return
    from dmf_agent.rag.dictionary import seed_dictionary

    tenant_ids = [""]
    if settings.auth.enabled:
        try:
            tenants = await db.tenant_repo.list_all()
            tenant_ids = [t.id for t in tenants] or [""]
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取租户列表失败，口径字典按无租户播种：%s", exc)
            tenant_ids = [""]

    for tid in tenant_ids:
        try:
            existing = await db.document_repo.list(
                authority_level="authoritative", tenant_id=tid
            )
            if existing:
                continue
            res = await seed_dictionary(
                document_repo=db.document_repo,
                chunk_repo=db.chunk_repo,
                vector_store=vector_store,
                settings=settings,
                layer=semantic_layer,
                tenant_id=tid,
            )
            logger.info(
                "启动自动播种口径字典完成（tenant=%s, %s 分块）", tid, res.get("chunks")
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("自动播种口径字典失败（tenant=%s，跳过）：%s", tid, exc)


async def model_watchdog(registry: ModelRegistry, interval: float) -> None:
    """模型生效覆盖的后台探活：周期重读默认条目，变化即重建运行时生效覆盖。

    目的：多实例部署下任一实例改默认 / 更新配置，其它实例在周期内自动收敛
    （与语义层基座「拉」式自愈同思路）。best-effort，失败仅告警、保留旧覆盖。
    """
    while True:
        await asyncio.sleep(interval)
        try:
            await registry.refresh()
        except Exception as exc:  # noqa: BLE001
            logger.warning("模型生效覆盖探活失败（保留上次）：%s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=logging.DEBUG if settings.app.debug else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    # 1. 数据层（MySQL / 内存降级）
    db = build_database(settings)
    await db.initialize(settings)

    # 1a. 模型管理注册表：对话 LLM 配置 CRUD + 连接测试 + 运行时生效覆盖。
    #     启动时先把库内默认模型解析成生效覆盖（无默认模型 -> 回落环境变量 DMF_LLM-*），
    #     之后管理面变更即时 refresh，多实例由下方 model_task 周期收敛。
    #     随后做一次「env → 库」引导：env 配了 LLM 且库内无默认时，把当前生效模型
    #     落成库内默认模型（幂等，一次性迁移；之后可从 env 移除 DMF_LLM-*，见 seed_from_env）。
    model_registry = ModelRegistry(db.llm_config_store)
    await model_registry.refresh()
    await model_registry.seed_from_env(settings.llm)

    # 1b. 认证引导：首次启动无任何用户时创建默认租户 + 管理员（可 DMF_AUTH-* 配置）
    #     此时 SecurityCoordinator 尚未构造，用直连 audit sink 的临时审计器留痕。
    from dmf_agent.security.auditor import AuditLogger

    _bootstrap_auditor = AuditLogger(sink=db.audit_log_repo, enabled=True)
    try:
        created = await AuthService(db, settings).ensure_default_admin()
        if created is not None:
            logger.warning(
                "已创建默认管理员：%s（请登录后尽快修改密码）", created.username
            )
            await _bootstrap_auditor.system_bootstrap(
                action="default_tenant",
                tenant_id=created.tenant_id,
                target=created.tenant_id,
                summary=f"首次启动兜底创建默认租户：{created.tenant_id}",
                detail={"tenant_code": settings.auth.default_tenant_code},
            )
            await _bootstrap_auditor.system_bootstrap(
                action="default_admin",
                tenant_id=created.tenant_id,
                user_id=created.id,
                target=created.username,
                summary=f"首次启动兜底创建默认管理员：{created.username}（请尽快改密）",
                detail={"username": created.username, "role": "admin"},
            )
    except Exception as exc:  # noqa: BLE001 - 引导失败不阻断服务启动
        logger.warning("默认管理员引导失败（跳过）：%s", exc)

    # 1c. 角色种子：幂等导入内置三角色（admin/analyst/viewer）到 roles 表，
    #     支持角色管理面增删改查；已存在则跳过，不覆盖管理面后续修改
    try:
        from dmf_agent.auth.roles import seed_builtin_roles

        n = await seed_builtin_roles(db.role_repo)
        if n:
            logger.info("已导入内置角色种子 %s 条", n)
    except Exception as exc:  # noqa: BLE001 - 种子失败不阻断服务启动（列表回落静态）
        logger.warning("内置角色种子导入失败（跳过）：%s", exc)

    # 2. RAG 检索（BM25 + 向量混合；Embedding 未配置时纯 BM25，知识问答离线可用）
    #    维度/维值黑话（老 candidates_cant）：由内置常量 DEFAULT_CANT_DICT 提供，
    #    已由新的三级 slang_entries 表统一取代其"可维护"职责（旧 cant_entries 表已删除，见迁移 0011）。
    cant_dict = CantDictionary.from_raw_rows(DEFAULT_CANT_DICT)
    vector_store = build_vector_store(settings)
    # 重排（FR7 检索第二阶段）：模型由 embedding-service 承载，应用只做 HTTP 调用；
    # 未启用/地址缺失 -> None（不重排）；服务在但模型未就位 -> 调用时降级为 RRF 顺序。
    reranker = build_reranker(settings)
    retriever = HybridKnowledgeRetriever(
        vector_store,
        db.chunk_repo,
        db.document_repo,
        settings,
        cant_dict=cant_dict,
        reranker=reranker,
        kb_repo=db.knowledge_base_repo,
    )
    rag_backend = "hybrid" if vector_store else "bm25"

    # 3. 语义层（MetricFlow 规范）+ MetricFlow 语义引擎（编译 SQL -> StarRocks 取数）
    #    混合方案：YAML 基座（manifest.d 多层目录）或 DB 发布快照
    #    （DMF_SEMANTIC-SOURCE=db），由 resolve_layer 按配置装配；DB 侧按
    #    「全局链 + 租户 overlay」解析，缺失 / 反序列化失败 / 仓储异常均自动回落 YAML。
    #    semantic_resolution 保留溯源信息（生效源 / 版本链），供管理面展示与热更新比对。
    semantic_registry = SemanticRegistry(db.semantic_published_repo)
    semantic_resolution = await resolve_layer(settings=settings, registry=semantic_registry)
    semantic_layer = semantic_resolution.layer
    starrocks_client = StarRocksClient(settings.effective_starrocks)
    metric_engine = MetricFlowEngine(semantic_layer, starrocks_client)

    # 3b. 启动自动播种指标口径字典（R4 内容兜底）：RAG 与语义层就绪后，若目标租户下
    #     无 authoritative 文档则自动灌入口径字典，保证 RAG 一上线即有内容可召回。
    #     best-effort：失败仅告警，不阻断启动。
    try:
        await _auto_seed_dictionary_if_needed(settings, db, vector_store, semantic_layer)
    except Exception as exc:  # noqa: BLE001
        logger.warning("启动自动播种口径字典失败（跳过）：%s", exc)
    # 请求级语义层解析（多租户）：按 tenant_id 解析「全局链 + 租户 overlay」生效层，
    # 带链指纹缓存；source=yaml / 无租户时直接返回上面的全局层（零额外开销）。
    # 除租户链外，它还会对**全局链**做节流探测并在变化时就地重建基座（"拉"式自愈），
    # 于是多实例下任一实例发布新快照，其他实例下一请求即自动收敛、无需广播。
    semantic_scope = SemanticScopeService(
        settings=settings,
        base=semantic_resolution,
        base_engine=metric_engine,
        registry=semantic_registry,
        client=starrocks_client,
        base_probe_ttl_s=getattr(
            getattr(settings, "semantic", None), "scope_base_probe_ttl_s", 1.0
        ),
        redis_client=getattr(db, "redis_client", None),
    )
    metric_recaller = MetricRecaller(settings.recall)
    dim_recaller = DimensionRecaller(settings.recall)
    filter_recaller = FilterRecaller(settings.recall)

    # 3d. Harness 强约束壳（PRD FR15）+ 合规安全层（PRD FR13）
    tool_registry: ToolRegistry = build_default_tool_registry()
    harness = HarnessController(
        settings=settings.harness,
        registry=tool_registry,
        auditor=lambda event: db.audit_log_repo.add(event),
    )
    # 插件注册表（Skill / MCP）与 Skill 执行沙箱：沙箱只编排白名单内工具 + 受控执行包内脚本
    # 启动时兜底清理上次进程被杀（SIGKILL/OOM）留下的解压临时目录（只删超龄的，见函数 docstring）
    cleaned = cleanup_stale_temp(settings.skill.package_dir)
    if cleaned:
        logger.info("Skill 包目录：已清理遗留解压临时目录 %d 个", cleaned)
    plugin_registry = PluginRegistry(db.plugin_store, package_base=settings.skill.package_dir)
    security = SecurityCoordinator(
        settings=settings.security,
        audit_sink=db.audit_log_repo,
        grant_repo=db.metric_grant_repo,
        scope_repo=db.data_scope_repo,
        policy_repo=db.dimension_policy_repo,
    )

    # 3b. 三级黑话词典解析器（PRD §5.3.1 查询改写：黑话翻译 / 同义扩展 / 口语归一）
    #     行业级为空时灌入内置种子（黑话 + 标准名自映射护栏，约 110 条）并预热 Redis 缓存。
    #     读路径 Redis 优先；写路径由管理面保证「先 MySQL，再 refresh_layer 同步 Redis」。
    slang_resolver = build_slang_resolver(db.slang_repo, db.redis_client, settings.slang)
    try:
        scope_counts = await db.slang_repo.count_by_scope()
        if scope_counts.get(SCOPE_INDUSTRY, 0) == 0:
            seeded = await db.slang_repo.replace_scope_entries(
                SCOPE_INDUSTRY,
                "",
                "",
                build_seed_entries(**collect_guard_sources(semantic_layer)),
            )
            logger.info("黑话词典：行业级为空，已灌入内置种子 %d 条", seeded)
            scope_counts = await db.slang_repo.count_by_scope()
        cached = await slang_resolver.refresh_layer(SCOPE_INDUSTRY)
        logger.info(
            "黑话词典就绪：行业级 %d 条 / 公司级 %d 条 / 用户级 %d 条（行业级缓存预热 %d 条）",
            scope_counts.get(SCOPE_INDUSTRY, 0),
            scope_counts.get(SCOPE_TENANT, 0),
            scope_counts.get(SCOPE_USER, 0),
            cached,
        )
    except Exception as exc:  # noqa: BLE001 - 词典初始化失败不阻断服务启动
        logger.warning("黑话词典初始化失败（改写将原句透传）：%s", exc)

    # 3c. 语义注册中心（P1 骨架）：启动时从 manifest.yml **幂等**导入种子（已存在的
    #     key 跳过）。不判断空库——幂等导入可自愈"上次启动半截失败"的状态。
    #     P1 阶段 manifest 仍是运行时真源，注册中心是"有稳定 key、可治理"的镜像；
    #     P3 切换 DB 真源 + 热更新后，本导入作为"空库首次启动种子"保留。
    try:
        from dmf_agent.semantic.registry_seed import build_registry_seed

        report = await db.semantic_registry_repo.import_seed(
            build_registry_seed(semantic_layer), operator="system"
        )
        if report.get("models_imported") or report.get("metrics_imported"):
            logger.info(
                "语义注册中心：已从 manifest.yml 导入种子（模型 %d 新增 %d 跳过 / 指标 %d 新增 %d 跳过）",
                report.get("models_imported", 0),
                report.get("models_skipped", 0),
                report.get("metrics_imported", 0),
                report.get("metrics_skipped", 0),
            )
    except Exception as exc:  # noqa: BLE001 - 注册中心初始化失败不阻断服务启动
        logger.warning("语义注册中心种子导入失败（只读接口将返回空）：%s", exc)

    # Skill 沙箱执行器（FR14）：AppRuntime 的管理面接口与主图 execute_skill 节点共用同一实例。
    # 在 AgentDeps 之前构造以便注入；注入后 nodes/mcp_tool 的 plugin_registry 也由"恒未装配"变为可用。
    skill_executor = SkillExecutor(
        harness=harness,
        metric_engine=metric_engine,
        retriever=retriever,
        plugin_registry=plugin_registry,
        get_chat_model=lambda: get_chat_model(
            settings, llm_override=model_registry.effective_llm
        ),
        script_runner=ScriptRunner(
            timeout_seconds=settings.harness.skill_script_timeout_seconds,
            max_output_bytes=settings.harness.skill_script_max_output_bytes,
        ),
        script_enabled=settings.skill.script_enabled,
        max_steps=settings.harness.skill_max_steps,
    )

    deps = AgentDeps(
        settings=settings,
        get_chat_model=lambda: get_chat_model(
            settings, llm_override=model_registry.effective_llm
        ),
        semantic_layer=semantic_layer,
        metric_engine=metric_engine,
        retriever=retriever,
        recaller=metric_recaller,
        dim_recaller=dim_recaller,
        filter_recaller=filter_recaller,
        cant_dict=cant_dict,
        slang_resolver=slang_resolver,
        few_shot_repo=db.few_shot_repo,
        domain_memory_repo=db.domain_memory_repo,
        user_pref_repo=db.preference_repo,
        harness=harness,
        security=security,
        error_sample_repo=db.error_sample_repo,
        # 结构化对话状态（P0-6 多轮对话状态管理）；反问澄清节点据此读取 prior 澄清次数
        conv_state_repo=db.conversation_state_repo,
        # 语义注册中心（P1-6 / FR8 create 意图）：指标草稿落库经管理面审批发布
        semantic_registry_repo=db.semantic_registry_repo,
        # 插件与 Skill（FR14）：主图 execute_skill 节点据此列出/执行已上传的声明式 skill
        plugin_registry=plugin_registry,
        skill_executor=skill_executor,
    )
    graph = build_metric_agent_graph(deps)
    logger.info(
        "语义层就绪：指标 %d 个 / 维度 %d 个 / 语义模型 %d 个 | StarRocks=%s",
        len(semantic_layer.list_metrics()),
        len(semantic_layer.list_dimensions()),
        len(semantic_layer.manifest.models),
        (
            f"{settings.effective_starrocks.host}:{settings.effective_starrocks.port}/{settings.effective_starrocks.database}"
            if settings.effective_starrocks.configured
            else "未配置（取数将明确报错）"
        ),
    )

    # 4. 记忆装配器（相关性检查 + 滚动压缩 + query 改写）
    history_assembler = HistoryAssembler(
        message_repo=db.message_repo,
        summary_repo=db.memory_repo,
        settings=settings,
        get_chat_model=lambda: get_chat_model(
            settings, llm_override=model_registry.effective_llm
        ),
    )

    # 5. 对话服务（观测遵循 OpenTelemetry：span 落 trace_spans + 轮级 trace 落 trace_records
    #    + 可选文件留痕；不导出到任何外部观测平台，见 observability/）
    trace_archiver = build_trace_archiver(settings)
    if trace_archiver is not None:
        logger.info("会话 trace 留痕已开启：%s", settings.session.archive_dir)
    # 用户级"一键关闭记忆"开关：ChatService 与 AppRuntime 共享同一字典引用，路由开关即时生效
    memory_disabled: dict = {}
    # 会话级串行化闸：进程内锁兜底 + Redis 可用时叠加分布式锁（Redis 异常时内部自动降级）
    session_lock = SessionLock(
        enabled=settings.session.lock_enabled,
        wait_seconds=settings.session.lock_wait_seconds,
        ttl_seconds=settings.session.lock_ttl_seconds,
        redis=db.redis_client,
    )
    chat_service = ChatService(
        graph=graph,
        session_repo=db.session_repo,
        message_repo=db.message_repo,
        settings=settings,
        history_assembler=history_assembler,
        trace_repo=db.trace_repo,
        trace_archiver=trace_archiver,
        span_repo=db.span_repo,
        # L2 用户偏好仓储：记忆开启时加载当前用户 active 偏好注入要素抽取与答复 prompt
        preference_repo=db.preference_repo,
        memory_disabled=memory_disabled,
        # 合规安全层：输入守卫在 ChatService 入口拦截，输出/脱敏/审计在各节点
        security=security,
        # 错误样本池（PRD FR6）
        error_sample_repo=db.error_sample_repo,
        # 结构化对话状态（P0-6 多轮对话状态管理）
        conversation_state_repo=db.conversation_state_repo,
        # 请求级语义层解析（多租户）：图执行期按租户绑定 ContextVar
        semantic_scope=semantic_scope,
        # 会话级串行化闸（同会话轮次互斥）
        session_lock=session_lock,
    )

    app.state.runtime = AppRuntime(
        settings=settings,
        db=db,
        graph=graph,
        chat_service=chat_service,
        retriever=retriever,
        rag_backend=rag_backend,
        # 重排客户端已装配（配置层面可用）；模型就位与否由检索调用时惰性探测
        rerank_available=reranker is not None,
        semantic_layer=semantic_layer,
        metric_engine=metric_engine,
        # 语义层溯源（管理面「语义层来源」面板）+ 热更新所需句柄：
        # starrocks_client 复用同一连接池重建引擎；agent_deps 就地改写即让图节点生效
        semantic_resolution=semantic_resolution,
        starrocks_client=starrocks_client,
        agent_deps=deps,
        semantic_scope=semantic_scope,
        semantic_refresh_lock=asyncio.Lock(),
        plugin_registry=plugin_registry,
        # 与 AgentDeps 注入的是同一实例（上文在 AgentDeps 之前构造）
        skill_executor=skill_executor,
        model_registry=model_registry,
        prompt_manager=PromptManager(
            db.prompt_store, gray_enabled=get_settings().prompts.gray_enabled
        ),
        memory_disabled=memory_disabled,
        slang_resolver=slang_resolver,
        harness=harness,
        security=security,
        error_sample_repo=db.error_sample_repo,
    )

    # P0-8 数据源启动自检：runtime 已构造，best-effort 跑一次深度探针（心跳/延迟/表
    # 存量），直接写入 app.state.runtime.datasource_status 供前端徽标；在线但有空表/
    # 陈旧/失败表时告警。探针内部带超时且不抛错，任何情况都不阻断启动。
    try:
        ds_probe = await metric_engine.probe()
        app.state.runtime.datasource_status = ds_probe
        if ds_probe.get("reachable"):
            summ = ds_probe.get("summary") or {}
            if ds_probe.get("stale"):
                logger.warning(
                    "⚠️ 数据源自检：%s 在线，但 空表 %s / 陈旧 %s / 探测失败 %s（data_as_of=%s）",
                    ds_probe.get("mode"),
                    summ.get("empty_tables"),
                    summ.get("stale_tables"),
                    summ.get("failed_tables"),
                    ds_probe.get("data_as_of"),
                )
            else:
                logger.info(
                    "数据源自检通过：%s | latency=%sms | %s 张底表可探 | data_as_of=%s",
                    ds_probe.get("detail"),
                    ds_probe.get("latency_ms"),
                    summ.get("reachable_tables"),
                    ds_probe.get("data_as_of"),
                )
        else:
            logger.warning(
                "数据源自检：不可达（%s），取数将明确报错", ds_probe.get("detail")
            )
    except Exception as exc:  # noqa: BLE001 - 自检失败不阻断启动
        logger.warning("数据源启动自检失败（跳过）：%s", exc)

    # 数据源后台周期看门狗：按 probe_interval 周期深度探针，状态翻转即告警（best-effort）。
    # probe_interval<=0 时不启动（仅保留上面的启动自检 + 按需 /semantic/health）。
    datasource_task = None
    if settings.effective_starrocks.probe_enabled:
        datasource_task = asyncio.create_task(
            datasource_watchdog(app.state.runtime)
        )

    # 基座"拉"式自愈的回写钩子：scope 服务探到全局链变化后重建基座，经此把新层同步给
    # runtime 与 deps（图节点读的就是 deps）。刻意用 apply_resolved 而非 refresh_semantic_layer：
    # 后者会重新解析并回调 rebase，与探到变化后的重建重复且可能自环。
    semantic_scope.on_base_changed = (
        lambda resolved, engine: apply_resolved(app.state.runtime, resolved, engine)
    )

    # 系统提示词：从存储加载历史 published 版本为运行时覆盖（无则用仓库 YAML 基线）
    prompt_mode = await app.state.runtime.prompt_manager.reload()
    logger.info("系统提示词生效来源：%s", prompt_mode)

    # 嵌入服务探活（仅 openai 后端有意义）：先写初始状态，再起后台看门狗周期性探活与告警。
    # 看门狗失败只告警、不阻断启动（向量不可用时 RAG 自动降级 BM25）。
    embedding_task = None
    if settings.llm.embedding_backend == "openai" and settings.llm.embedding_configured:
        app.state.runtime.embedding_status = {
            "reachable": None,
            "mode": "openai",
            "detail": "probing...",
        }
        embedding_task = asyncio.create_task(
            embedding_watchdog(app.state.runtime, interval=30)
        )

    # 模型生效覆盖后台探活（多实例收敛）：周期重读默认模型，变化即重建运行时
    # 生效覆盖；probe_interval_s<=0 时仅「启动 + 管理面变更」即时刷新，单实例够用。
    model_task = None
    if settings.model.probe_interval_s > 0:
        model_task = asyncio.create_task(
            model_watchdog(model_registry, settings.model.probe_interval_s)
        )

    # 3f. 口径字典周期重建调度（Task C）：零依赖异步周期任务。复用启动播种的口径重建逻辑，
    #     按 settings.rag.kb_auto_refresh_* 周期同步语义层口径到 RAG 权威源；未启用则静默不启动。
    dict_refresh = DictionaryRefreshScheduler()
    dict_refresh.start(settings, db, vector_store, semantic_layer)

    llm_mode = (
        "configured"
        if (model_registry.effective_llm is not None or settings.llm.chat_configured)
        else "mock"
    )
    if model_registry.effective_llm is not None:
        logger.info(
            "运行时生效模型：%s（库内默认，覆盖 DMF_LLM-*）",
            model_registry.effective_name or "?",
        )
    logger.info(
        "DmfDataAgent 就绪 | db=%s | llm=%s | rag=%s | otel_tracing=%s",
        db.mode,
        llm_mode,
        rag_backend,
        "on" if settings.observability.enabled else "off",
    )
    # 跨进程缓存失效监听（无 Redis 时 start() 内部自动跳过）
    await semantic_scope.start()
    yield
    if datasource_task is not None:
        datasource_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await datasource_task
    if embedding_task is not None:
        embedding_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await embedding_task
    if model_task is not None:
        model_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await model_task
    if dict_refresh.running:
        await dict_refresh.stop()
    # 停止跨进程失效监听（释放 Redis 订阅）
    with contextlib.suppress(Exception):
        await semantic_scope.stop()
    # StarRocks 连接池优雅关闭（未建立连接时为空操作）
    with contextlib.suppress(Exception):
        await starrocks_client.close()
    await db.dispose()


def create_app() -> FastAPI:
    """构造 FastAPI 应用：CORS + 注册各业务路由（全部 /api/v1 前缀）+ 根别名与根探活。

    装配要点：lifespan 里已把全部依赖写进 app.state.runtime（AppRuntime），
    路由层通过 Depends(get_runtime) 取用，这里只负责「挂哪些路由、哪些免鉴权、哪些需登录」。
    各路由的细粒度权限（管理面读写 / admin）在路由内部用 require_permission 控制，不在此处体现。
    """
    settings = get_settings()
    app = FastAPI(
        title="DmfDataAgent",
        version=__version__,
        description="指标问答 Agent（FastAPI + LangGraph + LangChain + MySQL + RAG + OpenTelemetry 观测）",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.app.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ============ 路由注册（统一挂 /api/v1 前缀，OpenAPI tags 按此分组）============

    # ---- 免鉴权（登录前即可访问）----
    # 健康探活：GET /health；下方再以根前缀注册一次，兼容探活直接写 /health。
    app.include_router(health.router, prefix="/api/v1", tags=["health"])
    # 认证：/auth/login、/auth/refresh 公开；/auth/me、/auth/change-password 在路由内自带令牌校验。
    app.include_router(auth_router.router, prefix="/api/v1", tags=["auth"])

    # ---- 需登录：其余接口一律过 get_current_user ----
    # 鉴权关闭（DMF_AUTH-ENABLED=false）时返回合成 admin，放行全部；细粒度权限点在各自
    # 路由用 require_permission / require_role 控制（admin 通配放行）。
    protected = [Depends(get_current_user)]
    # 对话主链路：/chat（阻塞式）+ /chat/stream（SSE 流式），前端对话区唯一入口，内部跑 Agent 主图。
    app.include_router(chat.router, prefix="/api/v1", tags=["chat"], dependencies=protected)
    # 会话管理：新建 / 列表 / 详情 / 重命名 / 删除，及消息读取、滚动压缩、标题生成。
    app.include_router(sessions.router, prefix="/api/v1", tags=["sessions"], dependencies=protected)
    # 知识库：个人/企业库 CRUD；文档文本录入 / 文件上传 / 分块查看 / 删除；检索调试；口径字典灌入。
    app.include_router(knowledge.router, prefix="/api/v1", tags=["knowledge"], dependencies=protected)
    # 指标检索（语义层）：/metrics 列表 + /metrics/{code} 详情，供前端指标选择。
    app.include_router(metrics.router, prefix="/api/v1", tags=["metrics"], dependencies=protected)
    # 语义层只读面：指标/维度目录、清单概览、数据源探针、SQL 编译试算与查询执行（前端语义浏览器）。
    app.include_router(semantic.router, prefix="/api/v1", tags=["semantic"], dependencies=protected)
    # 语义注册中心治理：模型/指标/维度/度量 CRUD，快照起草-提交-审批-发布、热更新、租户 overlay 编辑。
    app.include_router(
        semantic_registry.router, prefix="/api/v1", tags=["semantic-registry"], dependencies=protected
    )
    # 模型管理：对话 LLM 配置入库 CRUD + 连接测试（弹窗预检 / 已存试连）+ 设默认（运行时覆盖 DMF_LLM-*）。
    app.include_router(models_router.router, prefix="/api/v1", tags=["models"], dependencies=protected)
    # 观测：汇总 KPI、trace/轮次/span 明细、成本与质量看板、错误样本、单会话观测、导出。
    app.include_router(obs.router, prefix="/api/v1", tags=["observability"], dependencies=protected)
    # 插件与 Skill（FR14）：Skill 列表/手动登记/zip 上传/详情/沙箱执行；MCP Server 注册/连接测试/调用/删除。
    app.include_router(plugins.router, prefix="/api/v1", tags=["plugins"], dependencies=protected)
    # 提示词管理（FR12）：模板列表/历史/详情/更新/回滚，及评测门禁、金丝雀晋级、A/B 对比。
    app.include_router(prompts.router, prefix="/api/v1", tags=["prompts"], dependencies=protected)
    # 评测：意图/要素用例集与数据集查询、bad-case、LLM 评判、结果回流。
    app.include_router(eval_router.router, prefix="/api/v1", tags=["eval"], dependencies=protected)
    # 反馈：点赞点踩提交 + 采纳状态查询（驱动质量看板采纳率）。
    app.include_router(feedback.router, prefix="/api/v1", tags=["feedback"], dependencies=protected)
    # 记忆分层：L1 热层 / L2 偏好 / L3 团队知识读写、按会话清理与压缩、一键开关、各层统计。
    app.include_router(memory.router, prefix="/api/v1", tags=["memory"], dependencies=protected)
    # Few-Shot 样本：CRUD、试召回、导入导出、统计与过滤选项（提示词少样本增强）。
    app.include_router(fewshots.router, prefix="/api/v1", tags=["fewshots"], dependencies=protected)
    # 黑话词典：词条 CRUD、发布/停用/版本/回滚、导入导出、试改写、命中率看板、用户反馈（查询改写）。
    app.include_router(slang_router.router, prefix="/api/v1", tags=["slang"], dependencies=protected)
    # 审计日志：查询（分页/过滤）+ 事件统计（仅管理员）。
    app.include_router(audit.router, prefix="/api/v1", tags=["audit"], dependencies=protected)
    # 角色管理：内置三角色 + 自定义角色 CRUD、全量可分配权限点清单。
    app.include_router(roles_router.router, prefix="/api/v1", tags=["roles"], dependencies=protected)
    # 租户管理：新建 / 列表 / 更新 / 删除租户（有用户时禁止删）。
    app.include_router(tenants_router.router, prefix="/api/v1", tags=["tenants"], dependencies=protected)
    # 用户管理：CRUD + 管理员重置密码。
    app.include_router(users_router.router, prefix="/api/v1", tags=["users"], dependencies=protected)
    # 数据权限（admin）：指标授权、数据范围、维度策略的授予与查询。
    app.include_router(
        permission.router, prefix="/api/v1", tags=["permission"], dependencies=protected
    )

    # 根路径别名（探活常用 /health）：不挂 /api/v1 前缀再注册一次 health 路由。
    app.include_router(health.router, tags=["health"])

    @app.get("/", include_in_schema=False)
    async def root() -> dict:
        return {"service": "dmf-agent", "docs": "/docs", "health": "/health"}

    return app


app = create_app()
