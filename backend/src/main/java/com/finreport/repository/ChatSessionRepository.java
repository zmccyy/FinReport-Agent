package com.finreport.repository;

import org.springframework.data.repository.reactive.ReactiveCrudRepository;
import org.springframework.stereotype.Repository;

import com.finreport.domain.entity.ChatSession;

import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * 问答会话 Repository — chat_session 表。
 */
@Repository
public interface ChatSessionRepository extends ReactiveCrudRepository<ChatSession, Long> {

    /**
     * 查询某用户的全部会话（按更新时间倒序，最新在前）。
     *
     * @param userId 用户 ID
     * @return 会话列表
     */
    Flux<ChatSession> findAllByUserIdOrderByUpdatedAtDesc(Long userId);

    /**
     * 在用户归属范围内按 ID 查找会话（用户隔离校验用）。
     *
     * @param id     会话 ID
     * @param userId 用户 ID
     * @return 会话（可能为空）
     */
    Mono<ChatSession> findByIdAndUserId(Long id, Long userId);
}
