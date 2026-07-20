# M1 MQ 可靠性与成功事件重放补偿决策

**日期**：2026-07-19
**背景**：M1 收尾审查发现三个会使任务永久卡住的 P1 缺陷：L3 handler 失败未回传给 L2、L3 progress 发布没有 broker 确认、L2 对重复成功事件直接返回而不能补偿下游投递中断窗口。

## 决策

1. L3 worker 对业务 handler 异常构造持久化 `FAILED` progress（稳定幂等键为 `taskId:step`，透传 `traceId`）；仅在该事件经 producer 成功确认后才 ack 原任务。无法解析或无法安全关联的消息继续直接进入 DLQ。
2. L3 progress producer 为每个发布 channel 启用 RabbitMQ Publisher Confirm，并使用 `mandatory=true`；Negative Confirm、不可路由和连接错误均视为发布失败，经一次重连重试后仍失败则不伪报成功。
3. L2 收到已为 `SUCCESS` 的 progress 时执行幂等补偿：只投递仍为 `PENDING` 的下游步骤；任务已经进入 CHECK/REPORT 等后续阶段时绝不回退状态。
4. 本轮不引入新依赖或 transactional outbox。MQ 发布与数据库状态仍非同一原子提交；稳定幂等键、Publisher Confirm、FAILED 闭环和成功重放补偿作为 M1 的可验证可靠性边界。

## 已完成 checklist

- [x] 覆盖 L3 handler failure -> `FAILED` progress -> ack 的回归测试。
- [x] 覆盖 Publisher Confirm、`mandatory=true`、Negative Confirm 和重连后失败的回归测试。
- [x] 覆盖 PARSE 成功重放补发遗失的三条抽取消息，以及 EXTRACT 成功重放不回退 CHECK 阶段的 Java 回归测试。
- [x] `backend/mvnw.cmd test`：190 tests 通过；Checkstyle 0 违规，SpotBugs 0 问题。
- [x] `ai-service`：ruff、black、compileall 通过；pytest 51 passed，coverage 91.49%。
- [x] `frontend`：lint、type-check、production build 通过。
- [x] 重新构建 Compose 后浏览器 E2E 通过；任务 `task-dd099cbc15b2` 为 `COMPLETED`，六条 `task_step` 均为 `SUCCESS`。

## 风险与后续行动

- M1.17 仍需本次提交推送后的 Backend CI、AI Service CI、Frontend CI 三条远程流水线全部成功，才可勾选最终 CI 验收项。
- transactional outbox 与依赖 lockfile 为后续可靠性/可复现性改进项，不阻塞当前 M1 mock 链路验收。
