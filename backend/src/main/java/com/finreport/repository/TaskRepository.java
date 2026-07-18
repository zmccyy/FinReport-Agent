package com.finreport.repository;

import org.springframework.data.repository.reactive.ReactiveCrudRepository;
import org.springframework.stereotype.Repository;

import com.finreport.domain.entity.Task;

import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * 任务 R2DBC Repository。
 */
@Repository
public interface TaskRepository extends ReactiveCrudRepository<Task, String> {

    /**
     * 按用户 ID 查询任务列表（按创建时间倒序）。
     */
    Flux<Task> findByUserIdOrderByCreatedAtDesc(Long userId);

    /**
     * 按状态查询任务。
     */
    Flux<Task> findByStatus(String status);

    /**
     * 查询用户特定状态的任务。
     */
    Flux<Task> findByUserIdAndStatus(Long userId, String status);

    /**
     * 在所属用户范围内查询任务，避免按 taskId 越权访问。
     */
    Mono<Task> findByIdAndUserId(String id, Long userId);
}
