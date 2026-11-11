-- ============================================================================
-- 银行零售（存款 / 贷款）语义底表（StarRocks）
--   与 backend/dmf_agent/semantic/manifest.yml 的两个语义模型一一对应：
--     dws_retail_deposit_day -> semantic_model: retail_deposit_day
--     dws_retail_loan_day    -> semantic_model: retail_loan_day
-- ============================================================================
-- 用途：为"银行零售存款 / 贷款"主题提供 10 个业务指标的物理底表，覆盖
--   原子指标（simple） / 衍生指标（derived） / 派生指标（ratio）三类。
--
--   dws_retail_deposit_day（零售存款日汇总）
--     measures : deposit_balance_amount  <- deposit_balance      (SUM 时点余额)
--                deposit_interest_amount <- deposit_interest     (SUM 当日利息支出)
--                deposit_customer_count  <- deposit_customer_cnt (MAX 客户数快照)
--     dimensions: 统计日期 / 机构 / 零售渠道 / 产品类型 / 客户类型 / 客户等级
--
--   dws_retail_loan_day（零售贷款日汇总）
--     measures : loan_balance_amount   <- loan_balance      (SUM 时点余额)
--                loan_interest_amount  <- loan_interest     (SUM 当日利息收入)
--                npl_balance_amount    <- npl_balance       (SUM 不良余额)
--                loan_customer_count   <- loan_customer_cnt (MAX 客户数快照)
--     dimensions: 与存款表**完全一致**
--
-- 设计要点：
-- 1) 两张表的维度集合刻意保持完全一致 —— 跨表指标（净利息收入 / 存贷比）
--    才能按任意维度分组；语义层的 supported_dimensions 取的是各模型维度交集。
-- 2) 余额类为**时点值**、利息类为**当日计提值**、客户数为**当日户数快照**：
--    - 三者在"跨机构 / 渠道 / 产品"方向上都可加总（户数按机构加总是银行业标准口径），
--      因此统一声明 SUM；
--    - 但客户数在"跨时间"方向**不可加总**（SUM 会退化成"户日"），跨月请用
--      advanced.metric_result.agg=avg 取日均户数，或只在 day 粒度下取时点值；
--    - 早期版本曾把客户数声明为 MAX（想表达"期间峰值"），实测会导致
--      "当日全行客户数"退化为"单个维度组合的最大值"，户均贷款随之放大 100 倍 —— 
--      已修正为 SUM。
--    DDL 侧的聚合类型必须与语义层 measures.agg 一致，否则 StarRocks 写入时就会聚合掉。
-- 3) "日均"类派生（日均存款余额）不落到物理列，而是查询期用聚合覆盖
--    （advanced.metric_result.agg=avg）表达；"同环比"由 pop_types 表达。
--
-- 执行：mysql -h<FE_HOST> -P9030 -u<USER> -p < bank_schema.sql
--   或：cd backend && ./.venv/bin/python ../deploy/starrocks/seed_bank.py --execute
-- ============================================================================

-- ---------- 零售存款域：日 × 机构 × 渠道 × 产品 × 客户类型 × 客户等级 ----------
CREATE TABLE IF NOT EXISTS dws_retail_deposit_day (
    stat_date            DATE          NOT NULL              COMMENT '统计日期',
    org_name             VARCHAR(64)   NOT NULL DEFAULT ''   COMMENT '机构（分行）',
    channel_name         VARCHAR(64)   NOT NULL DEFAULT ''   COMMENT '零售渠道（手机银行/网上银行/柜面）',
    product_type         VARCHAR(64)   NOT NULL DEFAULT ''   COMMENT '产品类型（活期/定期/大额存单/结构性存款）',
    customer_type        VARCHAR(64)   NOT NULL DEFAULT ''   COMMENT '客户类型（个人/个人经营）',
    customer_level       VARCHAR(64)   NOT NULL DEFAULT ''   COMMENT '客户等级（普通/贵宾）',

    deposit_balance      DECIMAL(20,2) SUM DEFAULT '0'       COMMENT '存款余额（元，时点）',
    deposit_interest     DECIMAL(20,2) SUM DEFAULT '0'       COMMENT '存款利息支出（元，当日计提）',
    deposit_customer_cnt BIGINT        SUM DEFAULT '0'       COMMENT '存款客户数（户，当日快照）'
) ENGINE = OLAP
AGGREGATE KEY(stat_date, org_name, channel_name, product_type, customer_type, customer_level)
COMMENT '零售存款日汇总（DWS 预聚合）'
DISTRIBUTED BY HASH(stat_date, org_name) BUCKETS 4
PROPERTIES (
    "replication_num" = "1"
);

-- ---------- 零售贷款域：与存款域维度完全对齐 ----------
CREATE TABLE IF NOT EXISTS dws_retail_loan_day (
    stat_date          DATE          NOT NULL              COMMENT '统计日期',
    org_name           VARCHAR(64)   NOT NULL DEFAULT ''   COMMENT '机构（分行）',
    channel_name       VARCHAR(64)   NOT NULL DEFAULT ''   COMMENT '零售渠道（手机银行/网上银行/柜面）',
    product_type       VARCHAR(64)   NOT NULL DEFAULT ''   COMMENT '产品类型（住房/消费/经营/信用卡分期）',
    customer_type      VARCHAR(64)   NOT NULL DEFAULT ''   COMMENT '客户类型（个人/个人经营）',
    customer_level     VARCHAR(64)   NOT NULL DEFAULT ''   COMMENT '客户等级（普通/贵宾）',

    loan_balance       DECIMAL(20,2) SUM DEFAULT '0'       COMMENT '贷款余额（元，时点）',
    loan_interest      DECIMAL(20,2) SUM DEFAULT '0'       COMMENT '贷款利息收入（元，当日计提）',
    npl_balance        DECIMAL(20,2) SUM DEFAULT '0'       COMMENT '不良贷款余额（元，时点）',
    loan_customer_cnt  BIGINT        SUM DEFAULT '0'       COMMENT '贷款客户数（户，当日快照）'
) ENGINE = OLAP
AGGREGATE KEY(stat_date, org_name, channel_name, product_type, customer_type, customer_level)
COMMENT '零售贷款日汇总（DWS 预聚合）'
DISTRIBUTED BY HASH(stat_date, org_name) BUCKETS 4
PROPERTIES (
    "replication_num" = "1"
);
