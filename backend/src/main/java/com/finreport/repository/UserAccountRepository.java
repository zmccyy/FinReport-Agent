package com.finreport.repository;

import org.springframework.data.repository.reactive.ReactiveCrudRepository;
import org.springframework.stereotype.Repository;

import com.finreport.domain.entity.UserAccount;

import reactor.core.publisher.Mono;

/**
 * 用户账户 R2DBC Repository。
 */
@Repository
public interface UserAccountRepository extends ReactiveCrudRepository<UserAccount, Long> {

    /**
     * 按用户名查找用户。
     *
     * @param username 用户名
     * @return 用户实体（可能为 empty）
     */
    Mono<UserAccount> findByUsername(String username);

    /**
     * 检查用户名是否已存在。
     *
     * @param username 用户名
     * @return true 如果已存在
     */
    Mono<Boolean> existsByUsername(String username);
}
