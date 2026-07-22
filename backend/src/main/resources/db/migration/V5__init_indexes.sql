-- ================================================================================
-- FinReport Agent — V5 额外性能索引
-- 补充 spec §5.2.2 行内索引未覆盖的查询路径
-- 版本：v1.0 | 日期：2026-07-14
-- ================================================================================

-- ----- 财报域 ----------------------------------------------------------------

-- 按解析状态筛选未完成任务（parse_status 查询）
CREATE INDEX idx_report_parse_status ON report (parse_status);

-- 用户维度查询财报（用户级列表、仪表盘）
CREATE INDEX idx_report_user_id ON report (user_id);

-- 按勾稽规则类型筛选（如仅查 balance_sheet_identity）
CREATE INDEX idx_check_rule_type ON accounting_check (rule_type);

-- 异常按类型 + 严重度组合筛选（如筛选所有 ERROR 的 yoy_change）
CREATE INDEX idx_anomaly_type_severity ON anomaly (anomaly_type, severity);

-- 异常按严重度快速筛选
CREATE INDEX idx_anomaly_severity ON anomaly (severity);

-- 按产物类型 + 状态查询（如查找处理中的 PDF 产物）
CREATE INDEX idx_artifact_type_status ON report_artifact (artifact_type, status);

-- ----- 任务域 ----------------------------------------------------------------

-- 按关联财报查询任务（一个财报的所有任务）
CREATE INDEX idx_task_ref_report ON task (ref_report_id);

-- 任务步骤按 task_id + status 联合查询（监控某任务的未完成步骤）
CREATE INDEX idx_task_step_status ON task_step (task_id, status);

-- 对话会话按财报维度查询（某财报的所有对话）
CREATE INDEX idx_session_report ON chat_session (report_id);

-- ----- 模型域 ----------------------------------------------------------------

-- 按模型用途筛选（如查所有 EXTRACT 任务模型）
CREATE INDEX idx_model_task ON model_registry (task);
