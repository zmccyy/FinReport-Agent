package com.finreport.security;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.security.core.userdetails.ReactiveUserDetailsService;
import org.springframework.security.core.userdetails.User;
import org.springframework.security.core.userdetails.UserDetails;
import org.springframework.stereotype.Service;

import com.finreport.repository.UserAccountRepository;

import reactor.core.publisher.Mono;

/**
 * 用户详情服务 — 从 MySQL user_account 表加载用户信息。
 *
 * <p>M1.08：接入真实数据库，替换 M1.07 mock 实现。</p>
 */
@Service
public class UserDetailsServiceImpl implements ReactiveUserDetailsService {

    private static final Logger log = LoggerFactory.getLogger(UserDetailsServiceImpl.class);

    private final UserAccountRepository userRepo;

    public UserDetailsServiceImpl(UserAccountRepository userRepo) {
        this.userRepo = userRepo;
    }

    /**
     * 按用户名查找用户。
     *
     * <p>从 user_account 表加载，返回 Spring Security UserDetails。</p>
     */
    @Override
    public Mono<UserDetails> findByUsername(String username) {
        log.debug("[UserDetailsService] 查找用户 username={}", username);
        return userRepo.findByUsername(username)
                .map(userAccount -> {
                    log.debug("[UserDetailsService] 用户已找到 userId={} role={}",
                            userAccount.getId(), userAccount.getRole());
                    boolean isActive = userAccount.getStatus() != null && userAccount.getStatus() == 1;
                    return User.builder()
                            .username(userAccount.getUsername())
                            .password(userAccount.getPasswordHash())
                            .roles(userAccount.getRole()) // Spring Security 自动加 ROLE_ 前缀
                            .accountExpired(false)
                            .accountLocked(false)
                            .credentialsExpired(false)
                            .disabled(!isActive)
                            .build();
                })
                .switchIfEmpty(Mono.defer(() -> {
                    log.debug("[UserDetailsService] 用户不存在 username={}", username);
                    return Mono.empty();
                }));
    }
}
