"""黑话词典持久化模型（三级：行业级 / 公司级 / 用户级）+ 查询改写记录。

对齐 PRD §5.3.1「黑话字典结构」：
    词条 {term, standard, type, priority, source, status, version}
    匹配按最长优先 + 优先级排序

与既有 `cant_entries`（老 aigc_cant_name 表，仅 dim / dim_value）的关系：
本表是**统一升级版**——把维度黑话、维值黑话、指标黑话、术语同义词、停用词
收敛到一张表，并引入三级作用域（老系统 domain=dm/dc 两级 -> 三级推广）。
旧表对应的数据由迁移 0010 搬入本表（scope=industry），旧表保留只读。

三级作用域（解析时**同名以高层级为准**）：
    user（用户级，仅本人可见） > tenant（公司级，租户内共享） > industry（行业级，平台内置）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

# ---------- 作用域（层级）----------
SCOPE_INDUSTRY = "industry"  # 行业级：平台内置，全局共享
SCOPE_TENANT = "tenant"  # 公司级：租户（公司）内共享
SCOPE_USER = "user"  # 用户级：仅本人可见
ALL_SCOPES = (SCOPE_INDUSTRY, SCOPE_TENANT, SCOPE_USER)
SCOPE_LABELS = {SCOPE_INDUSTRY: "行业级", SCOPE_TENANT: "公司级", SCOPE_USER: "用户级"}
# 优先级权重：数字越大越优先（同名覆盖用）
SCOPE_RANK = {SCOPE_USER: 3, SCOPE_TENANT: 2, SCOPE_INDUSTRY: 1}

# ---------- 词条类型 ----------
ENTRY_METRIC = "metric"  # 指标黑话：口语 -> 标准指标名
ENTRY_DIMENSION = "dimension"  # 维度黑话：口语 -> 标准维度名
ENTRY_DIM_VALUE = "dim_value"  # 枚举值黑话：口语 -> 维度 + 维值（落成过滤）
ENTRY_TERM = "term"  # 术语 / 同义词 / 缩写：仅做替换与检索扩展
ENTRY_STOP = "stop"  # 停用词：命中即从问句中删除（口语噪声）
ALL_ENTRY_TYPES = (
    ENTRY_METRIC,
    ENTRY_DIMENSION,
    ENTRY_DIM_VALUE,
    ENTRY_TERM,
    ENTRY_STOP,
)
ENTRY_TYPE_LABELS = {
    ENTRY_METRIC: "指标",
    ENTRY_DIMENSION: "维度",
    ENTRY_DIM_VALUE: "枚举值",
    ENTRY_TERM: "术语",
    ENTRY_STOP: "停用",
}

# ---------- 改写模式 ----------
MODE_EXPAND = "expand"  # 保留原词，句尾追加标准名（默认；保留"比值/时间"等语义线索）
MODE_REPLACE = "replace"  # 原文替换为标准名
MODE_STOP = "stop"  # 从问句中删除
ALL_REWRITE_MODES = (MODE_EXPAND, MODE_REPLACE, MODE_STOP)
REWRITE_MODE_LABELS = {
    MODE_EXPAND: "扩展（保留原词）",
    MODE_REPLACE: "替换",
    MODE_STOP: "删除",
}

# ---------- 状态 ----------
STATUS_DRAFT = "draft"  # 草稿：不参与线上解析
STATUS_PUBLISHED = "published"  # 已发布：参与线上解析
STATUS_DISABLED = "disabled"  # 已停用：保留记录、不参与解析
ALL_STATUSES = (STATUS_DRAFT, STATUS_PUBLISHED, STATUS_DISABLED)
STATUS_LABELS = {
    STATUS_DRAFT: "草稿",
    STATUS_PUBLISHED: "已发布",
    STATUS_DISABLED: "已停用",
}

# ---------- 来源 ----------
SOURCE_BUILTIN = "builtin"  # 平台内置种子
SOURCE_MANUAL = "manual"  # 管理面手工录入
SOURCE_IMPORTED = "imported"  # 批量导入
ALL_SOURCES = (SOURCE_BUILTIN, SOURCE_MANUAL, SOURCE_IMPORTED)


def is_valid_scope(scope: str) -> bool:
    return scope in ALL_SCOPES


def is_valid_entry_type(entry_type: str) -> bool:
    return entry_type in ALL_ENTRY_TYPES


def is_valid_rewrite_mode(mode: str) -> bool:
    return mode in ALL_REWRITE_MODES


def is_valid_status(status: str) -> bool:
    return status in ALL_STATUSES


def layer_key(scope: str, tenant_id: str = "", user_id: str = "") -> str:
    """层级缓存键（Redis 与进程内共用）。

    行业级用 `-`（无归属），公司级带 tenant，用户级带 tenant + user；
    这样一次查询最多取 2 个 key（公司级 + 行业级）或 3 个（用户级），
    写入时按词条归属精确失效，不会误伤其他层级。
    """
    if scope == SCOPE_USER:
        return f"u:{tenant_id}:{user_id}"
    if scope == SCOPE_TENANT:
        return f"t:{tenant_id}"
    return "i:-"


class SlangEntryData(BaseModel):
    """黑话词条（三级词典的统一持久化形态）。"""

    id: Optional[int] = None
    scope: str = SCOPE_INDUSTRY
    tenant_id: str = ""
    user_id: str = ""
    term: str  # 黑话 / 口语 / 缩写（匹配源）
    standard: str = ""  # 标准名（指标名 / 维度名 / 术语；stop 类型为空）
    entry_type: str = ENTRY_TERM
    dim_value: str = ""  # entry_type=dim_value 时逗号分隔的维值
    rewrite_mode: str = MODE_EXPAND
    priority: int = 100
    source: str = SOURCE_MANUAL
    status: str = STATUS_DRAFT
    version: int = 1
    confidence: float = 1.0
    hit_count: int = 0
    remark: str = ""
    created_by: str = ""
    updated_by: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def effective_mode(self) -> str:
        """实际改写模式：stop 类型强制 stop；dim_value 强制 expand（维值要追加不能替换）。"""
        if self.entry_type == ENTRY_STOP or self.rewrite_mode == MODE_STOP:
            return MODE_STOP
        if self.entry_type == ENTRY_DIM_VALUE:
            return MODE_EXPAND
        if self.rewrite_mode == MODE_REPLACE:
            return MODE_REPLACE
        return MODE_EXPAND

    def expanded_terms(self) -> List[str]:
        """标准名展开（dim_value 用维值；其余用 standard）。"""
        if self.entry_type == ENTRY_DIM_VALUE:
            return [v.strip() for v in (self.dim_value or "").split(",") if v.strip()]
        return [self.standard] if self.standard else []

    def to_cache(self) -> Dict[str, Any]:
        """Redis 缓存形态（精简字段，够解析用；避免缓存整个 row）。"""
        return {
            "id": self.id,
            "scope": self.scope,
            "term": self.term,
            "standard": self.standard,
            "entry_type": self.entry_type,
            "dim_value": self.dim_value,
            "rewrite_mode": self.rewrite_mode,
            "priority": self.priority,
            "source": self.source,
            "status": self.status,
        }

    @classmethod
    def from_cache(cls, row: Dict[str, Any]) -> "SlangEntryData":
        return cls(
            id=row.get("id"),
            scope=str(row.get("scope") or SCOPE_INDUSTRY),
            term=str(row.get("term") or ""),
            standard=str(row.get("standard") or ""),
            entry_type=str(row.get("entry_type") or ENTRY_TERM),
            dim_value=str(row.get("dim_value") or ""),
            rewrite_mode=str(row.get("rewrite_mode") or MODE_EXPAND),
            priority=int(row.get("priority") or 0),
            source=str(row.get("source") or SOURCE_MANUAL),
            status=str(row.get("status") or STATUS_PUBLISHED),
        )


class SlangVersionData(BaseModel):
    """词条版本审计记录（支持版本对比与回滚）。"""

    id: Optional[int] = None
    entry_id: int
    version: int
    change_type: str  # create | update | publish | disable | delete | rollback
    before_json: Optional[Dict[str, Any]] = None
    after_json: Optional[Dict[str, Any]] = None
    operator: str = ""
    created_at: Optional[datetime] = None


class SlangHit(BaseModel):
    """一次命中（改写记录的单条）。"""

    term: str
    standard: str
    entry_type: str
    scope: str
    rewrite_mode: str
    priority: int = 100
    entry_id: Optional[int] = None
    span: Optional[List[int]] = None  # 命中区间 [start, end)
    # expand 模式要追加的名字（dim_value 命中时为展开后的维值，其余为标准名）
    expand: List[str] = Field(default_factory=list)


class SlangPreviewResult(BaseModel):
    """试改写结果（管理页「在线试改写」与改写节点共用）。"""

    original: str
    rewritten: str
    hits: List[SlangHit] = Field(default_factory=list)
    mode_counts: Dict[str, int] = Field(default_factory=dict)
    degraded: bool = False
    error: str = ""
    elapsed_ms: float = 0.0
    cache_source: str = ""  # redis | mysql | memory | none（观测用）


class SlangImportRequest(BaseModel):
    """批量导入请求（支持 dry_run 预检）。"""

    entries: List[Dict[str, Any]] = Field(default_factory=list)
    scope: str = SCOPE_INDUSTRY
    tenant_id: str = ""
    user_id: str = ""
    status: str = STATUS_DRAFT
    rewrite_mode: str = MODE_EXPAND
    source: str = SOURCE_IMPORTED
    dry_run: bool = True
    overwrite: bool = False  # 同层同类型同词已存在时是否覆盖


class SlangImportReport(BaseModel):
    total: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    invalid: int = 0
    conflicts: List[Dict[str, Any]] = Field(default_factory=list)
    errors: List[Dict[str, Any]] = Field(default_factory=list)


class SlangUpsert(BaseModel):
    """管理面新增 / 覆盖一条词条（`POST /slang`）。

    `scope` 由调用方给出，但**非 admin 只能落到本人 user 级**（router 做守卫）；
    `tenant_id` / `user_id` 由 router 按当前身份补全，不接受越权指定。
    """

    scope: str = SCOPE_USER
    term: str
    standard: str = ""
    entry_type: str = ENTRY_TERM
    dim_value: str = ""
    rewrite_mode: str = MODE_EXPAND
    priority: int = 100
    status: str = STATUS_DRAFT
    remark: str = ""
    # 仅鉴权关闭（本地调试）时允许显式指定归属；鉴权开启时被 router 覆盖为令牌身份
    tenant_id: str = ""
    user_id: str = ""


class SlangPatch(BaseModel):
    """部分更新（`PUT /slang/{id}`）；未传字段不动。

    `version` 为乐观锁：与库里当前版本不一致 → 409（防并发覆盖）。
    """

    term: Optional[str] = None
    standard: Optional[str] = None
    entry_type: Optional[str] = None
    dim_value: Optional[str] = None
    rewrite_mode: Optional[str] = None
    priority: Optional[int] = None
    status: Optional[str] = None
    remark: Optional[str] = None
    version: Optional[int] = None


class SlangPreviewRequest(BaseModel):
    """在线试改写（`POST /slang/preview`）。"""

    query: str
    # 视角：以某个租户 / 用户身份解析（默认取当前调用者身份）
    tenant_id: str = ""
    user_id: str = ""
    include_draft: bool = True  # 试改写默认带上草稿，便于"发布前预演"
    # 额外内联词条（未落库也能试），格式同 SlangUpsert
    extra_entries: List[Dict[str, Any]] = Field(default_factory=list)


class SlangRollbackRequest(BaseModel):
    """回滚到指定版本（`POST /slang/{id}/rollback`，P1）。"""

    version: int
    remark: str = ""


class SlangRewriteFeedback(BaseModel):
    """改写结果反馈（`POST /slang/rewrite/feedback`，误改写率看板数据源）。

    `original` 由后端归一化后取哈希，作为同一问句的反馈去重键，
    无需前端计算 hash；`is_correct=False` 即"误改写"。
    `cancel=True` 时撤销上一次反馈（把事件 feedback 置 NULL），忽略 `is_correct`。
    后端定位优先级：message_id（与状态回显同口径）> query_hash。
    """

    original: str
    is_correct: bool = True
    note: str = ""
    session_id: str = ""
    message_id: str = ""
    cancel: bool = False
