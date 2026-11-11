"""指标元数据导入模块测试（纯 Python，无外部依赖）。"""

import base64
import json

from dmf_agent.config.settings import HMSettings, Settings
from dmf_agent.db.memory import InMemoryMetricRepository
from dmf_agent.integrations.metric_platform.client import HmMetaClient, tenant_encode
from dmf_agent.integrations.metric_platform.loader import MetricMetadataLoader
from dmf_agent.integrations.metric_platform.mapper import hm_metric_to_data
from dmf_agent.schemas.metrics import MetricData


def test_tenant_encode_roundtrip():
    encoded = tenant_encode("50002", "admin50002", "管理员")
    decoded = json.loads(base64.b64decode(encoded).decode("utf-8"))
    assert decoded == {"userAccount": "admin50002", "userName": "管理员", "tenantId": "50002"}


def test_client_headers_access_token():
    settings = Settings(hm=HMSettings(api_base="http://h", access_token="tok"))
    headers = HmMetaClient(settings).build_headers()
    assert headers["Access-Token"] == "tok"
    assert "X_tenant" not in headers


def test_client_headers_tenant():
    settings = Settings(
        hm=HMSettings(api_base="http://h", tenant_id="1", user_account="u", user_name="n")
    )
    headers = HmMetaClient(settings).build_headers()
    assert "X_tenant" in headers
    assert "Access-Token" not in headers


def test_client_unconfigured():
    settings = Settings(hm=HMSettings(api_base=None))
    client = HmMetaClient(settings)
    assert client.configured is False


def test_hm_metric_to_data_full():
    raw = {
        "id": 1001,
        "cnName": "GMV",
        "businessMeaning": "成交总额",
        "metricsType": 2,
        "unit": "元",
        "dimensions": [{"id": 66, "cnName": "消费渠道"}, {"id": 67, "cnName": "地区"}],
    }
    m = hm_metric_to_data(raw)
    assert m.code == "1001"
    assert m.name == "GMV"
    assert m.description == "成交总额"
    assert m.unit == "元"
    assert m.category == "派生指标"
    assert m.dimensions == ["消费渠道", "地区"]


def test_hm_metric_to_data_defaults():
    m = hm_metric_to_data({"id": 5, "cnName": "订单数"})
    assert m.code == "5"
    assert m.description == ""
    assert m.unit == ""
    assert m.category == ""
    assert m.dimensions == []


def test_metrics_type_labels():
    assert hm_metric_to_data({"id": 1, "cnName": "a", "metricsType": 1}).category == "原子指标"
    assert hm_metric_to_data({"id": 2, "cnName": "b", "metricsType": 3}).category == "衍生指标"
    assert hm_metric_to_data({"id": 3, "cnName": "c", "metricsType": 4}).category == "中间派生指标"
    assert hm_metric_to_data({"id": 4, "cnName": "d", "metricsType": 99}).category == ""


async def test_loader_file_upserts_seed():
    repo = InMemoryMetricRepository(metrics=[])
    loader = MetricMetadataLoader(repo, Settings())
    result = await loader.load(source="file")
    assert result.upserted >= 5
    names = [m.name for m in await repo.list_all()]
    assert "GMV" in names
    assert "日活跃用户数" in names
    assert "复购率" in names


async def test_loader_file_dry_run():
    repo = InMemoryMetricRepository(metrics=[])
    loader = MetricMetadataLoader(repo, Settings())
    result = await loader.load(source="file", dry_run=True)
    assert result.dry_run is True
    assert result.upserted >= 5
    assert await repo.list_all() == []


async def test_upsert_many_dedup_by_code():
    repo = InMemoryMetricRepository(metrics=[])
    upserted = await repo.upsert_many(
        [MetricData(code="1001", name="GMV"), MetricData(code="1001", name="GMV改")]
    )
    assert upserted == 2
    all_metrics = await repo.list_all()
    assert len(all_metrics) == 1
    assert all_metrics[0].name == "GMV改"
