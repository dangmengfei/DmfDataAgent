"""few-shot 经验库（迁移自老系统 memory/experience.py + sa_utils/experience_mgr.py）。

老系统通过 HM `v1/standard-api/sa/examples/select-examples` 按 query 相似度召回
（fixed 锁定 + 非锁定混合），把示例 content 注入 prompt。v2 用本地 JSON 数据 +
BM25 检索（复用 rag/bm25.py）做等价实现；本地 JSON 导出见 export.py。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from dmf_agent.rag.bm25 import BM25Index


@dataclass
class Example:
    """一条 few-shot 示例（老 HmExampleObject：query + content）。"""

    query: str
    content: str  # 注入 prompt 的示例文本（含期望输出）


def format_dynamic_examples(examples: List[Example]) -> str:
    """把示例拼成可直接追加进 prompt 的文本（老 DynamicExamplePrompt 同款结构）。"""
    return "\n".join(
        f"用户输入: {e.query}\n输出: {e.content}" for e in examples
    )


class ExampleStore:
    """按类别存储 few-shot 示例，用 BM25 按 query 相似度召回 top_k。"""

    def __init__(
        self,
        examples: Optional[Dict[str, List[Example]]] = None,
    ) -> None:
        self._examples: Dict[str, List[Example]] = examples or {}

    @classmethod
    def from_json(cls, path: str) -> "ExampleStore":
        """从 JSON 加载：{category: [{query, content}]}。"""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        examples = {
            cat: [Example(**item) for item in items]
            for cat, items in data.items()
        }
        return cls(examples)

    def categories(self) -> List[str]:
        return list(self._examples)

    def get_examples(
        self, query: str, category: str, top_k: int = 1
    ) -> List[Example]:
        """按 query 相似度召回 top_k 个示例（老 get_mixed_example_objects 本地等价）。"""
        pool = self._examples.get(category) or []
        if not pool or not query:
            return []
        index = BM25Index([(e.query, "") for e in pool])
        hits = index.search_indices(query, top_k)
        return [pool[idx] for idx, _ in hits]


# 内置示例（few-shot 数据源；可替换为 JSON 文件 / 在线示例接口）
DEFAULT_EXAMPLES: Dict[str, List[Example]] = {
    "intention": [
        Example(
            query="查一下上个月的销售额同比",
            content='{"intent": "metric_query", "skills": [], "multi_query": false, "reason": "查询指标数值"}',
        ),
        Example(
            query="GMV为什么下降了，帮我归因分析一下",
            content='{"intent": "diagnosis", "skills": [], "multi_query": false, "reason": "指标归因分析"}',
        ),
        Example(
            query="画个图看看近30天用户数的趋势",
            content='{"intent": "metric_query", "skills": ["chart_visualization"], "multi_query": false, "reason": "查询并绘图"}',
        ),
    ],
    "element": [
        Example(
            query="查询今年国内日销量和海外日销量",
            content='{"metrics": ["国内日销量", "海外日销量"], "dimensions": [], "filters": []}',
        ),
        Example(
            query="按照商品名称和城市看下最近的综合毛利情况",
            content='{"metrics": ["综合毛利"], "dimensions": ["商品名称"], "filters": []}',
        ),
    ],
    "time": [
        Example(
            query="看一下上周的GMV",
            content='{"time_range": {"start": "2026-08-24", "end": "2026-08-30", "granularity": "day"}}',
        ),
    ],
}


def default_example_store() -> ExampleStore:
    return ExampleStore(DEFAULT_EXAMPLES)
