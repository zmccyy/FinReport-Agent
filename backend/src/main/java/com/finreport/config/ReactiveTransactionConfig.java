package com.finreport.config;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.transaction.ReactiveTransactionManager;
import org.springframework.transaction.reactive.TransactionalOperator;

/**
 * R2DBC 响应式事务配置。
 */
@Configuration
public class ReactiveTransactionConfig {

    /**
     * 提供任务编排等服务使用的响应式事务操作器。
     *
     * @param transactionManager R2DBC 事务管理器
     * @return 响应式事务操作器
     */
    @Bean
    public TransactionalOperator transactionalOperator(ReactiveTransactionManager transactionManager) {
        return TransactionalOperator.create(transactionManager);
    }
}
