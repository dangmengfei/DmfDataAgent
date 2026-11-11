"""DmfDataAgent - 指标问答 Agent 框架.

基于 LLM + Harness 架构重构（面向企业生产部署的重构版本）：

- FastAPI    : Web 服务壳（REST / SSE / 生命周期管理）
- LangGraph  : Agent 编排引擎（状态图即业务 Harness）
- LangChain  : LLM 统一抽象 / RAG / 工具生态
- 观测       : trace 落库（trace_records 表）+ 可选 OpenTelemetry 导出
- MySQL      : 会话、消息、知识库、指标元数据持久化
"""

__version__ = "0.1.0"
