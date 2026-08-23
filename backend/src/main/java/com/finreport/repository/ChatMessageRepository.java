package com.finreport.repository;

import org.springframework.data.repository.reactive.ReactiveCrudRepository;
import org.springframework.stereotype.Repository;

import com.finreport.domain.entity.ChatMessage;

import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * 问答消息 Repository — chat_message 表。
 */
@Repository
public interface ChatMessageRepository extends ReactiveCrudRepository<ChatMessage, Long> {

    /**
     * 查询会话消息（按创建时间倒序，最新在前）。
     *
     * @param sessionId 会话 ID
     * @return 消息列表（倒序）
     */
    Flux<ChatMessage> findBySessionIdOrderByCreatedAtDesc(Long sessionId);

    /**
     * 删除会话的全部消息（会话删除级联）。
     *
     * @param sessionId 会话 ID
     * @return 删除行数
     */
    Mono<Void> deleteBySessionId(Long sessionId);
}
