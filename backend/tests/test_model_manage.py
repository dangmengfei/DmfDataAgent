"""模型管理：LLMConfigStore / ModelRegistry / /api/v1/models 接口。

零外部依赖：conftest 已强制 DB 关（内存 store）、鉴权关（合成 admin）、LLM 空。
连接测试只断言「不可达端点失败且不冒泡」（127.0.0.1:1 必拒），不触达外部服务。
"""

from types import SimpleNamespace

from fastapi.testclient import TestClient

from dmf_agent.api.app import app
from dmf_agent.config.settings import LLMSettings, get_settings
from dmf_agent.db.stores import LLMConfigStore
from dmf_agent.llm.providers import get_chat_model
from dmf_agent.services.models import ModelRegistry, mask_api_key, probe_chat_model


def _registry() -> ModelRegistry:
    return ModelRegistry(LLMConfigStore())


def _add(name: str, status: str = "enabled", **kw) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        model=kw.get("model", "m"),
        base_url=kw.get("base_url", "http://127.0.0.1:1/v1"),
        api_key=kw.get("api_key", "secret1234"),
        temperature=kw.get("temperature", 0.1),
        timeout=kw.get("timeout", 120),
        max_retries=kw.get("max_retries", 2),
        status=status,
        is_default=kw.get("is_default", False),
    )


# ---------- 工具 ----------
def test_mask_api_key():
    assert mask_api_key("") == ""
    assert mask_api_key("abcd") == "****"
    masked = mask_api_key("sk-abcdefgh1234")
    assert masked.endswith("1234")
    assert masked.startswith("*")
    assert len(masked) == len("sk-abcdefgh1234")  # 长度不变，仅打星
    assert "abcdefgh" not in masked


# ---------- ModelRegistry（内存 store）----------
async def test_registry_crud_and_effective():
    reg = _registry()
    # 无默认 -> 无生效覆盖
    assert await reg.refresh() is None
    assert reg.effective_llm is None

    # 新增（is_default=True -> 立即生效）
    res = await reg.create(_add("qwen", is_default=True))
    assert res["ok"]
    lst = await reg.list()
    assert lst["count"] == 1
    assert lst["default"] == "qwen"
    assert lst["effective"] == "qwen"
    assert lst["items"][0]["api_key"].endswith("1234")
    assert lst["items"][0]["api_key"] != "secret1234"
    eff = reg.effective_llm
    assert eff is not None and eff.model == "m"
    assert eff.base_url == "http://127.0.0.1:1/v1"
    assert eff.api_key == "secret1234"

    # 更新：model 变更 + api_key 留空 -> 保留原 key；base_url 尾斜杠被规范化
    res = await reg.update(
        "qwen",
        SimpleNamespace(
            model="qwen-plus", base_url="http://127.0.0.1:1/v1/", api_key="",
            temperature=None, timeout=None, max_retries=None, status=None, is_default=None,
        ),
    )
    assert res["ok"]
    row = await reg.get("qwen")
    assert row["model"] == "qwen-plus"
    assert row["base_url"] == "http://127.0.0.1:1/v1"  # 去尾斜杠
    assert row["api_key"].endswith("1234")
    assert reg.effective_llm.model == "qwen-plus"

    # 删除默认 -> 生效覆盖清空（回落环境变量）
    res = await reg.delete("qwen")
    assert res["ok"]
    assert reg.effective_llm is None


async def test_registry_set_default_requires_enabled_and_unique():
    reg = _registry()
    await reg.create(_add("a"))
    await reg.create(_add("b", status="disabled"))
    # 默认只能设为 enabled 条目
    res = await reg.set_default("b")
    assert not res["ok"] and "未启用" in res["message"]
    res = await reg.set_default("a")
    assert res["ok"]
    assert reg.effective_name == "a"
    # 启用 b 后设为默认：唯一默认
    await reg.update("b", SimpleNamespace(status="enabled", model=None, base_url=None,
                                          api_key=None, temperature=None, timeout=None,
                                          max_retries=None, is_default=None))
    res = await reg.set_default("b")
    assert res["ok"]
    assert reg.effective_name == "b"
    lst = await reg.list()
    assert sum(1 for i in lst["items"] if i["is_default"]) == 1


async def test_registry_probe_missing_fields():
    reg = _registry()
    res = await reg.probe(SimpleNamespace(name="", model="", base_url="", api_key="", timeout=5))
    assert not res["ok"] and "model" in res["message"]
    res = await reg.probe(SimpleNamespace(name="", model="m", base_url="", api_key="", timeout=5))
    assert not res["ok"] and "base_url" in res["message"]
    # 编辑场景：name 传入且表单留空 -> 用已存配置补齐
    await reg.create(_add("qwen", api_key="saved-secret-1234"))
    res = await reg.probe(SimpleNamespace(name="qwen", model="", base_url="", api_key="", timeout=2))
    assert not res["ok"] and "连接失败" in res["message"]  # 补齐到不可达端点，失败不冒泡


