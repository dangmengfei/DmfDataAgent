"""统一配置（pydantic-settings）。

配置读取规则：环境变量前缀 DMF_ + 连字符分层。
例如 DMF_LLM-MODEL 对应 llm.model，DMF_DB-HOST 对应 db.host。
支持 .env 文件（置于运行目录）。

各模块均设计为"未配置自动降级"：
- LLM 未配置      -> Agent 走 mock 模式（规则/模板），链路仍可完整跑通
- MySQL 未配置    -> 会话/消息/知识库降级为内存存储
- 观测未配置   -> 不导出 OTLP / 不落盘 trace
- RAG 未配置      -> 知识库问答返回未配置提示
"""

from __future__ import annotations

from functools import lru_cache
from typing import Dict, List
from urllib.parse import quote_plus

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseModel):
    name: str = "dmf-agent"
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])


class LLMSettings(BaseModel):
    """LLM 配置：统一走 OpenAI 兼容协议，覆盖 qwen(dashscope)、zhipu、deepseek、vllm、ollama 等。

    迁移说明：替代老系统 llm/ 下的十余个自研厂商适配
    （qwen.py / zhipuai.py / baichuan.py / ernie.py / azure_ml.py ...）。
    """

    # 预留（当前未参与分支）：代码里只有 openai_compatible 一条路径，保留字段是为了
    # 后续多厂商适配时不必改动调用方。**不要**据它判断"已支持多 provider"。
    provider: str = "openai_compatible"
    model: str = ""
    base_url: str | None = None
    api_key: str | None = None
    temperature: float = 0.1
    timeout: int = 120
    max_retries: int = 2

    # Embedding（RAG / 召回用；向量化模型单独部署为嵌入服务，应用通过 HTTP 接口调用）
    # backend:
    #   - "openai" : 【生产/推荐】调用 OpenAI 兼容的嵌入服务（本项目 embedding-service，
    #                或生产环境的小模型服务）。只需 model + base_url，api_key 可选（自托管免鉴权）。
    #                上线生产时改 DMF_LLM-EMBEDDING_BASE_URL 指向生产小模型即可，应用代码不变。
    #   - "local"  : 【开发兜底】进程内 LangChain HuggingFaceEmbeddings 本地推理，不依赖任何服务；
    #                适合不想起独立嵌入服务时的本地开发。
    #                容错：backend=openai 时若嵌入服务调用失败，按 embedding_retry 重试，
    #                仍失败则降级到本地模型（embedding_fallback_to_local，需与远端同维度），
    #                最终不可用抛 EmbeddingUnavailableError，由检索降级 BM25 / 入库接口返回 503 处理。
    embedding_backend: str = "local"
    embedding_model: str = ""
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None
    # 本地向量化（backend=local 时生效，开发兜底用）
    embedding_local_model: str = "BAAI/bge-large-zh"  # 1024 维，中文优化；可换成其它本地模型
    embedding_local_cache_folder: str | None = None   # 离线模型目录；留空则用默认缓存目录
    embedding_local_device: str = "cpu"               # cpu / cuda / mps
    embedding_local_normalize: bool = True             # 向量 L2 归一化
    # openai 后端容错（backend=openai 时生效）
    embedding_retry: int = 2                           # 调用嵌入服务失败时的重试次数（指数退避）
    embedding_fallback_to_local: bool = True           # 主后端持续失败时降级到进程内本地模型（需本地模型与远端同维度；权重须在 app 容器内可用）

    @property
    def chat_configured(self) -> bool:
        return bool(self.model and self.base_url and self.api_key)

    @property
    def embedding_configured(self) -> bool:
        # local: 进程内 LangChain 向量化，只需有模型名即可
        if self.embedding_backend == "local":
            return bool(self.embedding_local_model)
        # openai: 调用 OpenAI 兼容的嵌入服务（自托管 embedding-service / 生产小模型），
        #         api_key 可选（自托管服务通常免鉴权）
        return bool(self.embedding_model and self.embedding_base_url)


class MySQLSettings(BaseModel):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "dmf_agent"
    password: str = "dmf_agent"
    database: str = "dmf_agent"
    pool_size: int = 5
    # 开发环境启动时自动建表；生产环境请使用 alembic 迁移后置为 false
    auto_create_tables: bool = True

    @property
    def async_dsn(self) -> str:
        pwd = quote_plus(self.password)
        return (
            f"mysql+aiomysql://{self.user}:{pwd}@{self.host}:{self.port}"
            f"/{self.database}?charset=utf8mb4"
        )

    @property
    def sync_dsn(self) -> str:
        pwd = quote_plus(self.password)
        return f"mysql+pymysql://{self.user}:{pwd}@{self.host}:{self.port}/{self.database}?charset=utf8mb4"


