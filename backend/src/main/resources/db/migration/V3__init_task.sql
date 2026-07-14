-- ================================================================================
-- FinReport Agent — V3 任务域建表
-- 对应 spec §5.2.2 任务域
-- 版本：v1.0 | 日期：2026-07-14
-- ================================================================================

-- 3.1 任务编排主表
CREATE TABLE task (
  id            VARCHAR(64) PRIMARY KEY,
  user_id       BIGINT      NOT NULL,
  task_type     VARCHAR(16) NOT NULL,
  ref_report_id BIGINT,
  status        VARCHAR(16) NOT NULL DEFAULT 'PENDING',
  current_step  VARCHAR(32),
  progress      TINYINT     NOT NULL DEFAULT 0,
  payload       JSON,
  result        JSON,
  error_msg     TEXT,
  started_at    DATETIME,
  finished_at   DATETIME,
  created_at    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_user_status (user_id, status),
  INDEX idx_status_created (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 3.2 任务步骤明细
CREATE TABLE task_step (
  id          BIGINT PRIMARY KEY AUTO_INCREMENT,
  task_id     VARCHAR(64) NOT NULL,
  step_name   VARCHAR(32) NOT NULL,
  status      VARCHAR(16) NOT NULL,
  started_at  DATETIME,
  finished_at DATETIME,
  duration_ms INT,
  message_id  VARCHAR(128),
  error_msg   TEXT,
  INDEX idx_task (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 3.3 对话会话
CREATE TABLE chat_session (
  id           BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id      BIGINT      NOT NULL,
  report_id    BIGINT      NOT NULL,
  title        VARCHAR(128),
  created_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 3.4 对话消息
CREATE TABLE chat_message (
  id          BIGINT PRIMARY KEY AUTO_INCREMENT,
  session_id  BIGINT      NOT NULL,
  role        VARCHAR(16) NOT NULL,
  content     MEDIUMTEXT  NOT NULL,
  tools_used  JSON,
  token_count INT,
  created_at  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_session (session_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
