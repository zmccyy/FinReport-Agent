package com.finreport.exception;

import org.springframework.http.HttpStatus;

/**
 * 认证/授权异常。
 *
 * <p>包括：凭据无效、Token 过期、权限不足、用户不存在等场景。
 * 统一映射为 HTTP 401/403 响应。</p>
 */
public class AuthException extends BusinessException {

    public AuthException(String errorCode, String message) {
        super(HttpStatus.UNAUTHORIZED, errorCode, message);
    }

    public AuthException(HttpStatus status, String errorCode, String message) {
        super(status, errorCode, message);
    }

    public AuthException(String errorCode, String message, Throwable cause) {
        super(HttpStatus.UNAUTHORIZED, errorCode, message, cause);
    }
}
