package com.finreport;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/**
 * FinReport Agent — L2 应用层入口。
 *
 * <p>Spring Boot 3.2.x + WebFlux，提供 REST/SSE 接入、任务编排、
 * 会话管理、文件管理、审计日志等服务。</p>
 */
@SpringBootApplication
public class FinReportApplication {

    public static void main(String[] args) {
        SpringApplication.run(FinReportApplication.class, args);
    }
}
