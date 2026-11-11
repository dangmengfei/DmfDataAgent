"""评测 CLI：python -m dmf_agent.eval [--json PATH] [--verbose]

跑默认用例集，输出准确率；--json 把报告写入指定 JSON 文件（默认 data/eval/report.json）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from dmf_agent.eval.runner import run_suite, save_scores_to_json


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DmfDataAgent 评测")
    parser.add_argument(
        "--json", nargs="?", const="data/eval/report.json", default=None,
        help="把评测报告写入 JSON 文件（默认 data/eval/report.json）",
    )
    parser.add_argument("--verbose", action="store_true", help="逐用例输出")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)

    report = await run_suite()
    print(f"\n评测结果：{report['passed']}/{report['total']} 通过，准确率 {report['accuracy']:.1%}")
    if args.verbose:
        for r in report["results"]:
            flag = "PASS" if r.passed else "FAIL"
            print(
                f"  [{flag}] {r.case.name}: query='{r.case.query}' "
                f"intent={r.actual_intent}(期望 {r.case.expected_intent}) "
                f"metrics={r.actual_metrics} skills={r.actual_skills}"
            )

    if args.json:
        path = save_scores_to_json(report, args.json)
        print(f"评测报告已写入 {path}" if path else "评测报告落盘失败")
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
