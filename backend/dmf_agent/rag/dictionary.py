"""指标口径字典：由语义层**确定性生成**，灌入知识库作为「权威源」（FR7）。

为什么是"生成"而不是"手写"
--------------------------
指标口径的唯一事实来源是 `semantic/manifest.yml`（P0 起物理列/表达式只存在于该文件）。
知识库里若再手写一份口径说明，两份必然漂移——用户问"GMV 怎么算的"，检索到的却是
过期文档，比没有更糟。

因此本模块**确定性渲染**知识库文档：内容 100% 来自语义层，manifest 改了就重新灌，
不存在需要人工维护的第二份真相。灌入时打 `authority_level="authoritative"`，
检索阶段按分级加权（`RAGSettings.authority_weights`），权威口径优先于用户上传的资料。

幂等
----
以固定 `source_uri`（`semantic://metric-dictionary`）为键：已存在则**覆盖更新**
（删旧分块 + 旧向量，重新入库），不产生重复文档，重复调用安全。

检索侧收益
----------
1. 口径类提问（"客单价怎么算的""不良率口径"）命中权威文档，回答有据可依；
2. 其余用户上传资料的排序被权威源压制（权重 1.5 vs 1.0），减少"野口径"干扰。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from dmf_agent.config.settings import Settings
from dmf_agent.db.base import ChunkRepository, DocumentRepository
from dmf_agent.rag.ingestion import ingest_text
from dmf_agent.rag.vector_store import VectorStore
from dmf_agent.semantic.layer import SemanticLayer

logger = logging.getLogger(__name__)

# 权威源文档的固定标识（幂等键：同一 source_uri 视为同一份文档，重复灌入即覆盖）
DICTIONARY_SOURCE_URI = "semantic://metric-dictionary"
DICTIONARY_TITLE = "指标口径字典（权威源）"

# 口径类型的中文名（渲染用；未知类型原样输出，不静默吞掉）
_TYPE_LABELS = {
    "simple": "原子指标",
    "ratio": "比率指标",
    "derived": "衍生指标",
}


def _measure_expr(layer: SemanticLayer, measure_name: str) -> str:
    """measure 的「聚合(物理表达式)」文本；查不到时退化为裸名字。"""
    model = layer.manifest.measure_owner(measure_name)
    if model is None:
        return measure_name
    measure = model.measure(measure_name)
    if measure is None:
        return measure_name
    agg = (measure.agg or "sum").upper()
    return f"{agg}({measure.expr})"


def _formula(layer: SemanticLayer, name: str) -> str:
    """指标的确定性口径表达式。

    - simple : `SUM(gmv)`（measure 的聚合 + 物理列）
    - ratio  : `分子 / 分母`（保留中文指标名，便于业务方核对）
    - derived: 清单里写的表达式原文
    """
    metric = layer.manifest.metric(name)
    if metric is None:
        return ""
    params = metric.type_params or {}
    if metric.type == "simple":
        measure = params.get("measure")
        return _measure_expr(layer, measure) if measure else ""
    if metric.type == "ratio":
        num, den = params.get("numerator"), params.get("denominator")
        if num and den:
            return f"{num} / {den}"
        return ""
    # derived：表达式原文（清单里已是可读公式）
    return str(params.get("expr") or "")


def build_dictionary_text(layer: SemanticLayer) -> str:
    """把语义层渲染成 Markdown 口径字典（纯函数，便于单测与快照比对）。"""
    snapshot: Dict[str, Any] = layer.describe()
    models: List[Dict[str, Any]] = snapshot.get("semantic_models") or []
    metrics: List[Dict[str, Any]] = snapshot.get("metrics") or []
    time_dim = layer.time_dimension_name() or "统计日期"

    lines: List[str] = [
        f"# {DICTIONARY_TITLE}",
        "",
        "本文件由系统从指标语义层自动生成，是**指标口径的唯一权威说明**。",
        "如与其它资料（如个人整理的口径笔记）冲突，**以本文件为准**。",
        "",
        f"覆盖范围：{len(models)} 个语义模型、{len(metrics)} 个指标。",
        f"时间维度统一为「{time_dim}」（按天存储，可按日/周/月/季/年聚合）。",
        "",
    ]

    # ---- 指标速查表：先给"有哪些指标、叫什么、单位是什么"，便于口语别名落到标准名 ----
    lines += ["## 一、指标速查表", ""]
    lines += ["| 指标 | 常见别名 | 类型 | 单位 | 口径说明 |", "| --- | --- | --- | --- | --- |"]
    for m in sorted(metrics, key=lambda x: x["name"]):
        aliases = "、".join(m.get("aliases") or []) or "—"
        desc = (m.get("description") or "").replace("|", "／") or "—"
        label = _TYPE_LABELS.get(m.get("type", ""), m.get("type", ""))
        lines.append(
            f"| {m['name']} | {aliases} | {label} | {m.get('unit') or '—'} | {desc} |"
        )
    lines.append("")

    # ---- 逐指标口径：公式 + 支持维度 + 支持粒度 + 同环比 ----
    lines += ["## 二、指标口径明细", ""]
    for m in sorted(metrics, key=lambda x: x["name"]):
        name = m["name"]
        lines.append(f"### {name}")
        formula = _formula(layer, name)
        if formula:
            lines.append(f"- **计算口径**：{formula}")
        if m.get("description"):
            lines.append(f"- **业务含义**：{m['description']}")
        if m.get("aliases"):
            lines.append(f"- **常见叫法**：{'、'.join(m['aliases'])}（与「{name}」等价）")
        base = m.get("base_measures") or []
        if base:
            lines.append(f"- **底表度量**：{'、'.join(base)}")
        dims = [d for d in (m.get("dimensions") or []) if d != time_dim]
        lines.append(
            f"- **可用拆分维度**：{'、'.join(dims) if dims else '无（仅全量汇总）'}"
            f"（时间维度「{time_dim}」始终可用）"
        )
        lines.append(f"- **可用时间粒度**：{'、'.join(m.get('granularities') or [])}")
        lines.append(f"- **可用同环比**：{'、'.join(m.get('pop_types') or [])}")
        if m.get("unit"):
            lines.append(f"- **单位**：{m['unit']}")
        if m.get("round") is not None:
            lines.append(f"- **保留小数位**：{m['round']}")
        lines.append("")

    # ---- 维度字典：维度归属哪个模型，答"XX 能按哪些维度拆" ----
    lines += ["## 三、维度字典", ""]
    lines += ["| 维度 | 类型 | 可用指标 |", "| --- | --- | --- |"]
    for dim in layer.list_dimensions():
        if dim.name == time_dim:
            continue
        owners = [
            m["name"]
            for m in metrics
            if dim.name in (m.get("dimensions") or [])
        ]
        dim_type = "时间" if dim.is_time else "分类"
        lines.append(
            f"| {dim.name} | {dim_type} | {'、'.join(owners) if owners else '—'} |"
        )
    lines.append("")

    # ---- 语义模型 -> 底表：口径溯源（问"数据从哪来"时命中） ----
    lines += ["## 四、语义模型与底表", ""]
    lines += ["| 语义模型 | 物理表 | 说明 |", "| --- | --- | --- |"]
    for mdl in models:
        desc = (mdl.get("description") or "").replace("|", "／") or "—"
        lines.append(f"| {mdl['name']} | {mdl['table']} | {desc} |")
    lines.append("")

    # ---- 口径红线：把银行域/聚合语义的硬约束写进权威文档，减少误读 ----
    lines += [
        "## 五、口径红线（易误读项）",
        "",
        "1. **比率 / 衍生指标必须在同一维度组合下计算**：分母为 0 时结果为空，不等同于 0。",
        "2. **客户数类指标用求和口径**（当日快照），跨时间请用日均，不要跨天直接相加。",
        "3. **余额类指标跨天求和得到的是「存款积数」（余额 × 天数）**，"
        "不是日均余额；日均需除以天数。",
        "4. **同环比依赖连续的时间范围**：若区间内缺数据，对比期可能为空，属正常现象。",
        "",
    ]
    return "\n".join(lines)


async def seed_dictionary(
    *,
    document_repo: DocumentRepository,
    chunk_repo: ChunkRepository,
    vector_store: Optional[VectorStore],
    settings: Settings,
    layer: SemanticLayer,
    tenant_id: str = "",
    knowledge_base_id: Optional[str] = None,
) -> Dict[str, Any]:
    """把口径字典灌入知识库（权威源，幂等覆盖）。

    返回 `{document_id, title, chunks, replaced, authority_level, text_length}`。
    已存在同 `source_uri` 的文档时**先清旧分块与旧向量再重建**，避免同一份口径
    在库里出现多份（检索会重复命中，且旧的过期口径可能被排到前面）。
    """
    text = build_dictionary_text(layer)
    replaced = False

    existing = await document_repo.list(source_uri=DICTIONARY_SOURCE_URI)
    for old in existing:
        # 只覆盖同一租户/同一归属的旧文档，避免误删他人知识库里的同名文档
        if (old.tenant_id or "") != tenant_id:
            continue
        if knowledge_base_id and (old.knowledge_base_id or "") != knowledge_base_id:
            continue
        try:
            old_chunks = await chunk_repo.list_by_document(old.id)
            if vector_store is not None and old_chunks:
                refs = [c.vector_ref for c in old_chunks if c.vector_ref]
                if refs:
                    vector_store.delete(refs)
            await chunk_repo.delete_by_document(old.id)
            await document_repo.delete(old.id)
            replaced = True
        except Exception as exc:  # noqa: BLE001 - 清理失败不阻断重新灌入
            logger.warning("清理旧口径字典失败（继续重建）：%s", exc)

    doc = await ingest_text(
        title=DICTIONARY_TITLE,
        text=text,
        document_repo=document_repo,
        chunk_repo=chunk_repo,
        vector_store=vector_store,
        settings=settings,
        source_type="md",
        source_uri=DICTIONARY_SOURCE_URI,
        knowledge_base_id=knowledge_base_id,
        tenant_id=tenant_id,
        authority_level="authoritative",
    )
    logger.info(
        "口径字典已灌入（权威源）：doc=%s chunks=%s replaced=%s",
        doc.id,
        doc.chunk_count,
        replaced,
    )
    return {
        "document_id": doc.id,
        "title": DICTIONARY_TITLE,
        "chunks": doc.chunk_count,
        "replaced": replaced,
        "authority_level": doc.authority_level,
        "text_length": len(text),
    }