class ObservabilitySettings(BaseModel):
    """观测配置：遵循 OpenTelemetry 语义，span 直接落库（不接任何外部观测平台）。

    - 一轮对话 = 一条 root span（`chat_round`），每个图节点 = 一条子 span
      （`node.<name>`），字段遵循 OTel Span 规范（trace_id / span_id /
      parent_span_id / name / kind / start_time / end_time / status / attributes）
    - span 落 `trace_spans` 表，轮级汇总落 `trace_records`（前端"运行观测"页数据源）
    - 已移除 agent-insight / OTLP collector 导出与相关配置（不再依赖外部观测平台）
    """

    # span 采集开关（关闭后仅保留轮级 trace 落库，span 明细不产生）
    enabled: bool = True
    service_name: str = "dmf-agent"
    # 写入 span attributes 的字符串值截断长度（避免 node_trace / 输出过大）
    max_attribute_length: int = 2048
    # 单轮最多落库的 span 数（防御性上限）
    max_spans_per_round: int = 64


class RecallSettings(BaseModel):
    """候选集召回配置（迁移自老系统 get_metric_candidates 的指标配置阈值）。"""

    # 部分匹配（Jaccard）最低分数，低于该值不召回
    min_score: float = 0.5
    # 候选集最大数量
    max_nums: int = 20
    # 容错公共子串的最大间隔字符数（老系统 interval=3）
    interval: int = 3
    # 维度召回（candidates_dim）：mapping/exactly_mapping 最低分数与最大数量
    dim_min_value: float = 0.4
    dim_max_nums: int = 10
    # 过滤召回（candidates_filter）：mapping/exactly_mapping 最低分数与最大数量
    # （对齐老系统 aigc_indicator_config 默认 filter_min_value=0.5 / filter_max_nums=10）
    filter_min_value: float = 0.5
    filter_max_nums: int = 10
    # 向量召回开关（依赖 embedding 服务，Phase 5 启用）
    # ⚠️ 预留未接线：当前无任何代码读取它，向量召回通道尚未实现。置 True 不会改变行为。
    enable_embedding: bool = False

    # 融合分层权重（对齐老系统 weight_sort：full=1.0 / embed=0.6 / part=0.4；
    # black_talk 老系统加权被注释，v2 显式生效取 1.0）
    weight_full: float = 1.0
    weight_black_talk: float = 1.0
    weight_embed: float = 0.6
    weight_part: float = 0.4

    # 相对阈值过滤（对齐老 weight_sort 的 >= 最高分/3）：
    # 融合后按"最高分 * ratio"过滤低分候选，抑制强命中时的部分匹配噪声
    enable_relative_threshold: bool = True
    relative_threshold_ratio: float = 1.0 / 3.0


class MemorySettings(BaseModel):
    """对话记忆策略（迁移自老系统 RoundCompressor 配置）。"""
    enabled: bool = True
    # 触发压缩所需的最少轮数：未压缩轮数 >= rounds_to_compress + rounds_to_retain 时压缩
    rounds_to_compress: int = 2
    # 最近保留完整（不压缩）的轮数
    rounds_to_retain: int = 2
    # 是否启用历史相关性检查（不相关则截断历史，迁移老系统 history_relevance_check）
    history_relevance_check: bool = True
    # 参与相关性判断的最近历史轮数（迁移老系统 query_rewrite_round_nums）
    relevance_check_rounds: int = 3

    # --- FR9 记忆分层重排权重（score = α·sim + β·recency + γ·freq + δ·level − ε·privacy）---
    rerank_alpha: float = 1.0  # sim 权重（语义相关性，BM25 归一化）
    rerank_beta: float = 0.3  # recency 权重（时效）
    rerank_gamma: float = 0.5  # freq 权重（频次/置信）
    rerank_delta: float = 0.4  # level 权重（层固定加成）
    rerank_epsilon: float = 0.2  # privacy 惩罚（敏感候选扣分）
    recency_lambda: float = 0.05  # 时效衰减系数（天数；越小遗忘越慢）
    level_weight_l2: float = 1.0  # L2 用户偏好固定层权重
    level_weight_l3: float = 2.0  # L3 领域知识固定层权重（高于 L2）
    recall_budget_chars: int = 1500  # 注入预算截断上限（≤1.5K，PRD §5.11.2）


