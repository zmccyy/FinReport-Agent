package com.finreport.service.statement;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import com.finreport.domain.entity.FinancialStatementItem;
import com.finreport.repository.FinancialStatementRepository;
import com.finreport.repository.ReportRepository;
import com.finreport.repository.TaskRepository;

import reactor.core.publisher.Mono;

/**
 * 抽取结果写入器 — spec §3.2 链路 A + plan §4 M2.09。
 *
 * <p>消费 L3 extract progress 携带的 {@code result} payload，把三表科目批量写入
 * {@code financial_statement} 表。{@code TaskOrchestrator.handleStepSuccess}
 * 在 EXTRACT_BS/IS/CF 首次 SUCCESS 时调用本类，先于 {@code handleExtractStepSuccess}
 * 触发 CHECK（spec §3.2.1：3 条 progress 都到后触发 CHECK）。</p>
 *
 * <p><b>失败策略</b>：写库失败只 log error 不抛异常，避免阻断状态机推进；
 * CHECK 阶段会因数据缺失自然失败并暴露问题（spec §8.4 "失败不强制回滚，
 * 保留数据供排查"）。这样保持 M2.08 已稳定的 EXTRACT→CHECK 路径不被回滚。</p>
 *
 * <p><b>幂等性</b>：L3 progress 携带 {@code idempotencyKey=taskId:step}，
 * {@code TaskOrchestrator.handleStepSuccess} 已对重放 SUCCESS 做去重（reconcile
 * 路径不调本类），因此 {@code writeStatement} 不再做额外幂等检查。</p>
 */
@Service
public class StatementWriter {

    private static final Logger log = LoggerFactory.getLogger(StatementWriter.class);

    private static final String STATEMENT_TYPE_BS = "balance_sheet";
    private static final String STATEMENT_TYPE_IS = "income_statement";
    private static final String STATEMENT_TYPE_CF = "cash_flow";

    private final TaskRepository taskRepo;
    private final ReportRepository reportRepo;
    private final FinancialStatementRepository fsRepo;

    public StatementWriter(
            TaskRepository taskRepo,
            ReportRepository reportRepo,
            FinancialStatementRepository fsRepo) {
        this.taskRepo = taskRepo;
        this.reportRepo = reportRepo;
        this.fsRepo = fsRepo;
    }

