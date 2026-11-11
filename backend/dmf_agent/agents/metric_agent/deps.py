"""Agent 节点运行依赖（显式注入，替代老系统的 injector DI + 线程共享资源）。

主流程元信息与取数来源：
- semantic_layer  : 语义层（指标/维度/别名/白名单校验）—— 唯一元信息入口
- metric_engine   : MetricFlow 语义引擎（语义查询 -> SQL -> StarRocks）

注意：本文件**不再引入任何指标平台（HM）相关依赖**。指标平台接入见
dmf_agent/integrations/metric_platform（离线元信息导入，主流程不感知）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from dmf_agent.db.base import ConversationStateRepository, SemanticRegistryRepository

from langchain_core.language_models import BaseChatModel

from dmf_agent.config.settings import AgentSettings, RecoverySettings, Settings, SkillSettings, SubAgentSettings
from dmf_agent.db.base import DomainMemoryRepository, FewShotRepository, UserPreferenceRepository
from dmf_agent.experience.store import ExampleStore
from dmf_agent.rag.retriever import KnowledgeRetriever
from dmf_agent.recall.black_talk import CantDictionary
from dmf_agent.recall.dimension import DimensionRecaller
from dmf_agent.recall.filter import FilterRecaller
from dmf_agent.recall.recall import MetricRecaller
from dmf_agent.semantic.engine import MetricFlowEngine
from dmf_agent.semantic.layer import SemanticLayer
from dmf_agent.semantic.scope import current_scope


# ---------- 请求级作用域覆盖（设计说明见 dmf_agent/semantic/scope.py） ----------
# `semantic_layer` / `metric_engine` 在 dataclass 里仍是普通字段（关键字构造、类型标注、
# repr、默认值都不变），只是在类体外被替换成 property：
#   - **读**：先看本请求的 ContextVar 覆盖（租户 overlay），再回落进程级基座；
#   - **写**：落到 `_base_*` 存储 —— 热更新"就地改写"的语义完全不变。
# 这样 ~17 处节点里的 `deps.semantic_layer` 一行都不用改，却拿到了请求级隔离。


def _get_semantic_layer(self) -> Optional[SemanticLayer]:
    scope = current_scope()
    if scope is not None and scope.layer is not None:
        return scope.layer
    return getattr(self, "_base_semantic_layer", None)


def _set_semantic_layer(self, value: Optional[SemanticLayer]) -> None:
    self._base_semantic_layer = value


def _get_metric_engine(self) -> Optional[MetricFlowEngine]:
    scope = current_scope()
    if scope is not None and scope.engine is not None:
        return scope.engine
    return getattr(self, "_base_metric_engine", None)


def _set_metric_engine(self, value: Optional[MetricFlowEngine]) -> None:
    self._base_metric_engine = value


@dataclass
class AgentDeps:
    settings: Settings
    # 返回 None 表示 LLM 未配置，节点自动降级规则/模板模式
    get_chat_model: Callable[[], Optional[BaseChatModel]]
    # 语义层（指标/维度解析、粒度与同环比白名单、维度可用性校验）
    semantic_layer: Optional[SemanticLayer] = None
    # MetricFlow 语义引擎（编译 SQL + StarRocks 取数）
    metric_engine: Optional[MetricFlowEngine] = None
    # 知识检索器（RAG）
    retriever: Optional[KnowledgeRetriever] = None
    # 指标候选召回（full/black_talk/part 三路，迁移自老系统 candidates_metric）
    recaller: Optional[MetricRecaller] = None
    # 维度候选召回（迁移自老系统 candidates_dim）
    dim_recaller: Optional[DimensionRecaller] = None
    # 过滤候选召回（数据源为语义层维值/黑话词典）
    filter_recaller: Optional[FilterRecaller] = None
    # 维度/维值黑话词典（迁移自老系统 candidates_cant，cantType=dim/dim_value）
    # 注：已被 slang_resolver 统一取代，保留仅供旧链路只读兼容
    cant_dict: Optional[CantDictionary] = None
    # 三级黑话词典解析器 + 查询改写（PRD §5.3.1；rewrite_query 节点的数据源）
    slang_resolver: Optional[Any] = None
    # few-shot 经验库（Phase 7：动态示例召回进 Prompt；未配置时跳过）
    example_store: Optional[ExampleStore] = None
    # 点赞样本库（few-shot 数据源；意图识别阶段 BM25 召回注入 prompt；未配置时跳过）
    few_shot_repo: Optional[FewShotRepository] = None
    # 领域知识记忆（L3，团队 / 域级沉淀；意图识别阶段与 few-shot 合并召回；未配置时跳过）
    domain_memory_repo: Optional[DomainMemoryRepository] = None
    # 用户偏好（L2，按用户长期生效；意图识别阶段与 L3 一并并行召回重排；未配置时跳过）
    user_pref_repo: Optional[UserPreferenceRepository] = None
    # Harness 强约束壳（PRD FR15）：工具白名单/参数校验/循环控制/审计
    harness: Optional[Any] = None
    # 合规安全层（PRD FR13）：输入/输出过滤、脱敏、权限审计
    security: Optional[Any] = None
    # 错误样本池（PRD FR6）：节点级报错分类 + 限次自愈样本回写
    error_sample_repo: Optional[Any] = None
    # 结构化对话状态仓储（P0-6 多轮对话状态管理）：反问澄清节点读取 prior 澄清次数以实施上限
    conv_state_repo: Optional[ConversationStateRepository] = None
    # 语义注册中心仓储（P1-6 / FR8 create 意图）：落指标/维度草稿、提交审核、发布、回滚
    semantic_registry_repo: Optional[SemanticRegistryRepository] = None
    # 插件注册中心（FR14）：主图 `execute_skill` 节点据此列出可执行的声明式 skill
    plugin_registry: Optional[Any] = None
    # Skill 沙箱执行器（FR14）：与 `AppRuntime.skill_executor` 是同一实例
    skill_executor: Optional[Any] = None

    @property
    def recovery_settings(self) -> RecoverySettings:
        """自愈配置；settings 为 None（测试/降级）时回落默认值。"""
        if self.settings is not None:
            return self.settings.recovery
        return RecoverySettings()

    @property
    def agent_settings(self) -> AgentSettings:
        """Agent 通用阈值；settings 为 None（测试/降级）时回落默认值。"""
        if self.settings is not None:
            return self.settings.agent
        return AgentSettings()

    @property
    def subagent_settings(self) -> SubAgentSettings:
        """Sub-Agent 编排配置；settings 为 None（测试/降级）时回落默认值。"""
        if self.settings is not None:
            return self.settings.subagent
        return SubAgentSettings()

    @property
    def skill_settings(self) -> SkillSettings:
        """声明式 Skill 触发配置；settings 为 None（测试/降级）时回落默认值。"""
        if self.settings is not None:
            return self.settings.skill
        return SkillSettings()


# 类体外替换为 property：必须在 `@dataclass` 装饰之后执行 —— 装饰期读到的仍是被注解的
# 默认值（None），生成的 `__init__` 里 `self.semantic_layer = <入参>` 走 setter，
# 因此 `AgentDeps(semantic_layer=...)` 的关键字构造行为完全不变。
AgentDeps.semantic_layer = property(_get_semantic_layer, _set_semantic_layer)
AgentDeps.metric_engine = property(_get_metric_engine, _set_metric_engine)