class SessionSettings(BaseModel):
    """会话留痕（合规审计）：每轮对话 trace 以 JSON 归档到本地目录。

    与 trace 落库（MySQL / 内存）互补——落库供前端"运行观测"面板查询，
    落盘供合规留痕 / 离线审计。目录布局：{archive_dir}/{session_id}/{round_id}.json，
    字段与 GET /obs/traces/{session_id}/{round_id} 响应一致。
    archive_enabled 为假或 archive_dir 为空时只落库不落盘。
    生产部署建议挂载独立数据卷并配置绝对路径。
    """

    archive_enabled: bool = True
    archive_dir: str = "data/sessions"

    # 会话级串行化闸（services/session_lock.py）：同一 session_id 的对话轮次互斥，
    # 避免共享 user_id 的多人并发写坏历史（消息交错 / 滚动压缩竞态 / 对话状态覆盖）。
    lock_enabled: bool = True
    # 等锁上限（秒）：<= 0 表示不等待、立即可判「会话忙」；正数给一个短暂缓冲。
    lock_wait_seconds: float = 2.0
    # Redis 分布式锁 TTL（秒）：需大于单轮最长耗时（LLM 超时 × 重试），否则锁中途过期。
    lock_ttl_seconds: int = 600


class RAGSettings(BaseModel):
    # 知识库向量后端：对齐老系统默认 Milvus（HNSW/L2）。
    # milvus_uri 默认指向本地内嵌库文件（data/milvus.db，免 Docker，需 pip install -e '.[milvus]'）；
    # 生产将 milvus_uri 指向 Milvus server。未装 pymilvus 时优雅降级纯 BM25。
    # 需零依赖本地运行可显式置为 faiss（pip install -e '.[rag]'）。
    backend: str = "milvus"  # milvus | faiss
    persist_dir: str = "data/vector_store"
    collection_name: str = "dmf_agent_kb"
    # Milvus 后端（测试环境用 pymilvus 内嵌库：uri 指向本地文件；生产指向 Milvus server）
    milvus_uri: str = "data/milvus.db"
    milvus_token: str = ""
    top_k: int = 4
    chunk_size: int = 500
    # 对齐老系统 knowledge_service：重叠 = chunk_size * 20% = 100
    chunk_overlap: int = 100

    # ---- 重排（FR7 知识库检索第二阶段：cross-encoder 精排）----
    # 召回（BM25 + 向量 RRF）追求"不漏"，重排追求"排序准"。
    # 关闭 / 服务不可用 / 调用失败 时**优雅降级**为纯 RRF 顺序，不影响知识问答可用性。
    rerank_enabled: bool = True
    # 重排服务地址；留空则复用嵌入服务（DMF_LLM-EMBEDDING_BASE_URL + /rerank）
    rerank_base_url: str = ""
    rerank_model: str = "bge-reranker-base"
    rerank_api_key: str = ""
    # 送入重排的候选数（越大召回越全但越慢）；最终仍按 top_k 返回
    rerank_top_n: int = 20
    rerank_timeout: float = 20.0
    # 来源分级权重（FR7 知识库分级）：最终分数 = 重排/RRF 分数 × 该权重。
    # 权威源（口径文档/指标字典）优先于沉淀源，沉淀源优先于用户上传。
    authority_weights: Dict[str, float] = {
        "authoritative": 1.5,
        "sediment": 1.15,
        "user": 1.0,
    }

    # ---- 定时增量（Task C）：周期重建口径字典 ----
    # 仅当 enabled=True 且 interval_s>0 时启用（默认关闭，避免无谓重建）。启用后后台
    # 异步周期重建权威源口径文档，使 RAG 召回口径与语义层最新定义保持同步。
    # 零依赖实现：asyncio 周期任务，不引入 apscheduler 等第三方依赖。
    kb_auto_refresh_enabled: bool = False
    kb_auto_refresh_interval_s: int = 3600


