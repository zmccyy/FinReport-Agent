package com.finreport.service;

import java.time.Duration;

import io.jsonwebtoken.JwtException;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.data.redis.core.ReactiveRedisTemplate;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Service;

import com.finreport.config.JwtConfig;
import com.finreport.domain.dto.LoginRequest;
import com.finreport.domain.dto.RefreshRequest;
import com.finreport.domain.dto.RegisterRequest;
import com.finreport.domain.dto.TokenResponse;
import com.finreport.domain.dto.UserInfoResponse;
import com.finreport.domain.entity.UserAccount;
import com.finreport.exception.AuthException;
import com.finreport.repository.UserAccountRepository;
import com.finreport.security.JwtUtil;

import reactor.core.publisher.Mono;

/**
 * 认证服务 — 注册、登录、刷新、登出。
 *
 * <p>密码使用 BCrypt（cost=10）哈希。登出时通过 Redis 黑名单
 * 令 Refresh Token 失效。黑名单 TTL 与 Token 剩余有效期一致。</p>
 */
@Service
public class AuthService {

    private static final Logger log = LoggerFactory.getLogger(AuthService.class);
    private static final String BLACKLIST_PREFIX = "fin:bl:";
    private static final int ACCOUNT_STATUS_ACTIVE = 1;
    private static final String DEFAULT_ROLE = "USER";

    private final UserAccountRepository userRepo;
    private final PasswordEncoder passwordEncoder;
    private final JwtUtil jwtUtil;
    private final JwtConfig jwtConfig;
    private final ReactiveRedisTemplate<String, String> redisTemplate;

    public AuthService(
            UserAccountRepository userRepo,
            PasswordEncoder passwordEncoder,
            JwtUtil jwtUtil,
            JwtConfig jwtConfig,
            ReactiveRedisTemplate<String, String> redisTemplate) {
        this.userRepo = userRepo;
        this.passwordEncoder = passwordEncoder;
        this.jwtUtil = jwtUtil;
        this.jwtConfig = jwtConfig;
        this.redisTemplate = redisTemplate;
    }

    /**
     * 用户注册。
     *
     * @param req 注册请求
     * @return Token 响应
     * @throws AuthException 用户名已存在
     */
    public Mono<TokenResponse> register(RegisterRequest req) {
        log.debug("[AuthService] register 入参 username={}", req.username());
        return userRepo.existsByUsername(req.username())
                .flatMap(exists -> {
                    if (Boolean.TRUE.equals(exists)) {
                        return Mono.<UserAccount>error(
                                new AuthException("USERNAME_EXISTS", "用户名已存在: " + req.username()));
                    }
                    UserAccount user = UserAccount.builder()
                            .username(req.username())
                            .passwordHash(passwordEncoder.encode(req.password()))
                            .email(req.email())
                            .role(DEFAULT_ROLE)
                            .status(ACCOUNT_STATUS_ACTIVE)
                            .build();
                    return userRepo.save(user);
                })
                .flatMap(saved -> {
                    log.info("[AuthService] 用户注册成功 userId={} username={}", saved.getId(), saved.getUsername());
                    return buildTokenResponse(saved);
                });
    }

    /**
     * 用户登录。
     *
     * @param req 登录请求
     * @return Token 响应
     * @throws AuthException 用户名或密码错误、账户已禁用
     */
    public Mono<TokenResponse> login(LoginRequest req) {
        log.debug("[AuthService] login 入参 username={}", req.username());
        return userRepo.findByUsername(req.username())
                .switchIfEmpty(Mono.error(
                        new AuthException("BAD_CREDENTIALS", "用户名或密码错误")))
                .flatMap(user -> {
                    if (user.getStatus() == null || user.getStatus() != 1) {
                        return Mono.<TokenResponse>error(
                                new AuthException("ACCOUNT_DISABLED", "账户已被禁用"));
                    }
                    if (!passwordEncoder.matches(req.password(), user.getPasswordHash())) {
                        return Mono.<TokenResponse>error(
                                new AuthException("BAD_CREDENTIALS", "用户名或密码错误"));
                    }
                    log.info("[AuthService] 用户登录成功 userId={} username={}", user.getId(), user.getUsername());
                    return buildTokenResponse(user);
                });
    }

