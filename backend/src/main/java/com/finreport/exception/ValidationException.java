package com.finreport.exception;

import org.springframework.http.HttpStatus;

/**
 * 请求参数校验异常。
 *
 * <p>用于业务层校验失败场景（如文件大小超限、格式不支持等），
 * 不同于 Bean Validation 的自动校验。</p>
 */
public class ValidationException extends BusinessException {

    public ValidationException(String message) {
        super(HttpStatus.UNPROCESSABLE_ENTITY, "VALIDATION_ERROR", message);
    }

    public ValidationException(String errorCode, String message) {
        super(HttpStatus.UNPROCESSABLE_ENTITY, errorCode, message);
    }

    public ValidationException(String errorCode, String message, Throwable cause) {
        super(HttpStatus.UNPROCESSABLE_ENTITY, errorCode, message, cause);
    }
}
