package com.finreport.service.reasoner;

import java.math.BigDecimal;
import java.util.List;
import java.util.Map;
import java.util.Set;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.reactive.TransactionalOperator;

import com.finreport.domain.entity.AccountingCheck;
import com.finreport.domain.entity.AnomalyRecord;
import com.finreport.repository.AccountingCheckRepository;
import com.finreport.repository.AnomalyRepository;
import com.finreport.repository.ReportRepository;
import com.finreport.repository.TaskRepository;

import reactor.core.publisher.Mono;

/**
 * 勾稽结果写入器 — spec §3.2 链路 B + plan §4 M3.04。
 *
 * <p>消费 L3 CHECK progress 携带的 {@code result} payload，把勾稽规则结果 +
 * 异常检测结果批量写入 {@code accounting_check} 和 {@code anomaly} 表。
 * {@code TaskOrchestrator.handleStepSuccess} 在 CHECK 首次 SUCCESS 时调用本类，
 * 先于 {@code handleNonExtractStepSuccess} 触发 REPORT（spec §3.2.1：
 * CHECK 完成后触发 REPORT）。</p>
 *
 * <p><b>失败策略</b>：写库失败只 log error 不抛异常，避免阻断状态机推进；
 * REPORT 阶段会因数据缺失自然失败并暴露问题（spec §8.4 "失败不强制回滚，
 * 保留数据供排查"）。这样保持 M2.08 已稳定的 CHECK→REPORT 路径不被回滚。</p>
 *
 * <p><b>幂等性</b>：L3 progress 携带 {@code idempotencyKey=taskId:step}，
 * {@code TaskOrchestrator.handleStepSuccess} 已对重放 SUCCESS 做去重（reconcile
 * 路径不调本类），因此 {@code writeCheckResult} 不再做额外幂等检查。</p>
 *
 * <p><b>reconcile 路径说明</b>：进程崩溃发生在 step.setStatus(SUCCESS) 之后、
 * writeCheckResult 完成之前时，MQ 重放 CHECK SUCCESS 只调度 REPORT，不补写
 * check 结果。CHECK 链路无等价于 {@code ExtractCacheService} 的结果缓存，
 * 重放时拿不到原 result。此场景下 REPORT 会因数据缺失失败，由用户重传 PDF
 * 触发整体重跑恢复（spec §8.4 失败不强制回滚，保留数据供排查）。</p>
 *
 * <p><b>payload 形状</b>（M3.04 契约，对齐 L3 {@code CheckResult.to_dict()}）：
 * <pre>{@code
 * {
 *   "rules": [
 *     {
 *       "rule_type": "balance_sheet_identity",
 *       "rule_name": "资产=负债+所有者权益",
 *       "expected": "1000.00",      // Decimal → str in JSON mode
 *       "actual": "1000.00",
 *       "diff": "0.00",
 *       "is_pass": true,
 *       "severity": "INFO",
 *       "tolerance": "0.01",
 *       "note": "",
 *       "missing_items": [],
 *       "llm_reviewed": false
 *     },
 *     ...
 *   ],
 *   "anomalies": [
 *     {
 *       "item_name": "货币资金",
 *       "anomaly_type": "yoy_change",
 *       "metric_value": "1.0",
 *       "threshold": "0.30",
 *       "description": "货币资金同比变动增长 100.0%...",
 *       "severity": "ERROR"
 *     },
 *     ...
 *   ],
 *   "confidence": 0.85,
 *   "report_period": "2024-12-31"
 * }
 * }</pre></p>
 *
 * <p><b>事务边界</b>：accounting_check + anomaly 两张表在同一事务内顺序写入
 * （spec §8.4 任务边界 = 事务边界）；任一表写入失败整体回滚，避免半成品数据。
 * 通过 {@link TransactionalOperator} 包裹 saveAll 链实现。</p>
 */
@Service
public class CheckResultWriter {

    private static final Logger log = LoggerFactory.getLogger(CheckResultWriter.class);

    /**
     * 合法的异常类型白名单 — 对齐 spec §2.3 M8 + L3 {@code AnomalyType} 枚举。
     * 非白名单值跳过写库并 log warn，避免脏数据污染 anomaly 表。
     */
    private static final Set<String> VALID_ANOMALY_TYPES =
            Set.of("yoy_change", "qoq_change", "logic_conflict");

    private final TaskRepository taskRepo;
    private final ReportRepository reportRepo;
    private final AccountingCheckRepository checkRepo;
    private final AnomalyRepository anomalyRepo;
    private final TransactionalOperator transactionalOperator;

    public CheckResultWriter(
            TaskRepository taskRepo,
            ReportRepository reportRepo,
            AccountingCheckRepository checkRepo,
            AnomalyRepository anomalyRepo,
            TransactionalOperator transactionalOperator) {
        this.taskRepo = taskRepo;
        this.reportRepo = reportRepo;
        this.checkRepo = checkRepo;
        this.anomalyRepo = anomalyRepo;
        this.transactionalOperator = transactionalOperator;
    }

