# StarRocks 示例底表

主流程脱离指标平台后，取数链路为：

```
语义层(manifest.yml) → MetricFlowSqlCompiler → StarRocks SQL → aiomysql 执行
```

本目录提供**示例指标底表**，用于把这条链路端到端跑通（不含任何 mock）。

| 文件 | 说明 |
|---|---|
| `dws_schema.sql` | 两张 DWS 预聚合宽表 DDL，与 `backend/dmf_agent/semantic/manifest.yml` 一一对应 |
| `seed_dws.py` | 确定性造数脚本；可只生成 SQL，也可直连建表灌数并校验 |
| `dws_seed.sql` | 生成产物（**不入库**，见根 `.gitignore`），由下面的命令随时重建 |

## 语义模型 ↔ 物理表

| 语义模型 | 物理表 | 度量（measure） | 维度 |
|---|---|---|---|
| `trade_day` | `dws_trade_day` | `gmv_amount` / `order_count` / `paid_user_count` / `repurchase_user_count` | 统计日期 渠道 地区 品类 平台 门店 |
| `user_day` | `dws_user_day` | `active_user_count` / `new_user_count` | 统计日期 渠道 平台 |

两表共享 **统计日期 / 渠道 / 平台**，因此跨模型指标（`付费转化率`、`复购率`）
可在编译器生成的 `FULL OUTER JOIN` 上计算；`客单价` 是交易域内派生指标。

## 使用

```bash
cd <repo-root>

# 1) 配置连接（写入 backend/.env）
cat >> backend/.env <<'EOF'
DMF_STARROCKS-HOST=<FE_HOST>
DMF_STARROCKS-PORT=9030
DMF_STARROCKS-USER=root
DMF_STARROCKS-PASSWORD=<password>
DMF_STARROCKS-DATABASE=dmf_demo
EOF

# 2) 建库 + 建表 + 灌数（默认：近 42 天 + 去年同期 11 天）
backend/.venv/bin/python deploy/starrocks/seed_dws.py --execute --create-database --verify

# 或：只生成 SQL，人工审查后用 mysql 客户端执行
backend/.venv/bin/python deploy/starrocks/seed_dws.py --emit-sql deploy/starrocks/dws_seed.sql
mysql -h<FE_HOST> -P9030 -u<USER> -p < deploy/starrocks/dws_seed.sql
```

## 数据说明

- **确定性**：随机数由固定 seed（默认 `20260911`）驱动，同一 `(seed, 日期范围)`
  必得同一份数据；改范围或 seed 即可换一批。
- **时间覆盖**：默认当期 `今天-41天 ~ 今天`（够算环比），外加
  `去年同日-10天 ~ 去年同日`（够算同比）。
- **量级**：约 7.6k 行交易域 + 0.5k 行用户域（约 0.6 MB SQL）。
- **口径自洽**：付费/复购用户先在 (渠道 × 平台) 定量，再分摊到
  (地区, 品类, 门店)，因此按渠道汇总时 `付费用户数 < 日活跃用户数`，
  付费转化率落在 22%~35%、复购率落在 8%~12%，不会出现 >100% 的脏结论。
- 非门店渠道的 `store_name` 为空串（该渠道无门店归属），按门店分组时会出现一行 `''`。

## 校验链路

```bash
# 语义清单结构 + 引用完整性
cd backend && ./.venv/bin/python -m dmf_agent.semantic check

# 只编译（不连库）——确认生成的 SQL 与底表列名一致
./.venv/bin/python -m dmf_agent.semantic sql \
  "metric:付费转化率 group_by:统计日期,渠道 time:2026-09-05~2026-09-11:day"

# 编译并执行（需已配置 StarRocks）
./.venv/bin/python -m dmf_agent.semantic query \
  "metric:GMV,客单价 group_by:统计日期 time:2026-09-05~2026-09-11:day"
```

`StarRocksSettings.join_type` 默认为 `full`（跨模型用 `FULL OUTER JOIN`）；
若实例对 `FULL OUTER JOIN` 支持不佳，可改为 `left` 让主模型驱动。
