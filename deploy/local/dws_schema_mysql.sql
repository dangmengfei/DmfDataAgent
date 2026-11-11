-- ============================================================================
-- DmfDataAgent 语义层示例底表（本地 MySQL 版本）
-- ============================================================================
-- 与 deploy/starrocks/dws_schema.sql 等价，但 StarRocks 语法 (ENGINE=OLAP /
-- AGGREGATE KEY / DISTRIBUTED BY HASH) 替换为 MySQL/InnoDB 标准语法。
--
-- 主流程并不区分底层到底是 StarRocks 还是 MySQL ——
-- semantic/starrocks.py 用 aiomysql 直连，host/port/user/database 指向哪里就连哪里，
-- 把 `DMF_STARROCKS-*` 指向本地 MySQL 即可跑通全链路。
--
-- 复用 build_rows/build_sql 生成的确定型造数（与 StarRocks 版同 seed），
-- 适合在没有真实 StarRocks 的本地环境做端到端冒烟。
-- ============================================================================

-- ---------- 交易域：下单/支付粒度日预聚合 ----------
CREATE TABLE IF NOT EXISTS `dws_trade_day` (
    `stat_date`           DATE          NOT NULL                COMMENT '统计日期',
    `channel_name`        VARCHAR(64)   NOT NULL DEFAULT ''     COMMENT '渠道（线上/门店/小程序）',
    `region_name`         VARCHAR(64)   NOT NULL DEFAULT ''     COMMENT '地区（华东/华南/华北/西南）',
    `category_name`       VARCHAR(64)   NOT NULL DEFAULT ''     COMMENT '品类（家电/数码/服饰）',
    `platform_name`       VARCHAR(64)   NOT NULL DEFAULT ''     COMMENT '平台（APP/小程序/H5）',
    `store_name`          VARCHAR(128)  NOT NULL DEFAULT ''     COMMENT '门店（仅门店渠道有值）',

    `gmv`                 DECIMAL(20,2) NOT NULL DEFAULT 0      COMMENT '成交额（元）',
    `order_cnt`           BIGINT        NOT NULL DEFAULT 0      COMMENT '订单数（笔）',
    `paid_user_cnt`       BIGINT        NOT NULL DEFAULT 0      COMMENT '付费用户数（人）',
    `repurchase_user_cnt` BIGINT        NOT NULL DEFAULT 0      COMMENT '复购用户数（人）',

    PRIMARY KEY (`stat_date`, `channel_name`, `region_name`,
                 `category_name`, `platform_name`, `store_name`),
    KEY `idx_date_channel_platform` (`stat_date`, `channel_name`, `platform_name`),
    KEY `idx_date_platform` (`stat_date`, `platform_name`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci
  COMMENT = '交易日汇总（DWS 预聚合）';

-- ---------- 用户域：活跃/新增粒度日预聚合 ----------
CREATE TABLE IF NOT EXISTS `dws_user_day` (
    `stat_date`      DATE         NOT NULL                COMMENT '统计日期',
    `channel_name`   VARCHAR(64)  NOT NULL DEFAULT ''     COMMENT '渠道（线上/门店/小程序）',
    `platform_name`  VARCHAR(64)  NOT NULL DEFAULT ''     COMMENT '平台（APP/小程序/H5）',

    `active_user_cnt` BIGINT      NOT NULL DEFAULT 0      COMMENT '日活跃用户数（人）',
    `new_user_cnt`    BIGINT      NOT NULL DEFAULT 0      COMMENT '新增用户数（人）',

    PRIMARY KEY (`stat_date`, `channel_name`, `platform_name`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci
  COMMENT = '用户日汇总（DWS 预聚合）';
