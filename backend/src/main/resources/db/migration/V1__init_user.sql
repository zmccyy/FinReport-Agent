-- ================================================================================
-- FinReport Agent — V1 用户域建表
-- 对应 spec §5.2.2 用户域
-- 版本：v1.0 | 日期：2026-07-14
-- ================================================================================

CREATE TABLE user_account (
  id            BIGINT PRIMARY KEY AUTO_INCREMENT,
  username      VARCHAR(64)  NOT NULL UNIQUE,
  password_hash VARCHAR(128) NOT NULL,             -- BCrypt
  email         VARCHAR(128),
  role          VARCHAR(16)  NOT NULL DEFAULT 'USER',
  status        TINYINT      NOT NULL DEFAULT 1,
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
