"""外部指标平台（HM）接口注册表 —— 单一事实源。

依据 `doc/指标平台接口.docx` 重新规整：所有 HM 接口的方法 / 路径 / 说明集中在此，
业务代码（metadata/client.py、tools/hm_api.py）只引用本注册表，不再散落硬编码 URL。

约定：
- path 不含 host（host 取 `settings.hm.api_base`），鉴权头见 metadata.client.build_hm_headers
- 所有路径带统一网关前缀 `v1/`（与现网一致；docx 里省略了该前缀）
- `used` 标注 v2 当前是否已接入调用（未接入的接口先登记，后续按需要接入）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

# HM 统一网关前缀
API_PREFIX = "v1"


def _p(suffix: str) -> str:
    return f"{API_PREFIX}{suffix}"


@dataclass(frozen=True)
class HmApi:
    method: str  # GET | POST
    path: str
    description: str
    used: bool = False


# 接口目录（key 为 v2 内部使用的稳定标识）
HM_APIS: Dict[str, HmApi] = {
    # 一、用户
    "user_permission_list": HmApi(
        "GET", _p("/user-management"),
        "根据指标用户名分页查询用户授权列表（getUserPermissionList）",
    ),
    # 二、维度
    "dim_by_value": HmApi(
        "POST", _p("/standard-api/sa/dim/getByValue"),
        "根据维度值反查维度（getDimByValue）：exactList + chatList", used=True,
    ),
    "dim_values": HmApi(
        "POST", _p("/standard-api/sa/dim/getValues"),
        "获取维度值列表（getValues）",
    ),
    # 三、示例数据（few-shot）
    "examples": HmApi(
        "POST", _p("/standard-api/sa/examples/select-examples"),
        "根据用户提问查询相似样本（selectExamples）",
    ),
    # 四、指标取数
    "gain_data": HmApi(
        "POST", _p("/standard-api/sa/gainData/simplified"),
        "指标取数-简化版-返回列原始类型值（getQueryResultSimplifiedWithColType）", used=True,
    ),
    "metrics_privilege": HmApi(
        "POST", _p("/standard-api/sa/gainData/metricsPrivilege"),
        "指标权限查询（listMetricsPrivilege）",
    ),
    # 五、元信息
    "metrics_meta": HmApi(
        "POST", _p("/standard-api/sa/metaInfo/metrics"),
        "指标元信息（getMetricsMetaInfo）", used=True,
    ),
    "dimension_meta": HmApi(
        "POST", _p("/standard-api/sa/metaInfo/dimension"),
        "维度元信息（getDimensionMetaInfo）", used=True,
    ),
    "vector_topk": HmApi(
        "POST", _p("/standard-api/sa/metaInfo/getVectorTopK"),
        "指标向量 TopK 召回（getVectorTopN）",
    ),
    "metrics_blood": HmApi(
        "POST", _p("/standard-api/sa/metricsBlood"),
        "指标血缘（getMetricsBlood，因子归因所需）",
    ),
    # 六、黑话
    "cant_list": HmApi(
        "POST", _p("/aigcCantName/listBySa"),
        "获取黑话 mapping 表列表（dm/dc，listBySa）", used=True,
    ),
    # 七、事件
    "sa_event_save": HmApi(
        "POST", _p("/sa/event/save"), "保存 sa 事件",
    ),
    # 八、知识检索
    "document_find_relevant": HmApi(
        "POST", _p("/standard-api/sa/document/find-relevant"),
        "文档检索（findRelevant）",
    ),
    "metadata_find_relevant": HmApi(
        "POST", _p("/standard-api/sa/metadata/find-relevant"),
        "元信息检索（findRelevantMetadata）",
    ),
    # 九、归因
    "metrics_conclusion": HmApi(
        "POST", _p("/analysis/getMetricsConclusion"),
        "获取指标的归因结论（getMetricsConclusion）",
    ),
}


def hm_path(key: str) -> str:
    """按稳定标识取接口路径；未登记抛 KeyError（及早暴露接口名拼写错误）。"""
    return HM_APIS[key].path
