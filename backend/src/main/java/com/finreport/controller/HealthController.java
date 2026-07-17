package com.finreport.controller;

import java.time.Duration;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;

import org.springframework.amqp.rabbit.connection.ConnectionFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.r2dbc.core.DatabaseClient;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.reactive.function.client.WebClient;

import io.minio.BucketExistsArgs;
import io.minio.MinioClient;
import reactor.core.publisher.Mono;
import reactor.core.scheduler.Schedulers;

/** Readiness and liveness probes for the backend process. */
@RestController
public class HealthController {

    private static final Duration PROBE_TIMEOUT = Duration.ofSeconds(2);
    private static final String UP = "UP";
    private static final String DOWN = "DOWN";
    private static final String UPLOAD_BUCKET = "finreport-uploads";

    private final DatabaseClient databaseClient;
    private final org.springframework.data.redis.connection.ReactiveRedisConnectionFactory redis;
    private final ConnectionFactory rabbit;
    private final MinioClient minio;
    private final WebClient webClient;

    /** Creates health probes with bounded I/O timeouts. */
    public HealthController(
            DatabaseClient databaseClient,
            org.springframework.data.redis.connection.ReactiveRedisConnectionFactory redis,
            ConnectionFactory rabbit,
            MinioClient minio,
            WebClient.Builder webClientBuilder,
            @org.springframework.beans.factory.annotation.Value("${ai-service.base-url}") String aiBaseUrl) {
        this.databaseClient = databaseClient;
        this.redis = redis;
        this.rabbit = rabbit;
        this.minio = minio;
        this.webClient = webClientBuilder.baseUrl(aiBaseUrl).build();
    }

    /** Compatibility readiness endpoint. */
    @GetMapping("/api/v1/system/health")
    public Mono<ResponseEntity<Map<String, Object>>> health() {
        return readiness();
    }

    /** True readiness endpoint: all persistent dependencies must be reachable. */
    @GetMapping("/internal/health")
    public Mono<ResponseEntity<Map<String, Object>>> readiness() {
        return Mono.zip(mysql(), redis(), rabbit(), minio(), ai())
                .map(tuple -> response(Map.of(
                        "mysql", tuple.getT1(), "redis", tuple.getT2(), "rabbitmq", tuple.getT3(),
                        "minio", tuple.getT4(), "aiService", tuple.getT5())));
    }

    /** Liveness intentionally reflects only that the backend process is running. */
    @GetMapping("/internal/live")
    public Mono<ResponseEntity<Map<String, Object>>> liveness() {
        return Mono.just(ResponseEntity.ok(Map.of(
                "status", UP, "service", "finreport-backend", "timestamp", Instant.now().toString())));
    }

    private Mono<Map<String, Object>> mysql() {
        return databaseClient.sql("SELECT 1").fetch().rowsUpdated().thenReturn(up())
                .timeout(PROBE_TIMEOUT).onErrorReturn(down());
    }

    private Mono<Map<String, Object>> redis() {
        return redis.getReactiveConnection().ping().thenReturn(up())
                .timeout(PROBE_TIMEOUT).onErrorReturn(down());
    }

    private Mono<Map<String, Object>> rabbit() {
        return Mono.fromCallable(() -> {
            try (var connection = rabbit.createConnection(); var channel = connection.createChannel(false)) {
                return up();
            }
        }).subscribeOn(Schedulers.boundedElastic()).timeout(PROBE_TIMEOUT).onErrorReturn(down());
    }

    private Mono<Map<String, Object>> minio() {
        return Mono.fromCallable(() -> minio.bucketExists(BucketExistsArgs.builder().bucket(UPLOAD_BUCKET).build()))
                .subscribeOn(Schedulers.boundedElastic()).timeout(PROBE_TIMEOUT)
                .map(exists -> exists ? up() : down()).onErrorReturn(down());
    }

    private Mono<Map<String, Object>> ai() {
        return webClient.get().uri("/internal/health").exchangeToMono(response ->
                        response.statusCode().is2xxSuccessful() ? Mono.just(up()) : Mono.just(down()))
                .timeout(PROBE_TIMEOUT).onErrorReturn(down());
    }

    private ResponseEntity<Map<String, Object>> response(Map<String, Map<String, Object>> components) {
        boolean unavailable = components.values().stream().anyMatch(status -> DOWN.equals(status.get("status")));
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("status", unavailable ? DOWN : UP);
        body.put("service", "finreport-backend");
        body.put("timestamp", Instant.now().toString());
        body.put("components", components);
        return ResponseEntity.status(unavailable ? HttpStatus.SERVICE_UNAVAILABLE : HttpStatus.OK).body(body);
    }

    private static Map<String, Object> up() { return Map.of("status", UP); }
    private static Map<String, Object> down() { return Map.of("status", DOWN); }
}
