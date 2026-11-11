"""外部指标平台（HM）接口注册表测试（依据 doc/指标平台接口.docx 整理）。"""

import pytest

from dmf_agent.integrations.metric_platform.endpoints import API_PREFIX, HM_APIS, hm_path
from dmf_agent.integrations.metric_platform.gain_api import GAIN_DATA_URL


def test_registry_covers_documented_interfaces():
    # docx 共 15 个接口
    assert len(HM_APIS) == 15
    for api in HM_APIS.values():
        assert api.method in ("GET", "POST")
        assert api.path.startswith(API_PREFIX + "/")
        assert api.description


def test_all_paths_have_gateway_prefix():
    assert hm_path("gain_data") == "v1/standard-api/sa/gainData/simplified"
    assert hm_path("metrics_meta") == "v1/standard-api/sa/metaInfo/metrics"
    assert hm_path("dimension_meta") == "v1/standard-api/sa/metaInfo/dimension"
    assert hm_path("dim_by_value") == "v1/standard-api/sa/dim/getByValue"
    assert hm_path("cant_list") == "v1/aigcCantName/listBySa"


def test_currently_used_interfaces_flagged():
    used = {k for k, v in HM_APIS.items() if v.used}
    assert used == {
        "gain_data", "metrics_meta", "dimension_meta", "dim_by_value", "cant_list",
    }


def test_unknown_key_raises():
    with pytest.raises(KeyError):
        hm_path("not_exist")


def test_hm_api_uses_registry():
    # tools/hm_api 的取数 URL 来自统一注册表（不再散落硬编码）
    assert GAIN_DATA_URL == hm_path("gain_data")