    /**
     * 把一条 extract progress 的 result payload 写入 financial_statement 表。
     *
     * <p>payload 形状（M2.09 契约）：
     * <pre>{@code
     * {
     *   "success": true,
     *   "statement": {
     *     "report_period": "2024-12-31",
     *     "currency": "CNY",
     *     "unit": "元",
     *     "statements": {
     *       "balance_sheet": [
     *         {"item": "货币资金", "value": 1.23e9, "scope": "合并", "period": "本期"},
     *         ...
     *       ]
     *     }
     *   },
     *   "validation": {"is_valid": true, "issues": [...], "error_hint": ""},
     *   "confidence": 0.92,
     *   "source_page": 5,
     *   "retried": false,
     *   "tokens_used": 1234,
     *   "latency_ms": 5600
     * }
     * }</pre></p>
     *
     * @param taskId   任务 ID
     * @param stepName 步骤名称（EXTRACT_BS / EXTRACT_IS / EXTRACT_CF）
     * @param result   L3 progress 携带的 result payload
     * @return 写入的科目行数；解析失败或无数据返回 0
     */
    public Mono<Integer> writeStatement(String taskId, String stepName, Map<String, Object> result) {
        log.debug("[StatementWriter] writeStatement taskId={} step={} resultKeys={}",
                taskId, stepName, result == null ? "[]" : result.keySet());

        if (result == null || result.isEmpty()) {
            log.warn("[StatementWriter] 空 result，跳过写库 taskId={} step={}", taskId, stepName);
            return Mono.just(0);
        }

        Object successFlag = result.get("success");
        if (successFlag instanceof Boolean b && !b) {
            log.warn("[StatementWriter] result.success=false，跳过写库 taskId={} step={}",
                    taskId, stepName);
            return Mono.just(0);
        }

        String statementType = mapStatementType(stepName);
        if (statementType == null) {
            log.warn("[StatementWriter] 非 EXTRACT 步骤，跳过写库 taskId={} step={}", taskId, stepName);
            return Mono.just(0);
        }

        ParsedStatement parsed = parseStatement(result, statementType);
        if (parsed == null) {
            log.warn("[StatementWriter] statement 解析失败，跳过写库 taskId={} step={}",
                    taskId, stepName);
            return Mono.just(0);
        }

        if (parsed.items.isEmpty()) {
            log.warn("[StatementWriter] statements[{}] 为空，跳过写库 taskId={} step={}",
                    statementType, taskId, stepName);
            return Mono.just(0);
        }

        return resolveReportId(taskId)
                .map(reportId -> {
                    if (reportId == null) {
                        log.error("[StatementWriter] 无法解析 reportId，跳过写库 taskId={} step={}",
                                taskId, stepName);
                    }
                    return reportId;
                })
                .flatMap(reportId -> {
                    if (reportId == null) {
                        return Mono.just(0);
                    }
                    return persistItems(reportId, statementType, parsed)
                            .map(List::size)
                            .doOnSuccess(count -> log.info(
                                    "[StatementWriter] 写入成功 taskId={} step={} reportId={} "
                                            + "statementType={} rows={}",
                                    taskId, stepName, reportId, statementType, count))
                            .onErrorResume(error -> {
                                log.error("[StatementWriter] 写入失败 taskId={} step={} reportId={}",
                                        taskId, stepName, reportId, error);
                                return Mono.just(0);
                            });
                })
                .switchIfEmpty(Mono.defer(() -> {
                    log.error("[StatementWriter] task 不存在或无 reportId 关联，跳过写库 taskId={} step={}",
                            taskId, stepName);
                    return Mono.just(0);
                }));
    }

    /**
     * 把 stepName 映射为 L3 {@code StatementType.value}。
     *
     * <p>使用 String 常量而非 enum，避免在 L2 端引入对 L3 枚举的依赖；
     * 字符串值在 spec §5.2 + L3 {@code StatementType} 双方锁定。</p>
     */
    private static String mapStatementType(String stepName) {
        return switch (stepName) {
            case "EXTRACT_BS" -> STATEMENT_TYPE_BS;
            case "EXTRACT_IS" -> STATEMENT_TYPE_IS;
            case "EXTRACT_CF" -> STATEMENT_TYPE_CF;
            default -> null;
        };
    }

    /**
     * 从 result payload 解析 statement + items + 元数据。
     *
     * <p>容错策略：任何字段缺失都返回 null，由 caller 决定是否跳过写库。</p>
     */
    private static ParsedStatement parseStatement(Map<String, Object> result, String statementType) {
        Object statementObj = result.get("statement");
        if (!(statementObj instanceof Map<?, ?> statementMap)) {
            return null;
        }

        Object statementsObj = statementMap.get("statements");
        if (!(statementsObj instanceof Map<?, ?> statementsMap)) {
            return null;
        }

        Object itemsObj = statementsMap.get(statementType);
        if (!(itemsObj instanceof List<?> itemsList)) {
            return null;
        }

        List<ItemRow> items = new ArrayList<>();
        for (Object itemObj : itemsList) {
            if (!(itemObj instanceof Map<?, ?> itemMap)) {
                continue;
            }
            ItemRow row = parseItem(itemMap);
            if (row != null) {
                items.add(row);
            }
        }

        String currency = stringOrDefault(statementMap.get("currency"), "CNY");
        String unit = stringOrDefault(statementMap.get("unit"), "元");

        BigDecimal confidence = bigDecimalOrNull(result.get("confidence"));
        Integer sourcePage = integerOrNull(result.get("source_page"));

        return new ParsedStatement(items, currency, unit, confidence, sourcePage);
    }

