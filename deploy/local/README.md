# 本地 MySQL 跑语义层 / StarRocks 全链路

> 没有真实 StarRocks 时，用本地 MySQL 替代 `DMF_STARROCKS-*` 数据源。
> Aiomysql 协议兼容（StarRocks FE 也用 MySQL 协议），所以 dialect=mysql 时
> semantic/compiler.py 自动改写 StarRocks 方言到 MySQL 兼容写法，跑通「语义层编译 → 真实执行 → 数据返回」。

## 一次性配置

1) `backend/.env` 把 `DMF_STARROCKS-*` 指向本地 MySQL（同 DMF_DB-*），
   并追加 `DMF_STARROCKS-DIALECT=mysql`。

   ```ini
   DMF_STARROCKS-HOST=127.0.0.1
   DMF_STARROCKS-PORT=3306
   DMF_STARROCKS-USER=dmf_agent
   DMF_STARROCKS-PASSWORD=<与 DMF_DB-PASSWORD 一致>
   DMF_STARROCKS-DATABASE=dmf_agent
   DMF_STARROCKS-CHARSET=utf8mb4
   DMF_STARROCKS-POOL_MIN_SIZE=1
   DMF_STARROCKS-POOL_MAX_SIZE=5
   DMF_STARROCKS-DIALECT=mysql     # 关键：让编译器生成 MySQL 兼容 SQL
   ```

2) 建表 + 灌数（用 backend venv）：

   ```bash
   cd backend
   ./.venv/bin/python ../deploy/local/seed_mysql.py --execute   # 建表 + 灌数
   ./.venv/bin/python ../deploy/local/seed_mysql.py --verify    # 仅校验
   ./.venv/bin/python ../deploy/local/seed_mysql.py --emit-sql /tmp/dws_seed_mysql.sql  # 只导出
   ```

3) 重启后端（启动时会按 `DIALECT=mysql` 编译 SQL）：

   ```bash
   cd backend && ./run.sh
   ```

## 已自动兼容的方言差异

| StarRocks | MySQL（dialect=mysql 替换） |
|---|---|
| `date_trunc('day', x)` | `DATE(x)` |
| `date_trunc('month', x)` | `STR_TO_DATE(DATE_FORMAT(x,'%Y-%m-01'),'%Y-%m-%d')` |
| `date_trunc('week', x)` | `DATE_SUB(DATE(x), INTERVAL WEEKDAY(x) DAY)` |
| `date_trunc('quarter', x)` | `DATE_ADD(MAKEDATE(YEAR(x),1), INTERVAL (QUARTER(x)-1) QUARTER)` |
| `date_trunc('year', x)` | `STR_TO_DATE(DATE_FORMAT(x,'%Y-01-01'),'%Y-%m-%d')` |
| `add_months(x, n)` | `DATE_ADD(x, INTERVAL n MONTH)` |
| `FULL OUTER JOIN a ON b` | `LEFT JOIN a ON b`（缺一侧值的行表现为 NULL，ratio 退化） |

> 设计取舍：MySQL 没有 FULL OUTER JOIN；本项目强制降级为 LEFT JOIN。
> 真实业务里（例如付费用户数 < 日活）active 不会为 NULL，影响只在边缘日期出现。

## 数据规模（默认）

- 本期：近 42 天（默认 end=2026-09-11 → 2026-08-01 起）
- 去年同期：同期 42 天（默认覆盖 2025-08-01~09-11，让 `period_comparison=yoy` 能拿到前期值）
- 表行数：`dws_trade_day ≈ 12000` 行、`dws_user_day ≈ 756` 行

可调：
```bash
./.venv/bin/python ../deploy/local/seed_mysql.py --execute --end 2026-09-11 --past-days 90 --last-year-days 90
```

## 列名对齐

完全对齐 `backend/dmf_agent/semantic/manifest.yml`：

| 维度 | expr |
|---|---|
| 统计日期 | stat_date |
| 渠道 / 地区 / 品类 / 平台 / 门店 | channel_name / region_name / category_name / platform_name / store_name |

| 度量 | expr | 单位 |
|---|---|---|
| gmv_amount | gmv | 元 |
| order_count | order_cnt | 笔 |
| paid_user_count | paid_user_cnt | 人 |
| repurchase_user_count | repurchase_user_cnt | 人 |
| active_user_count | active_user_cnt | 人 |
| new_user_count | new_user_cnt | 人 |

## 端到端验证清单

