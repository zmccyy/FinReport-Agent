# 2026-08-23 M5 代码审查与修复记录

## 背景

对 M5.01-06 全部代码（`3c382e6...HEAD`，4 提交 / 39 文件 / +4550 行）执行
双轴代码审查（Standards 轴 + Spec 轴，并行子代理）。审查报告后按优先级修复。

## 审查发现与修复清单

### 已修复（本次提交）

| # | 发现 | 严重度 | 修复 |
|---|---|---|---|
| 1 | 超时注释引错 spec 条目（§12.1 无「完整 <60s」条目），整流超时 120s/150s 为 spec §3.7 60s 的 2 倍且无理由记录 | 高 | 注释改为引用 spec §3.7，记录放宽理由：多步 ReAct（步数上限 8、单步 SLA 300s）实测单轮可达 127s，60s 会切断正常链路；120s/150s 是多步整流上限（防挂起），完整回答 SLA 留 M5.09 实测 |
| 2 | Redis 会话 key 偏离 spec §5.4.1（`fin:session:{userId}:{sessionId}` TTL 24h）——实现为 `fin:chat:context:{sessionId}` TTL 7d，缺 userId 维度弱化多租户隔离 | 高 | key 改为 `fin:session:{userId}:{sessionId}`，TTL 24h；loadContext/appendRound 签名加 userId，ChatService 透传 |
| 3 | SessionContextService 违反 AGENTS.md §3.1：loadContext/appendRound 无首行入参日志；compress 抛 `IllegalStateException` 而非项目异常体系 | 中 | 两方法补首行日志；空摘要改抛 `IntegrationException`（BAD_GATEWAY / AI_SERVICE_COMPRESS_FAILED） |
| 4 | done.messageId 语义漂移（spec 示例 messageId=8888 即消息 ID）——实现为 L2 随机 UUID，前端无法关联已持久化消息 | 中 | messageId 改为用户消息的 DB 自增 ID（save 后回填），done.messageId 可直接关联 chat_message 行 |

### 已确认保持（理由记录）

| 发现 | 处理 |
|---|---|
| M5.03「tool_call 解析失败率 <5% / 100 次推理测试」未落地 | pivot 为 json_mode 后 react_parser 三级容错 + 单测覆盖；失败率测量归 M5.09 集成验收 |
| compute_qoq 永远返回 ok_null | 数据事实：年报只有本期/上期，环比不可计算（季度报表场景才可用）；工具按 spec 注册、行为正确 |
| 首 token <15s 未实测 | M5.04 冒烟已见 1.4-6.9s；正式计时归 M5.09 验收 |
| SSE 新增 error 事件 + done 的 finishedReason/error 字段 | 防御性扩展（L3 失败必须可区分），M5.09 同步 spec §6.3.3 事件契约 |
| tokenCount = 按句切块数（非真实 token 数） | LLM 后端非流式，token 事件按 64 字符/块切分；tokenCount 语义为「发送给前端的 token 事件数」，口径在 chat.py 注释说明，M5.09 文档化 |
| 提交粒度超限（4 提交均 >500 行） | 历史提交已审查归档，不改写；后续任务按 AGENTS.md §4.5 拆分 |
| role 用裸 String、ObjectMapper 四处 new | 判断项：仓库既有惯例（ProgressConsumer 等同样 new ObjectMapper()），role 枚举列入 M6 清理项 |

## 下一步行动项

- [ ] M5.09 集成验收：命中率 ≥0.85 / 首 token <15s 计时 / 失败率测量，并同步 spec §6.3.3 事件契约
- [ ] M6 清理项：ChatMessage.role 枚举化
- [ ] 后续任务提交粒度 ≤500 行/commit