class StarRocksSettings(BaseModel):
    """StarRocks（FE 的 MySQL 协议端口）连接配置 —— 主流程唯一取数数据源。

    调用链：语义层（MetricFlow 规范）编译语义查询 -> SQL -> 本连接直连 StarRocks 执行。
    与 MySQL 业务库（db.*）完全独立：db 存会话/知识库/观测，starrocks 存指标底表。

    注意：本对象未配置（host 为空）时取数直接失败并抛出明确错误，**不回退 mock**。
    但经 ``Settings.effective_starrocks`` 解析时，若未显式配置则自动复用 MySQL 业务库
    连接（同协议）并将方言切到 mysql——即「和现在连接的 MySQL 一个协议」，无需独立连接串。
    """

    host: str = ""
    port: int = 9030  # FE 查询端口（MySQL 协议），非 BE 的 8040
    user: str = "root"
    password: str = ""
    database: str = ""
    charset: str = "utf8mb4"
    connect_timeout: int = 10
    query_timeout: int = 60
    pool_min_size: int = 1
    pool_max_size: int = 5
    # 结果集行数上限（保护内存；超出即截断并在返回值中标注 truncated）
    max_rows: int = 200000
    # 多指标跨语义模型时生成的 JOIN 类型：full（FULL OUTER JOIN，需 StarRocks >= 2.5）| left
    join_type: str = "full"
    # 时间分组列的输出别名（前端与统计逻辑按列名识别时间轴）
    time_column_alias: str = "date"
    # 维值候选召回：过滤条件的候选值从 StarRocks 取 DISTINCT（默认关闭，避免每轮多打一次查询）
    enable_dim_value_recall: bool = False
    dim_value_recall_limit: int = 20
    # ⚠️ 预留未接线：维值候选召回目前无缓存层（每轮直查 DISTINCT），本 TTL 无人读取。
    dim_value_recall_cache_ttl: int = 300
    # SQL 方言：starrocks（默认，用 date_trunc/FULL OUTER JOIN/add_months）｜mysql（本地复用 MySQL）
    # 选择 mysql 后会自动改写：
    #   date_trunc('day', x)        -> DATE(x)
    #   date_trunc('month', x)      -> DATE_FORMAT(x, '%Y-%m-01')
    #   date_trunc('week', x)       -> DATE_SUB(DATE(x), INTERVAL WEEKDAY(x) DAY)
    #   date_trunc('quarter', x)    -> MAKEDATE(YEAR(x), 1) + INTERVAL (QUARTER(x)-1) QUARTER
    #   date_trunc('year', x)       -> DATE_FORMAT(x, '%Y-01-01')
    #   add_months(x, n)            -> DATE_ADD(x, INTERVAL n MONTH)
    #   FULL OUTER JOIN             -> LEFT JOIN ... UNION ... RIGHT JOIN（同名列靠 COALESCE 兜底）
    dialect: str = "starrocks"
    # 数据源深度探针后台看门狗：周期探活/延迟/表存量，状态翻转即告警。
    # 间隔秒（默认 60s）；置 0 关闭后台周期探活（仍保留启动一次自检 + 按需 /semantic/health）。
    probe_interval: int = 60

    @property
    def configured(self) -> bool:
        return bool(self.host and self.port)

    @property
    def probe_enabled(self) -> bool:
        return self.probe_interval > 0


class HMSettings(BaseModel):
    """【已移出主流程】指标平台（HM）接入配置 —— 仅 integrations/metric_platform 使用。

    主流程已完全脱离指标平台：要素校验/候选召回来自语义层，取数走 StarRocks。
    本配置保留给后续"从指标平台拉取元信息 -> 生成语义层清单"的离线导入功能。
    api_base 为空时相关导入功能不可用（不影响主流程）。
    """

    api_base: str | None = None
    timeout: int = 30
    # 取数接口单页条数（对齐老 HmGainData.pageSize=200000；过大内存压力大时可按需下调）
    page_size: int = 200000
    # 取数失败重试次数（网络/超时类瞬时错误，指数退避）
    retry_times: int = 2
    # 同环比默认 lagOffsetType（用户未明确 同比/环比 时，默认同比 yoy；该粒度不支持时回退 pop）
    pop_type: str = "yoy"
    # 租户鉴权（老系统两种模式）：
    # - access_token：nginx 转发模式（Access-Token 头）
    # - tenant_id / user_account / user_name：内部直连模式（X_tenant 头，base64 编码）
    access_token: str = ""
    tenant_id: str = ""
    user_account: str = ""
    user_name: str = ""


class RedisSettings(BaseModel):
    """Redis 接入（记忆分层 L1 热层 + 热点缓存；本地部署可直接启用）。

    - 不可用时（未配置 / 连接失败）所有读写优雅降级为 MySQL / 内存，不影响主链路；
    - memory_ttl：会话摘要热层过期时间（秒），到期回源 MySQL 并续期；
    - 生产建议独立实例 + 密码，并配置合理的 maxmemory-policy=allkeys-lru。
    """

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 6379
    db: int = 0
    password: str = ""
    # 连接 / 读写超时（秒），避免 Redis 抖动拖慢主链路
    socket_timeout: float = 1.0
    socket_connect_timeout: float = 1.0
    # 会话摘要（L1）热层 TTL（秒）；0 表示不过期（仅依赖 MySQL 主存）
    memory_ttl: int = 86400


