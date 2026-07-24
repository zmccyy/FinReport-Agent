-- M3.10 SLA 端到端测试发现：task_step.duration_ms 出现负值（如 -383ms）。
--
-- 根因：MySQL DATETIME 默认无小数秒精度（DATETIME(0)），存储时按「四舍五入」
-- 取整到秒。当 JVM 设置 startedAt = 09:45:06.580 时，MySQL 存为 09:45:07；
-- handleStepSuccess 读回 startedAt = 09:45:07.000，而 LocalDateTime.now() 仍
-- 在 09:45:06.615 附近（finishedAt 计算时刻），导致
-- Duration.between(startedAt, now).toMillis() = -385ms（负值）。
--
-- MySQL 官方文档：
--   "When a DATE, DATETIME, or TIMESTAMP value with a fractional second part is
--    inserted into a column of the same type but with a smaller number of
--    fractional seconds digits, the fractional seconds are rounded, not truncated."
--
-- 修复：把 task 与 task_step 的 started_at / finished_at 升级到 DATETIME(3) 毫秒精度，
-- 与 JVM LocalDateTime.now() 的毫秒精度对齐，避免四舍五入引入的负 duration。
-- task_step.duration_ms 由 L2 在 SUCCESS 时计算并写入，不依赖 DB 精度，但
-- started_at/finished_at 列本身用于 SLA 监控查询（spec §3.7）必须保证精度。
--
-- 同步更新 spec §5.2.2 任务域表结构。

ALTER TABLE task
  MODIFY COLUMN started_at  DATETIME(3),
  MODIFY COLUMN finished_at DATETIME(3);

ALTER TABLE task_step
  MODIFY COLUMN started_at  DATETIME(3),
  MODIFY COLUMN finished_at DATETIME(3);
