package com.finreport.domain.entity;

import java.time.LocalDateTime;

import org.springframework.data.annotation.Id;
import org.springframework.data.relational.core.mapping.Table;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

/**
 * 用户账户实体，映射 user_account 表。
 *
 * <p>V1__init_user.sql 定义的表结构。</p>
 */
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
@Table("user_account")
public class UserAccount {

    @Id
    private Long id;

    private String username;

    private String passwordHash;

    private String email;

    private String role;

    private Integer status;

    private LocalDateTime createdAt;

    private LocalDateTime updatedAt;
}