class SlangSettings(BaseModel):
    """黑话词典与查询改写（三级词典 + Redis 优先缓存）。

    - enabled=False 时改写节点直接原句透传（保留链路但不改写，便于线上开关）；
    - redis_ttl：词条缓存在 Redis 的过期时间（秒），0 表示不过期；
    - mem_fallback_ttl：Redis 不可用时的进程内兜底 TTL（秒），避免每次打 MySQL；
    - max_query_len：超长问句跳过改写（防异常输入拖慢主链路）。
    """

    enabled: bool = True
    redis_ttl: int = 300
    mem_fallback_ttl: int = 60
    max_query_len: int = 512
    # 改写时是否把命中词条计入 hit_count（管理页观察词条热度）
    track_hits: bool = True


class HarnessSettings(BaseModel):
    """PRD FR15 Harness 强约束：Agent 执行外部约束壳。"""

    enabled: bool = True
    # 循环控制
    max_rounds: int = 10
    max_tool_calls: int = 20
    # token 预算（单次请求输入+输出；0 表示不限制）
    token_budget: int = 0
    # 节点级超时（秒；0 表示不限制）
    node_timeout_seconds: int = 0
    # FR15 输出约束：LLM/工具输出按声明 Schema 解析失败时的最大重试次数
    # （仅对声明了 output_schema 的动作生效；单个动作可在 registry 覆盖此值）
    output_retries: int = 1
    # 未注册工具一律拒绝（True 时仅 registry 内工具可执行）
    enforce_whitelist: bool = True
    # 强制埋点开关（默认开启，写入观测 span 与审计日志）
    force_telemetry: bool = True
    # ---- FR15 安全沙箱（harness/sandbox.SandboxCheck，已接入生产链路）----
    # 总开关：关闭则沙箱不参与任何校验（仅 FR13 输出守卫仍在）。
    sandbox_enabled: bool = True
    # 规则文件路径（可配置 = FR15 沙箱「路径可配置」）：YAML / JSON / TXT 三种格式，
    # 支持 sql / text 两层自定义危险模式（格式见 config/sandbox_rules.example.yaml）。
    # 空 = 仅用内置规则；文件不存在 / 解析失败仅告警、回退内置规则，不阻断主链路。
    # 环境变量示例：DMF_HARNESS-SANDBOX_RULE_PATH=config/sandbox_rules.yaml
    sandbox_rule_path: str = ""
    # 环境变量内联追加规则（无需改文件即可热扩）：值为 JSON 数组字符串。
    # 例：DMF_HARNESS-SANDBOX_EXTRA_TEXT_PATTERNS='["某些自定义短语"]'
    sandbox_extra_sql_patterns: List[str] = Field(default_factory=list)
    sandbox_extra_text_patterns: List[str] = Field(default_factory=list)
    # 接入点：① 取数 SQL（semantic/starrocks.fetch 的 scan_sql，兜底数据外泄/DDL）；
    #        ② 最终答复文本（synthesize_answer._finalize_answer 的 scan_text，兜底注入回声/代码执行/外联）。
    # 设计要点：SQL 层与文本层分离 —— 文本层**不含** drop/delete 等 SQL 关键字，
    # 因此「请删除该过滤条件」这类正常中文答复不会被误伤（详见 sandbox.py 头部说明）。
    # ---- Skill 执行沙箱（FR14：声明式 skill 的受治理执行，见 skills/executor.py）----
    # 上传的 skill 压缩包大小上限（字节）；超限直接拒绝，避免把大包塞进插件表。
    skill_max_file_bytes: int = 262144
    # 单个 skill 的步骤数上限（0 = 不限制）；超出直接拒绝，防止无限编排。
    skill_max_steps: int = 8
    # 压缩包解压后的总大小上限 / 包内文件数上限（防 zip 炸弹与海量小文件）。
    skill_max_package_bytes: int = 5242880
    skill_max_package_files: int = 100
    # skill 自带脚本（run_skill_script）的单次执行超时与输出截断上限。
    skill_script_timeout_seconds: int = 30
    skill_script_max_output_bytes: int = 65536


class RecoverySettings(BaseModel):
    """PRD FR6 报错分析处理：可恢复错误的限次自动修复重试策略。

    - max_retries：解析类（LLM -> 规则兜底）最多重试次数（含首次 LLM 共 2 次）
    - datasource_max_retries：数据源瞬时失败（连接/查询超时）重试次数
    - backoff_seconds：重试之间的退避间隔（秒）
    """

    enabled: bool = True
    max_retries: int = 2
    datasource_max_retries: int = 2
    backoff_seconds: float = 0.3


