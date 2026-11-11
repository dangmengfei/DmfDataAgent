"""指标元数据导入 CLI（指标平台接入层，主流程不感知）。

    python -m dmf_agent.integrations.metric_platform --source file|api [--file xx.json] [--dry-run]

历史路径为 `python -m dmf_agent.metadata`（2026-09-12 主流程脱离指标平台后移到本包）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from dmf_agent.config.settings import get_settings
from dmf_agent.db.factory import build_database
from dmf_agent.integrations.metric_platform.loader import MetricMetadataLoader


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="DmfDataAgent 指标元数据导入")
    parser.add_argument(
        "--source", choices=["file", "api"], default="file",
        help="数据源：file=本地 JSON（默认内置种子），api=HM 元数据接口",
    )
    parser.add_argument("--file", type=str, default=None, help="本地 JSON 路径（source=file 时生效）")
    parser.add_argument("--dry-run", action="store_true", help="仅解析不落库")
    parser.add_argument(
        "--emit-manifest", type=str, default=None,
        help="S3：额外把 HM 指标导出为 manifest.d YAML 写入该目录（物理表为占位，需后续绑定）",
    )
    parser.add_argument("--domain", type=str, default="imported", help="导出 manifest 的业务域")
    parser.add_argument("--group", type=str, default="hm", help="导出 manifest 的分组名")
    args = parser.parse_args()

    settings = get_settings()
    db = build_database(settings)
    await db.initialize(settings)
    if db.mode != "mysql":
        logging.warning("当前为内存存储模式，导入数据不会持久化（请检查 MySQL 配置）")

    loader = MetricMetadataLoader(db.metric_repo, settings)
    result = await loader.load(
        source=args.source, file_path=args.file, dry_run=args.dry_run,
        emit_manifest_dir=args.emit_manifest, domain=args.domain, group=args.group,
    )
    suffix = "（dry-run，未落库）" if result.dry_run else ""
    print(
        f"导入完成：source={result.source} | 解析={result.fetched} | "
        f"upsert={result.upserted}{suffix}"
    )
    if result.manifest_dir:
        print(
            f"manifest.d 已导出：dir={result.manifest_dir} | 指标条数={result.emitted} | "
            f"校验：python -m dmf_agent.semantic check"
        )
    await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