    private static ItemRow parseItem(Map<?, ?> itemMap) {
        Object nameObj = itemMap.get("item");
        if (!(nameObj instanceof String itemName) || itemName.isBlank()) {
            return null;
        }

        Object valueObj = itemMap.get("value");
        BigDecimal value = bigDecimalOrNull(valueObj);
        if (value == null) {
            // value 缺失视为无效行；spec §2.3 M7 要求 value 非 NaN（但允许 0 / 负数）。
            return null;
        }

        String scope = stringOrDefault(itemMap.get("scope"), "合并");
        String period = stringOrDefault(itemMap.get("period"), "本期");
        return new ItemRow(itemName, value, scope, period);
    }

    private static String stringOrDefault(Object value, String fallback) {
        return value instanceof String s && !s.isBlank() ? s : fallback;
    }

    private static BigDecimal bigDecimalOrNull(Object value) {
        if (value == null) {
            return null;
        }
        if (value instanceof BigDecimal bd) {
            return bd;
        }
        if (value instanceof Number n) {
            // M2 review fix: 避免 BigDecimal.valueOf(double) 的精度损失。
            // L3 Pydantic value 是 float,JSON 序列化为 number,Jackson 反序列化为 Double。
            // 万亿级数值(1e12+)带小数时直接 double → BigDecimal 会丢精度。
            // 改用 n.toString() 中转:Double.toString 输出最精确的 double 可表示形式,
            // BigDecimal 能正确解析科学计数法,保留 double 能承载的全部有效数字。
            // 根本解法是 L3 schema 把 value 改 str 序列化(留作 follow-up),
            // 当前 L2 侧先用 String 中转兜底。
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

    private static Integer integerOrNull(Object value) {
        if (value == null) {
            return null;
        }
        if (value instanceof Integer i) {
            return i;
        }
        if (value instanceof Number n) {
            return n.intValue();
        }
        if (value instanceof String s) {
            try {
                return Integer.parseInt(s.trim());
            } catch (NumberFormatException e) {
                return null;
            }
        }
        return null;
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
     * 批量持久化科目行。
     *
     * <p>用 {@code Flux.flatMap(repo::save)} 而非单条循环；R2DBC 自动批处理
     * 在 150 条规模下毫秒级完成（spec §12.1 EXTRACT 阶段 SLA 60s 余量充足）。</p>
     */
    private Mono<List<FinancialStatementItem>> persistItems(
            Long reportId, String statementType, ParsedStatement parsed) {
        List<FinancialStatementItem> entities = new ArrayList<>(parsed.items.size());
        for (ItemRow row : parsed.items) {
            entities.add(FinancialStatementItem.builder()
                    .reportId(reportId)
                    .statementType(statementType)
                    .itemName(row.itemName)
                    .itemValue(row.value)
                    .currency(parsed.currency)
                    .unit(parsed.unit)
                    .scope(row.scope)
                    .periodType(row.period)
                    .confidence(parsed.confidence)
                    .sourcePage(parsed.sourcePage)
                    .build());
        }
        return fsRepo.saveAll(entities).collectList();
    }

    /** 内部解析结果容器。 */
    private static final class ParsedStatement {
        final List<ItemRow> items;
        final String currency;
        final String unit;
        final BigDecimal confidence;
        final Integer sourcePage;

        ParsedStatement(List<ItemRow> items, String currency, String unit,
                BigDecimal confidence, Integer sourcePage) {
            this.items = items;
            this.currency = currency;
            this.unit = unit;
            this.confidence = confidence;
            this.sourcePage = sourcePage;
        }
    }

    /** 内部科目行容器。 */
    private static final class ItemRow {
        final String itemName;
        final BigDecimal value;
        final String scope;
        final String period;

        ItemRow(String itemName, BigDecimal value, String scope, String period) {
            this.itemName = itemName;
            this.value = value;
            this.scope = scope;
            this.period = period;
        }
    }
}
