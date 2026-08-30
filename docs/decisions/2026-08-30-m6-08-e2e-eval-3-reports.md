# M6.08 端到端评估缩减口径决策（3 份基准年报 + AI 代评）

> 日期：2026-08-30
> 关联任务：M6.08（plan §4 / spec §7.4）
> 状态：已执行

## 背景

M6.08 原要求 30 份基准年报（10 行业 × 3 公司）+ 人工标注 ground truth。
执行时用户明确缩减口径：

1. 仅使用已有的 3 份年报（`data/sample_reports/`：平安银行/宁德时代/贵州茅台 2025 年报）
2. 用户无金融知识，无法承担 ground truth 人工标注与质量人工评分
3. DeepSeek LLM-as-judge 作为设计决策保留入口，开发期评分由 AI（ZCode）代评

## 决策列表

- **D1 数据集规模 30 → 3**：以现有 3 份年报跑通全量评估；30 份集齐后可无缝
  重跑（`eval_e2e.py` 的 `COMPANIES` 表 + GT/问答基准按公司注册即可扩展）。
- **D2 GT 全量自动化重建**：`scripts/rebuild_gt.py`（`rebuild_moutai_gt.py`
  参数化泛化）从 PDF 文本层重建三份全量 GT（合并+本期），无需金融知识：
  - 平安银行（银行年报）差异处理：母公司段标题为「银行…」而非「母公司…」；
    数值为千分位整数 + 括号负数（茅台版 regex 强制两位小数，直接套用解析出
    0 行）；单位为**百万元**（茅台=元、宁德=千元，GT 与抽取链路同源文档单位）；
    长科目名折行（「…收回/的现金」）需 `--wrap-join` 拼接续行残片。
  - 宁德时代：附注交叉引用（「合并资产负债表项目注释」）会重触发段状态机
    （BS 误吞 890 行），改为严格标题匹配（去编号/续表后缀后须整体等于标题）。
  - 茅台回归验证：`rebuild_gt.py` 重生成茅台 GT 与 M4.10 已验证文件 diff=0。
- **D3 勾稽预期 = 生产规则跑 GT**：非循环论证——GT 来自 PDF 文本层（与 LLM
  抽取链路相互独立），用生产 `BalanceSheetIdentityRule` 等三个规则类对 GT 重算
  期望 verdict，与管线 API `isPass` 对照。三份 GT 恒等式校验 diff=0。
  规则 2/3 为生产既定启发式（WARN 级、5%/20% 相对容差），对真实年报预期即
  FAIL，故口径为「管线 verdict 与 GT 期望是否一致」而非「规则须全过」。
- **D4 质量评分双模式**：`eval_e2e.py --judge manual|deepseek`（默认 manual）。
  本次开发期由 AI 代评（阅读落盘产物按 rubric 打分，报告标注「AI 代评，待
  人工复核」）；`--judge deepseek` 保留 LLM-as-judge 入口（rubric prompt →
  DeepSeek json_mode），未来一键切换。
- **D5 异常召回本次不计分**：召回率需专家标注异常清单，无金融知识无法标注；
  评估输出异常分布（类型/级别）作描述性记录。
- **D6 dev 镜像补装管线验收必需依赖（修复 M6.08 首跑暴露的连锁缺陷）**：本机
  Docker 环境全新重建镜像后暴露 dev 阶段依赖缺口，四处连锁——
  1. `pika` 仅在 `[prod]` extra：MQ 消费者线程启动即 `import pika`（非惰性），
     缺失时线程静默死亡 → `q.parse.requests` 0 消费者 → 全链路卡死
     PARSE_RUNNING（healthcheck 不覆盖消费者存活）；
  2. `pymupdf`：PARSE 硬依赖（缺失直接 AiException，重试耗尽任务 FAILED）；
  3. `paddlepaddle`/`paddleocr` 全组（paddlex 强制校验 ocr extra 依赖集）：
     PARSE 的 PP-Structure 表格还原是抽取质量前提——dev 缺 paddle 时每页
     「Layout analyzer failed」→ 抽取输入残缺（37 行、无上期/母公司，实测
     F1 0.29）；决策与 M5.09 补 torch 同理（验收能力 dev 必须可跑），首次
     PARSE 的模型下载持久化于 ai_cache 卷；
  4. `weasyprint`/`matplotlib`/`pillow` + 系统库 pango/中文字体：REPORT 阶段
     产物五类 GENERATED 是验收硬门槛。
  修复：dev 阶段安装行与 apt 清单补齐。教训：「惰性 import 降级」策略仅对
  有 fallback 的依赖成立，线程启动 import 与验收必需路径上的依赖必须在
  dev 阶段显式安装。
- **D7 场景 token 预算适配推理型模型**：`LLM_API_MODEL=deepseek-v4-flash`
  的 reasoning 计入 `max_tokens`，report 场景默认 2048 连 reasoning 都不够
  （finish_reason=length、content 空 → ReportGenerator 降级模板报告，首跑
  实测），reviewer 场景 512 同病。修复：report 2048 → 8192、reviewer
  512 → 4096（均在 `model_max_new_tokens=16384` 钳制内）；extractor 走
  settings 全量预算无需改。属被测系统的先行缺陷修复（与 D6 同性质），
  修复后评估才反映真实能力。

## 已完成 checklist

- [x] `scripts/rebuild_gt.py`：通用 GT 重建（单位无关/千分位整数/括号负数/
      严格标题匹配/银行段边界/`--wrap-join` 折行拼接）