async def test_registry_seed_from_env():
    """env → 库 引导：把环境变量配置的对话 LLM 落成库内默认（幂等，0 值不吞）。"""
    reg = _registry()
    # env 未配置 -> skip
    res = await reg.seed_from_env(LLMSettings())
    assert not res["ok"] and res["skipped"] == "env_chat_not_configured"
    llm = LLMSettings(model="m", base_url="http://127.0.0.1:1/v1", api_key="k",
                      temperature=0.0, timeout=60, max_retries=3)
    # 首次引导 -> 建为默认，temperature=0.0 保留
    res = await reg.seed_from_env(llm)
    assert res["ok"]
    assert reg.effective_name == "m"
    assert reg.effective_llm.temperature == 0.0
    # 幂等：同名 -> 跳过（不覆盖）
    res = await reg.seed_from_env(llm)
    assert not res["ok"] and res["skipped"] == "same_name_exists"
    # 已有默认（管理面接管）-> 跳过
    res = await reg.seed_from_env(LLMSettings(model="other", base_url="http://other/v1", api_key="k"))
    assert not res["ok"] and res["skipped"] == "default_exists"


async def test_registry_seed_from_env_endpoint_dedup():
    """同 (base_url, model) 但配置名不同 -> 也跳过（防御性幂等）。"""
    reg = _registry()
    await reg._store.save(
        {
            "name": "生产大模型", "model": "m", "base_url": "http://127.0.0.1:1/v1", "api_key": "x",
            "temperature": 0.1, "timeout": 120, "max_retries": 2, "status": "enabled", "is_default": False,
        }
    )
    res = await reg.seed_from_env(LLMSettings(model="m", base_url="http://127.0.0.1:1/v1", api_key="k"))
    assert not res["ok"] and res["skipped"] == "same_endpoint_exists"


# ---------- 连接测试（不可达端点，失败不冒泡）----------
async def test_probe_unreachable():
    res = await probe_chat_model("m", "http://127.0.0.1:1", api_key="k", timeout=2)
    assert res["ok"] is False
    assert "连接失败" in res["message"]
    assert res["elapsed_ms"] >= 0


# ---------- get_chat_model 覆盖 ----------
def test_get_chat_model_override():
    settings = get_settings()
    override = LLMSettings(model="m", base_url="http://127.0.0.1:1/v1", api_key="k")
    llm = get_chat_model(settings, llm_override=override)
    assert llm is not None  # 覆盖优先，构建 ChatOpenAI
    # 无覆盖且环境 LLM 为空（conftest）-> None，mock 降级
    assert get_chat_model(settings, llm_override=None) is None


# ---------- /api/v1/models 接口 ----------
def test_models_api_crud():
    with TestClient(app) as client:
        # 新增（is_default -> 运行时生效）
        r = client.post(
            "/api/v1/models",
            json={
                "name": "qwen",
                "model": "qwen-max",
                "base_url": "http://127.0.0.1:1/v1",
                "api_key": "sk-secret1234",
                "temperature": 0.1,
                "timeout": 60,
                "max_retries": 2,
                "status": "enabled",
                "is_default": True,
            },
        )
        assert r.status_code == 200 and r.json()["ok"]
        # 重名 400
        r = client.post("/api/v1/models", json={"name": "qwen", "model": "x", "base_url": "http://127.0.0.1:1/v1"})
        assert r.status_code == 400
        # 列表：脱敏 + default/effective
        j = client.get("/api/v1/models").json()
        assert j["count"] == 1 and j["default"] == "qwen" and j["effective"] == "qwen"
        assert j["items"][0]["api_key"].endswith("1234")
        # 详情
        j = client.get("/api/v1/models/qwen").json()
        assert j["item"]["model"] == "qwen-max"
        # 更新：api_key 留空 -> 保留原 key
        r = client.put("/api/v1/models/qwen", json={"model": "qwen-plus", "api_key": ""})
        assert r.status_code == 200 and r.json()["ok"]
        j = client.get("/api/v1/models/qwen").json()
        assert j["item"]["model"] == "qwen-plus"
        assert j["item"]["api_key"].endswith("1234")
        # 已保存模型连接测试：不可达 -> ok=False 但接口 200，且 last_test 回写
        r = client.post("/api/v1/models/qwen/test")
        assert r.status_code == 200 and r.json()["ok"] is False
        j = client.get("/api/v1/models/qwen").json()
        assert j["item"]["last_test"]["ok"] is False
        assert j["item"]["last_test"]["error"]
        # 预检试连（不落库）
        r = client.post("/api/v1/models/probe", json={"model": "m", "base_url": "http://127.0.0.1:1", "timeout": 2})
        assert r.status_code == 200 and r.json()["ok"] is False
        # 删除
        r = client.delete("/api/v1/models/qwen")
        assert r.status_code == 200 and r.json()["ok"]
        assert client.get("/api/v1/models").json()["count"] == 0
