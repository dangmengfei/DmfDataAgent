"""指标问答 Agent 主图。

graph.py   : 图拓扑（业务 Harness）
deps.py    : 节点运行依赖注入
nodes/     : 各业务节点（标注老系统迁移来源，按模块逐步替换实现）
"""

from dmf_agent.agents.metric_agent.deps import AgentDeps
from dmf_agent.agents.metric_agent.graph import build_metric_agent_graph

__all__ = ["AgentDeps", "build_metric_agent_graph"]
