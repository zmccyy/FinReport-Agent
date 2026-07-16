package com.finreport.controller;

import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import com.finreport.domain.dto.LoginRequest;
import com.finreport.domain.dto.RefreshRequest;
import com.finreport.domain.dto.RegisterRequest;
import com.finreport.domain.dto.TokenResponse;
import com.finreport.exception.AuthException;
import com.finreport.service.AuthService;

import jakarta.validation.Valid;
import reactor.core.publisher.Mono;

/**
 * 认证控制器 — spec §6.2.1。
 *
 * <p>提供注册、登录、Token 刷新、登出和当前用户查询端点。
 * 所有端点在 SecurityConfig 中配置为公开访问。</p>
 */
@RestController
@RequestMapping("/api/v1")
public class AuthController {

    private static final Logger log = LoggerFactory.getLogger(AuthController.class);

    private final AuthService authService;

    public AuthController(AuthService authService) {
        this.authService = authService;
    }

    /**
     * 用户注册。
     *
     * <p>POST /api/v1/auth/register</p>
     *
     * @param req 注册请求
     * @return Token 响应（HTTP 201）
     */
    @PostMapping("/auth/register")
    public Mono<ResponseEntity<TokenResponse>> register(@Valid @RequestBody RegisterRequest req) {
        log.debug("[AuthController] POST /auth/register username={}", req.username());
        return authService.register(req)
                .map(token -> ResponseEntity.status(HttpStatus.CREATED).body(token));
    }

    /**
     * 用户登录。
     *
     * <p>POST /api/v1/auth/login</p>
     *
     * @param req 登录请求
     * @return Token 响应
     */
    @PostMapping("/auth/login")
    public Mono<ResponseEntity<TokenResponse>> login(@Valid @RequestBody LoginRequest req) {
        log.debug("[AuthController] POST /auth/login username={}", req.username());
        return authService.login(req)
                .map(ResponseEntity::ok);
    }

    /**
     * 刷新 Access Token。
     *
     * <p>POST /api/v1/auth/refresh</p>
     *
     * @param req 刷新请求（含 Refresh Token）
     * @return 新的 Token 响应
     */
    @PostMapping("/auth/refresh")
    public Mono<ResponseEntity<TokenResponse>> refresh(@Valid @RequestBody RefreshRequest req) {
        log.debug("[AuthController] POST /auth/refresh");
        return authService.refresh(req)
                .map(ResponseEntity::ok);
    }

    /**
     * 登出。
     *
     * <p>POST /api/v1/auth/logout
     * 将 Refresh Token 加入黑名单。请求体为 {"refreshToken": "..."}。</p>
     *
     * @param body 包含 refreshToken 字段
     * @return 空响应（HTTP 204）
     */
    @PostMapping("/auth/logout")
    public Mono<ResponseEntity<Void>> logout(@RequestBody Map<String, String> body) {
        String refreshToken = body.get("refreshToken");
        log.debug("[AuthController] POST /auth/logout");
        return authService.logout(refreshToken)
                .thenReturn(ResponseEntity.noContent().<Void>build());
    }

    /**
     * 查询当前用户信息。
     *
     * <p>GET /api/v1/users/me
     * 从 X-User-Id Header（由 JwtFilter 注入）获取 userId。</p>
     *
     * @param userIdHeader X-User-Id Header 值
     * @return 用户信息
     */
    @GetMapping("/users/me")
    public Mono<ResponseEntity<?>> getCurrentUser(
            @RequestHeader(value = "X-User-Id", required = false) String userIdHeader) {
        log.debug("[AuthController] GET /users/me");
        if (userIdHeader == null) {
            return Mono.error(new AuthException("UNAUTHORIZED", "未认证"));
        }
        try {
            Long userId = Long.parseLong(userIdHeader);
            return authService.getUserInfo(userId)
                    .map(ResponseEntity::ok);
        } catch (NumberFormatException e) {
            return Mono.error(new AuthException("BAD_TOKEN", "Token 中 userId 格式异常"));
        }
    }
}
