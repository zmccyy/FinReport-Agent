package com.finreport.domain.dto;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

import com.fasterxml.jackson.annotation.JsonPropertyOrder;

/**
 * 三表数据响应 — spec §6.2.2 / M2.11。
 *
 * <p>{@code GET /api/v1/reports/{reportId}/statements} 的返回体。
 * 把 {@code financial_statement} 表中按 {@code statement_type} 分组的三张报表
 * 一次性返回，前端按 Tab 切换渲染，避免来回请求。</p>
 *
 * <p>三表 key 固定：{@code balance_sheet} / {@code income_statement} / {@code cash_flow}，
 * 与 L3 {@code StatementType.value} 一致；空表返回空数组而非省略 key，
 * 让前端无需做 nullish 判断。</p>
 *
 * <p><b>不可变拷贝</b>：构造时把 List 复制为不可变 List，
 * 避免外部修改破坏封装（SpotBugs EI_EXPOSE_REP）。</p>
 *
 * @param balanceSheet     资产负债表科目列表
 * @param incomeStatement  利润表科目列表
 * @param cashFlow         现金流量表科目列表
 */
@JsonPropertyOrder({"balanceSheet", "incomeStatement", "cashFlow"})
public record StatementsResponse(
        List<StatementItemResponse> balanceSheet,
        List<StatementItemResponse> incomeStatement,
        List<StatementItemResponse> cashFlow
) {
    /**
     * 防御性拷贝：把入参 List 包成不可变 List，避免外部修改破坏 record 的不可变性约定。
     */
    public StatementsResponse {
        balanceSheet = balanceSheet == null ? List.of() : List.copyOf(balanceSheet);
        incomeStatement = incomeStatement == null ? List.of() : List.copyOf(incomeStatement);
        cashFlow = cashFlow == null ? List.of() : List.copyOf(cashFlow);
    }

    /** 空响应（report 存在但尚无抽取数据时使用）。 */
    public static StatementsResponse empty() {
        return new StatementsResponse(Collections.emptyList(), Collections.emptyList(), Collections.emptyList());
    }

    /** 提供给 Service 层使用的可变 List 构造器（用于流式收集后再不可变）。 */
    public static StatementsResponse fromLists(
            List<StatementItemResponse> balanceSheet,
            List<StatementItemResponse> incomeStatement,
            List<StatementItemResponse> cashFlow) {
        return new StatementsResponse(
                new ArrayList<>(balanceSheet),
                new ArrayList<>(incomeStatement),
                new ArrayList<>(cashFlow));
    }
}
