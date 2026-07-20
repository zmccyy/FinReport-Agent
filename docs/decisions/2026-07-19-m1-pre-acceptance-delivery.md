# M1 阶段验收前交付记录

- 日期：2026-07-19
- 范围：M1.01–M1.17（基础设施、L2/L3 mock 链路和前端骨架）

## 背景

M1 在浏览器 E2E、代码质量检查和远程 CI 收尾前，发现 AI Service CI 在测试全部通过时出现 0% coverage 的误报。

## 决策

1. 在 AI CI 的 pytest-cov 步骤显式设置 `PYTHONPATH=${{ github.workspace }}/ai-service`，使 coverage 采集目标与实际导入源码一致。
2. 不扩大 M1 范围；保留 transactional outbox、真实模型推理和 Qwen 7B 下载为后续里程碑事项。

## 已完成检查清单

- [x] L3 handler 失败闭环、Publisher Confirm 和 L2 成功 progress 重放补偿已修复。
- [x] Java：190 个测试通过，Checkstyle 和 SpotBugs 均为 0 问题。
- [x] Python：ruff、black、51 个测试及 91.49% coverage 通过；Python 3.11 Linux 容器等效验证通过。
- [x] 前端：lint、type-check、production build 通过。
- [x] 最新镜像浏览器 E2E 通过：`task-dd099cbc15b2` 的 PARSE、三个 EXTRACT、CHECK、REPORT 均为 SUCCESS，任务为 COMPLETED。
- [x] 功能提交 `4918fdf88725234beb755237b54f5c8104b510af` 的 Backend CI、AI Service CI、Frontend CI 均为 success。

## 风险与后续

- RabbitMQ 进度发布已使用 publisher confirm；跨数据库与消息代理的原子性尚未采用 transactional outbox，作为后续可靠性增强项。
- 当前 L3 为 mock 服务，真实模型接入、显存锁和降级链属于后续 M2+ 范围。
- Qwen2.5-7B-Instruct GGUF 4-bit 仍需下载，阻塞后续 M2.04，但不阻塞 M1 验收。
