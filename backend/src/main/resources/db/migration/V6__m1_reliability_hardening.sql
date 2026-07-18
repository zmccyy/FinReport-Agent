-- M1.01-M1.12 reliability hardening: user-scoped report dedupe and retry accounting.
-- Historical migrations deliberately remain immutable.

ALTER TABLE report DROP INDEX uk_md5;
ALTER TABLE report DROP INDEX task_id;
ALTER TABLE report ADD UNIQUE KEY uk_report_user_md5 (user_id, pdf_md5);
ALTER TABLE report ADD INDEX idx_report_task (task_id);

ALTER TABLE task_step
  ADD COLUMN retry_count INT NOT NULL DEFAULT 0 AFTER error_msg;

ALTER TABLE task_step
  ADD INDEX idx_task_step_idempotency (task_id, step_name, status);
