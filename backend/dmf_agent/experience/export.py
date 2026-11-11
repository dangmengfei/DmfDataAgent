"""经验库本地导出（JSON 文件，供后续分析/评测/版本管理）。

few-shot 经验库本地 JSON 导出，便于人工维护与离线分析。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def export_examples_to_json(examples: Dict[str, List[Any]], path: str) -> int:
    """把 {category: [Example]} 导出为 JSON；返回导出条目数，失败返回 0。"""
    try:
        data = {
            cat: [{"query": e.query, "content": e.content} for e in items]
            for cat, items in examples.items()
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return sum(len(items) for items in examples.values())
    except Exception as exc:  # noqa: BLE001 - 导出失败不阻断
        logger.warning("经验库导出 JSON 失败：%s", exc)
        return 0
