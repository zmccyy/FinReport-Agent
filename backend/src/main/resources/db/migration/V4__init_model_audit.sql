-- ================================================================================
-- FinReport Agent — V4 模型与审计域建表
-- 对应 spec §5.2.2 模型与审计域
-- 版本：v1.0 | 日期：2026-07-14
-- ================================================================================

-- 4.1 模型注册表
CREATE TABLE model_registry (
  id             BIGINT PRIMARY KEY AUTO_INCREMENT,
  task           VARCHAR(32)  NOT NULL,
  base_model     VARCHAR(128) NOT NULL,
  adapter_path   VARCHAR(256) NOT NULL,
  version        VARCHAR(32)  NOT NULL,
  metrics        JSON,
  status         VARCHAR(16)  NOT NULL DEFAULT 'CANDIDATE',
  data_version   VARCHAR(64),
  train_cmd_hash VARCHAR(64),
  trained_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_task_version (task, version),
  INDEX idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 4.2 审计日志
CREATE TABLE audit_log (
  id         BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id    BIGINT,
  action     VARCHAR(64) NOT NULL,
  target     VARCHAR(128),
  ip         VARCHAR(64),
  user_agent VARCHAR(256),
  payload    JSON,
  created_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_user_time (user_id, created_at),
  INDEX idx_action (action)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
