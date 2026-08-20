# 抽取 F1 评估报告（M2.12 / M4.10）

> 生成时间：2026-08-20T13:06:44+00:00
> PDF：`data\sample_reports\600519_贵州茅台_2025年年度报告.pdf`
> Ground truth：`data\benchmark\ground_truth\moutai_2025.json`
> LLM 模式：真实 E2E 管道（DeepSeek API，backend=http://localhost:8080）

## 总体指标

| 指标 | 值 |
|---|---|
| Overall F1 | **0.9106** |
| Overall Precision | 0.9370 |
| Overall Recall | 0.8887 |
| 门槛 (F1 ≥ 0.85) | ✅ 通过 |

## 各表指标

| 表类型 | Precision | Recall | F1 | TP | FP | FN |
|---|---|---|---|---|---|---|
| balance_sheet | 0.9800 | 0.9245 | **0.9515** | 49 | 1 | 4 |
| income_statement | 0.9000 | 0.9474 | **0.9231** | 36 | 4 | 2 |
| cash_flow | 0.9310 | 0.7941 | **0.8571** | 27 | 2 | 7 |

## 各表详情

### balance_sheet

- 匹配项（TP）：49
- 多余项（FP）：1 — ['货币资金']
- 漏项（FN）：4 — ['货币资金', '拆出资金', '交易性金融资产', '应收票据']

### income_statement

- 匹配项（TP）：36
- 多余项（FP）：4 — ['信用减值损失', '基本每股收益', '将重分类进损益的其他综合收益', '稀释每股收益']
- 漏项（FN）：2 — ['信用减值损失损失以-', '2．将重分类进损益的其他综合收益']

### cash_flow

- 匹配项（TP）：27
- 多余项（FP）：2 — ['处置固定资产、无形资产和其他长期资产收回的现金净额', '收到再保业务现金净额保户储金及投资款净增加额收取利息、手续费及佣金的现金']
- 漏项（FN）：7 — ['收取利息、手续费及佣金的现金', '收到其他与经营活动有关的现金', '购买商品、接受劳务支付的现金', '支付利息、手续费及佣金的现金', '经营活动产生的现金流量净额']

## 备注

- F1 计算口径：item 名严格相等 + value 相对误差 ≤ 1%
- 粒度对齐：ground truth 全部科目同 scope/period 时，预测行先筛选到同粒度再匹配
- Mock 模式仅验证脚本可运行性，F1 必为 1.0（用 ground truth 自身作为模型输出）
- 真实模式（M4.10）走完整 E2E 管道：上传 PDF → MQ 编排 → DeepSeek API 抽取 → StatementWriter 落库 → 回读 financial_statement
- 真实评估请参考 `data/benchmark/README.md` 补齐完整 ground truth JSON