    /**
     * 把一条 CHECK progress 的 result payload 写入 accounting_check + anomaly 表。
     *
     * @param taskId 任务 ID
     * @param result L3 progress 携带的 result payload（CheckResult.to_dict() 输出）
     * @return 写入的总行数（rules + anomalies）；解析失败或无数据返回 0
     */
    public Mono<Integer> writeCheckResult(String taskId, Map<String, Object> result) {
        log.debug("[CheckResultWriter] writeCheckResult taskId={} resultKeys={}",
                taskId, result == null ? "[]" : result.keySet());

        if (result == null || result.isEmpty()) {
            log.warn("[CheckResultWriter] 空 result，跳过写库 taskId={}", taskId);
            return Mono.just(0);
        }

        ParsedCheckResult parsed = parseCheckResult(result);
        if (parsed == null) {
            log.warn("[CheckResultWriter] result 解析失败，跳过写库 taskId={}", taskId);
            return Mono.just(0);
        }

        return resolveReportId(taskId)
                .map(reportId -> {
                    if (reportId == null) {
                        log.error("[CheckResultWriter] 无法解析 reportId，跳过写库 taskId={}", taskId);
                    }
                    return reportId;
                })
                .flatMap(reportId -> {
                    if (reportId == null) {
                        return Mono.just(0);
                    }
                    return persistAll(reportId, parsed)
                            .doOnSuccess(count -> log.info(
                                    "[CheckResultWriter] 写入成功 taskId={} reportId={} "
                                            + "rules={} anomalies={} total={}",
                                    taskId, reportId, parsed.rules().size(),
                                    parsed.anomalies().size(), count))
                            .onErrorResume(error -> {
                                log.error("[CheckResultWriter] 写入失败 taskId={} reportId={}",
                                        taskId, reportId, error);
                                return Mono.just(0);
                            });
                })
                .switchIfEmpty(Mono.defer(() -> {
                    log.error("[CheckResultWriter] task 不存在或无 reportId 关联，跳过写库 taskId={}",
                            taskId);
                    return Mono.just(0);
                }));
    }

    // ========================================================================
    // Parsing
    // ========================================================================

    /**
     * 从 result payload 解析 rules + anomalies。
     *
     * <p>容错策略：rules 字段缺失或非 List 返回 null；anomalies 字段缺失视为空列表
     * （M3.01 阶段 L3 还未集成 AnomalyDetector，anomalies 为空是合法情况）。</p>
     */
    static ParsedCheckResult parseCheckResult(Map<String, Object> result) {
        Object rulesObj = result.get("rules");
        if (!(rulesObj instanceof List<?> rulesList)) {
            return null;
        }

        List<AccountingCheck> rules = new java.util.ArrayList<>();
        for (Object ruleObj : rulesList) {
            if (!(ruleObj instanceof Map<?, ?> ruleMap)) {
                continue;
            }
            AccountingCheck row = parseRule(ruleMap);
            if (row != null) {
                rules.add(row);
            }
        }

        if (rules.isEmpty()) {
            // rules 为空视为无效 payload（CHECK 阶段至少应产出 3 条规则结果）。
            return null;
        }

        List<AnomalyRecord> anomalies = new java.util.ArrayList<>();
        Object anomaliesObj = result.get("anomalies");
        if (anomaliesObj instanceof List<?> anomaliesList) {
            for (Object anomalyObj : anomaliesList) {
                if (!(anomalyObj instanceof Map<?, ?> anomalyMap)) {
                    continue;
                }
                AnomalyRecord row = parseAnomaly(anomalyMap);
                if (row != null) {
                    anomalies.add(row);
                }
            }
        }

        return new ParsedCheckResult(rules, anomalies);
    }

    private static AccountingCheck parseRule(Map<?, ?> ruleMap) {
        String ruleType = stringOrNull(ruleMap.get("rule_type"));
        String ruleName = stringOrNull(ruleMap.get("rule_name"));
        if (ruleType == null || ruleName == null) {
            return null;
        }

        Object isPassObj = ruleMap.get("is_pass");
        Boolean isPass = isPassObj instanceof Boolean b ? b : null;

        String severity = stringOrDefault(ruleMap.get("severity"), "INFO");
        String note = stringOrDefault(ruleMap.get("note"), "");

        // llm_reviewed 字段未在 accounting_check 表中存储（M3.04 决策：避免 schema 膨胀）；
        // L2 不需要区分 note 来源，note 字段已含 LLM 复核标记（"[LLM 复核] ..."）。

        return AccountingCheck.builder()
                .ruleType(ruleType)
                .ruleName(ruleName)
                .expected(bigDecimalOrNull(ruleMap.get("expected")))
                .actual(bigDecimalOrNull(ruleMap.get("actual")))
                .diff(bigDecimalOrNull(ruleMap.get("diff")))
                .isPass(isPass)
                .severity(severity)
                .note(note)
                .build();
    }

