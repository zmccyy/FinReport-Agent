# M2.12 抽取 F1 评估报告

> 生成时间：2026-07-21T04:51:34+00:00
> PDF：`data\sample_reports\300750_宁德时代：2025年年度报告.pdf`
> Ground truth：`data\benchmark\ground_truth\catl_2025_sample.json`
> LLM 模式：mock（无 GPU）

## 总体指标

| 指标 | 值 |
|---|---|
| Overall F1 | **1.0000** |
| Overall Precision | 1.0000 |
| Overall Recall | 1.0000 |
| M2.12 门槛 (F1 ≥ 0.70) | ✅ 通过 |

## 各表指标

| 表类型 | Precision | Recall | F1 | TP | FP | FN |
|---|---|---|---|---|---|---|
| balance_sheet | 1.0000 | 1.0000 | **1.0000** | 7 | 0 | 0 |
| income_statement | 1.0000 | 1.0000 | **1.0000** | 6 | 0 | 0 |
| cash_flow | 1.0000 | 1.0000 | **1.0000** | 4 | 0 | 0 |

## 各表详情

### balance_sheet

- 匹配项（TP）：7
- 多余项（FP）：0 — []
- 漏项（FN）：0 — []

### income_statement

- 匹配项（TP）：6
- 多余项（FP）：0 — []
- 漏项（FN）：0 — []

### cash_flow

- 匹配项（TP）：4
- 多余项（FP）：0 — []
- 漏项（FN）：0 — []

## 备注

- F1 计算口径：item 名严格相等 + value 相对误差 ≤ 1%
- Mock 模式仅验证脚本可运行性，F1 必为 1.0（用 ground truth 自身作为模型输出）
- Real 7B 模式需要 GPU + 已加载 Qwen2.5-7B-Int4 模型
- 真实评估请参考 `data/benchmark/README.md` 补齐完整 ground truth JSON
