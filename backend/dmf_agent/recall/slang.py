"""三级黑话词典解析器 + 查询改写（PRD §5.3.1「查询改写与黑话字典」）。

定位：**六要素识别之前的问题规范化环节**。把用户口语 / 黑话 / 缩写转换为
可被字典匹配的标准问句，提升实体识别与整体准确率（FR16 / FR17）。

三级作用域（同名以高层为准）：
    user（用户级）> tenant（公司级）> industry（行业级）

缓存策略（**Redis 优先**）：
    读：Redis 命中即返回 -> 未命中回源 MySQL -> 回填 Redis
    写：调用方先落 MySQL（权威源），再调 `refresh_layer` **写穿** Redis
    Redis 不可用：退化为进程内短 TTL 缓存 -> 再退化直连 MySQL（优雅降级，不阻断）

匹配规则（PRD「匹配按最长优先 + 优先级排序」）：
    1. 候选按 `term` 分组，同名只保留最高层级的一条（层级 > 优先级 > 新近）；
    2. 扫描按 `term` 长度降序 -> 优先级降序 -> 层级降序；
    3. **已命中区间不重复命中**（长词吃掉短词，避免「不良贷款率」与「不良」重复扩展）。

三种改写模式：
    expand  保留原词 + 句尾追加标准名（默认；保留"比值 / 时间"等语义线索）
    replace 原地替换为标准名
    stop    从问句中删除（口语噪声）
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from dmf_agent.config.settings import SlangSettings
from dmf_agent.db.base import SlangRepository
from dmf_agent.schemas.slang import (
    ALL_SCOPES,
    MODE_REPLACE,
    MODE_STOP,
    SCOPE_INDUSTRY,
    SCOPE_RANK,
    SCOPE_TENANT,
    SCOPE_USER,
    STATUS_DRAFT,
    STATUS_PUBLISHED,
    SlangEntryData,
    SlangHit,
    SlangPreviewResult,
    layer_key,
)

logger = logging.getLogger(__name__)

# Redis 缓存 key 前缀（带版本号，结构变更时直接换前缀，旧 key 自然过期）
_REDIS_PREFIX = "slang:v1:"

# 全角 -> 半角（仅 ASCII 可见范围；中文全角标点一并归一到半角）
# 注意：str.translate 的表必须是 {ordinal: str}，用 {str: str} 会静默失效。
_FULLWIDTH = {i + 0xFEE0: chr(i) for i in range(0x21, 0x7F)}
_FULLWIDTH[0x3000] = " "
_WS_RE = re.compile(r"\s+")
# 句首 / 句尾的连续标点（保留问号，避免影响"是不是问句"的判断）
_EDGE_PUNCT_RE = re.compile(r"^[\s,，。;；:：、!！~]+|[\s,，。;；:：、!！~]+$")


def normalize(text: str) -> str:
    """轻量口语归一：全角转半角 + 空白折叠 + 首尾标点清理。

    时间表达式（"上个月 / 近30天"）由 `time` 模块负责，此处**不碰**——
    职责单一，避免两处都改时间造成冲突。
    """
    if not text:
        return ""
    out = text.translate(_FULLWIDTH)
    out = _WS_RE.sub(" ", out)
    out = _EDGE_PUNCT_RE.sub("", out)
    return out.strip()


@dataclass
class _Candidate:
    """参与匹配的生效词条（已按同名覆盖 + 排序整理）。"""

    term: str
    entry: SlangEntryData

    @property
    def mode(self) -> str:
        return self.entry.effective_mode()

    @property
    def rank(self) -> int:
        return SCOPE_RANK.get(self.entry.scope, 0)


@dataclass
class _LayerSnapshot:
    """一层（行业 / 公司 / 用户）的词条快照。"""

    entries: List[SlangEntryData] = field(default_factory=list)

    def to_cache(self) -> Dict[str, Any]:
        return {"entries": [e.to_cache() for e in self.entries]}

    @classmethod
    def from_cache(cls, payload: Any) -> Optional["_LayerSnapshot"]:
        if not isinstance(payload, dict) or "entries" not in payload:
            return None
        rows = payload.get("entries") or []
        if not isinstance(rows, list):
            return None
        return cls(entries=[SlangEntryData.from_cache(r) for r in rows if isinstance(r, dict)])


class SlangResolver:
    """三级黑话词典解析器（Redis 优先；写路径由调用方保证 MySQL 先行）。"""

    def __init__(
        self,
        repo: Optional[SlangRepository],
        redis_client: Any = None,
        settings: Optional[SlangSettings] = None,
    ) -> None:
        self._repo = repo
        self._redis = redis_client
        self._settings = settings or SlangSettings()
        # 进程内兜底缓存：仅在 Redis 不可用时使用，避免每轮都打 MySQL
        self._mem: Dict[str, Tuple[float, _LayerSnapshot]] = {}
        # 上一次读取的数据来源（观测用）
        self.last_source: str = "none"

    # ---------------- 只读：取三层生效词条 ----------------
    @staticmethod
    def _needed_layers(tenant_id: str, user_id: str) -> List[Tuple[str, str, str]]:
        """当前身份需要读取的层级（scope, tenant_id, user_id）。"""
        layers: List[Tuple[str, str, str]] = [(SCOPE_INDUSTRY, "", "")]
        if tenant_id:
            layers.append((SCOPE_TENANT, tenant_id, ""))
        if tenant_id and user_id:
            layers.append((SCOPE_USER, tenant_id, user_id))
        return layers

    def _redis_key(self, scope: str, tenant_id: str, user_id: str) -> str:
        return f"{_REDIS_PREFIX}{layer_key(scope, tenant_id, user_id)}"

    async def _read_layer_redis(
        self, scope: str, tenant_id: str, user_id: str
    ) -> Optional[_LayerSnapshot]:
        if self._redis is None:
            return None
        payload = await self._redis.get_json(self._redis_key(scope, tenant_id, user_id))
        if payload is None:
            return None
        return _LayerSnapshot.from_cache(payload)

    async def _write_layer_redis(
        self, scope: str, tenant_id: str, user_id: str, snapshot: _LayerSnapshot
    ) -> bool:
        if self._redis is None:
            return False
        return await self._redis.set_json(
            self._redis_key(scope, tenant_id, user_id),
            snapshot.to_cache(),
            ttl=self._settings.redis_ttl,
        )

    def _read_layer_mem(
        self, scope: str, tenant_id: str, user_id: str
    ) -> Optional[_LayerSnapshot]:
        hit = self._mem.get(layer_key(scope, tenant_id, user_id))
        if hit is None:
            return None
        ts, snapshot = hit
        if time.time() - ts > self._settings.mem_fallback_ttl:
            return None
        return snapshot

    def _write_layer_mem(
        self, scope: str, tenant_id: str, user_id: str, snapshot: _LayerSnapshot
    ) -> None:
        self._mem[layer_key(scope, tenant_id, user_id)] = (time.time(), snapshot)

    async def _load_all_layers(
        self, tenant_id: str, user_id: str
    ) -> Dict[str, _LayerSnapshot]:
        """回源 MySQL（一次性取齐三层已发布词条）。"""
        if self._repo is None:
            return {}
        raw = await self._repo.list_layers(tenant_id=tenant_id, user_id=user_id)
        return {
            scope: _LayerSnapshot(entries=list(raw.get(scope) or []))
            for scope in ALL_SCOPES
        }

    async def load_layers(
        self, tenant_id: str = "", user_id: str = ""
    ) -> Dict[str, _LayerSnapshot]:
        """取三层词条快照（Redis 优先 -> MySQL 回填）。

        三层需**同时命中**才算缓存命中；任一层缺失则整体回源并回填三层，
        避免"半新半旧"的错配（写路径会精确刷新单层，因此不会互相污染）。
        """
        needed = self._needed_layers(tenant_id, user_id)
        snapshots: Dict[str, _LayerSnapshot] = {}
        miss = False
        if self._redis is not None:
            for scope, tid, uid in needed:
                snap = await self._read_layer_redis(scope, tid, uid)
                if snap is None:
                    miss = True
                    break
                snapshots[scope] = snap
            if not miss:
                self.last_source = "redis"
                return snapshots

        # Redis 未配置 / 未命中 -> 进程内兜底
        if self._redis is None:
            for scope, tid, uid in needed:
                snap = self._read_layer_mem(scope, tid, uid)
                if snap is None:
                    miss = True
                    break
                snapshots[scope] = snap
            if not miss:
                self.last_source = "memory"
                return snapshots

        # 回源 MySQL，并回填两层缓存
        fresh = await self._load_all_layers(tenant_id, user_id)
        for scope, tid, uid in needed:
            snap = fresh.get(scope) or _LayerSnapshot()
            await self._write_layer_redis(scope, tid, uid, snap)
            self._write_layer_mem(scope, tid, uid, snap)
        self.last_source = "mysql"
        return {scope: fresh.get(scope) or _LayerSnapshot() for scope, _, _ in needed}

    # ---------------- 写后同步（MySQL -> Redis） ----------------
    async def refresh_layer(self, scope: str, tenant_id: str = "", user_id: str = "") -> int:
        """**写穿**：把某一层从 MySQL 重新读出来并覆盖到 Redis / 进程内缓存。

        调用契约（对齐需求「更新时先更新 MySQL 端，然后自动更新 Redis 端数据」）：
            1. 调用方先完成 `repo.add / update / delete`（MySQL 已是权威新值）；
            2. 再调本方法，本方法**只回读该层**并刷新对应缓存 key。
        只刷新受影响的那一层，因此不会误伤其他用户 / 其他层级的缓存。
        """
        if self._repo is None:
            return 0
        raw = await self._repo.list_layers(tenant_id=tenant_id, user_id=user_id)
        entries = list(raw.get(scope) or [])
        snapshot = _LayerSnapshot(entries=entries)
        await self._write_layer_redis(scope, tenant_id, user_id, snapshot)
        self._write_layer_mem(scope, tenant_id, user_id, snapshot)
        return len(entries)

    async def invalidate(self, scope: str, tenant_id: str = "", user_id: str = "") -> None:
        """仅删除某层缓存（MySQL 直读场景用；正常写路径用 refresh_layer 更好）。"""
        if self._redis is not None:
            await self._redis.delete(self._redis_key(scope, tenant_id, user_id))
        self._mem.pop(layer_key(scope, tenant_id, user_id), None)

    # ---------------- 匹配 + 改写 ----------------
    @staticmethod
    def _effective_candidates(
        snapshots: Dict[str, _LayerSnapshot], *, allow_unpublished: bool = False
    ) -> List[_Candidate]:
        """同名覆盖（高层级胜出）后按匹配优先级排序。

        `allow_unpublished=True` 时草稿也参与（「试改写」用于发布前预览）。
        """
        by_term: Dict[str, SlangEntryData] = {}
        for scope, snap in snapshots.items():
            for e in snap.entries:
                if not e.term:
                    continue
                if not allow_unpublished and e.status != STATUS_PUBLISHED:
                    continue
                current = by_term.get(e.term)
                if current is None:
                    by_term[e.term] = e
                    continue
                if _is_stronger(e, current):
                    by_term[e.term] = e
        candidates = [_Candidate(term=t, entry=e) for t, e in by_term.items()]
        # 最长优先 -> 优先级降序 -> 层级降序 -> id 降序（稳定）
        candidates.sort(
            key=lambda c: (
                -len(c.term),
                -int(c.entry.priority or 0),
                -c.rank,
                -(c.entry.id or 0),
            )
        )
        return candidates

    @staticmethod
    def match(text: str, candidates: Sequence[_Candidate]) -> List[SlangHit]:
        """最长优先 + 区间互斥的多模式匹配（不依赖正则，避免特殊字符转义问题）。"""
        if not text:
            return []
        occupied: List[Tuple[int, int]] = []
        hits: List[Tuple[int, SlangHit]] = []

        def _overlaps(start: int, end: int) -> bool:
            return any(not (end <= s or start >= e) for s, e in occupied)

        for cand in candidates:
            term = cand.term
            if not term:
                continue
            start = text.find(term)
            while start != -1:
                end = start + len(term)
                if not _overlaps(start, end):
                    occupied.append((start, end))
                    hits.append(
                        (
                            start,
                            SlangHit(
                                term=term,
                                standard=cand.entry.standard,
                                entry_type=cand.entry.entry_type,
                                scope=cand.entry.scope,
                                rewrite_mode=cand.mode,
                                priority=int(cand.entry.priority or 0),
                                entry_id=cand.entry.id,
                                span=[start, end],
                                expand=cand.entry.expanded_terms(),
                            ),
                        )
                    )
                start = text.find(term, start + 1)
        hits.sort(key=lambda h: (h[0], h[1].span[1] if h[1].span else 0))
        return [h for _, h in hits]

    @staticmethod
    def _apply(text: str, hits: List[SlangHit]) -> Tuple[str, List[SlangHit], Dict[str, int]]:
        """按模式改写：stop 删除 / replace 替换 / expand 收集后追加。"""
        edits: List[Tuple[int, int, str]] = []
        expands: List[str] = []
        mode_counts: Dict[str, int] = {}

        for h in hits:
            mode_counts[h.rewrite_mode] = mode_counts.get(h.rewrite_mode, 0) + 1
            span = h.span or [0, 0]
            start, end = int(span[0]), int(span[1])
            if h.rewrite_mode == MODE_STOP:
                edits.append((start, end, ""))
            elif h.rewrite_mode == MODE_REPLACE:
                if h.standard:
                    edits.append((start, end, h.standard))
            else:  # expand：收集需要追加的标准名 / 维值
                for name in h.expand or ([h.standard] if h.standard else []):
                    if name and name not in text and name not in expands:
                        expands.append(name)

        out = text
        for start, end, replacement in sorted(edits, key=lambda e: -e[0]):
            out = f"{out[:start]}{replacement}{out[end:]}"
        out = _WS_RE.sub(" ", out).strip()
        if expands:
            out = f"{out} {' '.join(expands)}".strip()
        return out, hits, mode_counts

    def resolve_entries(
        self,
        text: str,
        snapshots: Dict[str, _LayerSnapshot],
        *,
        cache_source: str = "",
        extra_entries: Optional[Iterable[SlangEntryData]] = None,
        allow_unpublished: bool = False,
    ) -> SlangPreviewResult:
        """纯函数式改写（不触达仓储），便于单测与「试改写」复用。"""
        started = time.perf_counter()
        original = text or ""
        normalized = normalize(original)
        merged: Dict[str, _LayerSnapshot] = dict(snapshots)
        if extra_entries:
            bucket = merged.setdefault(SCOPE_USER, _LayerSnapshot())
            for e in extra_entries:
                bucket.entries = [*bucket.entries, e]
        candidates = self._effective_candidates(
            merged, allow_unpublished=allow_unpublished
        )
        hits = self.match(normalized, candidates)
        rewritten, hits, mode_counts = self._apply(normalized, hits)
        if not hits and normalized == original:
            rewritten = original
        return SlangPreviewResult(
            original=original,
            rewritten=rewritten,
            hits=hits,
            mode_counts=mode_counts,
            cache_source=cache_source or self.last_source,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
        )

    async def resolve(
        self,
        text: str,
        tenant_id: str = "",
        user_id: str = "",
        *,
        include_draft: bool = False,
        extra_entries: Optional[Iterable[SlangEntryData]] = None,
    ) -> SlangPreviewResult:
        """主入口：读三层词条 -> 改写。

        任何异常都降级为「原句透传 + degraded 标记」，绝不阻断对话主链路。
        """
        if not self._settings.enabled:
            return SlangPreviewResult(
                original=text or "", rewritten=normalize(text or ""), degraded=True,
                error="slang disabled", cache_source="disabled",
            )
        if text and len(text) > self._settings.max_query_len:
            return SlangPreviewResult(
                original=text, rewritten=normalize(text), degraded=True,
                error="query too long", cache_source="skipped",
            )
        try:
            if include_draft or extra_entries:
                snapshots = await self._load_uncached(
                    tenant_id, user_id, include_draft=bool(include_draft or extra_entries)
                )
                source = "mysql"
            else:
                snapshots = await self.load_layers(tenant_id, user_id)
                source = self.last_source
            return self.resolve_entries(
                text,
                snapshots,
                cache_source=source,
                extra_entries=extra_entries,
                allow_unpublished=bool(include_draft or extra_entries),
            )
        except Exception as exc:  # noqa: BLE001 - 字典异常不影响对话
            logger.warning("黑话改写失败，按原句透传：%s", exc)
            return SlangPreviewResult(
                original=text or "",
                rewritten=normalize(text or ""),
                degraded=True,
                error=str(exc),
                cache_source="error",
            )

    async def _load_uncached(
        self, tenant_id: str, user_id: str, *, include_draft: bool = True
    ) -> Dict[str, _LayerSnapshot]:
        """绕过缓存直读 MySQL（试改写场景）。

        取 `published`（线上生效）与 `draft`（发布前预览）——
        **不含 `disabled`**：停用是"明确不要"，预览也不该生效。
        """
        if self._repo is None:
            return {SCOPE_USER: _LayerSnapshot()}
        allow = {STATUS_PUBLISHED, STATUS_DRAFT} if include_draft else {STATUS_PUBLISHED}
        out: Dict[str, _LayerSnapshot] = {SCOPE_USER: _LayerSnapshot()}
        rows, _ = await self._repo.query(status=None, limit=5000, offset=0)
        for e in rows:
            if e.status not in allow:
                continue
            if not _visible(e, tenant_id, user_id):
                continue
            out.setdefault(e.scope, _LayerSnapshot()).entries.append(e)
        return out

    async def bump_hits(self, hits: Sequence[SlangHit]) -> int:
        """命中计数 +1（管理页观察词条热度）；失败静默。"""
        if not self._settings.track_hits or self._repo is None:
            return 0
        ids = [h.entry_id for h in hits if h.entry_id]
        if not ids:
            return 0
        try:
            return await self._repo.bump_hit(ids)
        except Exception as exc:  # noqa: BLE001
            logger.debug("命中计数失败（忽略）：%s", exc)
            return 0

    async def record_attempt(
        self,
        *,
        query: str,
        hit_count: int,
        tenant_id: str = "",
        user_id: str = "",
        session_id: str = "",
        message_id: str = "",
    ) -> None:
        """改写节点埋点：每次改写 best-effort 落一条事件（供命中率/误改写率看板）。

        失败静默——绝不影响主对话链路。
        """
        if self._repo is None:
            return
        try:
            await self._repo.log_rewrite_event(
                tenant_id=tenant_id,
                user_id=user_id,
                query_hash=hash_query(query),
                query_text=query,
                hit_count=hit_count,
                is_rewritten=bool(hit_count),
                session_id=session_id,
                message_id=message_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("改写埋点失败（忽略）：%s", exc)


def hash_query(text: str) -> str:
    """归一化问句后取哈希，作为"同一问句"的反馈去重键（不含 PII）。"""
    import hashlib

    return hashlib.sha1(normalize(text).encode("utf-8")).hexdigest()[:16]


def _is_stronger(candidate: SlangEntryData, current: SlangEntryData) -> bool:
    """同名覆盖比较：层级 > 优先级 > id（新近）。"""
    c_rank = SCOPE_RANK.get(candidate.scope, 0)
    o_rank = SCOPE_RANK.get(current.scope, 0)
    if c_rank != o_rank:
        return c_rank > o_rank
    c_pri = int(candidate.priority or 0)
    o_pri = int(current.priority or 0)
    if c_pri != o_pri:
        return c_pri > o_pri
    return (candidate.id or 0) > (current.id or 0)


def _visible(e: SlangEntryData, tenant_id: str, user_id: str) -> bool:
    """层级归属可见性（试改写时防止读到他人词条）。"""
    if e.scope == SCOPE_INDUSTRY:
        return True
    if e.scope == SCOPE_TENANT:
        return e.tenant_id == tenant_id
    if e.scope == SCOPE_USER:
        return e.tenant_id == tenant_id and e.user_id == user_id
    return False


def validate_standard(
    layer: Any, entry_type: str, standard: str, dim_value: str = ""
) -> Optional[str]:
    """校验词条的标准名是否存在于语义层；返回错误描述（None = 通过）。

    护栏理由：字典写错会导致「改写后反而解析失败」，比不写更糟——
    因此录入 / 导入 / 种子生成都要过这一关（`layer` 为 None 时放行，避免误伤降级场景）。
    """
    from dmf_agent.schemas.slang import (
        ENTRY_DIMENSION,
        ENTRY_DIM_VALUE,
        ENTRY_METRIC,
        ENTRY_STOP,
    )

    if entry_type == ENTRY_STOP:
        return None
    name = (standard or "").strip()
    if not name:
        return "标准名不能为空"
    if layer is None:
        return None
    if entry_type == ENTRY_METRIC:
        return None if layer.resolve_metric(name) else f"指标「{name}」不存在于语义层"
    if entry_type == ENTRY_DIMENSION:
        return None if name in layer.dimension_names() else f"维度「{name}」不存在于语义层"
    if entry_type == ENTRY_DIM_VALUE:
        if name not in layer.dimension_names():
            return f"维度「{name}」不存在于语义层"
        if not (dim_value or "").strip():
            return "枚举值词条必须填写维值"
        return None
    return None


def build_slang_resolver(
    repo: Optional[SlangRepository],
    redis_client: Any = None,
    settings: Optional[SlangSettings] = None,
) -> SlangResolver:
    """装配解析器（供 app 启动与测试共用）。"""
    return SlangResolver(repo=repo, redis_client=redis_client, settings=settings)