    private static AnomalyRecord parseAnomaly(Map<?, ?> anomalyMap) {
        String anomalyType = stringOrNull(anomalyMap.get("anomaly_type"));
        if (anomalyType == null) {
            return null;
        }
        if (!VALID_ANOMALY_TYPES.contains(anomalyType)) {
            log.warn("[CheckResultWriter] 未知 anomaly_type={}, 跳过", anomalyType);
            return null;
        }

        String itemName = stringOrDefault(anomalyMap.get("item_name"), "");
        String severity = stringOrDefault(anomalyMap.get("severity"), "WARN");
        String description = stringOrDefault(anomalyMap.get("description"), "");

        return AnomalyRecord.builder()
                .itemName(itemName)
                .anomalyType(anomalyType)
                .metricValue(bigDecimalOrNull(anomalyMap.get("metric_value")))
                .threshold(bigDecimalOrNull(anomalyMap.get("threshold")))
                .description(description)
                .severity(severity)
                .build();
    }

    private static String stringOrNull(Object value) {
        if (value instanceof String s && !s.isBlank()) {
            return s;
        }
        return null;
    }

    private static String stringOrDefault(Object value, String fallback) {
        if (value instanceof String s && !s.isBlank()) {
            return s;
        }
        return fallback;
    }

    /**
     * 把 L3 传来的 Decimal（JSON 序列化为 str / number）转为 BigDecimal。
     *
     * <p>对齐 StatementWriter.bigDecimalOrNull 实现：Number 用 toString() 中转
     * 避免 double 精度损失；String 直接解析。</p>
     */
    private static BigDecimal bigDecimalOrNull(Object value) {
        if (value == null) {
            return null;
        }
        if (value instanceof BigDecimal bd) {
            return bd;
        }
        if (value instanceof Number n) {
            try {
                return new BigDecimal(n.toString());
            } catch (NumberFormatException e) {
                return null;
            }
        }
        if (value instanceof String s) {
            try {
                return new BigDecimal(s.trim().replace(",", ""));
            } catch (NumberFormatException e) {
                return null;
            }
        }
        return null;
    }

    // ========================================================================
    // Persistence
    // ========================================================================

    /**
     * 批量持久化 rules + anomalies，包裹在事务内保证原子性。
     *
     * <p>顺序执行 {@code saveRules.then(saveAnomalies)} 而非并行 zip：
     * 3 条 rule + N 条 anomaly 规模下顺序执行毫秒级完成，事务内并行 save
     * 可能产生连接池压力且无显著收益。任一表写入失败整体回滚（spec §8.4）。</p>
     */
    private Mono<Integer> persistAll(Long reportId, ParsedCheckResult parsed) {
        // 直接 setReportId 复用 parseRule/parseAnomaly 已构建的实体，避免重建。
        List<AccountingCheck> ruleEntities = parsed.rules();
        for (AccountingCheck rule : ruleEntities) {
            rule.setReportId(reportId);
        }

        List<AnomalyRecord> anomalyEntities = parsed.anomalies();
        for (AnomalyRecord anomaly : anomalyEntities) {
            anomaly.setReportId(reportId);
        }

        Mono<Integer> saveRules = checkRepo.saveAll(ruleEntities).count()
                .map(Long::intValue);
        Mono<Integer> saveAnomalies = anomalyRepo.saveAll(anomalyEntities).count()
                .map(Long::intValue);

        // 事务包裹顺序链：saveRules 成功后才 saveAnomalies，任一失败回滚两张表。
        Mono<Integer> tx = saveRules
                .flatMap(rulesCount -> saveAnomalies.map(anomalyCount -> rulesCount + anomalyCount));

        return transactionalOperator.transactional(tx);
    }

    /**
     * 解析 reportId：优先用 task.ref_report_id，缺失时回退到 report.task_id 关联查询。
     */
    private Mono<Long> resolveReportId(String taskId) {
        return taskRepo.findById(taskId)
                .flatMap(task -> {
                    if (task.getRefReportId() != null) {
                        return Mono.just(task.getRefReportId());
                    }
                    return reportRepo.findByTaskId(taskId)
                            .map(report -> report.getId())
                            .switchIfEmpty(Mono.<Long>empty());
                })
                .switchIfEmpty(Mono.<Long>empty());
    }

    /**
     * 内部解析结果容器（不可变）。
     *
     * @param rules     解析出的勾稽规则列表（至少 1 条，否则 parseCheckResult 返回 null）
     * @param anomalies 解析出的异常列表（可为空）
     */
    record ParsedCheckResult(List<AccountingCheck> rules, List<AnomalyRecord> anomalies) {
    }
}
