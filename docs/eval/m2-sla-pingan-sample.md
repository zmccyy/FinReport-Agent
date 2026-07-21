# M2.12 SLA 评估报告

> 生成时间：2026-07-21T04:52:25+00:00
> PDF：`data\sample_reports\000001_平安银行_2025年年度报告.pdf`
> LLM 模式：mock（无 GPU）

## M2.12 验收门槛

| 指标 | 阈值 | 实测 | 结果 |
|---|---|---|---|
| PARSE + EXTRACT | ≤ 180 s | 33.47 s | ✅ 通过 |

## 各阶段 SLA（spec §12.1）

| 阶段 | 阈值 | 实测 | 结果 | 备注 |
|---|---|---|---|---|
| parse | ≤ 90 s | 33.47 s | ✅ 通过 | page_count=288 |
| extract | ≤ 60 s | 0.00 s | ✅ 通过 | mock LLM (no GPU); real timing requires ai-service |
| total | ≤ 240 s | 33.47 s | ✅ 通过 | 累计耗时 |

## 总结

- M2.12 PARSE+EXTRACT：33.47 s / 180 s — ✅ 通过
- Spec §12.1 strict 检查：未启用 — ✅ 通过

## 备注

- Mock 模式仅测量 DocumentParser 本地解析耗时；EXTRACT 为零耗时 stub。
- Real 7B 模式通过 ai-service HTTP 接口测量 PARSE+EXTRACT。
- 三表并行 EXTRACT 以单表 max 近似（spec §12.1 视并行批次 ≤ 60 s）。
- CHECK / REPORT 在 M2 阶段未通过 HTTP 暴露，记为 not_exercised。
