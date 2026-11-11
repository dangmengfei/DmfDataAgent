"""评测体系（P1-7：judge / 数据集版本化 / 聚合指标 / badcase 回流）。

- judge.py    : LLM-as-judge（含确定性兜底）
- dataset.py  : 评测数据集版本化
- metrics.py  : 聚合指标（TCR / 六要素 / TOA / 归因 / 幻觉率）
- badcase.py  : badcase 回流（接已有点踩源）
- runner.py   : 执行器（全图 run_suite + 可注入 answer_provider 的 run_suite_with_judge）
- cases.py    : 历史用例集（意图/要素断言）
- __main__.py : CLI（python -m dmf_agent.eval）
"""

from dmf_agent.eval.cases import DEFAULT_CASES, EvalCase
from dmf_agent.eval.runner import (
    EvalResult,
    eval_report_to_dict,
    run_case,
    run_suite,
    run_suite_with_judge,
    save_scores_to_json,
)
from dmf_agent.eval.judge import JudgeResult, deterministic_judge, judge_answer
from dmf_agent.eval.dataset import (
    EvalDataset,
    add_cases_to_dataset,
    get_or_seed_default,
    list_datasets,
    load_dataset,
    save_dataset,
    snapshot_dataset,
)
from dmf_agent.eval.metrics import EvalCaseResult, EvalReport, compute_metrics
from dmf_agent.eval.badcase import (
    BadcaseRecord,
    BadcaseStore,
    backflow_from_feedback,
    badcase_store,
)

__all__ = [
    "DEFAULT_CASES",
    "EvalCase",
    "EvalResult",
    "run_case",
    "run_suite",
    "run_suite_with_judge",
    "eval_report_to_dict",
    "save_scores_to_json",
    "JudgeResult",
    "deterministic_judge",
    "judge_answer",
    "EvalDataset",
    "add_cases_to_dataset",
    "get_or_seed_default",
    "list_datasets",
    "load_dataset",
    "save_dataset",
    "snapshot_dataset",
    "EvalCaseResult",
    "EvalReport",
    "compute_metrics",
    "BadcaseRecord",
    "BadcaseStore",
    "backflow_from_feedback",
    "badcase_store",
]