```bash
# 1) StarRocks 配置生效
curl -sS http://127.0.0.1:8000/api/v1/semantic/health
# 期望: {"reachable":true,"mode":"starrocks","detail":"127.0.0.1:3306",...}

# 2) 编译（产 SQL 不执行）
curl -sS -X POST http://127.0.0.1:8000/api/v1/semantic/compile \
  -H 'Content-Type: application/json' \
  -d '{"metrics":["GMV"],"group_by":["统计日期","渠道"],"time_range":{"start":"2026-09-05","end":"2026-09-11","granularity":"day"},"limit":50}'

# 3) 真实执行（看真实数据流出，HTTP 200 + rows）
curl -sS -X POST http://127.0.0.1:8000/api/v1/semantic/query \
  -H 'Content-Type: application/json' \
  -d '{"metrics":["付费转化率"],"group_by":["统计日期","渠道"],"time_range":{"start":"2026-09-09","end":"2026-09-11","granularity":"day"},"limit":50}'

# 4) 同环比（要求去年同期数据存在）
curl -sS -X POST http://127.0.0.1:8000/api/v1/semantic/query \
  -H 'Content-Type: application/json' \
  -d '{"metrics":["GMV"],"group_by":["统计日期"],"time_range":{"start":"2026-08-15","end":"2026-08-31","granularity":"day"},"period_comparison":true,"period_comparison_type":"yoy","limit":50}'
```

## 银行零售存款 / 贷款底表（2026-09-14 新增）

除交易 / 用户两域外，另有一套**银行零售存款 / 贷款**底表，用于验收语义层的跨域比率、同环比与二次分析：

| 物理表 | 语义模型 | 度量（measure） |
|---|---|---|
| `dws_retail_deposit_day` | `retail_deposit_day` | `retail_deposit_balance` / `retail_deposit_interest` / `retail_deposit_customer` |
| `dws_retail_loan_day` | `retail_loan_day` | `retail_loan_balance` / `retail_loan_interest` / `retail_npl_balance` / `retail_loan_customer` |

- 维度（两表**完全一致**，跨表指标才能按任意维度分组）：
  `stat_date / org_name / channel_name / product_type / customer_type / customer_level`
- 覆盖 10 个指标：原子 6（零售存款余额、零售贷款余额、存款利息支出、贷款利息收入、不良贷款余额、贷款客户数）
  + 衍生 2（净利息收入、户均贷款）+ 派生 2（存贷比、不良贷款率）
- 规模：每表 16128 行（192 组合/天 × 84 天，含去年同期）；存款 ≈ 1200 亿、贷款 ≈ 900 亿、存贷比 ≈ 76%

```bash
cd backend
./.venv/bin/python ../deploy/local/seed_bank_mysql.py --execute --verify   # 建表 + 灌数 + 体检
./.venv/bin/python ../deploy/local/seed_bank_mysql.py --verify             # 仅体检
./.venv/bin/python ../deploy/local/seed_bank_mysql.py --emit-sql /tmp/bank_seed_mysql.sql
```

数据窗口默认 `end=今天`、近 42 天 + 去年同期 42 天（去年同期按 0.94 折算，让同比有真实增长）；
明细数据由 `deploy/starrocks/seed_bank.py` 确定性生成（seed=20260913），与 StarRocks 版同 seed 同输出。

> **客户数类 measure 必须用 `SUM`**：户数按机构加总是银行业标准口径。
> 早期误用 `MAX`（想表达"期间峰值"）会让"当日全行客户数"退化为**单个维度组合的最大值**，
> 户均贷款从 24.6 万放大到 528 万，已在 `tests/test_semantic.py` 加回归用例锁定。
> 客户数在"跨时间"方向不可加总（SUM 会退化成"户日"），需要期间均值时用二次分析的聚合覆盖表达。

验收清单见 [`doc/银行零售指标-管理员手工测试清单.md`](../../doc/银行零售指标-管理员手工测试清单.md)。

## 注意事项

- `seed_mysql.py` 假定 `Settings()` 已能读到 `.env`（cwd 必须在 `backend/` 下）。
- aiomysql 不认 `database=` 关键字，要写 `db=`（脚本已处理）。
- 后端**未开 `--reload`**，改了 `compiler.py` / `settings.py` 必须重启。
- 真 StarRocks 上线后，把 `.env` 的 `DMF_STARROCKS-DIALECT=starrocks`（默认），其它字段指向真 StarRocks 即可。
