package com.finreport.exception;

import org.springframework.http.HttpStatus;

/**
 * 业务异常基类。
 *
 * <p>所有 L2 业务异常均继承此类。ControllerAdvice 统一拦截并
 * 转换为 RFC 9457 格式的错误响应。</p>
 */
public class BusinessException extends RuntimeException {

    private final HttpStatus status;
    private final String errorCode;

    public BusinessException(HttpStatus status, String errorCode, String message) {
        super(message);
        this.status = status;
        this.errorCode = errorCode;
    }

    public BusinessException(HttpStatus status, String errorCode, String message, Throwable cause) {
        super(message, cause);
        this.status = status;
        this.errorCode = errorCode;
    }

    public HttpStatus getStatus() {
        return status;
    }

    public String getErrorCode() {
        return errorCode;
    }
}
