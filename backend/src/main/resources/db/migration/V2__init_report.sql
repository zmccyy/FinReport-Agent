-- ================================================================================
-- FinReport Agent — V2 财报域建表
-- 对应 spec §5.2.2 财报域
-- 版本：v1.0 | 日期：2026-07-14
-- ================================================================================

-- 2.1 财报元数据
CREATE TABLE report (
  id             BIGINT PRIMARY KEY AUTO_INCREMENT,
  task_id        VARCHAR(64)  NOT NULL UNIQUE,
  user_id        BIGINT       NOT NULL,
  company_code   VARCHAR(16)  NOT NULL,
  company_name   VARCHAR(128) NOT NULL,
  report_type    VARCHAR(16)  NOT NULL,
  report_period  VARCHAR(16)  NOT NULL,
  pdf_md5        CHAR(32)     NOT NULL,
  pdf_object_key VARCHAR(256) NOT NULL,
  page_count     INT,
  parse_status   VARCHAR(16)  NOT NULL DEFAULT 'PENDING',
  created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_md5 (pdf_md5),
  INDEX idx_user_period (user_id, report_period),
  INDEX idx_company (company_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2.2 财报科目明细（三表抽取结果）
CREATE TABLE financial_statement (
  id              BIGINT PRIMARY KEY AUTO_INCREMENT,
  report_id       BIGINT       NOT NULL,
  statement_type  VARCHAR(16)  NOT NULL,
  item_name       VARCHAR(128) NOT NULL,
  item_value      DECIMAL(20,4),
  currency        VARCHAR(8)   DEFAULT 'CNY',
  unit            VARCHAR(16)  DEFAULT '元',
  scope           VARCHAR(16)  DEFAULT '合并',
  period_type     VARCHAR(16)  DEFAULT '本期',
  confidence      DECIMAL(4,3),
  source_page     INT,
  source_bbox     VARCHAR(64),
  created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_report_type (report_id, statement_type),
  INDEX idx_item (report_id, item_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2.3 勾稽核对结果
CREATE TABLE accounting_check (
  id           BIGINT PRIMARY KEY AUTO_INCREMENT,
  report_id    BIGINT      NOT NULL,
  rule_name    VARCHAR(64) NOT NULL,
  rule_type    VARCHAR(16) NOT NULL,
  expected     DECIMAL(20,4),
  actual       DECIMAL(20,4),
  diff         DECIMAL(20,4),
  is_pass      TINYINT     NOT NULL,
  severity     VARCHAR(16) DEFAULT 'INFO',
  note         TEXT,
  created_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_report (report_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2.4 异常检测结果
CREATE TABLE anomaly (
  id           BIGINT PRIMARY KEY AUTO_INCREMENT,
  report_id    BIGINT      NOT NULL,
  item_name    VARCHAR(128),
  anomaly_type VARCHAR(32) NOT NULL,
  metric_value DECIMAL(20,4),
  threshold    DECIMAL(20,4),
  description  TEXT,
  severity     VARCHAR(16) NOT NULL,
  created_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_report (report_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2.5 报告产物（PDF / Markdown / 图表 PNG）
CREATE TABLE report_artifact (
  id            BIGINT PRIMARY KEY AUTO_INCREMENT,
  report_id     BIGINT      NOT NULL,
  artifact_type VARCHAR(16) NOT NULL,
  object_key    VARCHAR(256) NOT NULL,
  status        VARCHAR(16) NOT NULL DEFAULT 'GENERATED',
  created_at    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_report (report_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
