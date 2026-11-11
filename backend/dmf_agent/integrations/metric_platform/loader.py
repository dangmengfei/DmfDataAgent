"""指标元数据导入编排：HM API / 本地 JSON -> metric_definitions 表。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from dmf_agent.config.settings import Settings
from dmf_agent.db.base import MetricRepository
from dmf_agent.integrations.metric_platform.client import HmMetaClient
from dmf_agent.integrations.metric_platform.mapper import hm_metric_to_data
from dmf_agent.integrations.metric_platform.manifest_emitter import emit_manifest
from dmf_agent.schemas.metrics import MetricData

# 内置种子（HM 原始结构），无外部依赖时可用 --source file 联调导入
DEFAULT_SEED = Path(__file__).with_name("seed_metrics.json")


@dataclass
class LoadResult:
    source: str
    fetched: int
    upserted: int
    dry_run: bool = False
    emitted: int = 0  # 导出的 manifest.d 指标条数（S3）
    manifest_dir: str = ""  # 实际写入的 manifest.d 目录（空=未导出）


class MetricMetadataLoader:
    """从 HM API 或本地 JSON 读取指标元数据并 upsert 到仓储。"""

    def __init__(self, repo: MetricRepository, settings: Settings) -> None:
        self._repo = repo
        self._settings = settings

    def _to_metrics(self, raw: List[dict]) -> List[MetricData]:
        metrics = [hm_metric_to_data(d) for d in raw if d.get("cnName")]
        # 按 code 去重（保留最后一条），code 为表主键
        seen: Dict[str, MetricData] = {}
        for m in metrics:
            if m.code:
                seen[m.code] = m
        return list(seen.values())

    async def load(
        self,
        source: str = "file",
        file_path: Optional[str] = None,
        dry_run: bool = False,
        emit_manifest_dir: Optional[str] = None,
        domain: str = "imported",
        group: str = "hm",
    ) -> LoadResult:
        if source == "api":
            raw = await HmMetaClient(self._settings).fetch_metrics()
        else:
            path = Path(file_path) if file_path else DEFAULT_SEED
            data = json.loads(path.read_text(encoding="utf-8"))
            raw = data if isinstance(data, list) else []

        metrics = self._to_metrics(raw)
        # S3：导出用原始 HM 字典（过滤掉缺 cnName 的脏数据），emit 接受 HM 格式而非 MetricData
        raw_for_emit = [d for d in raw if isinstance(d, dict) and d.get("cnName")]
        if dry_run:
            emitted = 0
            if emit_manifest_dir:
                emitted = emit_manifest(raw_for_emit, emit_manifest_dir, domain=domain,
                                       group=group, dry_run=True).parsed
            return LoadResult(
                source=source, fetched=len(raw), upserted=len(metrics), dry_run=True,
                emitted=emitted, manifest_dir=emit_manifest_dir or "",
            )
        count = await self._repo.upsert_many(metrics)
        emitted = 0
        manifest_dir = ""
        if emit_manifest_dir:
            res = emit_manifest(raw_for_emit, emit_manifest_dir, domain=domain, group=group)
            emitted = res.parsed
            manifest_dir = str(res.model_file.parent.parent) if res.model_file else ""
        return LoadResult(
            source=source, fetched=len(raw), upserted=count,
            emitted=emitted, manifest_dir=manifest_dir,
        )