class CostSettings(BaseModel):
    """PRD FR11 成本可观测：LLM token 计价（按模型 / 用户 / 租户 / 意图归集）。

    - price_table：模型单价表，**单位 = 每 100 万 token 的金额**（币种见 currency）。
      键为模型名（精确匹配优先，其次最长前缀匹配，最后回落 default_*），
      值形如 `{"input": 2.0, "output": 8.0}`。
      环境变量写法：`DMF_COST-PRICE_TABLE='{"my-model":{"input":1,"output":2}}'`（JSON 字符串）。
    - default_input_price / default_output_price：未在单价表命中时的兜底单价。
    - daily_budget / monthly_budget：预算上限（0 = 不限制）；达到 alert_ratio 比例即告警。
    - 计价只做**估算**（基于响应 usage），不对接厂商账单；未启用时 cost 一律记 0。
    """

    enabled: bool = True
    currency: str = "CNY"
    price_table: Dict[str, Dict[str, float]] = Field(
        default_factory=lambda: {
            # DeepSeek（元 / 百万 token，2026-09 公开价，缓存命中等细分价未展开）
            "deepseek-chat": {"input": 2.0, "output": 8.0},
            "deepseek-reasoner": {"input": 4.0, "output": 16.0},
            # 通义千问
            "qwen-plus": {"input": 0.8, "output": 8.0},
            "qwen-max": {"input": 20.0, "output": 60.0},
            "qwen-turbo": {"input": 0.3, "output": 0.6},
            # 智谱
            "glm-4": {"input": 50.0, "output": 50.0},
            "glm-4-flash": {"input": 0.1, "output": 0.1},
            # OpenAI
            "gpt-4o": {"input": 18.0, "output": 72.0},
            "gpt-4o-mini": {"input": 1.1, "output": 4.4},
        }
    )
    default_input_price: float = 2.0
    default_output_price: float = 8.0
    daily_budget: float = 0.0
    monthly_budget: float = 0.0
    alert_ratio: float = 0.8


class SemanticSettings(BaseModel):
    """语义层定义来源（混合方案：YAML 基座 / DB 发布快照）。

    - source: "yaml"（默认，读 manifest.d 目录或单文件）| "db"
      （读 DB 最新 published 快照；无快照自动回落 YAML，不阻断取数）。
    - manifest_dir: 覆盖默认清单路径（目录或单文件）；空 = 内置 manifest.d。
    - scope_base_probe_ttl_s: 请求级装配时探测**全局链**指纹的节流窗口（秒）。
      多实例部署下各实例靠这个"拉"式探测自动收敛，不依赖广播：
      窗口内共享一次轻查询，窗口外发现指纹变化就地重建基座。
      0 = 每请求都探（最即时但每请求多一条查询）；调大 = 更省查询、收敛更慢。
    """

    source: str = "yaml"
    manifest_dir: str = ""
    scope_base_probe_ttl_s: float = 1.0
    # P2-1 查询结果缓存：同参数 TTL 内存缓存（无 Redis 依赖），命中直出。
    # enabled=False 或 ttl_sec<=0 时关闭；max_size 由 query_cache 模块默认 1024。
    query_cache_enabled: bool = True
    query_cache_ttl_sec: int = 300


class SubAgentSettings(BaseModel):
    """PRD FR10 Sub-Agent 编排：Orchestrator–Worker 模式的调度护栏。

    - enabled：是否启用子 Agent 编排能力（关闭时分解/派发节点完全透传主图）
    - wire_into_graph：是否把 `subagent_dispatch` 节点接入主图拓扑（默认 True，
      复杂问题自动走多子任务并行编排；单任务/未识别到拆分时节点完全透传，回归安全）
    - max_concurrency：子任务并行上限（Semaphore）
    - max_depth：子任务递归深度上限（深度 > 该值直接 skipped；防止无限递归）
    - subtask_timeout_seconds：单个子任务执行超时（秒），超时回收为 timeout 态
    - max_subtasks：单次编排派发的子任务数量上限，超出部分 skipped
    """

    enabled: bool = True
    wire_into_graph: bool = True
    max_concurrency: int = 4
    max_depth: int = 2
    subtask_timeout_seconds: float = 20.0
    max_subtasks: int = 6


class SkillSettings(BaseModel):
    """PRD FR14 声明式 Skill 接入对话主图：触发与匹配。

    - enabled：是否启用对话内 skill 触发（关闭时 `execute_skill` 节点完全透传主图）
    - wire_into_graph：是否把 `execute_skill` 节点接入主图拓扑（默认 True；
      未注册任何可执行 skill 时节点直接透传，拓扑行为与接线前一致，回归安全）
    - model_driven：**模型驱动优先**（默认 True，对齐 Claude Code / Codex 的机制）——
      LLM 先从 skill 清单（名 + 描述）里选一个，返回 none 即不触发；LLM 不可用或选中失败时
      回落到 `match_package` 的 triggers 确定性命中（守住降级矩阵）。
      置 False 则回到旧顺序：triggers 确定性命中优先，未命中再由 LLM 兜底（见 match_llm_fallback）。
    - match_llm_fallback：仅在 model_driven=False 时生效——triggers 未命中后是否再用 LLM 兜底选。
    - package_dir：skill 压缩包解压落盘根目录（沿用 data/ 相对路径惯例）
    - script_enabled：是否允许执行包内自带脚本（run_skill_script）；关闭即一律拒绝执行

    单次执行限额（文件大小 / 步数 / 脚本超时 / 包大小）在 `HarnessSettings`，见 `skill_*`。
    """

    enabled: bool = True
    wire_into_graph: bool = True
    model_driven: bool = True
    match_llm_fallback: bool = True
    package_dir: str = "data/skills"
    script_enabled: bool = True


