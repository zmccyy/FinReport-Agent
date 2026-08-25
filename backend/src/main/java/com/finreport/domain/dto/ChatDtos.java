package com.finreport.domain.dto;

import java.util.List;

import com.fasterxml.jackson.annotation.JsonPropertyOrder;

/**
 * 问答接口 DTO — spec §6.2.3 / M5.05。
 */
public final class ChatDtos {

    private ChatDtos() {
    }

    /**
     * 创建会话请求体。
     *
     * @param reportId 会话绑定的报表 ID
     * @param title    会话标题（可空）
     */
    public record CreateSessionRequest(Long reportId, String title) {
    }

    /**
     * 发送消息请求体（SSE 流式响应）。
     *
     * @param content 用户消息内容
     */
    @JsonPropertyOrder({"content"})
    public record SendMessageRequest(String content) {
    }

    /**
     * 会话摘要响应。
     *
     * @param id       会话 ID
     * @param reportId 绑定的报表 ID
     * @param title    会话标题
     */
    @JsonPropertyOrder({"id", "reportId", "title"})
    public record ChatSessionResponse(Long id, Long reportId, String title) {
    }

    /**
     * 消息响应。
     *
     * @param id        消息 ID
     * @param role      user / assistant
     * @param content   消息内容
     * @param toolsUsed 工具使用清单（assistant 消息）
     * @param createdAt 创建时间
     */
    @JsonPropertyOrder({"id", "role", "content", "toolsUsed", "createdAt"})
    public record ChatMessageResponse(
            Long id, String role, String content, List<String> toolsUsed, String createdAt) {
        /** 防御性拷贝：toolsUsed 对外不可变（spotbugs EI_EXPOSE_REP）。 */
        public ChatMessageResponse {
            toolsUsed = toolsUsed == null ? List.of() : List.copyOf(toolsUsed);
        }
    }

    /**
     * 多轮对话历史中的一轮（发给 L3 的 context 项）。
     *
     * @param role    user / assistant
     * @param content 消息内容
     */
    public record ChatTurn(String role, String content) {
    }

    /**
     * L2 → L3 /internal/chat/stream 请求体。
     *
     * @param sessionId      会话 ID（字符串化）
     * @param messageId      本轮消息 ID（L2 生成的 UUID）
     * @param reportId       绑定的报表 ID
     * @param question       用户问题
     * @param companyContext 公司上下文（如「贵州茅台（600519），报告期 2025-12-31」）
     * @param history        此前对话轮次（最近 10 轮）
     * @param summary        窗口外轮次的压缩摘要（可能为空）
     */
    @JsonPropertyOrder({"sessionId", "messageId", "reportId", "question", "companyContext", "history", "summary"})
    public record ChatStreamRequest(
            String sessionId,
            String messageId,
            Long reportId,
            String question,
            String companyContext,
            List<ChatTurn> history,
            String summary) {
        /** 防御性拷贝：history 对外不可变（spotbugs EI_EXPOSE_REP）。 */
        public ChatStreamRequest {
            history = history == null ? List.of() : List.copyOf(history);
        }
    }
}