    /**
     * 刷新 Access Token。
     *
     * <p>校验 Refresh Token 有效性（未被黑名单、未过期），
     * 签发新的 Access Token。</p>
     *
     * @param req 刷新请求
     * @return 新的 Token 响应
     * @throws AuthException Token 无效或已在黑名单
     */
    public Mono<TokenResponse> refresh(RefreshRequest req) {
        log.debug("[AuthService] refresh 入参");
        String refreshToken = req.refreshToken();

        if (!jwtUtil.validate(refreshToken)) {
            return Mono.error(new AuthException("TOKEN_INVALID", "Refresh Token 无效或已过期"));
        }

        String jti = jwtUtil.getJti(refreshToken);
        return redisTemplate.hasKey(BLACKLIST_PREFIX + jti)
                .flatMap(isBlacklisted -> {
                    if (Boolean.TRUE.equals(isBlacklisted)) {
                        return Mono.<TokenResponse>error(
                                new AuthException("TOKEN_REVOKED", "Refresh Token 已被撤销"));
                    }
                    Long userId = jwtUtil.getUserId(refreshToken);
                    String username = jwtUtil.getUsername(refreshToken);

                    String newAccess = jwtUtil.generateAccessToken(userId, username);
                    String newRefresh = jwtUtil.generateRefreshToken(userId, username);

                    // 将旧 Refresh Token 加入黑名单，实现 token rotation
                    long ttl = calculateRemainingTtl(refreshToken);
                    return redisTemplate.opsForValue()
                            .set(BLACKLIST_PREFIX + jti, "1", Duration.ofSeconds(ttl))
                            .thenReturn(new TokenResponse(newAccess, newRefresh, jwtConfig.getAccessExpiration()));
                });
    }

    /**
     * 登出 — 将 Refresh Token 加入黑名单。
     *
     * <p>黑名单 TTL 与 Token 剩余有效期相匹配，到期自动清理。</p>
     *
     * @param refreshToken 待失效的 Refresh Token
     * @return 空 Mono
     */
    public Mono<Void> logout(String refreshToken) {
        log.debug("[AuthService] logout 入参");
        if (refreshToken == null || refreshToken.isEmpty()) {
            return Mono.empty();
        }
        if (!jwtUtil.validate(refreshToken)) {
            log.debug("[AuthService] logout Token 已无效，无需加入黑名单");
            return Mono.empty();
        }

        String jti = jwtUtil.getJti(refreshToken);
        long ttlSeconds = calculateRemainingTtl(refreshToken);

        return redisTemplate.opsForValue()
                .set(BLACKLIST_PREFIX + jti, "1", Duration.ofSeconds(ttlSeconds))
                .doOnSuccess(_v -> log.info("[AuthService] 用户登出成功 jti={}", jti))
                .then();
    }

    /**
     * 查询当前用户信息。
     *
     * @param userId 用户 ID（从 JWT 中提取）
     * @return 用户信息
     */
    public Mono<UserInfoResponse> getUserInfo(Long userId) {
        log.debug("[AuthService] getUserInfo userId={}", userId);
        return userRepo.findById(userId)
                .map(user -> new UserInfoResponse(
                        user.getId(),
                        user.getUsername(),
                        user.getEmail(),
                        user.getRole(),
                        user.getCreatedAt() != null ? user.getCreatedAt().toInstant(java.time.ZoneOffset.UTC) : null
                ))
                .switchIfEmpty(Mono.error(
                        new AuthException("USER_NOT_FOUND", "用户不存在")));
    }

    /**
     * 检查 Token 是否在 Redis 黑名单中。
     *
     * @param jti JWT ID
     * @return true 如果已被撤销
     */
    public Mono<Boolean> isBlacklisted(String jti) {
        log.debug("[AuthService] isBlacklisted jti={}", jti);
        return redisTemplate.hasKey(BLACKLIST_PREFIX + jti);
    }

    // --- private ---

    /**
     * 计算 Token 剩余有效秒数，用于黑名单 TTL。
     */
    private long calculateRemainingTtl(String token) {
        try {
            var claims = jwtUtil.parseToken(token);
            long remainingMs = claims.getExpiration().getTime() - System.currentTimeMillis();
            if (remainingMs > 0) {
                return remainingMs / 1000;
            }
        } catch (JwtException | IllegalArgumentException e) {
            log.debug("[AuthService] Token 解析失败，使用默认 TTL: {}", e.getMessage());
        }
        return jwtConfig.getRefreshExpiration();
    }

    private Mono<TokenResponse> buildTokenResponse(UserAccount user) {
        String accessToken = jwtUtil.generateAccessToken(user.getId(), user.getUsername());
        String refreshToken = jwtUtil.generateRefreshToken(user.getId(), user.getUsername());
        return Mono.just(new TokenResponse(accessToken, refreshToken, jwtConfig.getAccessExpiration()));
    }
}
