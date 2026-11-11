"""行业级黑话词典内置种子（银行零售 + 通用电商）。

设计原则：
1. **只收「语义层没有的东西」或「别名覆盖不到的写法」** —— 语义层 `manifest.yml` 里已有的
   `aliases`（如"存款余额 -> 零售存款余额"）由 `MetricRecaller` 负责召回；本种子负责
   **问句改写**需要的那部分（口语、缩写、行话、单位词、停用词、维值口语）。
2. **`standard` 必须真实存在于语义层**（`validate_standard` 强校验），否则改写出来的
   标准名会解析失败 —— 那比不写词条更糟。
3. 口径有出入的一律在 `remark` 里写明风险，管理页标黄可查
   （如 AUM 严格指管理资产规模，系统暂无该指标，仅作近似；息差/NIM 是比率而净利息收入是绝对额）。
4. 全部落 `scope=industry` + `status=published` + `source=builtin`，
   即「平台内置且可直接被用户停用」，而不是硬编码在代码里。

维值类（`entry_type=dim_value`）的 `dim_value` 取自底表真实 DISTINCT 值
（见 `deploy/local/README.md`：机构 4 / 零售渠道 3 / 产品类型 4 / 客户类型 2 / 客户等级 2）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any, Dict, Iterable, List, Optional

from dmf_agent.schemas.slang import (
    ENTRY_DIM_VALUE,
    ENTRY_DIMENSION,
    ENTRY_METRIC,
    ENTRY_STOP,
    ENTRY_TERM,
    MODE_EXPAND,
    MODE_REPLACE,
    MODE_STOP,
    SCOPE_INDUSTRY,
    SOURCE_BUILTIN,
    STATUS_PUBLISHED,
    SlangEntryData,
)

logger = logging.getLogger(__name__)


# ---------------- 指标黑话（term -> 标准指标名，expand） ----------------
_METRICS: List[Dict[str, Any]] = [
    # 存款
    {"term": "AUM", "standard": "零售存款余额",
     "remark": "口径提醒：AUM 严格指管理资产规模，系统暂无该指标，此处为近似映射"},
    {"term": "管理资产规模", "standard": "零售存款余额", "remark": "同上，近似映射"},
    {"term": "资产规模", "standard": "零售存款余额", "remark": "同上，近似映射"},
    {"term": "DDA", "standard": "零售存款余额",
     "remark": "口径提醒：DDA 指活期存款日均，系统暂无该指标，此处为近似映射"},
    {"term": "储蓄存款", "standard": "零售存款余额"},
    {"term": "储蓄余额", "standard": "零售存款余额"},
    {"term": "个人存款", "standard": "零售存款余额"},
    {"term": "存款规模", "standard": "零售存款余额"},
    # 贷款
    {"term": "放款余额", "standard": "零售贷款余额"},
    {"term": "信贷余额", "standard": "零售贷款余额"},
    {"term": "个贷", "standard": "零售贷款余额"},
    {"term": "个贷余额", "standard": "零售贷款余额"},
    {"term": "个人贷款", "standard": "零售贷款余额"},
    {"term": "贷款规模", "standard": "零售贷款余额"},
    # 利息
    {"term": "付息成本", "standard": "存款利息支出"},
    {"term": "存款成本", "standard": "存款利息支出"},
    {"term": "付息率成本", "standard": "存款利息支出"},
    {"term": "利息收入", "standard": "贷款利息收入"},
    {"term": "收息收入", "standard": "贷款利息收入"},
    {"term": "NII", "standard": "净利息收入", "remark": "英文缩写：Net Interest Income"},
    {"term": "息差", "standard": "净利息收入",
     "remark": "口径提醒：息差 / NIM 是比率，净利息收入是绝对额，此处为近似映射"},
    # 风险
    {"term": "不良", "standard": "不良贷款余额"},
    {"term": "不良额", "standard": "不良贷款余额"},
    {"term": "不良资产", "standard": "不良贷款余额"},
    {"term": "不良比率", "standard": "不良贷款率"},
    # 客户
    {"term": "客户数", "standard": "贷款客户数"},
    {"term": "贷款客户", "standard": "贷款客户数"},
    {"term": "户均", "standard": "户均贷款"},
    {"term": "笔均贷款", "standard": "户均贷款"},
    # 比例
    {"term": "存贷比例", "standard": "存贷比"},
    {"term": "贷存比", "standard": "存贷比"},
]

# ---------------- 维度黑话（term -> 标准维度名，expand） ----------------
_DIMENSIONS: List[Dict[str, Any]] = [
    {"term": "分行", "standard": "机构"},
    {"term": "支行", "standard": "机构"},
    {"term": "网点", "standard": "机构"},
    {"term": "经营机构", "standard": "机构"},
    {"term": "所在机构", "standard": "机构"},
    {"term": "贷种", "standard": "产品类型"},
    {"term": "存款产品", "standard": "产品类型"},
    {"term": "贷款产品", "standard": "产品类型"},
    {"term": "客群", "standard": "客户类型"},
    {"term": "客层", "standard": "客户等级"},
    {"term": "星级", "standard": "客户等级"},
    {"term": "手机渠道", "standard": "零售渠道"},
]

# ---------------- 枚举值黑话（term -> 维度 + 真实维值，expand 追加维值） ----------------
_DIM_VALUES: List[Dict[str, Any]] = [
    # 维值取底表真实值：零售渠道 = 手机银行 / 柜面 / 网上银行
    {"term": "APP", "standard": "零售渠道", "dim_value": "手机银行"},
    {"term": "手机端", "standard": "零售渠道", "dim_value": "手机银行"},
    {"term": "网银", "standard": "零售渠道", "dim_value": "网上银行"},
    {"term": "网银端", "standard": "零售渠道", "dim_value": "网上银行"},
    {"term": "柜台", "standard": "零售渠道", "dim_value": "柜面"},
    {"term": "临柜", "standard": "零售渠道", "dim_value": "柜面"},
    # 产品类型 = 大额存单 / 定期存款 / 活期存款 / 结构性存款
    {"term": "结构存款", "standard": "产品类型", "dim_value": "结构性存款"},
    {"term": "存单", "standard": "产品类型", "dim_value": "大额存单"},
    {"term": "活期", "standard": "产品类型", "dim_value": "活期存款"},
    {"term": "定存", "standard": "产品类型", "dim_value": "定期存款"},
    # 客户类型 = 个人 / 个人经营
    {"term": "经营贷", "standard": "客户类型", "dim_value": "个人经营"},
    {"term": "对私", "standard": "客户类型", "dim_value": "个人"},
    {"term": "对私客户", "standard": "客户类型", "dim_value": "个人"},
    # 客户等级 = 普通 / 贵宾
    {"term": "贵宾客户", "standard": "客户等级", "dim_value": "贵宾"},
    {"term": "私人银行客户", "standard": "客户等级", "dim_value": "贵宾"},
]

# ---------------- 术语 / 单位（replace：原地替换为规范写法） ----------------
_TERMS: List[Dict[str, Any]] = [
    {"term": "个亿", "standard": "亿", "rewrite_mode": MODE_REPLACE, "remark": "单位归一"},
    {"term": "万块", "standard": "万", "rewrite_mode": MODE_REPLACE, "remark": "单位归一"},
    {"term": "块钱", "standard": "元", "rewrite_mode": MODE_REPLACE, "remark": "单位归一"},
    {"term": "个百分点", "standard": "%", "rewrite_mode": MODE_REPLACE, "remark": "单位归一"},
    # 通用电商域（与本项目既有 trade_day / user_day 语义模型对齐）
    {"term": "成交总额", "standard": "GMV", "remark": "同义扩展"},
    {"term": "营业额", "standard": "GMV", "remark": "同义扩展"},
    {"term": "单量", "standard": "订单数", "remark": "同义扩展"},
    {"term": "付费人数", "standard": "付费用户数", "remark": "同义扩展"},
    {"term": "重复购买率", "standard": "复购率", "remark": "同义扩展"},
]

# ---------------- 停用词（stop：命中即从问句删除，口语噪声） ----------------
_STOPS: List[Dict[str, Any]] = [
    {"term": "帮我看看"},
    {"term": "帮我看下"},
    {"term": "帮我查下"},
    {"term": "帮忙看下"},
    {"term": "麻烦帮我"},
    {"term": "麻烦问下"},
    {"term": "请问一下"},
    {"term": "我想知道"},
    {"term": "想知道"},
    {"term": "查一下"},
    {"term": "看一下"},
    {"term": "看下"},
    {"term": "一下"},
    {"term": "顺便"},
    {"term": "那个"},
    {"term": "这个"},
]


def _entry(row: Dict[str, Any], entry_type: str) -> SlangEntryData:
    return SlangEntryData(
        scope=SCOPE_INDUSTRY,
        tenant_id="",
        user_id="",
        term=str(row.get("term") or "").strip(),
        standard=str(row.get("standard") or "").strip() if row.get("standard") else "",
        entry_type=entry_type,
        dim_value=str(row.get("dim_value") or ""),
        rewrite_mode=str(row.get("rewrite_mode") or (MODE_STOP if entry_type == ENTRY_STOP else MODE_EXPAND)),
        priority=int(row.get("priority") or 100),
        source=SOURCE_BUILTIN,
        status=STATUS_PUBLISHED,
        version=1,
        confidence=1.0,
        remark=str(row.get("remark") or ""),
        created_by="builtin",
        updated_by="builtin",
    )


# 自映射护栏优先级：高于所有手工黑话，确保"长标准名"优先占位
GUARD_PRIORITY = 500


def build_guard_entries(
    metric_names: Iterable[str],
    dimension_names: Iterable[str],
    metric_aliases: Optional[Dict[str, str]] = None,
) -> List[SlangEntryData]:
    """护栏词条：把语义层的**标准名自身**与**标准别名**注册为最高优先级词条。

    为什么需要：黑话多为标准名（或其别名）的子串，例如"不良"既是"不良贷款率"的子串，
    也是别名"不良率"的子串。若只注册"不良 -> 不良贷款余额"，则用户问"不良贷款率"或
    "不良率"时都会被错误扩展成"不良贷款余额"（比率被换成余额）。

    把标准名/别名设为最高优先级后，**最长优先**自然让长词先占住区间，短黑话不再误命中：
      - 标准名自映射（term == standard）的 expand 是空操作（标准名已在句中），改写句不变；
      - 别名（term=别名, standard=标准名）的 expand 追加标准名，顺带完成"别名归一"，
        与 `MetricRecaller` 的别名召回口径一致，不会造成语义漂移。

    `metric_aliases` 形如 `{"不良率": "不良贷款率", ...}`（别名 -> 标准指标名）。
    """
    entries: List[SlangEntryData] = []
    for name in metric_names:
        if name:
            entries.append(
                _entry(
                    {"term": name, "standard": name, "priority": GUARD_PRIORITY,
                     "remark": "自映射护栏：防止长标准名内部误命中短黑话"},
                    ENTRY_METRIC,
                )
            )
    for alias, canonical in (metric_aliases or {}).items():
        alias = str(alias or "").strip()
        canonical = str(canonical or "").strip()
        if alias and canonical and alias != canonical:
            entries.append(
                _entry(
                    {"term": alias, "standard": canonical, "priority": GUARD_PRIORITY,
                     "remark": "别名护栏：防止别名内部误命中短黑话（顺带别名归一）"},
                    ENTRY_METRIC,
                )
            )
    for name in dimension_names:
        if name:
            entries.append(
                _entry(
                    {"term": name, "standard": name, "priority": GUARD_PRIORITY,
                     "remark": "自映射护栏：防止长标准名内部误命中短黑话"},
                    ENTRY_DIMENSION,
                )
            )
    return entries


def build_seed_entries(
    metric_names: Optional[Iterable[str]] = None,
    dimension_names: Optional[Iterable[str]] = None,
    metric_aliases: Optional[Dict[str, str]] = None,
) -> List[SlangEntryData]:
    """构建内置行业级种子（黑话 + 术语 + 停用词 + 标准名/别名护栏）。

    `metric_names` / `dimension_names` / `metric_aliases` 传入语义层的真实名字以生成护栏；
    省略时不生成护栏（仅用于离线查看黑话部分）。
    """
    entries: List[SlangEntryData] = []
    for row in _METRICS:
        entries.append(_entry(row, ENTRY_METRIC))
    for row in _DIMENSIONS:
        entries.append(_entry(row, ENTRY_DIMENSION))
    for row in _DIM_VALUES:
        entries.append(_entry(row, ENTRY_DIM_VALUE))
    for row in _TERMS:
        entries.append(_entry(row, ENTRY_TERM))
    for row in _STOPS:
        entries.append(_entry(row, ENTRY_STOP))
    entries.extend(
        build_guard_entries(
            metric_names or [], dimension_names or [], metric_aliases or {}
        )
    )
    # 去重：唯一键 = (scope, tenant_id, user_id, entry_type, term)。
    # 黑话条目在护栏之前构建，因此**显式黑话优先**（如"笔均贷款"既是黑话又是别名，
    # 保留黑话条目，丢弃重复的护栏条目）。
    deduped: List[SlangEntryData] = []
    seen: set = set()
    for e in entries:
        if not e.term:
            continue
        key = (e.scope, e.tenant_id, e.user_id, e.entry_type, e.term)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)
    return deduped


def collect_guard_sources(layer: Any) -> Dict[str, Any]:
    """从语义层收集护栏所需的名称与别名（供 seed()/app 启动共用）。"""
    metric_names = [m.name for m in layer.list_metrics()]
    dimension_names = list(layer.dimension_names())
    metric_aliases: Dict[str, str] = {}
    for m in layer.list_metrics():
        for alias in getattr(m, "aliases", None) or []:
            alias = str(alias or "").strip()
            if alias and alias != m.name:
                metric_aliases[alias] = m.name
    return {
        "metric_names": metric_names,
        "dimension_names": dimension_names,
        "metric_aliases": metric_aliases,
    }


async def seed(dry_run: bool = False) -> Dict[str, Any]:
    """把内置种子写入行业级（整体替换 `source=builtin` 的行业级词条）。"""
    from dmf_agent.config.settings import Settings
    from dmf_agent.db.factory import build_database
    from dmf_agent.recall.slang import validate_standard
    from dmf_agent.semantic.layer import SemanticLayer

    settings = Settings()
    layer = SemanticLayer()
    bundle = build_database(settings)
    await bundle.initialize(settings)

    entries = build_seed_entries(**collect_guard_sources(layer))
    invalid: List[str] = []
    for e in entries:
        err = validate_standard(layer, e.entry_type, e.standard, e.dim_value)
        if err:
            invalid.append(f"{e.term} -> {err}")

    report: Dict[str, Any] = {
        "total": len(entries),
        "invalid": invalid,
        "written": 0,
        "dry_run": dry_run,
    }
    if invalid or dry_run:
        return report
    report["written"] = await bundle.slang_repo.replace_scope_entries(
        SCOPE_INDUSTRY, "", "", entries
    )
    from dmf_agent.recall.slang import build_slang_resolver

    resolver = build_slang_resolver(bundle.slang_repo, bundle.redis_client, settings.slang)
    report["cached"] = await resolver.refresh_layer(SCOPE_INDUSTRY)
    await bundle.dispose()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="黑话词典行业级种子（可重复执行）")
    parser.add_argument("--seed", action="store_true", help="写入行业级种子（默认只校验）")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    # 默认 dry-run（只校验），显式 --seed 才写库，避免误执行
    report = asyncio.run(seed(dry_run=not args.seed))
    print(f"条目数 = {report['total']}")
    if report["invalid"]:
        print("校验未通过：")
        for line in report["invalid"]:
            print("  -", line)
        raise SystemExit(1)
    if report["dry_run"]:
        print("dry-run：校验全部通过，未写库（加 --seed 写入）")
    else:
        print(f"已写入行业级 {report['written']} 条；Redis 缓存已同步 {report.get('cached')} 条")


if __name__ == "__main__":
    main()
