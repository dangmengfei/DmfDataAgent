"""测试环境：禁用外部依赖，保证开箱可测。

必须在导入 dmf_agent 之前设置环境变量（pytest 先加载本文件）。
环境变量优先级高于 .env 文件，因此即使开发者本地 backend/.env 配置了真实
LLM/HM/观测，测试仍强制走 mock，保证结果确定、不触达外部服务。
"""

import os

# 存储：内存
os.environ.setdefault("DMF_DB-ENABLED", "false")
# 鉴权：默认关闭 -> get_current_user 返回合成 admin，既有接口用例无需令牌。
# 鉴权相关用例（tests/test_auth.py）自行构造 auth.enabled=True 的独立 app/runtime。
os.environ.setdefault("DMF_AUTH-ENABLED", "false")
# LLM：清空 -> 规则意图 + 模板答复（mock 模式）
os.environ.setdefault("DMF_LLM-MODEL", "")
os.environ.setdefault("DMF_LLM-BASE_URL", "")
os.environ.setdefault("DMF_LLM-API_KEY", "")
# HM 取数：api_base 为空 -> 确定性 mock
os.environ.setdefault("DMF_HM-API_BASE", "")
# 观测：遵循 OpenTelemetry，span / 轮级 trace 落内存仓储（纯本地，无任何外部网络依赖，
# 因此测试默认开启，以便覆盖 span 采集与观测接口）
os.environ.setdefault("DMF_OBSERVABILITY-ENABLED", "true")
# 会话留痕：不落盘 JSON（单测按需用 tmp_path 自行开启），避免污染工程目录
os.environ.setdefault("DMF_SESSION-ARCHIVE_ENABLED", "false")
# RAG：避免误连 Milvus / 读到开发者本地配置
os.environ.setdefault("DMF_RAG-BACKEND", "faiss")
os.environ.setdefault("DMF_APP-PORT", "8000")
# StarRocks：测试环境指 127.0.0.1:1（端口 1 必不可达 -> StarRocksError -> API 502），
# SQL 编译（compile/query 走内存字符串生成，与运行时无关）按默认 starrocks 方言，
# 这样编译字符串断言（date_trunc/add_months/FULL OUTER JOIN）与历史一致。
os.environ.setdefault("DMF_STARROCKS-HOST", "127.0.0.1")
os.environ.setdefault("DMF_STARROCKS-PORT", "1")
os.environ.setdefault("DMF_STARROCKS-USER", "dmf_agent")
os.environ.setdefault("DMF_STARROCKS-PASSWORD", "dmf@123456")
os.environ.setdefault("DMF_STARROCKS-DATABASE", "dmf_agent")
os.environ.setdefault("DMF_STARROCKS-DIALECT", "starrocks")
