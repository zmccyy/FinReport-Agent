package com.finreport.controller;

import java.time.Instant;
import java.util.Map;

import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

import reactor.core.publisher.Mono;

/**
 * 系统健康检查端点。
 *
 * <p>提供 {@code /api/v1/system/health} 用于 Docker Compose healthcheck
 * 和负载均衡器探活。M1.07 将扩展添加各组件状态检查。</p>
 */
@RestController
public class HealthController {

    /**
     * 系统健康检查。
     *
     * @return 包含状态和时间戳的 JSON
     */
    @GetMapping("/api/v1/system/health")
    public Mono<ResponseEntity<Map<String, Object>>> health() {
        Map<String, Object> body = Map.of(
                "status", "UP",
                "service", "finreport-backend",
                "timestamp", Instant.now().toString()
        );
        return Mono.just(ResponseEntity.ok(body));
    }
}
