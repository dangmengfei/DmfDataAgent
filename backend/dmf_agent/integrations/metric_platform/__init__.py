"""指标平台（HM）接入层 —— **不参与主流程**，供后续"元信息导入"能力使用。

主流程现状（2026-09 起，主流程已完全脱离指标平台）：
- 元信息（指标/维度/别名/支持粒度）来自语义清单 `dmf_agent/semantic/manifest.yml`
- 取数：语义查询 -> MetricFlow 规范编译器 -> StarRocks SQL -> 直连执行
- 因此 Agent 节点不再引用本包的任何模块

本包保留的内容与用途：
- `client.py`     : HM 元数据/维值/黑话接口客户端（异步 httpx + 双模式鉴权）
- `endpoints.py`  : HM 接口注册表（单一事实源，含方法/路径/是否已接入）
- `gain_api.py`   : HM 取数（gainData/simplified）请求组装与响应解析（历史实现，保留参考）
- `loader.py` / `mapper.py` / `seed_metrics.json` : HM 指标元信息 -> 本地快照（metric_definitions）
- `__main__.py`   : `python -m dmf_agent.integrations.metric_platform --source hm|file` 导入快照

后续要开发的"从指标平台拉取元信息到语义层"的落点就在本包：
1. 用 `HmMetaClient.fetch_metrics()/fetch_dimensions()` 拉取平台元信息；
2. 生成 `semantic/manifest.yml` 的**草稿**（指标 -> metrics 段；measure/dimension 的物理列
   需人工或按平台血缘补齐 `expr`，无法自动推断）；
3. 通过 `python -m dmf_agent.semantic check` 校验后启用。
配置见 `Settings.hm`（DMF_HM-*），未配置时本包功能不可用，但完全不影响主流程。
"""

from dmf_agent.integrations.metric_platform.client import HmMetaClient, tenant_encode
from dmf_agent.integrations.metric_platform.loader import MetricMetadataLoader
from dmf_agent.integrations.metric_platform.mapper import METRICS_TYPE_LABELS, hm_metric_to_data

__all__ = [
    "HmMetaClient",
    "METRICS_TYPE_LABELS",
    "MetricMetadataLoader",
    "hm_metric_to_data",
    "tenant_encode",
]
