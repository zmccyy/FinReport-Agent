# 2026-07-22 M3.03 异常检测实现

## 背景

M3.01 已完成 3 条硬编码勾稽规则引擎；M3.02 完成 LLM 复核解释差异原因。
但 spec §2.3 M8 L210-212 还要求异常检测：

* 同比变动 > 30%
* 环比变动 > 30%
* 科目逻辑异常（如应收账款激增但营收下滑）

plan M3.03 验收标准：能识别明显异常；severity 字段正确；构造测试用例
（同科目本期 100 → 下期 200，应触发异常）。

## 决策列表

1. **同步纯计算** — 与 `RuleEngine` 一致，`AnomalyDetector` 是同步类，
   无 IO、不调 LLM。M3.02 `LLMReviewer` 可独立扩展复核逻辑异常
   （M3.03 不集成，避免过度设计）；spec §8.1 单进程只能装 1 个 7B，
   异常检测阶段不应触发 LLM 加载。

2. **多期快照接口** — `StatementSnapshot` 是单期；`detect` 接受
   `previous` / `year_ago` 两个可选对比期。调用方（M3.04 L2 编排）
   负责从历史报告中查询并构造对比期 snapshot。同时提供同比和环比时
   各自独立检测，去重留给调用方（按 `item_name` + `anomaly_type`
   去重）。

3. **变动异常阈值分级** — `|ratio| >= 30%` 触发 WARN，
   `|ratio| >= 100%` 升级 ERROR（spec §2.3 M8 L211 默认 30%）。
   100% 是经验值：A 股年报科目同比变动 > 100% 通常是重大事件
   （并购、剥离、会计政策变更），需立即排查。

4. **逻辑异常 4 条规则** —
   - **应收账款激增 + 营收下滑** — AR +30% AND 营收 -10%
     （虚增收入嫌疑）
   - **存货激增 + 营收下滑** — Inventory +30% AND 营收 -10%
     （滞销或减值风险）
   - **营收增长 + 净利润下滑** — Revenue +30% AND NI -10%
     （成本失控或毛利率恶化）
   - **净利润增长 + OCF 下滑** — NI +30% AND OCF -30%
     （盈利质量恶化，可能存在应收/存货堆积）
   
   severity 固定为 ERROR（比变动异常更严重，可能存在财务造假）。

5. **零值保护** — 对比期值为 0 时 `_change_ratio` 返回 None 跳过
   （避免除零）；本期非零而对比期为零时也跳过（A 股年报科目从无到有
   是新增科目，单独规则处理而非变动异常）。

6. **科目同义词** — 复用 `StatementSnapshot.require` 按同义词组查询，
   容忍 A 股年报变体：
   - "营业收入" vs "营业总收入" vs "营业收入合计"
   - "应收账款" vs "应收账款净额" vs "应收账款余额"
   - "净利润" vs "归属母公司股东的净利润" vs "归属于母公司所有者的净利润"
   - "经营活动现金流量净额" vs "经营活动产生的现金流量净额"

7. **不可变输入** — `detect` 不修改输入 `StatementSnapshot`；产出新的
   `list[Anomaly]`，调用方 `model_copy` 到 `CheckResult.anomalies`。
   原始 snapshot 可被多次复用（如先送 RuleEngine 再送 AnomalyDetector）。

8. **confidence 不动** — 与 `LLMReviewer` 一致，`AnomalyDetector` 只
   产出 anomalies，不重新计算 `CheckResult.confidence`。M3.04 编排层
   可按需调整（避免双重扣分：规则失败已扣分，异常不应再扣）。这保持了
   `confidence` 的语义稳定：基于勾稽规则通过率，不掺杂异常计数。

9. **逻辑异常 anomaly_type 固定 LOGIC_CONFLICT** — `_build_logic_anomaly`
   写入 `Anomaly.anomaly_type = "logic_conflict"`，与触发时的对比类型
   （YOY/QOQ）解耦。对比类型仅体现在 `description` 中（如"应收账款激增
   但营收下滑（同比变动：ar_ratio=50.0%, revenue_ratio=-20.0%）"）。
   便于 L2 落表后按 `anomaly_type` 筛选逻辑异常单独展示。

10. **Decimal 精度** — 变动比例用 `Decimal` 计算，避免 float 累积误差
    （spec §8.4 数据一致性）。`(current - previous) / abs(previous)`
    保证负数基期也能正确计算。

## 已完成 checklist

