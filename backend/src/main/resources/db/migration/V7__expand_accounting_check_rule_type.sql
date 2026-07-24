-- M3.10 SLA 端到端测试发现：accounting_check.rule_type VARCHAR(16) 容量不足，
-- 无法容纳 L3 RuleType 枚举值（最长 23 字符：cash_flow_vs_net_income）。
--
-- 影响：CHECK 阶段写入 accounting_check 时报 "Data too long for column 'rule_type'"，
-- CheckResultWriter 的 onErrorResume 把异常吞掉返回 Mono.just(0)，但 R2DBC 事务
-- 已被回滚，下游 REPORT 链路虽然推进，task_step.duration_ms 因事务边界异常出现负值。
--
-- 修复：扩容到 VARCHAR(32)，覆盖现有 3 条规则枚举 + 未来扩展余量。
-- 同步更新 spec §5.2.2 财报域表结构。
--
-- 参考：app/schemas/reasoning.py RuleType 枚举定义。

ALTER TABLE accounting_check
  MODIFY COLUMN rule_type VARCHAR(32) NOT NULL;
