"""知识入库 CLI：python -m dmf_agent.rag --file 文档.pdf --title 标题"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from dmf_agent.config.settings import get_settings
from dmf_agent.db.factory import build_database
from dmf_agent.rag.ingestion import ingest_text, load_file_text
from dmf_agent.rag.vector_store import build_vector_store


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="DmfDataAgent 知识入库")
    parser.add_argument("--file", type=str, help="文档路径（txt/md/pdf）")
    parser.add_argument("--title", type=str, default=None, help="文档标题（默认取文件名）")
    args = parser.parse_args()

    if not args.file:
        parser.error("请通过 --file 指定文档路径")

    settings = get_settings()
    db = build_database(settings)
    await db.initialize(settings)
    if db.mode != "mysql":
        logging.warning("当前为内存存储模式，入库数据不会持久化（请检查 MySQL 配置）")

    path = Path(args.file)
    text, source_type = load_file_text(path)
    title = args.title or path.stem

    vector_store = build_vector_store(settings)
    doc = await ingest_text(
        title=title,
        text=text,
        document_repo=db.document_repo,
        chunk_repo=db.chunk_repo,
        vector_store=vector_store,
        settings=settings,
        source_type=source_type,
        source_uri=str(path),
    )
    print(f"入库完成：{doc.title} | 状态={doc.status} | 分块数={doc.chunk_count}")
    await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
