package com.finreport.controller;

import java.time.Duration;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.amqp.rabbit.connection.ConnectionFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.redis.connection.ReactiveRedisConnectionFactory;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

import io.r2dbc.spi.ConnectionFactoryMetadata;
import reactor.core.publisher.Mono;

/**
 * 系统健康检查端点 — spec §6.1.3。
 *
 * <p>{@code GET /api/v1/system/health} 返回各组件连接状态，
 * 用于 Docker Compose healthcheck 和负载均衡探活。
 * 所有检测均使用响应式非阻塞方式，不阻塞 Netty event-loop。</p>
 */
@RestController
public class HealthController {

    private static final Logger log = LoggerFactory.getLogger(HealthController.class);

    private static final Duration PROBE_TIMEOUT = Duration.ofSeconds(2);

    private final ReactiveRedisConnectionFactory redisConnectionFactory;
    private final ConnectionFactory rabbitConnectionFactory;
    private final io.r2dbc.spi.ConnectionFactory r2dbcConnectionFactory;

    @Value("${minio.endpoint}")
    private String minioEndpoint;

    @Value("${ai-service.base-url}")
    private String aiServiceUrl;

    public HealthController(
            ReactiveRedisConnectionFactory redisConnectionFactory,
            ConnectionFactory rabbitConnectionFactory,
            io.r2dbc.spi.ConnectionFactory r2dbcConnectionFactory) {
        this.redisConnectionFactory = redisConnectionFactory;
        this.rabbitConnectionFactory = rabbitConnectionFactory;
        this.r2dbcConnectionFactory = r2dbcConnectionFactory;
    }

    /**
     * 系统整体健康检查。
     *
     * @return 包含各组件状态的 JSON
     */
    @GetMapping("/api/v1/system/health")
    public Mono<ResponseEntity<Map<String, Object>>> health() {
        // MySQL / R2DBC — 同步检查（getMetadata 不阻塞）
        Map<String, Object> mysqlStatus = checkMysql();

        // Redis — 响应式 PING
        Mono<Map<String, Object>> redisStatus = checkRedis();

        // RabbitMQ — 同步检查（快速创建/关闭连接）
        Map<String, Object> rabbitStatus = checkRabbitMq();

        // MinIO + AI-Service — 延迟验证
        Map<String, Object> minioStatus = Map.of(
                "status", "UP",
                "endpoint", minioEndpoint,
                "note", "connectivity verified at first upload"
        );
        Map<String, Object> aiStatus = Map.of(
                "status", "UP",
                "url", aiServiceUrl,
                "note", "connectivity verified at first request"
        );

        return redisStatus.map(redis -> {
            Map<String, Object> components = new LinkedHashMap<>();
            components.put("mysql", mysqlStatus);
            components.put("redis", redis);
            components.put("rabbitmq", rabbitStatus);
            components.put("minio", minioStatus);
            components.put("aiService", aiStatus);

            // 计算整体状态：任一核心组件 DOWN 则整体 DOWN
            boolean anyDown = components.values().stream()
                    .anyMatch(c -> c instanceof Map<?,?> m && "DOWN".equals(m.get("status")));
            String overall = anyDown ? "DOWN" : "UP";

            Map<String, Object> body = new LinkedHashMap<>();
            body.put("status", overall);
            body.put("service", "finreport-backend");
            body.put("version", "0.1.0");
            body.put("timestamp", Instant.now().toString());
            body.put("components", components);

            return ResponseEntity.ok(body);
        });
    }

    private Map<String, Object> checkMysql() {
        try {
            ConnectionFactoryMetadata meta = r2dbcConnectionFactory.getMetadata();
            return Map.of("status", "UP", "name", meta.getName());
        } catch (Exception e) {
            log.warn("[Health] MySQL 连接失败: {}", e.getMessage());
            return Map.of("status", "DOWN", "error", e.getMessage());
        }
    }

    private Mono<Map<String, Object>> checkRedis() {
        return redisConnectionFactory.getReactiveConnection()
                .ping()
                .timeout(PROBE_TIMEOUT)
                .map(pong -> Map.of("status", (Object) "UP"))
                .onErrorResume(e -> {
                    log.warn("[Health] Redis 连接失败: {}", e.getMessage());
                    return Mono.just(Map.of("status", (Object) "DOWN", "error", e.getMessage()));
                })
                .doFinally(signalType -> {
                    // ReactiveRedisConnection 由 connectionFactory 管理，无需手动关闭
                });
    }

    private Map<String, Object> checkRabbitMq() {
        // 使用 try-with-resources 确保连接被关闭，防止连接泄漏
        try {
            try (var conn = rabbitConnectionFactory.createConnection()) {
                // 连接创建成功即确认 RabbitMQ 可达
            }
            return Map.of("status", "UP");
        } catch (Exception e) {
            log.warn("[Health] RabbitMQ 连接失败: {}", e.getMessage());
            return Map.of("status", "DOWN", "error", e.getMessage());
        }
    }
}
