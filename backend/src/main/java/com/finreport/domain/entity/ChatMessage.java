package com.finreport.domain.entity;

import java.time.LocalDateTime;

import org.springframework.data.annotation.Id;
import org.springframework.data.relational.core.mapping.Column;
import org.springframework.data.relational.core.mapping.Table;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

/**
 * 问答消息实体，映射 chat_message 表 — V3__init_task.sql（spec §5.2）。
 *
 * <p>tools_used 以 JSON 字符串存储（如 {@code ["query_statement"]}），
 * 由 ChatService 序列化/反序列化。</p>
 */
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
@Table("chat_message")
public class ChatMessage {

    @Id
    private Long id;

    @Column("session_id")
    private Long sessionId;

    private String role;

    private String content;

    @Column("tools_used")
    private String toolsUsed;

    @Column("token_count")
    private Integer tokenCount;

    @Column("created_at")
    private LocalDateTime createdAt;
}
