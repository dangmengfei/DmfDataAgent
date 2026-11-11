"""语义层发布快照注册中心（混合方案：YAML 基座 → DB 发布 → 运行时装配）。

职责
----
- ``publish``：把语义定义写成一个新版本快照。``mode="full"`` 存整份已合并
  manifest（来自 SemanticLayer 或现成 payload）；``mode="overlay"`` 只存差异条目
  （``semantic_models`` / ``metrics``），用于**局部租户覆盖**。
- 审批流：``submit``（draft→review）→ ``approve``（review→published）；
  ``deprecate`` / ``revive`` 与指标级状态机同构（复用 ``governance.can_transition``）。
- ``resolve_chain``：把「全局链 + 租户链」解析成一份合并后的 manifest 字典
  （链式叠加规则见 ``semantic/overlay.py``）。

设计要点
--------
- 版本号取**该租户已有最大版本 + 1**（而非 count+1）——快照可被删除，
  count+1 会在删除后产生版本号碰撞。
- 快照 payload 存**已合并的 manifest 原始字典**，与 YAML 合并结果无损往返，
  避免"定义即数据"导致的漂移。
- 反序列化失败只告警、返回 None——运行时装配随后回落 YAML，取数永不因此失败。
- 状态机守卫与指标级完全一致：**草稿不能直接发布**，必须经 review 闸口。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from dmf_agent.db.base import SemanticPublishedRepository
from dmf_agent.schemas.semantic_publish import SemanticPublishedData
from dmf_agent.semantic.governance import (
    DEPRECATED,
    DRAFT,
    METRIC_STATUSES,
    PUBLISHED,
    REVIEW,
    can_transition,
)
from dmf_agent.semantic.layer import SemanticLayer
from dmf_agent.semantic.overlay import (
    SNAPSHOT_FULL,
    chain_item,
    chain_signature_of,
    normalize_mode,
    normalize_overlay_payload,
    resolve_chain as _resolve_chain_rows,
)

logger = logging.getLogger(__name__)


class SemanticRegistry:
    """语义层发布快照的读写服务（仓储可注入，便于测试与多态存储）。"""

    def __init__(self, repo: SemanticPublishedRepository) -> None:
        self.repo = repo

    # ---------- 发布 ----------

    async def publish(
        self,
        *,
        layer: Optional[SemanticLayer] = None,
        tenant_id: str = "",
        by: str = "",
        state: str = PUBLISHED,
        mode: str = SNAPSHOT_FULL,
        payload: Optional[Dict[str, Any]] = None,
    ) -> SemanticPublishedData:
        """写出一条新版本快照。

        ``mode="full"`` 时必须给 ``layer`` 或 ``payload``（整份 manifest 字典）；
        ``mode="overlay"`` 时必须给 ``payload``（只含 ``semantic_models`` / ``metrics``）。
        """
        if state not in METRIC_STATUSES:
            raise ValueError(f"非法状态：{state!r}（允许 {sorted(METRIC_STATUSES)}）")
        resolved_mode = normalize_mode(mode)

        if resolved_mode == SNAPSHOT_FULL:
            if payload is not None:
                data = dict(payload)
                # **写入口先自校验**：典型误操作是"想发 overlay 却忘了 mode='overlay'"
                # （payload 只有 metrics 没有 semantic_models），此时若放行，故障会在
                # 每一次装载时表现为"快照反序列化失败 → 静默回落 YAML"，排查成本极高。
                # 让错误停在写入口，代价只有一次反序列化。
                if not data.get("semantic_models"):
                    raise ValueError(
                        "full 快照缺少 semantic_models；若只想覆盖若干条目，"
                        "请显式传 mode='overlay'"
                    )
                try:
                    SemanticLayer.from_manifest_dict(data)
                except Exception as exc:  # noqa: BLE001 - 统一收敛为参数错误
                    raise ValueError(f"full 快照无法装载，拒绝写入：{exc}") from exc
            elif layer is not None:
                data = layer.manifest.to_dict()
            else:
                raise ValueError("full 快照必须提供 layer 或 payload")
        else:
            if payload is None:
                # 只给了 layer（整份定义）却没给 payload：layer 不是差异条目，
                # 拿它当 overlay 只会在更深的地方报"不含任何条目"，指错方向。
                raise ValueError(
                    "overlay 快照必须提供 payload（只含被改动的 semantic_models / metrics）；"
                    "整份定义请用 mode='full'"
                )
            data = normalize_overlay_payload(payload)

        tid = tenant_id or ""
        rows = await self.repo.list_for(tenant_id=tid, limit=1)
        version = (rows[0].version + 1) if rows else 1

        rec = SemanticPublishedData(
            tenant_id=tid,
            version=version,
            state=state,
            mode=resolved_mode,
            payload=data,
            created_by=by,
        )
        saved = await self.repo.add(rec)
        logger.info(
            "语义层快照已写入：tenant=%r version=%d state=%s mode=%s（%s）",
            tid or "(全局)",
            saved.version,
            saved.state,
            saved.mode,
            self._describe(saved),
        )
        return saved

    @staticmethod
    def _describe(rec: SemanticPublishedData) -> str:
        payload = rec.payload or {}
        return "模型 %d / 指标 %d" % (
            len(payload.get("semantic_models") or []),
            len(payload.get("metrics") or []),
        )

    # ---------- 审批流 ----------

    async def _transition(self, snapshot_id: int, target: str, *, operator: str = "") -> SemanticPublishedData:
        rec = await self.repo.get(snapshot_id)
        if rec is None:
            raise LookupError(f"快照不存在：{snapshot_id}")
        current = rec.state or DRAFT
        # 有意与 `can_transition` 的幂等语义分道：治理模块对 from == to 返回 True
        # （指标流里"重复发布"视为无害空操作）。但审批接口上的重复动作通常来自
        # UI 竞态或陈旧视图，静默成功会把它藏掉 —— 这里显式拒绝。
        if current == target:
            raise ValueError(f"非法状态转移：{current} -> {target}（快照已处于该状态）")
        if not can_transition(current, target):
            raise ValueError(f"非法状态转移：{current} -> {target}")
        updated = await self.repo.set_state(
            snapshot_id=snapshot_id, state=target, operator=operator
        )
        if updated is None:  # pragma: no cover - 并发删除
            raise LookupError(f"快照不存在：{snapshot_id}")
        logger.info(
            "语义层快照 #%s v%s：%s -> %s（tenant=%r mode=%s operator=%r）",
            snapshot_id,
            updated.version,
            current,
            target,
            updated.tenant_id or "(全局)",
            updated.mode,
            operator,
        )
        return updated

    async def reject(
        self, snapshot_id: int, *, by: str = "", reason: str = ""
    ) -> SemanticPublishedData:
        """驳回快照（review → draft，并记录驳回人 / 原因；便于审计与重新提交）。

        仅 review 状态可被驳回回草稿；其它状态转移交给 ``_transition``。
        """
        rec = await self.repo.get(snapshot_id)
        if rec is None:
            raise LookupError(f"快照不存在：{snapshot_id}")
        if (rec.state or DRAFT) != REVIEW:
            raise ValueError(f"只有 review 状态可驳回（当前 {rec.state}）")
        if not reason or not reason.strip():
            raise ValueError("驳回必须填写原因")
        updated = await self.repo.set_state(
            snapshot_id=snapshot_id,
            state=DRAFT,
            rejected_by=by,
            reject_reason=reason.strip(),
        )
        logger.info(
            "语义层快照 #%s v%s 已驳回回草稿（by=%r reason=%r）",
            snapshot_id, rec.version, by, reason.strip(),
        )
        return updated

    async def batch_approve(self, snapshot_ids: List[int], *, by: str = "") -> Dict[str, Any]:
        """批量审批（review → published），逐条执行、容错收集失败。"""
        ok, failed = [], []
        for sid in snapshot_ids:
            try:
                rec = await self.approve(sid, by=by)
                ok.append(rec.id)
            except (LookupError, ValueError) as exc:
                failed.append({"id": sid, "error": str(exc)})
        return {"approved": ok, "failed": failed}

    async def batch_reject(
        self, snapshot_ids: List[int], *, by: str = "", reason: str = ""
    ) -> Dict[str, Any]:
        """批量驳回（review → draft），逐条执行、容错收集失败。"""
        if not reason or not reason.strip():
            raise ValueError("批量驳回必须填写原因")
        ok, failed = [], []
        for sid in snapshot_ids:
            try:
                rec = await self.reject(sid, by=by, reason=reason)
                ok.append(rec.id)
            except (LookupError, ValueError) as exc:
                failed.append({"id": sid, "error": str(exc)})
        return {"rejected": ok, "failed": failed}

    async def submit(self, snapshot_id: int) -> SemanticPublishedData:
        """提交审核（draft → review）。"""
        return await self._transition(snapshot_id, REVIEW)

    async def approve(self, snapshot_id: int, *, by: str = "") -> SemanticPublishedData:
        """审批发布（review → published，调用方须已校验 admin 角色）。"""
        return await self._transition(snapshot_id, PUBLISHED, operator=by)

    async def deprecate(self, snapshot_id: int) -> SemanticPublishedData:
        """废弃快照（published/review/draft → deprecated）。"""
        return await self._transition(snapshot_id, DEPRECATED)

    async def revive(self, snapshot_id: int) -> SemanticPublishedData:
        """复活为草稿（deprecated → draft）。"""
        return await self._transition(snapshot_id, DRAFT)

    async def rollback(
        self, *, snapshot_id: int, by: str = "", state: str = PUBLISHED
    ) -> SemanticPublishedData:
        """回滚：把某个历史版本的 payload **作为新版本**重新写入（非物理回退）。

        沿用「回滚 = 生成新版本」的项目口径：历史不可变、可审计，
        且新版本会重新走一遍反序列化校验（full 模式）以保证可加载。
        """
        src = await self.repo.get(snapshot_id)
        if src is None:
            raise LookupError(f"快照不存在：{snapshot_id}")
        mode = normalize_mode(src.mode)
        if mode == SNAPSHOT_FULL:
            # 校验目标版本在**当前代码口径**下仍可装载；不可装载则拒绝回滚
            SemanticLayer.from_manifest_dict(src.payload or {})
        new_rec = await self.publish(
            tenant_id=src.tenant_id,
            by=by,
            state=state,
            mode=mode,
            payload=src.payload or {},
        )
        logger.info(
            "语义层回滚：#%s(v%s) -> 新版本 v%s（tenant=%r mode=%s）",
            snapshot_id,
            src.version,
            new_rec.version,
            src.tenant_id or "(全局)",
            mode,
        )
        return new_rec

    # ---------- 读取 / 解析 ----------

    async def list_versions(
        self, *, tenant_id: Optional[str] = None, limit: int = 100, visible_scope: bool = False
    ) -> List[SemanticPublishedData]:
        """列出快照版本（管理面 / 审计用）。

        ``visible_scope=True`` 时按读作用域过滤（None=全量 / 空串=仅全局 / 具体 id=全局+该租户），
        用于治理审批流的跨租户可见性隔离，必须由 ``_read_scope`` 收敛过的 ``tenant_id`` 驱动，
        避免非 admin 用任意 ``tenant_id`` 窥探别租户草稿。
        """
        return await self.repo.list_for(tenant_id=tenant_id, limit=limit, visible_scope=visible_scope)

    async def get_snapshot(self, snapshot_id: int) -> Optional[SemanticPublishedData]:
        return await self.repo.get(snapshot_id)

    async def latest_published(self, *, tenant_id: str = "") -> Optional[SemanticPublishedData]:
        """该租户（或全局回落）最新的 published 快照，仅供「是否有更新」比对。"""
        return await self.repo.get_latest_published(tenant_id=tenant_id or "")

    async def chain_signature(self, *, tenant_id: str = "") -> Tuple[Any, ...]:
        """该租户生效链的**轻量指纹**（租户 id / 快照 id / 版本 / 状态 / 模式）。

        供请求级缓存比对"是否需要重建"：指纹只读元信息、**不反序列化 payload**，
        而重建的主要开销恰在 payload 反序列化 + 引用完整性校验
        （``SemanticLayer.from_manifest_dict``），因此命中即省下大头。

        指纹覆盖「租户链 + 全局链」两侧 —— 全局链变了也该让租户缓存失效。

        注意这是**触发条件**而非"生效链"本身：它把该租户名下所有快照都算进来
        （含 draft / review）。宁可多重建一次，也不能因为漏看一条记录而少重建 ——
        一条 draft 变成 published 时状态位就变了，指纹照样能捕捉到。
        要"当前 DB 会让运行时解析成哪条链"请用 ``effective_chain_signature``。
        """
        tid = tenant_id or ""
        rows: List[SemanticPublishedData] = []
        if tid:
            rows.extend(await self.repo.list_for(tenant_id=tid))
        rows.extend(await self.repo.list_for(tenant_id=""))
        return tuple(
            sorted(
                (
                    getattr(r, "tenant_id", "") or "",
                    getattr(r, "id", 0) or 0,
                    getattr(r, "version", 0) or 0,
                    getattr(r, "state", "") or "",
                    normalize_mode(getattr(r, "mode", SNAPSHOT_FULL)),
                )
                for r in rows
            )
        )

    async def effective_chain_signature(self, *, tenant_id: str = "") -> Tuple[Any, ...]:
        """「DB 现在会让运行时解析成哪条链」的指纹，形状与 ``ResolvedLayer.chain`` 一致。

        与 ``chain_signature`` 的分工：

        - ``chain_signature``  = 该租户名下**所有**快照 → 缓存失效的触发条件（宁可多重建）；
        - ``effective_chain_signature`` = 只取**真正参与合并**的 published 链，且用与
          ``ResolvedLayer.chain`` **完全相同**的归一化（``overlay.chain_item``）→ 可以直接与
          "本进程基座是哪条链"做等值比较，回答"基座是否落后于 DB"。

        这正是基座"拉"式自愈的判据：相等 ⇒ 无需重建（启动后第一次探测就不会白重建一次，
        也不会有对象 churn）；不等 ⇒ 别的地方改了定义，就地重建。

        ``used`` 的推导只依赖 ``rows``（``base_raw`` 只影响合并后的内容、不影响链本身），
        所以这里可以安全地不传基座。
        """
        tid = tenant_id or ""
        rows = await self.repo.list_for(tenant_id=tid)
        _, used = _resolve_chain_rows(rows, None)
        return chain_signature_of([chain_item(r) for r in used])

    async def resolve_chain(
        self, *, tenant_id: str = "", base_raw: Optional[Dict[str, Any]] = None
    ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        """解析「全局链 + 租户链」，返回 ``(raw, meta)``。

        先解全局链（租户链的兜底基座），再把该租户的 overlay 叠上去；
        该租户若发布了 ``full`` 快照，则完全取代全局（``used`` 只含租户链）。
        """
        tid = tenant_id or ""
        global_rows = await self.repo.list_for(tenant_id="")
        global_raw, global_used = _resolve_chain_rows(global_rows, base_raw)

        if not tid:
            return global_raw, {"scope": "global", "used": list(global_used)}

        tenant_rows = await self.repo.list_for(tenant_id=tid)
        tenant_raw, tenant_used = _resolve_chain_rows(tenant_rows, global_raw)
        if not tenant_used:
            return global_raw, {"scope": "global", "used": list(global_used)}

        first = tenant_used[0]
        if normalize_mode(getattr(first, "mode", SNAPSHOT_FULL)) == SNAPSHOT_FULL:
            used: List[Any] = list(tenant_used)
        else:
            used = [*global_used, *tenant_used]
        return tenant_raw, {"scope": "tenant", "used": used}

    async def resolve_layer(
        self, *, tenant_id: str = "", base_layer: Optional[SemanticLayer] = None
    ) -> Tuple[Optional[SemanticLayer], Dict[str, Any]]:
        """解析出可用语义层；``base_layer`` 作为 DB 无快照时的兜底基座。"""
        base_raw = base_layer.manifest.to_dict() if base_layer is not None else None
        raw, meta = await self.resolve_chain(tenant_id=tenant_id, base_raw=base_raw)
        if raw is None:
            return None, meta
        try:
            return SemanticLayer.from_manifest_dict(raw), meta
        except Exception as exc:  # noqa: BLE001 - 快照损坏不阻断取数
            logger.warning("发布快照反序列化失败（忽略，回落 YAML 基座）：%s", exc)
            return None, meta

    async def get_published_layer(
        self, *, tenant_id: str = "", base_layer: Optional[SemanticLayer] = None
    ) -> Optional[SemanticLayer]:
        """该租户生效语义层（全局链 + 租户 overlay）；无任何快照返回 None。"""
        layer, _ = await self.resolve_layer(tenant_id=tenant_id, base_layer=base_layer)
        return layer