- [x] `ai-service/app/modules/reasoner/anomaly_detector.py` 实现
      `AnomalyDetector` 类 + `AnomalyType` 枚举
- [x] 5 个同义词组常量（REVENUE/AR/INVENTORY/NET_INCOME/OCF_SYNONYMS）
- [x] `ai-service/tests/test_m3_anomaly_detector.py` 编写 44 个测试用例
      （12 个测试类）
- [x] M3.01 + M3.02 + M3.03 共 86 个测试全部通过
- [x] 全 L3 共 359 个测试全部通过（M1 + M2 + M3 回归无破坏）
- [x] 模块覆盖率：`anomaly_detector.py` 100% ≥ 80% 门槛
- [x] ruff / black 全部通过
- [x] `docs/progress/m3.md` 更新 M3.03 完成状态 + 交付说明
- [x] 验收标准对照全部 ✅

## 测试用例分布

| 测试类 | 用例数 | 覆盖场景 |
|---|---|---|
| `TestAnomalyType` | 2 | 枚举值与中文名 |
| `TestChangeRatio` | 5 | 正常计算/负基期/零值保护 |
| `TestDetectYoYChange` | 9 | 100%/30%/10%/−50%/−100% 边界 + 多科目 + 三表独立 |
| `TestDetectQoQChange` | 2 | 环比触发 + 同比环比并存 |
| `TestLogicConflicts` | 7 | 4 条规则 + 同向不触发 + 阈值未达 + metric_value |
| `TestSynonyms` | 4 | 营收/净利润/OCF 同义词 + 缺失科目降级 |
| `TestSeverity` | 4 | 30%/100%/300% 分级 + 逻辑异常固定 ERROR |
| `TestNoHistoricalData` | 2 | 无对比期 + 空 snapshot |
| `TestCustomThresholds` | 3 | 自定义 change/large/decline 阈值 |
| `TestImmutability` | 1 | 输入 snapshot 不被修改 |
| `TestIntegrationWithCheckResult` | 3 | model_copy 集成 + confidence 不变 + all_pass |
| `TestSerialization` | 2 | model_dump JSON + to_dict 含 anomalies |

## 发现的风险

- **历史报告查询接口未实现** — `detect` 需要 `previous` / `year_ago`
  两个对比期 snapshot，但 L2 还没有"按公司代码 + 报告期查询历史财报"
  的接口。M3.04 编排层需要先实现这个查询，否则 `AnomalyDetector`
  只能拿 `current` 单期跑（产出空 anomalies）。
- **逻辑异常阈值未调优** — 30% / -10% / -30% 是经验值，未基于真实
  A 股年报数据校准。M3.10 端到端测试时若发现误报/漏报率高，需要调整
  阈值或加入行业分类（如制造业 vs 互联网行业变动幅度差异大）。
- **未实现 LLM 复核逻辑异常** — M3.02 决策记录中提到"复用 LLMReviewer
  复核逻辑异常"，但 M3.03 未集成。逻辑异常的 description 是模板拼接，
  无法解释具体业务原因（如"应收激增可能是并购并表导致"）。后续可扩展
  `LLMReviewer.review_anomaly(anomaly, snapshot)` 方法。
- **变动异常未排除"科目重分类"** — A 股年报科目可能因会计政策变更
  重分类（如"应收账款"拆分为"应收账款" + "应收票据"），导致单项变动
  > 30% 但实际业务无变化。当前实现无法识别这种情况，会误报。

## 下一步行动项

1. **M3.04 L2 写 accounting_check + anomaly 表** — 实现
   `CheckResultWriter` 消费 `CheckResult`，按 `llm_reviewed` 字段区分
   note 来源；编排 `RuleEngine.check` → `LLMReviewer.review` →
   `AnomalyDetector.detect` → `CheckWriter.write` 链路；实现历史报告
   查询接口构造 `year_ago` / `previous` snapshot。
2. **M3.10 端到端 SLA 测试** — 真实茅台年报同比/环比数据验证异常
   检出合理性；调整阈值参数；评估是否需要 LLM 复核逻辑异常。
3. **逻辑异常 LLM 复核扩展** — 在 `LLMReviewer` 新增
   `review_anomaly` 方法，让 7B 解释逻辑异常的具体业务原因（如并购、
   会计政策变更、行业周期等）。
4. **行业分类阈值** — 后续可按申万行业分类调整变动阈值（制造业 30% /
   互联网 50% / 金融 20%），降低误报率。