class SecuritySettings(BaseModel):
    """PRD FR13 合规安全：输入/输出过滤 + 脱敏 + 审计。"""

    enabled: bool = True
    # 输入侧：提示注入 / 越权意图 / 敏感词 / 高危行为
    input_guard_enabled: bool = True
    # 输出侧：LLM 输出内容安全过滤 + 结果可信标注
    output_guard_enabled: bool = True
    # 结果脱敏（客户号 / 手机号 / 身份证 / 银行卡 / 邮箱）
    desensitize_enabled: bool = True
    # 权限模式：audit（只记录越权报告，不拦截）| enforce（拦截）
    # P0-2 先 audit；P6 切 enforce
    permission_mode: str = "audit"
    # 敏感词列表（逗号分隔；与内置规则合并）
    sensitive_words: str = ""
    # 输入最大长度（字符），超长直接拦截
    max_input_length: int = 4096
    # ---- FR13 导出治理（三个导出端点共用；详见 security/export_gate.py）----
    # 导出审批闸门：开启后导出额外要求 `export:approve` 权限点（admin 通配放行）。
    # 默认关闭 = 行为与改造前完全一致（向后兼容）。
    export_require_approval: bool = False
    # 单次导出行数上限（保护带宽与内存；超出即截断并在返回值/水印里标注 truncated）
    export_max_rows: int = 5000
    # 是否在导出件里注入水印块（导出人 / 租户 / 时间 / 行数 / 截断标记）
    export_watermark_enabled: bool = True


class AuthSettings(BaseModel):
    """认证与权限（JWT Bearer + 租户/角色）。

    - enabled=true 时 `/api/v1` 全站鉴权（`/auth/login`、`/health` 豁免）：
      请求头 `Authorization: Bearer <token>`，缺失/失效返回 401；
      测试环境置 false（conftest 已设），既有用例无需令牌。
    - 口令哈希：标准库 PBKDF2-HMAC-SHA256（无需 bcrypt 等外部依赖），
      存储格式 `pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>`。
    - jwt_secret 生产必须改为随机长串（可 DMF_AUTH-JWT_SECRET 覆盖），
      否则重启/多实例下旧令牌可被伪造。
    - 首次启动若无任何用户，自动引导：默认租户(default) + 管理员(admin)。
    """

    enabled: bool = True
    jwt_secret: str = "dmf-agent-dev-secret-change-me-in-production-32bytes"
    jwt_algorithm: str = "HS256"
    # 访问令牌有效期（分钟）；到期后由前端用 refresh_token 静默续期（见 refresh_token_ttl_days）
    access_token_ttl_minutes: int = 720
    # 刷新令牌有效期（天）；登录时一并签发，/auth/refresh 用其换取新访问令牌（滚动轮换）
    refresh_token_ttl_days: int = 14
    # 启动引导（无用户时创建）
    default_tenant_code: str = "default"
    default_tenant_name: str = "默认租户"
    default_admin_username: str = "admin"
    default_admin_password: str = "admin123"
    password_min_length: int = 6


class AgentSettings(BaseModel):
    """Agent 主流程通用阈值。"""

    # 要素识别低置信阈值（FR1/FR4）：低于该值且已识别到指标时反问澄清
    low_confidence_threshold: float = 0.5
    # 反问澄清次数上限（FR4）：同一澄清序列内最多追问 N 次，超出后优雅终止避免循环确认
    max_clarification_turns: int = 3
    # 模型上下文窗口大小（token）：上下文占比圆环的分母（当前模型的 max input tokens）。
    # 按实际接入模型调整（如 32k/64k/128k）；圆环口径 = 估算上下文 token / 该值。
    context_window_tokens: int = 32768


class PromptsSettings(BaseModel):
    """FR12 提示词 5 层 / 灰度 / 门禁 / A·B 开关。"""
    gray_enabled: bool = True
    default_gray_ratio: float = 0.1


