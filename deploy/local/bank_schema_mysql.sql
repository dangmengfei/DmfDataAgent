-- ============================================================================
-- 银行零售（存款 / 贷款）语义底表（本地 MySQL 版本）
-- ============================================================================
-- 与 deploy/starrocks/bank_schema.sql 等价：StarRocks 语法
-- （ENGINE=OLAP / AGGREGATE KEY / DISTRIBUTED BY HASH）替换为 MySQL/InnoDB。
--
-- 时间维度：客户数在"跨时间"方向无法加总（SUM 会退化成"户日"），
-- 跨月请用查询期聚合覆盖 advanced.metric_result.agg=avg 取日均户数。
-- 注意"聚合语义"的等价性：
--   StarRocks 用聚合列（SUM / MAX）在写入时按 key 归并；MySQL 用普通列 + 复合主键，
--   由**查询期**的聚合函数（语义层 measures 声明的 agg）完成同样归并 —— 结果一致。
--   因此 DDL 侧不再声明 SUM/MAX，聚合方式唯一由 manifest.yml 的 measures.agg 决定。
--
-- 对应语义模型：retail_deposit_day / retail_loan_day
-- ============================================================================

-- ---------- 零售存款域 ----------
CREATE TABLE IF NOT EXISTS `dws_retail_deposit_day` (
    `stat_date`            DATE          NOT NULL              COMMENT '统计日期',
    `org_name`             VARCHAR(64)   NOT NULL DEFAULT ''   COMMENT '机构（分行）',
    `channel_name`         VARCHAR(64)   NOT NULL DEFAULT ''   COMMENT '零售渠道',
    `product_type`         VARCHAR(64)   NOT NULL DEFAULT ''   COMMENT '产品类型',
    `customer_type`        VARCHAR(64)   NOT NULL DEFAULT ''   COMMENT '客户类型',
    `customer_level`       VARCHAR(64)   NOT NULL DEFAULT ''   COMMENT '客户等级',

    `deposit_balance`      DECIMAL(20,2) NOT NULL DEFAULT 0    COMMENT '存款余额（元，时点）',
    `deposit_interest`     DECIMAL(20,2) NOT NULL DEFAULT 0    COMMENT '存款利息支出（元，当日计提）',
    `deposit_customer_cnt` BIGINT        NOT NULL DEFAULT 0    COMMENT '存款客户数（户，当日快照）',

    PRIMARY KEY (`stat_date`, `org_name`, `channel_name`,
                 `product_type`, `customer_type`, `customer_level`),
    KEY `idx_dep_date_org` (`stat_date`, `org_name`),
    KEY `idx_dep_date_product` (`stat_date`, `product_type`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci
  COMMENT = '零售存款日汇总（DWS 预聚合）';

-- ---------- 零售贷款域 ----------
CREATE TABLE IF NOT EXISTS `dws_retail_loan_day` (
    `stat_date`        DATE          NOT NULL              COMMENT '统计日期',
    `org_name`         VARCHAR(64)   NOT NULL DEFAULT ''   COMMENT '机构（分行）',
    `channel_name`     VARCHAR(64)   NOT NULL DEFAULT ''   COMMENT '零售渠道',
    `product_type`     VARCHAR(64)   NOT NULL DEFAULT ''   COMMENT '产品类型',
    `customer_type`    VARCHAR(64)   NOT NULL DEFAULT ''   COMMENT '客户类型',
    `customer_level`   VARCHAR(64)   NOT NULL DEFAULT ''   COMMENT '客户等级',

    `loan_balance`     DECIMAL(20,2) NOT NULL DEFAULT 0    COMMENT '贷款余额（元，时点）',
    `loan_interest`    DECIMAL(20,2) NOT NULL DEFAULT 0    COMMENT '贷款利息收入（元，当日计提）',
    `npl_balance`      DECIMAL(20,2) NOT NULL DEFAULT 0    COMMENT '不良贷款余额（元，时点）',
    `loan_customer_cnt` BIGINT       NOT NULL DEFAULT 0    COMMENT '贷款客户数（户，当日快照）',

    PRIMARY KEY (`stat_date`, `org_name`, `channel_name`,
                 `product_type`, `customer_type`, `customer_level`),
    KEY `idx_loan_date_org` (`stat_date`, `org_name`),
    KEY `idx_loan_date_product` (`stat_date`, `product_type`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci
  COMMENT = '零售贷款日汇总（DWS 预聚合）';
