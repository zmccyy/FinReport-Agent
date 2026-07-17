package com.finreport.controller;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.when;

import java.util.Map;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import com.rabbitmq.client.Channel;
import org.springframework.amqp.rabbit.connection.Connection;
import org.springframework.amqp.rabbit.connection.ConnectionFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.r2dbc.core.DatabaseClient;
import org.springframework.r2dbc.core.FetchSpec;
import org.springframework.web.reactive.function.client.ClientResponse;
import org.springframework.web.reactive.function.client.WebClient;

import io.minio.MinioClient;
import reactor.core.publisher.Mono;

/** Health readiness and liveness tests. */
@ExtendWith(MockitoExtension.class)
@DisplayName("HealthController")
class HealthControllerTest {

    @Mock
    private DatabaseClient databaseClient;

    @Mock
    private DatabaseClient.GenericExecuteSpec executeSpec;

    @Mock
    private FetchSpec<Map<String, Object>> fetchSpec;

    @Mock
    private org.springframework.data.redis.connection.ReactiveRedisConnectionFactory redis;

    @Mock
    private org.springframework.data.redis.connection.ReactiveRedisConnection redisConnection;

    @Mock
    private ConnectionFactory rabbit;

    @Mock
    private Connection rabbitConnection;

    @Mock
    private Channel rabbitChannel;

    @Mock
    private MinioClient minio;

    @Test
    @DisplayName("should return UP only when every readiness dependency is reachable")
    void shouldReturnUpOnlyWhenEveryReadinessDependencyIsReachable() throws Exception {
        arrangeCoreDependencies(true);
        HealthController controller = controller(HttpStatus.OK);

        ResponseEntity<Map<String, Object>> response = controller.readiness().block();

        assertNotNull(response);
        assertEquals(HttpStatus.OK, response.getStatusCode());
        assertEquals("UP", response.getBody().get("status"));
        @SuppressWarnings("unchecked")
        Map<String, Map<String, Object>> components =
                (Map<String, Map<String, Object>>) response.getBody().get("components");
        assertEquals("UP", components.get("mysql").get("status"));
        assertEquals("UP", components.get("redis").get("status"));
        assertEquals("UP", components.get("rabbitmq").get("status"));
        assertEquals("UP", components.get("minio").get("status"));
        assertEquals("UP", components.get("aiService").get("status"));
    }

    @Test
    @DisplayName("should return DOWN when any readiness dependency is unavailable")
    void shouldReturnDownWhenAnyReadinessDependencyIsUnavailable() throws Exception {
        arrangeCoreDependencies(false);
        HealthController controller = controller(HttpStatus.SERVICE_UNAVAILABLE);

        ResponseEntity<Map<String, Object>> response = controller.health().block();

        assertNotNull(response);
        assertEquals(HttpStatus.SERVICE_UNAVAILABLE, response.getStatusCode());
        assertEquals("DOWN", response.getBody().get("status"));
        @SuppressWarnings("unchecked")
        Map<String, Map<String, Object>> components =
                (Map<String, Map<String, Object>>) response.getBody().get("components");
        assertEquals("DOWN", components.get("minio").get("status"));
        assertEquals("DOWN", components.get("aiService").get("status"));
    }

    @Test
    @DisplayName("should keep liveness UP without probing downstream dependencies")
    void shouldKeepLivenessUpWithoutProbingDownstreamDependencies() {
        ResponseEntity<Map<String, Object>> response = controller(HttpStatus.OK).liveness().block();

        assertNotNull(response);
        assertEquals(HttpStatus.OK, response.getStatusCode());
        assertEquals("UP", response.getBody().get("status"));
        assertEquals("finreport-backend", response.getBody().get("service"));
    }

    private HealthController controller(HttpStatus aiStatus) {
        WebClient.Builder builder = WebClient.builder().exchangeFunction(request ->
                Mono.just(ClientResponse.create(aiStatus).build()));
        return new HealthController(databaseClient, redis, rabbit, minio, builder, "http://ai-service:8000");
    }

    @SuppressWarnings({"unchecked", "rawtypes"})
    private void arrangeCoreDependencies(boolean minioAvailable) throws Exception {
        when(databaseClient.sql("SELECT 1")).thenReturn(executeSpec);
        when(executeSpec.fetch()).thenReturn(fetchSpec);
        when(fetchSpec.rowsUpdated()).thenReturn(Mono.just(1L));
        when(redis.getReactiveConnection()).thenReturn(redisConnection);
        when(redisConnection.ping()).thenReturn(Mono.just("PONG"));
        when(rabbit.createConnection()).thenReturn(rabbitConnection);
        when(rabbitConnection.createChannel(false)).thenReturn(rabbitChannel);
        when(minio.bucketExists(any())).thenReturn(minioAvailable);
    }
}