class ModelSettings(BaseModel):
    """模型管理：库内默认模型作为运行时生效模型的收敛策略。

    ``probe_interval_s``：后台探活周期 —— 周期重读 ``llm_configs`` 的默认条目，
    变化即重建运行时生效覆盖（多实例下任一实例改默认，其余实例在周期内自动收敛，
    与语义层基座「拉」式自愈同思路）。``0`` 关闭后台探活（仅启动时 + 管理面变更即时刷新，
    单实例部署通常够用）。
    """
    probe_interval_s: float = 30.0


class Settings(BaseSettings):
    app: AppSettings = Field(default_factory=AppSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    db: MySQLSettings = Field(default_factory=MySQLSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    rag: RAGSettings = Field(default_factory=RAGSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    session: SessionSettings = Field(default_factory=SessionSettings)
    recall: RecallSettings = Field(default_factory=RecallSettings)
    starrocks: StarRocksSettings = Field(default_factory=StarRocksSettings)
    # 指标平台接入（离线元信息导入，主流程不依赖）
    hm: HMSettings = Field(default_factory=HMSettings)

    @property
    def effective_starrocks(self) -> StarRocksSettings:
        """生效的 StarRocks 连接配置（解析「显式配置 → 复用 MySQL」）。

        StarRocks FE 走 **MySQL 线协议**（默认 9030 端口），与业务 MySQL **同一协议**。
        因此当未显式配置 StarRocks（host 为空）时，自动复用 MySQL 业务库连接，并把
        方言切到 ``mysql``（compiler 据此把 date_trunc / add_months / FULL OUTER JOIN
        自动改写为 MySQL 兼容写法）——即「和现在连接的 MySQL 一个协议」，无需独立连接串。

        显式配置 ``DMF_STARROCKS-HOST`` 后优先级最高，回退不发生（生产用独立 StarRocks）。
        仅当 MySQL 也未启用（`DMF_DB-ENABLED=false` 内存模式）时不回退，保留原始未配置对象。
        """
        sr = self.starrocks
        if sr.configured:
            return sr
        db = self.db
        if getattr(db, "enabled", False) and getattr(db, "host", ""):
            return StarRocksSettings(
                host=db.host,
                port=db.port,
                user=db.user,
                password=db.password,
                database=db.database,
                charset="utf8mb4",
                connect_timeout=sr.connect_timeout,
                query_timeout=sr.query_timeout,
                pool_min_size=sr.pool_min_size,
                pool_max_size=sr.pool_max_size,
                max_rows=sr.max_rows,
                join_type=sr.join_type,
                time_column_alias=sr.time_column_alias,
                enable_dim_value_recall=sr.enable_dim_value_recall,
                dim_value_recall_limit=sr.dim_value_recall_limit,
                dialect="mysql",
            )
        return sr
    # Redis（记忆分层 L1 热层 + 热点缓存；不可用时自动降级，不阻断主链路）
    redis: RedisSettings = Field(default_factory=RedisSettings)
    # 认证与权限（JWT Bearer + 租户/角色；全站鉴权，可开关）
    auth: AuthSettings = Field(default_factory=AuthSettings)
    # 黑话词典与查询改写（三级词典：行业级 / 公司级 / 用户级）
    slang: SlangSettings = Field(default_factory=SlangSettings)
    # Harness 强约束（PRD FR15）
    harness: HarnessSettings = Field(default_factory=HarnessSettings)
    # 合规安全（PRD FR13）
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    # 报错限次自愈（PRD FR6）
    recovery: RecoverySettings = Field(default_factory=RecoverySettings)
    # Sub-Agent 编排（PRD FR10）
    subagent: SubAgentSettings = Field(default_factory=SubAgentSettings)
    # 声明式 Skill 接入主图（PRD FR14）
    skill: SkillSettings = Field(default_factory=SkillSettings)
    # 成本计价与预算（PRD FR11）
    cost: CostSettings = Field(default_factory=CostSettings)
    # 语义层定义来源（混合方案：YAML 基座 / DB 发布快照）
    semantic: SemanticSettings = Field(default_factory=SemanticSettings)
    # 提示词治理（PRD FR12：5 层 / 灰度 / 门禁 / A·B）
    prompts: PromptsSettings = Field(default_factory=PromptsSettings)
    # 模型管理（对话 LLM 配置入库；默认模型覆盖环境变量 DMF_LLM-* 生效）
    model: ModelSettings = Field(default_factory=ModelSettings)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="DMF_",
        # 分层分隔符用连字符 "-"（如 DMF_LLM-MODEL）：Python 字段名只用下划线，
        # 不会与分隔符冲突，故字段名本身含下划线（LOW_CONFIDENCE_THRESHOLD 等）也无歧义。
        env_nested_delimiter="-",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
