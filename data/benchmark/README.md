# M2.12 集成测试 Benchmark 数据

> 用于 M2.12 端到端集成测试的真实年报样本与 ground truth 标注。

## 1. 真实年报 PDF

3 份 A 股 2025 年度报告（来源：巨潮资讯网公开披露）：

| 文件 | 股票代码 | 公司 | 行业 | 路径 |
|---|---|---|---|---|
| 600519_贵州茅台_2025年年度报告.pdf | 600519 | 贵州茅台 | 白酒 | `data/sample_reports/` |
| 000001_平安银行_2025年年度报告.pdf | 000001 | 平安银行 | 银行 | `data/sample_reports/` |
| 300750_宁德时代：2025年年度报告.pdf | 300750 | 宁德时代 | 新能源电池 | `data/sample_reports/` |

> **注意**：plan §4 M2.12 原文要求"茅台 / 平安 / 宁德 2024 年报"，但实际下载的是 2025 年报（2024 年报在 2025 年 4 月披露，2025 年报在 2026 年 4 月披露）。本文档对齐到 `data/sample_reports/` 中的真实文件名。

## 2. Ground Truth 标注

### 2.0 M6.08 全量基准（3 份缩减口径，当前有效）

M6.08 以 `scripts/rebuild_gt.py`（`rebuild_moutai_gt.py` 的参数化泛化）从
PDF 文本层重建三份全量 GT（合并+本期，科目名经 `normalize_item_name` 单源
规范化，数值保持文档原单位）：

| 文件 | 公司 | 单位 | BS | IS | CF | 重建要点 |
|---|---|---|---|---|---|---|
| `moutai_2025.json` | 贵州茅台 | 元 | 53 | 38 | 34 | M4.10 交付，`rebuild_gt.py` 回归 diff=0 |
| `pingan_2025.json` | 平安银行 | 百万元 | 44 | 36 | 66 | 银行年报：母公司段标题为「银行…」；长科目名折行需 `--wrap-join`；数值为千分位整数 + 括号负数 |
| `catl_2025.json` | 宁德时代 | 千元 | 61 | 44 | 37 | 标准编号标题；附注交叉引用需严格标题匹配 |

验证方式（无需金融知识）：三份 GT 的「资产总计 = 负债合计 + 所有者权益
（股东权益）合计」恒等式经生产勾稽规则类（`BalanceSheetIdentityRule`）
校验 diff=0；勾稽预期结果由生产规则类跑 GT 数值得出（`eval_e2e.py` 运行时
计算）；局限：regex/文本层自动重建、未人工逐项核对。

问答基准：`qa_questions.json`（茅台，M5.09 交付）+
`qa_questions_000001.json` / `qa_questions_300750.json`（M6.08 新增，
5 问/家，key_facts 由 GT 数值与 PDF 文本机械推导）。

端到端评估：`python scripts/eval_e2e.py`（详见脚本 docstring 与
`docs/eval/m6-e2e-3reports.md`）。

### 2.1 已交付（M2.12 历史样本，部分需人工核对）

通过 `scripts/extract_ground_truth.py` 从 PDF 文本层自动提取关键科目作为 ground truth 样本（regex 提取，少量数值需人工核对）：

| 文件 | 公司 | BS 项数 | IS 项数 | CF 项数 | 提取方式 |
|---|---|---|---|---|---|
| `moutai_2025_sample.json` | 贵州茅台 | 4 | 2 | 1 | 手工标注 |
| `moutai_2025.json` | 贵州茅台 | 7 | 6 | 4 | 自动提取（regex） |
| `pingan_2025_sample.json` | 平安银行 | 5 | 5 | 3 | 自动提取（regex） |
| `catl_2025_sample.json` | 宁德时代 | 7 | 6 | 4 | 自动提取（regex） |

### 2.2 人工核对要点

自动提取的 regex 偶尔抓到错误数字（典型错误项）：

- BS 中的"所有者权益合计" — regex 可能抓到附近脚注的小数字（如茅台被识别为 1.26B，实际应为 ~250B）
- IS 中的"营业利润" — regex 可能抓到表格中相邻行的数值
- 任何"上年同期"对比值 — regex 不区分本期/上期，需人工区分

用户在跑 real 7B F1 评估前应按 PDF 原文逐项核对 3 份 ground truth JSON 数值。

### 2.3 重新自动提取（可选）

如需对其他 PDF 重新提取或调整提取规则：

```bash
python scripts/extract_ground_truth.py \
    --pdf data/sample_reports/600519_贵州茅台_2025年年度报告.pdf \
    --output data/benchmark/ground_truth/moutai_2025.json \
    --report-period 2025-12-31 \
    --company-code 600519 \
    --company-name 贵州茅台
```

> 标注规范：每条记录包含 `item` / `value` / `scope`（合并/母公司）/ `period`（本期/上期）/ `source_page` 字段；value 必须是年报披露的精确数值（单位：元）。

## 3. F1 评估运行方式

```bash
# 最小样本（无 GPU 也可跑，仅验证脚本可运行）
python scripts/eval_m2_f1.py \
    --pdf data/sample_reports/600519_贵州茅台_2025年年度报告.pdf \
    --ground-truth data/benchmark/ground_truth/moutai_2025_sample.json \
    --mock-llm \
    --output docs/eval/m2-f1-moutai-sample.md

# 完整评估（需要 GPU + 已加载 7B 4-bit 模型 + ai-service 运行）
python scripts/eval_m2_f1.py \
    --pdf data/sample_reports/600519_贵州茅台_2025年年度报告.pdf \
    --ground-truth data/benchmark/ground_truth/moutai_2025.json \
    --ai-service-url http://localhost:8000 \
    --output docs/eval/m2-f1-moutai.md
```

## 4. SLA 评估运行方式

```bash
# Mock 模式（无 GPU，仅测 PARSE 耗时）
python scripts/eval_m2_sla.py \
    --pdf data/sample_reports/600519_贵州茅台_2025年年度报告.pdf \
    --mock-llm --strict \
    --output docs/eval/m2-sla-moutai-sample.md

# Real 7B 模式（需要 GPU + ai-service）
python scripts/eval_m2_sla.py \
    --pdf data/sample_reports/600519_贵州茅台_2025年年度报告.pdf \
    --ai-service-url http://localhost:8000 --strict \
    --output docs/eval/m2-sla-moutai.md
```

## 5. 合规提示

- 年报为上市公司公开披露信息，仅用于个人研究测试
- Ground truth 标注需人工对照原文，自动提取结果仅供人工标注起点
- 评估结果归档到 `docs/eval/` 目录
