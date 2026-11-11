-- ============================================================================
-- DmfDataAgent 语义层示例底表（StarRocks）—— 与 backend/dmf_agent/semantic/manifest.yml 一一对应
-- ============================================================================
-- 用途：主流程脱离指标平台后，语义层（manifest.yml）直接指向 StarRocks 物理表。
-- 本 DDL 建两张 DWS 预聚合宽表，作为示例"指标底表"跑通全链路：
--
--   dws_trade_day  -> semantic_model: trade_day
--     measures: gmv_amount(gmv) / order_count(order_cnt) /
--               paid_user_count(paid_user_cnt) / repurchase_user_count(repurchase_user_cnt)
--     dimensions: 统计日期(stat_date) / 渠道(channel_name) / 地区(region_name) /
--                 品类(category_name) / 平台(platform_name) / 门店(store_name)
--
--   dws_user_day   -> semantic_model: user_day
--     measures: active_user_count(active_user_cnt) / new_user_count(new_user_cnt)
--     dimensions: 统计日期(stat_date) / 渠道(channel_name) / 平台(platform_name)
--
-- 说明：
-- - 采用 StarRocks AGGREGATE KEY 模型（DWS 标准形态）：维度进 key，度量按 SUM 聚合，
--   语义层 measures 全部声明为 `agg: sum`，两侧语义一致。
-- - 两表共享"统计日期 / 渠道 / 平台"三个维度，因此跨模型指标（如 ratio
--   付费转化率 = 付费用户数 / 日活跃用户数）可在编译器生成的 FULL OUTER JOIN 上计算。
-- - 非门店渠道的 store_name 取空串（该渠道无门店归属），按门店分组时会出现一行 ''，
--   属预期行为。
--
-- 执行：mysql -h<FE_HOST> -P9030 -u<USER> -p < dws_schema.sql
--   或：cd backend && python ../deploy/starrocks/seed_dws.py --execute
-- ============================================================================

-- ---------- 交易域：下单/支付粒度日预聚合 ----------
CREATE TABLE IF NOT EXISTS dws_trade_day (
    stat_date           DATE          NOT NULL                COMMENT '统计日期',
    channel_name        VARCHAR(64)   NOT NULL DEFAULT ''     COMMENT '渠道（线上/门店/小程序）',
    region_name         VARCHAR(64)   NOT NULL DEFAULT ''     COMMENT '地区（华东/华南/华北/西南）',
    category_name       VARCHAR(64)   NOT NULL DEFAULT ''     COMMENT '品类（家电/数码/服饰）',
    platform_name       VARCHAR(64)   NOT NULL DEFAULT ''     COMMENT '平台（APP/小程序/H5）',
    store_name          VARCHAR(128)  NOT NULL DEFAULT ''     COMMENT '门店（仅门店渠道有值）',

    gmv                 DECIMAL(20,2) SUM DEFAULT '0'         COMMENT '成交额（元）',
    order_cnt           BIGINT        SUM DEFAULT '0'         COMMENT '订单数（笔）',
    paid_user_cnt       BIGINT        SUM DEFAULT '0'         COMMENT '付费用户数（人）',
    repurchase_user_cnt BIGINT        SUM DEFAULT '0'         COMMENT '复购用户数（人）'
) ENGINE = OLAP
AGGREGATE KEY(stat_date, channel_name, region_name, category_name, platform_name, store_name)
COMMENT '交易日汇总（DWS 预聚合）'
DISTRIBUTED BY HASH(stat_date, channel_name) BUCKETS 4
PROPERTIES (
    "replication_num" = "1"
);

-- ---------- 用户域：活跃/新增粒度日预聚合 ----------
CREATE TABLE IF NOT EXISTS dws_user_day (
    stat_date      DATE         NOT NULL              COMMENT '统计日期',
    channel_name   VARCHAR(64)  NOT NULL DEFAULT ''   COMMENT '渠道（线上/门店/小程序）',
    platform_name  VARCHAR(64)  NOT NULL DEFAULT ''   COMMENT '平台（APP/小程序/H5）',

    active_user_cnt BIGINT      SUM DEFAULT '0'       COMMENT '日活跃用户数（人）',
    new_user_cnt    BIGINT      SUM DEFAULT '0'       COMMENT '新增用户数（人）'
) ENGINE = OLAP
AGGREGATE KEY(stat_date, channel_name, platform_name)
COMMENT '用户日汇总（DWS 预聚合）'
DISTRIBUTED BY HASH(stat_date, channel_name) BUCKETS 4
PROPERTIES (
    "replication_num" = "1"
);
