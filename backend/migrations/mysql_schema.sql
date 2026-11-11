-- ============================================================================
-- DmfDataAgent MySQL 建表脚本（参考稿，非权威）
-- 数据库：dmf_agent  |  字符集：utf8mb4  |  引擎：InnoDB
--
-- ⚠️ 权威建表方式请使用 alembic 迁移：backend/migrations/versions/0001_initial.py
--    在 backend/ 目录下执行： alembic upgrade head
--    本文件仅作离线参考 / 手动建库兜底，与 ORM 保持一致但不参与版本管理。
-- ============================================================================

CREATE DATABASE IF NOT EXISTS `dmf_agent`
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_general_ci;

USE `dmf_agent`;

-- ----------------------------------------------------------------------------
-- 1. 会话表
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `sessions` (
  `id`          VARCHAR(64)  NOT NULL,
  `user_id`     VARCHAR(64)  NOT NULL,
  `title`       VARCHAR(255) NOT NULL DEFAULT '新会话',
  `status`      VARCHAR(16)  NOT NULL DEFAULT 'active',
  `created_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`  DATETIME     DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `ix_sessions_user_id` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ----------------------------------------------------------------------------
-- 2. 消息表（一对多：会话 -> 消息）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `messages` (
  `id`          VARCHAR(64)  NOT NULL,
  `session_id`  VARCHAR(64)  NOT NULL,
  `role`        VARCHAR(16)  NOT NULL,                 -- user | assistant
  `content`     TEXT         NOT NULL,
  `node_trace`  JSON         DEFAULT NULL,             -- 过程追踪：意图/要素/DSL
  `created_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `ix_messages_session_id` (`session_id`),
  KEY `ix_messages_created_at` (`created_at`),
  CONSTRAINT `fk_messages_session`
    FOREIGN KEY (`session_id`) REFERENCES `sessions` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ----------------------------------------------------------------------------
-- 3. 知识库文档表
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `knowledge_documents` (
  `id`          VARCHAR(64)  NOT NULL,
  `title`       VARCHAR(255) NOT NULL,
  `source_type` VARCHAR(32)  NOT NULL DEFAULT 'text',
  `source_uri`  VARCHAR(1024) DEFAULT NULL,
  `status`      VARCHAR(16)  NOT NULL DEFAULT 'pending', -- pending|indexed|failed
  `chunk_count` INT          NOT NULL DEFAULT 0,
  `created_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`  DATETIME     DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `ix_knowledge_documents_title` (`title`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ----------------------------------------------------------------------------
-- 4. 知识库切片表（一对多：文档 -> 切片）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `knowledge_chunks` (
  `id`          VARCHAR(64)  NOT NULL,
  `document_id` VARCHAR(64)  NOT NULL,
  `chunk_index` INT          NOT NULL DEFAULT 0,
  `content`     TEXT         NOT NULL,
  `vector_ref`  VARCHAR(128) DEFAULT NULL,
  `created_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `ix_knowledge_chunks_document_id` (`document_id`),
  CONSTRAINT `fk_knowledge_chunks_document`
    FOREIGN KEY (`document_id`) REFERENCES `knowledge_documents` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ----------------------------------------------------------------------------
-- 5. 会话滚动摘要表（一对一：会话 -> 摘要，主键即 session_id）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `memory_summaries` (
  `session_id`        VARCHAR(64) NOT NULL,
  `summary`           TEXT        NOT NULL,
  `upto_message_id`   VARCHAR(64) NOT NULL DEFAULT '',
  `updated_at`        DATETIME    DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`session_id`),
  CONSTRAINT `fk_memory_summaries_session`
    FOREIGN KEY (`session_id`) REFERENCES `sessions` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ----------------------------------------------------------------------------
-- 6. 指标元数据表
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `metric_definitions` (
  `code`        VARCHAR(64)  NOT NULL,
  `name`        VARCHAR(255) NOT NULL,
  `description` TEXT         NOT NULL DEFAULT '',
  `unit`        VARCHAR(32)  NOT NULL DEFAULT '',
  `category`    VARCHAR(64)  NOT NULL DEFAULT '',
  `dimensions`  JSON         DEFAULT NULL,
  `created_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`  DATETIME     DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`code`),
  KEY `ix_metric_definitions_name` (`name`),
  KEY `ix_metric_definitions_category` (`category`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ----------------------------------------------------------------------------
-- 7. 黑话词典表（对应老系统 aigc_cant_name）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `cant_entries` (
  `id`           INT          NOT NULL AUTO_INCREMENT,
  `tenant_id`    VARCHAR(64)  NOT NULL DEFAULT '',
  `cant_type`    VARCHAR(32)  NOT NULL,                -- dim | dim_value
  `cn_name`      VARCHAR(255) NOT NULL,
  `cant_cn_name` VARCHAR(255) NOT NULL,
  `dim_value`    VARCHAR(1024) NOT NULL DEFAULT '',
  `domain`       VARCHAR(16)  NOT NULL DEFAULT 'dc',   -- dm 公司 / dc 个人
  `status`       INT          NOT NULL DEFAULT 1,
  `created_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`  DATETIME     DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `ix_cant_entries_tenant_id` (`tenant_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ----------------------------------------------------------------------------
-- 8. 对话观测（遵循 OpenTelemetry：span 落 trace_spans，轮级汇总落 trace_records）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `trace_records` (
  `id`          INT          NOT NULL AUTO_INCREMENT,
  `session_id`  VARCHAR(64)  NOT NULL,
  `round_id`    VARCHAR(64)  NOT NULL,
  `intent`      VARCHAR(32)  DEFAULT NULL,
  `mock_mode`   TINYINT(1)   NOT NULL DEFAULT 0,
  `skills`      JSON         DEFAULT NULL,
  `elapsed_ms`  DOUBLE       DEFAULT NULL,
  `has_error`   TINYINT(1)   NOT NULL DEFAULT 0,
  `trace`       JSON         DEFAULT NULL,
  `created_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_trace_records_round_id` (`round_id`),
  KEY `ix_trace_records_session_id` (`session_id`),
  KEY `ix_trace_records_intent` (`intent`),
  KEY `ix_trace_records_has_error` (`has_error`),
  KEY `ix_trace_records_created_at` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- OTel span（一轮对话 = root span `chat_round` + 每个图节点一条 `node.<name>`）
CREATE TABLE IF NOT EXISTS `trace_spans` (
  `id`              INT          NOT NULL AUTO_INCREMENT,
  `trace_id`        VARCHAR(32)  NOT NULL,
  `span_id`         VARCHAR(16)  NOT NULL,
  `parent_span_id`  VARCHAR(16)  DEFAULT NULL,
  `session_id`      VARCHAR(64)  NOT NULL,
  `round_id`        VARCHAR(64)  NOT NULL,
  `name`            VARCHAR(128) NOT NULL,
  `kind`            VARCHAR(16)  NOT NULL DEFAULT 'INTERNAL',
  `start_time`      DATETIME     DEFAULT NULL,
  `end_time`        DATETIME     DEFAULT NULL,
  `duration_ms`     DOUBLE       DEFAULT NULL,
  `status_code`     VARCHAR(16)  NOT NULL DEFAULT 'UNSET',
  `status_message`  VARCHAR(512) NOT NULL DEFAULT '',
  `attributes`      JSON         DEFAULT NULL,
  `events`          JSON         DEFAULT NULL,
  `service_name`    VARCHAR(64)  NOT NULL DEFAULT '',
  `created_at`      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `ix_trace_spans_trace_id` (`trace_id`),
  KEY `ix_trace_spans_span_id` (`span_id`),
  KEY `ix_trace_spans_session_id` (`session_id`),
  KEY `ix_trace_spans_round_id` (`round_id`),
  KEY `ix_trace_spans_name` (`name`),
  KEY `ix_trace_spans_status_code` (`status_code`),
  KEY `ix_trace_spans_created_at` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
