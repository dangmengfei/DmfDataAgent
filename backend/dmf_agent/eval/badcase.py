"""Badcase 回流（P1-7）：把点踩样本沉淀为可回归的评测资产。

现状：``POST /feedback`` 的 dislike 已落库到 ``bad_case_repo``（BadCaseData），
但仅落库未闭环。本模块：

- ``BadcaseStore``：进程内单例 + JSON 持久化（``data/eval/badcases.json``），
  记录每条 bad case（来源 message_id / 问题 / 回答 / 原因 / 回流时间）。
- ``backflow_from_feedback``：把 ``bad_case_repo`` 中的点踩样本批量导入评测库，
  并可选追加进指定数据集（作为回归用例），实现「点踩 → 回归」闭环。

与 FR12 门禁联动：被 judge 判定为幻觉/低可信的用例，亦可由 runner 直接登记进本 store，
成为后续发布门禁的回归集。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_STORE_PATH = Path("data/eval/badcases.json")


@dataclass
class BadcaseRecord:
    id: str
    session_id: str
    message_id: str
    question: str
    answer: str
    reason: Optional[str] = None
    intent: Optional[str] = None
    source: str = "feedback"   # feedback（点踩）| judge（评测判定）| manual
    created_at: int = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "BadcaseRecord":
        return cls(
            id=d.get("id", ""),
            session_id=d.get("session_id", ""),
            message_id=d.get("message_id", ""),
            question=d.get("question", ""),
            answer=d.get("answer", ""),
            reason=d.get("reason"),
            intent=d.get("intent"),
            source=d.get("source", "feedback"),
            created_at=d.get("created_at", int(time.time())),
        )


class BadcaseStore:
    """bad case 内存单例 + JSON 落盘（无 DB 依赖，降级友好）。"""

    def __init__(self, path: Path = _STORE_PATH) -> None:
        self._path = path
        self._items: List[BadcaseRecord] = []
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                rows = json.loads(self._path.read_text(encoding="utf-8"))
                self._items = [BadcaseRecord.from_dict(r) for r in rows]
            except Exception as exc:  # noqa: BLE001
                logger.warning("badcase 文件解析失败，重置为空：%s", exc)
                self._items = []

    def _persist(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps([r.to_dict() for r in self._items], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001 - 持久化失败不阻断主流程
            logger.warning("badcase 持久化失败：%s", exc)

    def register(self, rec: BadcaseRecord) -> BadcaseRecord:
        # 同 message_id 去重（保留最新一条）
        self._items = [r for r in self._items if r.message_id != rec.message_id]
        self._items.append(rec)
        self._persist()
        return rec

    def list_all(self) -> List[BadcaseRecord]:
        return list(self._items)

    def count(self) -> int:
        return len(self._items)

    def clear(self) -> int:
        n = len(self._items)
        self._items = []
        self._persist()
        return n


# 进程内单例
_badcase_store: Optional[BadcaseStore] = None


def badcase_store() -> BadcaseStore:
    global _badcase_store
    if _badcase_store is None:
        _badcase_store = BadcaseStore()
    return _badcase_store


def backflow_from_feedback(bad_case_repo: Any, *, dataset_version: Optional[str] = None) -> Dict[str, Any]:
    """把点踩样本从 bad_case_repo 回流到评测库。

    :param bad_case_repo: 现有 ``bad_case_repo``（``list_recent`` / ``get_by_message``）
    :param dataset_version: 若给定，则把样本作为回归用例追加进该数据集
    :return: 回流统计 + 导入的 case 摘要
    """
    from dmf_agent.eval.dataset import EvalCase, add_cases_to_dataset, get_or_seed_default

    imported: List[Dict[str, Any]] = []
    try:
        rows = bad_case_repo.list_recent(limit=500)
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 bad_case_repo 失败：%s", exc)
        return {"imported": 0, "cases": [], "error": str(exc)}

    store = badcase_store()
    for row in rows:
        rec = BadcaseRecord(
            id=f"fb-{row.message_id}",
            session_id=getattr(row, "session_id", "") or "",
            message_id=getattr(row, "message_id", "") or "",
            question=getattr(row, "question", "") or "",
            answer=getattr(row, "answer", "") or "",
            reason=getattr(row, "reason", None),
            intent=getattr(row, "intent", None),
            source="feedback",
        )
        store.register(rec)
        imported.append({
            "message_id": rec.message_id,
            "question": rec.question,
            "reason": rec.reason,
        })

    if dataset_version is not None:
        get_or_seed_default()
        cases = [
            EvalCase(
                name=f"badcase-{c['message_id']}",
                query=c["question"],
                category="badcase",
                reference_answer="",
            )
            for c in imported
        ]
        if cases:
            add_cases_to_dataset(dataset_version, cases)

    return {"imported": len(imported), "cases": imported, "store_total": store.count()}
