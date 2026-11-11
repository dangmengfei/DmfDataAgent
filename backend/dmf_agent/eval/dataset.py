"""评测数据集版本化（P1-7）：用例集带版本、可快照派生。

- ``EvalCase``：在原有「意图 + 要素」断言之外，扩展 ``category``（回归分类）、
  ``reference_answer``（judge 的参考事实）、``expected_elements``（六要素期望，可选）。
- ``EvalDataset``：带 ``version`` 的用例集；``save_dataset/load_dataset/list_datasets/
  snapshot_dataset`` 落盘到 ``data/eval/datasets/{version}.json``。
- 首次运行惰性播种默认数据集（含原 6 例 + 补充的银行域用例），保证离线可跑。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path("data/eval/datasets")
DEFAULT_VERSION = "1.0.0"


@dataclass
class EvalCase:
    name: str
    query: str
    category: str = "intention"
    expected_intent: str = ""
    expected_metrics: List[str] = field(default_factory=list)
    expected_skills: List[str] = field(default_factory=list)
    # judge 参考事实（数据/口径/标准答案），为空时 judge 走无参考代理
    reference_answer: str = ""
    # 六要素期望（可选）：与运行期实际抽取结果比对
    expected_elements: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "query": self.query,
            "category": self.category,
            "expected_intent": self.expected_intent,
            "expected_metrics": list(self.expected_metrics),
            "expected_skills": list(self.expected_skills),
            "reference_answer": self.reference_answer,
            "expected_elements": dict(self.expected_elements),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EvalCase":
        return cls(
            name=d["name"],
            query=d["query"],
            category=d.get("category", "intention"),
            expected_intent=d.get("expected_intent", ""),
            expected_metrics=list(d.get("expected_metrics", [])),
            expected_skills=list(d.get("expected_skills", [])),
            reference_answer=d.get("reference_answer", ""),
            expected_elements=dict(d.get("expected_elements", {})),
        )


@dataclass
class EvalDataset:
    version: str
    cases: List[EvalCase] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "note": self.note,
            "cases": [c.to_dict() for c in self.cases],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EvalDataset":
        return cls(
            version=d["version"],
            cases=[EvalCase.from_dict(c) for c in d.get("cases", [])],
            note=d.get("note", ""),
        )


def _seed_default() -> EvalDataset:
    """默认数据集：原 6 例 + 银行域补充，覆盖意图/要素/口径/归因等回归维度。"""
    cases = [
        EvalCase(name="metric_simple", query="查一下近7天的GMV趋势",
                 category="intention", expected_intent="metric_query",
                 expected_metrics=["GMV"], reference_answer="近7天GMV呈上升趋势，分别为……"),
        EvalCase(name="metric_with_skill", query="查一下近7天的GMV趋势，画个图",
                 category="intention", expected_intent="metric_query",
                 expected_metrics=["GMV"], expected_skills=["chart_visualization"],
                 reference_answer="GMV趋势图已生成……"),
        EvalCase(name="diagnosis", query="近7天GMV为什么下降了，帮我做个归因分析",
                 category="attribution", expected_intent="diagnosis",
                 expected_metrics=["GMV"],
                 reference_answer="GMV下降主要归因于华东大区销量下滑……"),
        EvalCase(name="knowledge", query="什么是北极星指标，文档里有说明吗",
                 category="knowledge", expected_intent="knowledge_qa",
                 reference_answer="北极星指标是……"),
        EvalCase(name="dimension", query="北京有哪些门店",
                 category="intention", expected_intent="dimension_query",
                 reference_answer="北京门店包括……"),
        EvalCase(name="chitchat", query="你好呀",
                 category="chitchat", expected_intent="chitchat",
                 reference_answer="您好，我可以帮您查询指标数据。"),
        EvalCase(name="retail_deposit_rank", query="各分行零售存款余额排名",
                 category="rank", expected_intent="metric_query",
                 expected_metrics=["零售存款余额"],
                 reference_answer="零售存款余额排名：上海分行第一……"),
        EvalCase(name="loan_mom", query="零售贷款余额月度趋势与环比",
                 category="comparison", expected_intent="metric_query",
                 expected_metrics=["零售贷款余额"],
                 reference_answer="零售贷款余额本月环比上升2.3%……"),
    ]
    return EvalDataset(version=DEFAULT_VERSION, cases=cases,
                       note="默认回归集（含原意图/要素 6 例 + 银行域补充）")


def _path(version: str) -> Path:
    safe = version.replace("/", "_").replace("\\", "_")
    return DATA_DIR / f"{safe}.json"


def save_dataset(dataset: EvalDataset) -> str:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = _path(dataset.version)
    path.write_text(json.dumps(dataset.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return str(path)


def load_dataset(version: str = DEFAULT_VERSION) -> Optional[EvalDataset]:
    path = _path(version)
    if not path.exists():
        return None
    try:
        return EvalDataset.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001
        logger.warning("数据集 %s 解析失败：%s", version, exc)
        return None


def list_datasets() -> List[Dict[str, Any]]:
    if not DATA_DIR.exists():
        return []
    out: List[Dict[str, Any]] = []
    for p in sorted(DATA_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            out.append({
                "version": d.get("version", p.stem),
                "note": d.get("note", ""),
                "cases": len(d.get("cases", [])),
            })
        except Exception:  # noqa: BLE001
            continue
    return out


def get_or_seed_default() -> EvalDataset:
    """返回默认数据集；不存在则惰性播种并落盘。"""
    existing = load_dataset(DEFAULT_VERSION)
    if existing is not None:
        return existing
    ds = _seed_default()
    save_dataset(ds)
    return ds


def snapshot_dataset(from_version: str, new_version: str, note: str = "") -> EvalDataset:
    """从某版本快照派生新版本（深拷贝用例，便于在其上增删而不动基线）。"""
    src = load_dataset(from_version)
    if src is None:
        raise FileNotFoundError(f"源数据集不存在：{from_version}")
    new = EvalDataset(version=new_version, cases=list(src.cases), note=note or src.note)
    save_dataset(new)
    return new


def add_cases_to_dataset(version: str, cases: List[EvalCase], *, dedupe: bool = True) -> EvalDataset:
    """向某数据集追加用例（按 name 去重）；返回更新后的数据集。"""
    ds = load_dataset(version)
    if ds is None:
        ds = EvalDataset(version=version, cases=[])
    existing = {c.name for c in ds.cases} if dedupe else set()
    added = 0
    for c in cases:
        if c.name in existing:
            continue
        ds.cases.append(c)
        existing.add(c.name)
        added += 1
    save_dataset(ds)
    logger.info("数据集 %s 追加 %d 条用例（共 %d）", version, added, len(ds.cases))
    return ds
