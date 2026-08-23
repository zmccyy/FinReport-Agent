package com.finreport.service;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import com.finreport.domain.dto.ChatDtos.ChatTurn;
import com.finreport.repository.ChatMessageRepository;

import reactor.core.publisher.Mono;

/**
 * 会话上下文服务 — spec §5.2「会话上下文 | Hash | 存最近 10 轮」（M5.06 载体）。
 *
 * <p>M5.05 先以 chat_message 表为上下文唯一来源：发送消息时读取最近
 * {@value #CONTEXT_ROUNDS} 轮（每轮一问一答 = {@value #CONTEXT_MESSAGES}
 * 条消息）注入 L3 prompt。M5.06 在此基础上增加 Redis Hash 缓存与
 * 超长摘要压缩。</p>
 */
@Service
public class SessionContextService {

    private static final Logger log = LoggerFactory.getLogger(SessionContextService.class);

    /** 注入 L3 的上下文轮数上限（spec：最近 10 轮）。 */
    public static final int CONTEXT_ROUNDS = 10;

    /** 最近 10 轮 = 20 条消息（user + assistant）。 */
    public static final int CONTEXT_MESSAGES = CONTEXT_ROUNDS * 2;

    private final ChatMessageRepository chatMessageRepository;

    public SessionContextService(ChatMessageRepository chatMessageRepository) {
        this.chatMessageRepository = chatMessageRepository;
    }

    /**
     * 读取会话最近 {@value #CONTEXT_MESSAGES} 条消息作为多轮上下文。
     *
     * <p>消息按时间倒序存储，返回前逆序为时间正序（user → assistant 交替）。
     * 上下文随消息数自然截断：少于 10 轮时返回全部。</p>
     *
     * @param sessionId 会话 ID
     * @return 时间正序的最近对话轮次（可能为空）
     */
    public Mono<List<ChatTurn>> loadHistory(Long sessionId) {
        return chatMessageRepository.findBySessionIdOrderByCreatedAtDesc(sessionId)
                .take(CONTEXT_MESSAGES)
                .map(message -> new ChatTurn(message.getRole(), message.getContent()))
                .collectList()
                .map(SessionContextService::reverseOrder);
    }

    private static List<ChatTurn> reverseOrder(List<ChatTurn> turns) {
        List<ChatTurn> ordered = new ArrayList<>(turns);
        Collections.reverse(ordered);
        return ordered;
    }
}
