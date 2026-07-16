package com.finreport.repository;

import org.springframework.data.repository.reactive.ReactiveCrudRepository;
import org.springframework.stereotype.Repository;

import com.finreport.domain.entity.TaskStep;

import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * 任务步骤 R2DBC Repository。
 */
@Repository
public interface TaskStepRepository extends ReactiveCrudRepository<TaskStep, Long> {

    /**
     * 按任务 ID 查询所有步骤（按 ID 升序，即创建顺序）。
     */
    Flux<TaskStep> findByTaskIdOrderByIdAsc(String taskId);

    /**
     * 按任务 ID 和步骤名称查询。
     */
    Mono<TaskStep> findByTaskIdAndStepName(String taskId, String stepName);

    /**
     * 按任务 ID 和状态查询步骤。
     */
    Flux<TaskStep> findByTaskIdAndStatus(String taskId, String status);

    /**
     * 查询任务的非终态步骤。
     */
    Flux<TaskStep> findByTaskIdAndStatusNotIn(String taskId, java.util.Collection<String> statuses);
}
