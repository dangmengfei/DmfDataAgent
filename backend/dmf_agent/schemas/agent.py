"""Agent 结构化输出模型（LLM with_structured_output 用）。"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class IntentResult(BaseModel):
    """意图识别结果（2026-08-27 迁移自老系统 intention_recognition.py 的插件调度判定）。

    老系统插件字母 -> v2 字段映射：
    - A 取数插件     -> intent="metric_query"
    - F 维度查询插件 -> intent="dimension_query"
    - D 指标归因插件 -> intent="diagnosis"（老系统单一 D 会自动补前置取数，v2 归因同样先走取数链路）
    - B 绘图插件     -> skills=["chart_visualization"]
    - C 数据解读插件 -> skills=["data_interpretation"]
    - E 复杂计算插件 -> skills=["complex_calculation"]
    """

    intent: Literal[
        "metric_query",
        "dimension_query",
        "diagnosis",
        "knowledge_qa",
        "help",
        "exit",
        "chitchat",
        "create",
    ]
    # 意图置信度 0~1（明确命中高、靠兜底关键词低）；低置信可触发澄清
    confidence: Optional[float] = Field(None, ge=0, le=1)
    # 查询后需追加执行的技能（有序），对应老系统插件序列中的 B/C/E
    skills: List[Literal["chart_visualization", "data_interpretation", "complex_calculation"]] = (
        Field(default_factory=list)
    )
    # 涉及多个独立问题/多个非相邻时间点/多指标复杂运算，需拆分多次取数（对应老系统 plugin 中 A 重复出现）
    multi_query: bool = Field(False)
    reason: str = ""


class TimeRange(BaseModel):
    start: Optional[str] = Field(None, description="开始日期，如 2026-08-19")
    end: Optional[str] = Field(None, description="结束日期，如 2026-08-25")
    granularity: Optional[str] = Field(None, description="时间粒度：day | week | month")


class FilterCondition(BaseModel):
    field: str = Field(..., description="过滤字段/维度名")
    operator: str = Field("=", description="操作符 = != in not_in > <")
    values: List[str] = Field(default_factory=list)


class MetricThresholdFilter(BaseModel):
    """指标级阈值过滤（聚合后 HAVING 语义，区别于维度过滤）。"""

    metric: str = Field(..., description="指标名（须在候选指标集中）")
    operator: Literal[">", "<", ">=", "<=", "=", "==", "!="] = Field(
        ">", description="比较操作符"
    )
    value: float = Field(..., description="阈值数值（中文万/亿由调用方归一为数值）")


class QueryElements(BaseModel):
    """查询要素抽取结果。

    迁移自老系统 get_element 综合取数（指标/维度/过滤 N 合一）：
    plugin/get_indicators/get_element/get_element.py
    """

    metrics: List[str] = Field(default_factory=list, description="业务指标中文名列表")
    # FR8 未命中信号：用户问题中明确提到的指标名，但候选集（语义层）里没有对应项。
    # 独立于 metrics 承载，保证 metrics 仍严格来自候选集（不影响取数链路），
    # 同时让系统能据此**引导用户创建**（不自动创建）。
    unrecognized_metrics: List[str] = Field(
        default_factory=list,
        description="用户提到但候选集未收录的指标名（用于引导创建，不得填入 metrics）",
    )
    dimensions: List[str] = Field(default_factory=list, description="分组/展示维度")
    filters: List[FilterCondition] = Field(default_factory=list)
    # 指标级阈值过滤（聚合后 HAVING，如 GMV>100万）；区别于维度过滤 filters
    metric_filters: List[MetricThresholdFilter] = Field(default_factory=list)
    time_range: Optional[TimeRange] = None
    # 排序：形如 "<候选指标名> desc|asc"，无排序要求为 null（中文"前N/TopN/最高"由后端归一）
    order_by: Optional[str] = Field(
        None, description="排序列与方向，如 'GMV desc'；只能引用候选指标名，无排序为 null"
    )
    # TopN 限制（前N / TopN / N名）；无限制为 null
    limit: Optional[int] = Field(None, ge=1, description="TopN 数量，如前10/Top10 填 10；否则 null")
    # 排名范围（FR1）：global 全局排名（默认）| group 时间序列组内排名
    # （如"各分行每月存款余额排名"= 每个月内部各自排名，而非全期间混排）
    rank_scope: Optional[str] = Field(
        None, description="排名范围：global 全局 | group 组内（按时间周期分别排名）；无排名为 null"
    )
    # 要素识别置信度 0~1（指标/维度明确命中候选则高，模糊则低）；低置信触发澄清
    confidence: Optional[float] = Field(None, ge=0, le=1)
    period_comparison: bool = Field(False, description="是否需要同比/环比")
    # 同环比类型（老 get_pop：wow 周同比 / mom 环比 / qoq 季同比 / yoy 同比 / pop 通用）。
    # 用户未明确时留空，取数层按时间粒度给默认值（见 HMSettings.pop_type）。
    period_comparison_type: Optional[str] = Field(
        None, description="同环比类型：wow | mom | qoq | yoy | pop（可空）"
    )
    # 同环比可**并存**（"和上月比、也和去年同期比"）-> 多个平移窗口，各自输出增长值/增长率。
    # 单类型时与 period_comparison_type 保持一致，便于消费方二选一。
    period_comparison_types: List[str] = Field(
        default_factory=list,
        description="同环比类型列表，可并存（如 ['yoy','mom']）；无同环比为空列表",
    )


class MetricAnalysisConfig(BaseModel):
    """二次分析单指标配置（迁移自老 prompt_analysis.py 的 metric_result 项）。"""

    metric_name: str = Field(..., description="需要聚合的指标中文名")
    agg: str = Field("sum", description="聚合方式：sum | avg | max | min | median")
    filter_operator: str = Field("", description="指标过滤操作符：>/</=/>=/<=")
    condition_value: str = Field("", description="指标过滤条件值")
    orderby: str = Field("", description="排序方式：desc | asc")
    limit: str = Field("", description="限制个数（top-n）")
    proportion: bool = Field(False, description="是否计算占比")


class AdvancedAnalysis(BaseModel):
    """二次分析配置（迁移自老 get_advanced_analysis 的 AnalysisPrompt 输出）。

    触发条件见 analyze_data._needs_secondary_analysis（老 if_secondary_analysis：
    聚合/求均值/求和/top-n/占比等复杂分析需求）。
    """

    metric_result: List[MetricAnalysisConfig] = Field(default_factory=list)
    # 分组聚合字段（取自维度与时间粒度字段）
    groupby_filed: List[str] = Field(default_factory=list)
    # 排名方式：global 全局排名 / group 组内排名
    ranking_method: str = Field("", description="global | group（可空）")


class MetricDraftProposal(BaseModel):
    """LLM 为 `create` 意图提出的指标定义草稿（FR8：用户用自然语言定义新指标）。

    仅承载口径核心字段；落库时由治理层派生 metric_key 并补全版本/状态。
    - simple：依赖一个 measure（type_params.measure）
    - ratio：分子/分母（type_params.numerator/denominator，可引用指标名或 measure 名）
    - derived：表达式 + 引用指标列表（type_params.expr/metrics）
    """

    name: str = Field(..., description="指标中文名，如『月活客户数』")
    description: str = Field("", description="指标口径/业务含义说明")
    metric_type: Literal["simple", "ratio", "derived"] = Field(
        "simple", description="simple | ratio | derived"
    )
    measure: Optional[str] = Field(
        None, description="simple 指标的度量名（须在语义层 measures 中真实存在）"
    )
    numerator: Optional[str] = Field(None, description="ratio 分子（指标名或 measure 名）")
    denominator: Optional[str] = Field(None, description="ratio 分母（指标名或 measure 名）")
    expr: Optional[str] = Field(None, description="derived 指标计算表达式")
    metrics: List[str] = Field(
        default_factory=list, description="derived 指标引用的子指标/度量名列表"
    )
    unit: str = Field("", description="单位，如 元/人/%，无则空")
    aliases: List[str] = Field(default_factory=list, description="别名（黑话/简称）")
    owner: str = Field("", description="指标负责人/提出人，无则空")
