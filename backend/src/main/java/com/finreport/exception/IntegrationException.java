package com.finreport.exception;

import org.springframework.http.HttpStatus;

/**
 * L2 集成异常基类。
 *
 * <p>用于封装对 L3 AI 服务、MQ、MinIO 等外部依赖调用失败的场景。</p>
 */
public class IntegrationException extends RuntimeException {

    private final HttpStatus status;
    private final String errorCode;

    public IntegrationException(HttpStatus status, String errorCode, String message) {
        super(message);
        this.status = status;
        this.errorCode = errorCode;
    }

    public IntegrationException(HttpStatus status, String errorCode, String message, Throwable cause) {
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
