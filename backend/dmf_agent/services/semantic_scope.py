"""请求级语义层解析与缓存：把「租户 → 生效语义层」做成可缓存的纯读服务。

职责边界
--------
- **只解析、不治理**：给定 ``tenant_id`` 返回该租户生效的 ``ResolvedLayer``（含引擎）。
  发布 / 审批 / 回滚仍归 ``services/semantic_layer.py`` 与注册中心管。
- ``DMF_SEMANTIC-SOURCE=yaml`` 时不做租户解析（YAML 无租户概念），直接返回基座。
- 仓储异常 **回落基座** 并告警：取数绝不因定义源不可用而失败。
- 与 FR13 权限正交：按用户/角色的指标白名单、行权限、列 deny 仍在取数节点实时叠加。

为什么用「链指纹」而不是 TTL
-----------------------------
缓存键是 ``tenant_id``，值里带一份**链指纹**（``SemanticRegistry.chain_signature``：
租户链 + 全局链的 id/version/state/mode，只读元信息、不反序列化 payload）。
每次请求先取指纹、一致则复用已构建的层 —— 于是：

- 命中省掉重建的大头（payload 反序列化 + 引用完整性校验），只付一次索引查询；
- **多实例下自愈**：别人发布的快照会让本实例指纹变化，下一请求自动重建，
  不依赖广播（这也是「推 vs 拉」问题的轻量解法）；
- 明确的 ``invalidate()`` 仍保留，供本进程变更后少跑一次无用查询。

与基座同链时**返回基座本体**（而非新对象）：对象同一性有实际意义 ——
``runtime.semantic_layer`` / ``deps.semantic_layer`` / 热更新断言都依赖
"全局路径拿到的就是那一个层"。

基座也要能"拉"（本模块的第二个自愈层）
--------------------------------------
租户链的自愈有个盲区：**纯全局请求**（``tenant_id=""``，或本租户链与基座同链）压根不
走重建，节点读的是 ``deps`` 的进程级基座 —— 那份基座只有本进程被"推"（
``/semantic/registry/reload``）时才会更新。于是"另一个实例发布了新的 full 快照"
会表现为：本实例的租户请求已经看到新口径，全局请求继续看旧口径，直到有人手动点热更新。

修法是给基座也配一枚可比对的指纹：``registry.effective_chain_signature("")`` 回答
"DB 现在会让运行时解析成哪条链"，与基座自己的链（同一套归一化）等值比较，一致就什么都
不做，不一致就地重建基座并回写 ``runtime`` / ``deps``。为了不把请求路径变成查询热点，
比对做了**节流**（``base_probe_ttl_s``，默认 1 秒内共享一次）。代价是每请求多一条命中
索引的轻查询，换来的是"多实例免广播自愈"。``base_probe_ttl_s=0`` 表示每请求都探。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from dmf_agent.config.settings import Settings
from dmf_agent.semantic.engine import MetricFlowEngine
from dmf_agent.semantic.layer import SemanticLayer
from dmf_agent.semantic.overlay import chain_signature_of
from dmf_agent.semantic.registry import SemanticRegistry
from dmf_agent.semantic.runtime import ResolvedLayer, resolve_layer
from dmf_agent.semantic.scope import RequestScope

logger = logging.getLogger(__name__)


def _chain_key(resolved: Optional[ResolvedLayer]) -> Tuple[Any, ...]:
    """解析结果的可比较摘要：同链 ⇒ 可安全复用同一个层对象。"""
    if resolved is None:
        return ()
    return (
        resolved.effective_source,
        resolved.scope,
        resolved.base_version,
        tuple(resolved.overlay_versions or []),
        tuple(
            (
                c.get("tenant_id", ""),
                c.get("id"),
                c.get("version"),
                c.get("state", ""),
                c.get("mode", ""),
            )
            for c in (resolved.chain or [])
        ),
    )


@dataclass
class _Entry:
    signature: Tuple[Any, ...]
    resolved: ResolvedLayer
    engine: Optional[MetricFlowEngine]


class SemanticScopeService:
    """租户语义层的解析 + 指纹缓存（请求级装配的数据源）。"""

    def __init__(
        self,
        *,
        settings: Settings,
        base: ResolvedLayer,
        base_engine: Optional[MetricFlowEngine] = None,
        registry: Optional[SemanticRegistry] = None,
        client: Optional[Any] = None,
        max_entries: int = 64,
        base_probe_ttl_s: float = 1.0,
        yaml_base: Optional[SemanticLayer] = None,
        redis_client: Optional[Any] = None,
        pubsub_channel: str = "dmf:semantic_scope:invalidate",
    ) -> None:
        self._settings = settings
        self._base = base
        self._base_engine = base_engine
        self._registry = registry
        self._client = client
        # 跨进程失效：复用运行时已有的 Redis 客户端做 pub/sub 广播。
        # 无 Redis 时 _redis 为 None，退化为纯进程内缓存（与旧行为一致）。
        self._redis = redis_client
        self._channel = pubsub_channel
        self._pubsub_task: Optional[asyncio.Task] = None
        self._pubsub_ps: Optional[Any] = None
        self._max = max(1, int(max_entries))
        self._cache: "OrderedDict[str, _Entry]" = OrderedDict()
        # 单锁足够：重建很少发生（指纹一致即命中），不值得引入 per-key 锁的复杂度
        self._lock = asyncio.Lock()
        self._builds = 0
        self._hits = 0
        self._errors = 0

        # 基座自愈（"拉"）：_base_signature 是"上一次与 DB 对平过的全局生效链"。
        # 初值取自基座自己的链 —— 基座刚由 resolve_layer 从 DB 解析出来，那一刻它是新的，
        # 所以启动后第一次探测**不会**白重建一次、也不会churn 掉基座对象的同一性。
        # 对比过程若发现 DB 变成别的链，才重建。
        self._probe_ttl = max(0.0, float(base_probe_ttl_s))
        self._base_signature: Tuple[Any, ...] = chain_signature_of(base.chain)
        self._last_probe = 0.0
        self._base_rebuilds = 0
        self._probe_lock = asyncio.Lock()
        # 基座变更后的回写钩子（由 api/app.py 接到 apply_resolved）。刻意是可变属性而非
        # 构造参数：装配顺序里 scope 服务先于 AppRuntime 建出来，只能事后绑定。
        self.on_base_changed: Optional[
            Callable[[ResolvedLayer, Optional[MetricFlowEngine]], None]
        ] = None
        # 兜底基座：全局链被清空（全部废弃）时用它代替"上一份 DB 层"，语义上回到 YAML。
        # 惰性构造，失败则退回当前基座本体 —— 自愈绝不能因为读不到清单而把基座弄丢。
        self._yaml_base = yaml_base

    # ---------- 属性 ----------

    @property
    def base(self) -> ResolvedLayer:
        return self._base

    @property
    def enabled(self) -> bool:
        """是否具备租户解析条件：DB 源 + 仓储 + StarRocks 客户端齐备。

        客户端缺失时**不**装配租户层：层与引擎必须成对切换 —— 只换元信息不换引擎会
        出现"目录显示租户口径、取数仍按全局口径"的半应用状态，比不切换更难排查。
        生产装配（api/app.py）三者恒备，因此这条只在测试/降级路径上生效。
        """
        cfg = getattr(self._settings, "semantic", None)
        source = (getattr(cfg, "source", "yaml") or "yaml").strip().lower()
        return (
            source == "db" and self._registry is not None and self._client is not None
        )

    # ---------- 基座自愈（"拉"） ----------

    def _yaml_fallback(self) -> Optional[SemanticLayer]:
        """YAML 兜底基座（惰性构造一次）。"""
        if self._yaml_base is None:
            cfg = getattr(self._settings, "semantic", None)
            path = ((getattr(cfg, "manifest_dir", "") or "").strip()) or None
            try:
                self._yaml_base = SemanticLayer(path)
            except Exception as exc:  # noqa: BLE001 - 读不到清单不该让基座消失
                logger.warning("YAML 兜底基座构造失败（改用当前基座）：%s", exc)
                return None
        return self._yaml_base

    async def probe_base(self) -> bool:
        """公开的"拉"入口：探测全局链并按需重建基座；返回本次是否真的重建了。

        供管理面 ``GET /semantic/registry/runtime`` 调用 —— 那个接口只读
        ``runtime.semantic_resolution``，若没人问过问题就会显示"当前进程早已作废的基座"，
        面板上的 ``stale`` 也就永远亮着。让只读面板自己先拉一次，看到的就是真实状态。
        """
        return await self._probe_base()

    async def _probe_base(self) -> bool:
        """节流探测全局生效链；与基座不一致就地重建并回写运行时。

        判据是 ``effective_chain_signature("")``（"DB 现在会让运行时解析成哪条链"）与
        ``_base_signature``（"上一次对平过的链"）的等值比较 —— 两者用的是同一套归一化，
        所以相等就是真的没变，不必多花一次解析。

        节流口径：``_last_probe`` 为 0（本进程还没探过）时不受 TTL 限制，保证启动后第一次
        请求就完成比对；此后无论成功失败都受 TTL 约束，避免仓储持续故障时每请求都打一次库。
        """
        if not self.enabled or self._registry is None:
            return False
        now = time.monotonic()
        if self._last_probe and (now - self._last_probe) < self._probe_ttl:
            return False
        async with self._probe_lock:
            now = time.monotonic()
            if self._last_probe and (now - self._last_probe) < self._probe_ttl:
                return False
            self._last_probe = now
            try:
                signature = await self._registry.effective_chain_signature(tenant_id="")
            except Exception as exc:  # noqa: BLE001 - 探测失败保留旧基座
                self._errors += 1
                logger.warning("语义层全局链指纹读取失败（保留旧基座）：%s", exc)
                return False
            if self._base_signature == signature:
                return False
            await self._rebuild_base(signature)
            return True

    async def _rebuild_base(self, signature: Tuple[Any, ...]) -> None:
        """重建基座；``signature`` 是本次观测到的**DB 侧**指纹。

        记录的是**观测值**而不是"重建后基座自己的链指纹"：当 DB 里的快照 payload 损坏时，
        重建会回落 YAML（基座 chain 为空），若记基座自身指纹就会与 DB 永远不等，
        退化成每个节流窗口都重建一次。记观测值则"这次已经去看过了"，不再 churn。
        """
        try:
            resolved = await resolve_layer(
                settings=self._settings,
                tenant_id="",
                registry=self._registry,
                base_layer=self._yaml_fallback() or self._base.layer,
            )
        except Exception as exc:  # noqa: BLE001 - 重建失败保留旧基座
            self._errors += 1
            logger.warning("语义层基座自愈失败（保留旧基座）：%s", exc)
            return

        engine = (
            MetricFlowEngine(resolved.layer, self._client)
            if self._client is not None
            else None
        )
        previous = self._base
        self._set_base(resolved, engine, signature, notify=True)
        self._base_rebuilds += 1
        logger.info(
            "语义层基座已自愈（拉）：source=%s scope=%s 基座 v%s + overlay %s → 指标 %d 个"
            "（上一份基座 v%s）",
            resolved.effective_source,
            resolved.scope,
            resolved.base_version,
            resolved.overlay_versions or "无",
            len(resolved.layer.list_metrics()),
            previous.base_version,
        )

    def _set_base(
        self,
        resolved: ResolvedLayer,
        engine: Optional[MetricFlowEngine],
        signature: Tuple[Any, ...],
        *,
        notify: bool,
    ) -> None:
        self._base = resolved
        self._base_engine = engine
        self._base_signature = signature
        self.invalidate()
        if notify and self.on_base_changed is not None:
            try:
                self.on_base_changed(resolved, engine)
            except Exception:  # noqa: BLE001 - 回写失败不该影响本次取数
                logger.warning("语义层基座回写回调失败", exc_info=True)

    # ---------- 解析 ----------

    async def resolved_for(self, tenant_id: str = "") -> ResolvedLayer:
        """该租户生效的语义层（含溯源）；不可用/异常一律回落基座。"""
        tid = (tenant_id or "").strip()
        if not self.enabled:
            return self._base
        await self._probe_base()
        try:
            signature = await self._registry.chain_signature(tenant_id=tid)
        except Exception as exc:  # noqa: BLE001 - 仓储故障不阻断取数
            self._errors += 1
            logger.warning("语义层链指纹读取失败（回落基座，tenant=%r）：%s", tid, exc)
            return self._base

        cached = self._cache.get(tid)
        if cached is not None and cached.signature == signature:
            self._hits += 1
            self._cache.move_to_end(tid)
            return cached.resolved

        async with self._lock:
            # 双检：等锁期间可能已被并发请求建好（重建较慢时这很常见）
            cached = self._cache.get(tid)
            if cached is not None and cached.signature == signature:
                self._hits += 1
                return cached.resolved
            resolved = await self._build(tid)
            self._remember(tid, signature, resolved)
            return resolved

    async def scope_for(self, tenant_id: str = "") -> Optional[RequestScope]:
        """本请求应绑定的作用域；解析结果与基座同链时返回 None（无需覆盖）。

        即使本请求**不需要**作用域覆盖（全局请求 / 该租户与基座同链），也必须先探一次
        基座：全局请求读的正是进程级基座，它落后了就等于口径落后。
        """
        await self._probe_base()
        tid = (tenant_id or "").strip()
        if not self.enabled or not tid:
            # 全局 / YAML 源：无需覆盖，节点的 deps 基座即为正确值
            return None
        resolved = await self.resolved_for(tid)
        if resolved is self._base:
            return None
        entry = self._cache.get(tid)
        engine = entry.engine if entry is not None else self._base_engine
        return RequestScope(
            tenant_id=tid, layer=resolved.layer, engine=engine, resolved=resolved
        )

    async def _build(self, tenant_id: str) -> ResolvedLayer:
        # 只把**仓储读取**包进 try（与 services/semantic_layer.py 同口径）：装配期代码
        # 缺陷必须暴露，否则会被静默降级成"看起来正常的全局层"，排查零线索。
        try:
            resolved = await resolve_layer(
                settings=self._settings,
                tenant_id=tenant_id,
                registry=self._registry,
                base_layer=self._base.layer,
            )
        except Exception as exc:  # noqa: BLE001 - 仓储故障不阻断取数
            self._errors += 1
            logger.warning("租户语义层解析失败（回落基座，tenant=%r）：%s", tenant_id, exc)
            return self._base

        if _chain_key(resolved) == _chain_key(self._base):
            # 与基座同链（该租户无 overlay / 链未变）：复用基座本体，省一次重建并保持同一性
            return self._base

        self._builds += 1
        logger.info(
            "语义层租户装配：tenant=%r scope=%s 基座 v%s + overlay %s → 指标 %d 个",
            tenant_id or "(全局)",
            resolved.scope,
            resolved.base_version,
            resolved.overlay_versions or "无",
            len(resolved.layer.list_metrics()),
        )
        return resolved

    def _remember(
        self, tenant_id: str, signature: Tuple[Any, ...], resolved: ResolvedLayer
    ) -> None:
        if resolved is self._base:
            engine = self._base_engine
        else:
            # `enabled` 已保证 client 存在：层与引擎必须成对切换，不允许只换其一
            engine = MetricFlowEngine(resolved.layer, self._client)
        self._cache[tenant_id] = _Entry(signature=signature, resolved=resolved, engine=engine)
        self._cache.move_to_end(tenant_id)
        while len(self._cache) > self._max:
            evicted, _ = self._cache.popitem(last=False)
            logger.debug("语义层租户缓存淘汰：tenant=%r", evicted)

    # ---------- 失效 / 重定基 ----------

    def _invalidate_local(self, tenant_id: Optional[str] = None) -> int:
        """仅本进程清缓存（``tenant_id=None`` 清全部）；返回清理条数。"""
        if tenant_id is None:
            count = len(self._cache)
            self._cache.clear()
        else:
            count = 1 if self._cache.pop((tenant_id or "").strip(), None) else 0
        if count:
            logger.debug("语义层租户缓存已失效：%s", tenant_id or "(全部)")
        return count

    def invalidate(self, tenant_id: Optional[str] = None) -> int:
        """清空本地缓存并广播跨进程失效信号（仅本实例可经 Redis 通知其它 worker）。

        指纹比对已能自愈（别的 worker 下一请求自动重建），这里把"等 1s probe TTL"
        压缩到"即时"：任一 worker 发布/审批/回滚后，其余 worker 经 pub/sub 立即本地失效。
        无 Redis 时只做本地失效（退化为旧行为）。广播失败仅告警、不阻断主链路。
        """
        count = self._invalidate_local(tenant_id)
        if self._redis is not None:
            # 运行中的事件循环内才发广播；纯同步路径（无 loop）跳过，本地已失效。
            try:
                asyncio.create_task(self._publish_invalidation(tenant_id))
            except RuntimeError:
                pass
        return count

    # ---------- 跨进程失效（Redis pub/sub） ----------

    async def start(self) -> None:
        """启动监听：订阅 Redis 频道，收到广播即本地 invalidate。

        仅当传入可用 Redis 客户端时生效；订阅失败仅告警、退化为进程内缓存，
        绝不拖慢或打断取数主链路。
        """
        if self._redis is None or getattr(self._redis, "_client", None) is None:
            return
        if self._pubsub_task is not None:
            return
        try:
            self._pubsub_ps = self._redis._client.pubsub()
            await self._pubsub_ps.subscribe(self._channel)
            self._pubsub_task = asyncio.create_task(self._listen())
            logger.info("语义层租户缓存跨进程失效监听已启动：channel=%s", self._channel)
        except Exception as exc:  # noqa: BLE001 - 订阅失败仅告警
            logger.warning("语义层租户缓存 pubsub 订阅失败（退化为进程内）：%s", exc)
            self._pubsub_task = None
            self._pubsub_ps = None

    async def stop(self) -> None:
        """停止监听并释放订阅（进程退出时调用）。"""
        task = self._pubsub_task
        self._pubsub_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 取消即终态
                pass
        ps = self._pubsub_ps
        self._pubsub_ps = None
        if ps is not None:
            try:
                await ps.unsubscribe(self._channel)
                await ps.aclose()
            except Exception:  # noqa: BLE001
                pass

    async def _listen(self) -> None:
        ps = self._pubsub_ps
        if ps is None:
            return
        try:
            async for msg in ps.listen():
                if not isinstance(msg, dict) or msg.get("type") != "message":
                    continue
                try:
                    data = json.loads(msg.get("data") or "{}")
                    tid = data.get("tenant_id")
                    # 仅本地失效、不回发（避免广播风暴）
                    self._invalidate_local(tid)
                    logger.debug("语义层租户缓存跨进程失效：tenant=%r", tid)
                except Exception as exc:  # noqa: BLE001 - 单条消息失败不杀死监听
                    logger.warning("语义层租户缓存 pubsub 消息处理失败：%s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 监听断开仅告警
            logger.warning("语义层租户缓存 pubsub 监听中断（退化为进程内）：%s", exc)

    async def _publish_invalidation(self, tenant_id: Optional[str]) -> None:
        if self._redis is None or getattr(self._redis, "_client", None) is None:
            return
        try:
            payload = json.dumps({"tenant_id": tenant_id, "ts": time.time()}, ensure_ascii=False)
            await self._redis._client.publish(self._channel, payload)
        except Exception as exc:  # noqa: BLE001 - 发布失败不影响本地已失效
            logger.warning("语义层租户缓存失效广播失败：%s", exc)

    def rebase(self, resolved: ResolvedLayer, engine: Optional[MetricFlowEngine]) -> None:
        """热更新：把基座换成新解析结果并清空租户缓存。

        基座必须跟着热更新走 —— 否则租户缓存被清空后会拿旧基座重建，
        出现"面板显示已更新、实际取数仍是旧定义"。

        指纹取自 ``resolved.chain``：调用方（``_do_refresh``）刚解析过，基座本身是新的，
        它自己的链就是"上一次对平过的链"，于是下一次探测不会白重建一次。

        ``notify=False``：调用方自己就是"推"的发起者，回写已经做完；这里再回调会
        形成 ``rebase → on_base_changed → rebase`` 自环。
        """
        self._set_base(resolved, engine, chain_signature_of(resolved.chain), notify=False)

    # ---------- 观测 ----------

    def stats(self) -> Dict[str, Any]:
        """缓存与基座概况（管理面「语义层来源」面板用）。"""
        tenants: List[Dict[str, Any]] = [
            {
                "tenant_id": tid,
                "scope": entry.resolved.scope,
                "base_version": entry.resolved.base_version,
                "overlay_versions": list(entry.resolved.overlay_versions or []),
                "snapshots": len(entry.signature),
            }
            for tid, entry in self._cache.items()
        ]
        return {
            "enabled": self.enabled,
            "cached_tenants": len(self._cache),
            "max_entries": self._max,
            "builds": self._builds,
            "hits": self._hits,
            "errors": self._errors,
            "tenants": tenants,
            # 基座"拉"式自愈的可见性：重建次数 + 节流窗口 + 对平过的链规模
            "base_rebuilds": self._base_rebuilds,
            "base_probe_ttl_s": self._probe_ttl,
            "base_signature_len": len(self._base_signature),
            "base_base_version": self._base.base_version,
            "base_effective_source": self._base.effective_source,
        }
