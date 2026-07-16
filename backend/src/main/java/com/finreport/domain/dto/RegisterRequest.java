package com.finreport.domain.dto;

import jakarta.validation.constraints.Email;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

/**
 * 注册请求 DTO。
 *
 * <p>前端提交的用户注册信息，经 Bean Validation 校验后进入 AuthService。
 * 使用 Java 17 record，简洁不可变。</p>
 */
public record RegisterRequest(
        @NotBlank(message = "用户名不能为空")
        @Size(min = 3, max = 64, message = "用户名长度须在 3-64 之间")
        String username,

        @NotBlank(message = "密码不能为空")
        @Size(min = 6, max = 128, message = "密码长度须在 6-128 之间")
        String password,

        @Email(message = "邮箱格式不正确")
        @Size(max = 128)
        String email
) {}