- [x] 三份全量 GT 重建 + 恒等式校验（diff=0）+ 茅台回归（diff=0）
- [x] `data/benchmark/qa_questions_000001.json` / `qa_questions_300750.json`
      （5 问/家，key_facts 由 GT 数值与 PDF 文本机械推导）
- [x] `scripts/eval_e2e.py`：HTTP 驱动全链路评估（F1/勾稽对照/异常分布/
      产物完整性/问答命中与首 token/端到端耗时/成本计数器增量；
      429 Retry-After 感知；`--judge manual|deepseek`；产物落盘
      `docs/eval/artifacts/{run_ts}/`）
- [x] dev 镜像依赖缺口修复（D6：pika/pymupdf/paddle 全组/weasyprint 等 +
      pango/中文字体系统库）与场景 token 预算修复（D7：report 8192 /
      reviewer 4096）
- [x] 3 份全量实跑 + AI 代评（见 `docs/eval/m6-e2e-3reports.md`）

## 发现的风险

- **dev 镜像惰性依赖策略的盲区**（D6 已修，方法论教训）：线程启动即 import
  的依赖与验收必需路径上的依赖不属于「惰性可缺」范畴，缺失时无日志级告警、
  healthcheck 不覆盖；后续新增线程型组件或验收产物路径时，须在 dev 阶段
  显式装依赖或加启动失败告警。
- **GT 为文本层自动重建**：未人工逐项核对，孤立 regex 错误可能压低 F1
  （脚本输出 FN/FP 明细可按 source_page 回查）。
- **勾稽规则 2/3 的容差分支敏感**：可选调整项（盈余公积/折旧）是否被抽出
  决定容差分支（绝对 0.01 vs 相对 5%/20%），可能造成期望与实际 verdict 的
  非抽取性偏差（见评估报告勾稽对照表）。

## 追加决策（评估实跑阶段）

- **D8 抽取 token 预算提升至 32768（compose env + caps map）**：银行年报大表
  （平安 CF HTML ~8.6k 字符）抽取时，推理型模型的 reasoning 耗尽 16384 预算致
  content 为空（EXTRACT_CF 重试耗尽，任务 FAILED）。API 实测接受 max_tokens=32768；
  `MODEL_MAX_NEW_TOKENS` 经 docker-compose 注入 32768，并加入
  `_MODEL_MAX_TOKENS_CAPS`（deepseek-v4-flash: 32768）防超限 400。
- **D9 段定位修复（extractor/handler.py）**：银行年报目录页的干净标题块把段
  起点锚到目录页（select_table 返回 None → 「no X table found」）；且「银行…」
  母公司段标题不在边界清单，BS 段会吞并银行口径表。修复：目录页金额密度甄别
  （≥3 个千分位数值才作段起点，无密度命中回退旧行为）+ `_ALL_TITLES` 追加
  「银行资产负债表/银行利润表/银行现金流量表」。
- **D10 报表页金额密度门控放宽（parser/document_parser.py）**：原
  `_AMOUNT_CELL_RE` 强制两位小数（按茅台调优），平安（百万元整数）与宁德
  （千元整数）报表页密度恒为 0 → 报表页静默跳过 → 抽取无表格可用。修复：
  千分位数字即可，小数可选。

## 最终结果（2026-08-30，详见 docs/eval/m6-e2e-3reports.md）

| 指标 | 目标 | 实测 | 达标 |
|---|---|---|---|
| 三表抽取 F1（3 家宏平均） | ≥0.85 | **0.8636**（平安 0.7354 / 宁德 0.9150 / 茅台 0.9403） | ✅ |
| 勾稽判定准确率 | ≥0.95 | **1.0000**（9/9） | ✅ |
| 问答关键事实命中率 | ≥0.85 | **0.8000**（12/15） | ❌ |
| 问答首 token P95 | <15s | 7.73s | ✅ |
| 端到端耗时 P95 | ≤5min | **13.62min** | ❌ |
| 报告产物完整性 | 5 类齐备 | 3/3 家 | ✅ |
| 报告质量（AI 代评） | ≥4.0 | **4.53** | ✅ |
| 问答相关性（AI 代评） | ≥4.0 | **4.27** | ✅ |
| 异常召回 | ≥0.80 | 待人工标注，不计分 | ⏳ |

核心发现（详见评估报告「评估发现」节）：① 括号负数符号约定（抽取输出绝对值
vs GT 保留文档符号）压低平安 F1；② 多公司 KB 检索未按公司过滤，审计机构类
问题跨公司污染；③ 报告/问答金额单位未随 statement.unit 传递（数量级错误）；
④ 端到端 SLA 受 PARSE/EXTRACT 性能债务制约（风险 R4 兑现）；⑤ agent 偶发
空答案（8 次工具调用后放弃）。

## 下一步行动项

- 集齐 30 份年报（10 行业 × 3 公司）后扩展 `eval_e2e.py` COMPANIES 表与
  GT/问答基准，重跑全量评估（M6.08 原口径）
- 人工复核三份 GT 的孤立数值（对照 `source_page`）
- （可选）启用 `--judge deepseek` 交叉验证 AI 代评分数
- 落实评估发现五项改进：抽取符号约定 + normalize 半角前缀、KB 公司过滤、
  NLG 单位透传、PARSE/EXTRACT 性能优化、agent 兜底回答
