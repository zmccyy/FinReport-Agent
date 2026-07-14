package com.finreport.security;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.security.core.userdetails.ReactiveUserDetailsService;
import org.springframework.security.core.userdetails.User;
import org.springframework.security.core.userdetails.UserDetails;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Service;

import reactor.core.publisher.Mono;

/**
 * 用户详情服务 — 从 MySQL 加载用户信息。
 *
 * <p>M1.07 阶段：返回 mock 用户，用于验证 Security + JWT 链路。
 * M1.08 将对接 MySQL user_account 表。</p>
 */
@Service
public class UserDetailsServiceImpl implements ReactiveUserDetailsService {

    private static final Logger log = LoggerFactory.getLogger(UserDetailsServiceImpl.class);

    private final PasswordEncoder passwordEncoder = new BCryptPasswordEncoder(10);

    /**
     * 按用户名查找用户。
     *
     * <p>M1.07 mock：返回固定测试用户（username=admin, password=admin123）。</p>
     */
    @Override
    public Mono<UserDetails> findByUsername(String username) {
        log.debug("[UserDetailsService] 查找用户 username={}", username);

        // M1.07 mock 用户（M1.08 改为查 MySQL user_account 表）
        if ("admin".equals(username)) {
            UserDetails user = User.builder()
                    .username("admin")
                    .password(passwordEncoder.encode("admin123"))
                    .roles("USER", "ADMIN")
                    .accountExpired(false)
                    .accountLocked(false)
                    .credentialsExpired(false)
                    .disabled(false)
                    .build();
            return Mono.just(user);
        }

        log.debug("[UserDetailsService] 用户不存在 username={}", username);
        return Mono.empty();
    }

    /**
     * 暴露 PasswordEncoder 供 AuthService 使用。
     */
    public PasswordEncoder getPasswordEncoder() {
        return passwordEncoder;
    }
}
