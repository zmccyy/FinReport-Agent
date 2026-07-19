package com.finreport.security;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.util.concurrent.atomic.AtomicReference;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.mock.http.server.reactive.MockServerHttpRequest;
import org.springframework.mock.web.server.MockServerWebExchange;
import org.springframework.security.core.userdetails.UserDetails;
import org.springframework.web.server.ServerWebExchange;
import org.springframework.web.server.WebFilterChain;

import com.finreport.domain.entity.UserAccount;
import com.finreport.repository.UserAccountRepository;
import com.finreport.service.AuthService;

import reactor.core.publisher.Mono;

/** Security component unit tests. */
@ExtendWith(MockitoExtension.class)
@DisplayName("Security components")
class SecurityComponentsTest {

    @Mock
    private JwtUtil jwtUtil;

    @Mock
    private AuthService authService;

    @Mock
    private UserAccountRepository userAccountRepository;

    @Nested
    @DisplayName("JwtFilter")
    class JwtFilterTests {

        @Test
        @DisplayName("should bypass public endpoint without authorization")
        void shouldBypassPublicEndpointWithoutAuthorization() {
            JwtFilter filter = new JwtFilter(jwtUtil, authService);
            AtomicReference<ServerWebExchange> passed = new AtomicReference<>();

            filter.filter(exchange("/api/v1/auth/login", null), capture(passed)).block();

            assertEquals("/api/v1/auth/login", passed.get().getRequest().getPath().value());
        }

        @Test
        @DisplayName("should bypass internal health endpoints without authorization")
        void shouldBypassInternalHealthEndpointsWithoutAuthorization() {
            JwtFilter filter = new JwtFilter(jwtUtil, authService);
            AtomicReference<ServerWebExchange> livePassed = new AtomicReference<>();
            AtomicReference<ServerWebExchange> readinessPassed = new AtomicReference<>();

            filter.filter(exchange("/internal/live", null), capture(livePassed)).block();
            filter.filter(exchange("/internal/health", null), capture(readinessPassed)).block();

            assertEquals("/internal/live", livePassed.get().getRequest().getPath().value());
            assertEquals("/internal/health", readinessPassed.get().getRequest().getPath().value());
        }        @Test
        @DisplayName("should bypass CORS preflight without authorization")
        void shouldBypassCorsPreflightWithoutAuthorization() {
            JwtFilter filter = new JwtFilter(jwtUtil, authService);
            AtomicReference<ServerWebExchange> passed = new AtomicReference<>();
            MockServerWebExchange exchange = MockServerWebExchange.from(
                    MockServerHttpRequest.options("/api/v1/users/me").build());

            filter.filter(exchange, capture(passed)).block();

            assertEquals("OPTIONS", passed.get().getRequest().getMethod().name());
        }



        @Test
        @DisplayName("should respond unauthorized when authorization header is absent")
        void shouldRespondUnauthorizedWhenAuthorizationHeaderIsAbsent() {
            JwtFilter filter = new JwtFilter(jwtUtil, authService);
            MockServerWebExchange exchange = exchange("/api/v1/tasks/task-1", null);

            filter.filter(exchange, capture(new AtomicReference<>())).block();

            assertEquals(HttpStatus.UNAUTHORIZED, exchange.getResponse().getStatusCode());
        }

        @Test
        @DisplayName("should respond unauthorized for invalid token")
        void shouldRespondUnauthorizedForInvalidToken() {
            JwtFilter filter = new JwtFilter(jwtUtil, authService);
            MockServerWebExchange exchange = exchange("/api/v1/tasks/task-1", "Bearer bad-token");
            when(jwtUtil.validate("bad-token")).thenReturn(false);

            filter.filter(exchange, capture(new AtomicReference<>())).block();

            assertEquals(HttpStatus.UNAUTHORIZED, exchange.getResponse().getStatusCode());
        }

        @Test
        @DisplayName("should reject a blacklisted valid token")
        void shouldRejectBlacklistedValidToken() {
            JwtFilter filter = new JwtFilter(jwtUtil, authService);
            MockServerWebExchange exchange = exchange("/api/v1/tasks/task-1", "Bearer revoked-token");
            when(jwtUtil.validate("revoked-token")).thenReturn(true);
            when(jwtUtil.getJti("revoked-token")).thenReturn("jti-revoked");
            when(authService.isBlacklisted("jti-revoked")).thenReturn(Mono.just(true));

            filter.filter(exchange, capture(new AtomicReference<>())).block();

            assertEquals(HttpStatus.UNAUTHORIZED, exchange.getResponse().getStatusCode());
            verify(authService).isBlacklisted("jti-revoked");
        }

        @Test
        @DisplayName("should inject JWT claims for non-blacklisted token")
        void shouldInjectJwtClaimsForNonBlacklistedToken() {
            JwtFilter filter = new JwtFilter(jwtUtil, authService);
            AtomicReference<ServerWebExchange> passed = new AtomicReference<>();
            when(jwtUtil.validate("valid-token")).thenReturn(true);
            when(jwtUtil.getJti("valid-token")).thenReturn("jti-valid");
            when(authService.isBlacklisted("jti-valid")).thenReturn(Mono.just(false));
            when(jwtUtil.getUserId("valid-token")).thenReturn(42L);
            when(jwtUtil.getUsername("valid-token")).thenReturn("alice");

            filter.filter(exchange("/api/v1/tasks/task-1", "Bearer valid-token"), capture(passed)).block();

            assertEquals("42", passed.get().getRequest().getHeaders().getFirst("X-User-Id"));
            assertEquals("alice", passed.get().getRequest().getHeaders().getFirst("X-Username"));
        }

        @Test
        @DisplayName("should degrade open if blacklist lookup fails")
        void shouldDegradeOpenIfBlacklistLookupFails() {
            JwtFilter filter = new JwtFilter(jwtUtil, authService);
            AtomicReference<ServerWebExchange> passed = new AtomicReference<>();
            when(jwtUtil.validate("valid-token")).thenReturn(true);
            when(jwtUtil.getJti("valid-token")).thenReturn("jti-valid");
            when(authService.isBlacklisted("jti-valid")).thenReturn(Mono.error(new IllegalStateException("redis down")));
            when(jwtUtil.getUserId("valid-token")).thenReturn(42L);
            when(jwtUtil.getUsername("valid-token")).thenReturn("alice");

            filter.filter(exchange("/api/v1/tasks/task-1", "Bearer valid-token"), capture(passed)).block();

            assertEquals("42", passed.get().getRequest().getHeaders().getFirst("X-User-Id"));
        }

        @Test
        @DisplayName("should reject token that lacks required claims")
        void shouldRejectTokenThatLacksRequiredClaims() {
            JwtFilter filter = new JwtFilter(jwtUtil, authService);
            MockServerWebExchange exchange = exchange("/api/v1/tasks/task-1", "Bearer incomplete-token");
            when(jwtUtil.validate("incomplete-token")).thenReturn(true);
            when(jwtUtil.getJti("incomplete-token")).thenReturn("jti-incomplete");
            when(authService.isBlacklisted("jti-incomplete")).thenReturn(Mono.just(false));
            when(jwtUtil.getUserId("incomplete-token")).thenReturn(null);
            when(jwtUtil.getUsername("incomplete-token")).thenReturn("alice");

            filter.filter(exchange, capture(new AtomicReference<>())).block();

            assertEquals(HttpStatus.UNAUTHORIZED, exchange.getResponse().getStatusCode());
        }

        private MockServerWebExchange exchange(String path, String authorization) {
            MockServerHttpRequest.BaseBuilder<?> builder = MockServerHttpRequest.get(path);
            if (authorization != null) {
                builder.header(HttpHeaders.AUTHORIZATION, authorization);
            }
            return MockServerWebExchange.from(builder.build());
        }

        private WebFilterChain capture(AtomicReference<ServerWebExchange> passed) {
            return exchange -> {
                passed.set(exchange);
                return Mono.empty();
            };
        }
    }

    @Nested
    @DisplayName("UserDetailsServiceImpl")
    class UserDetailsServiceTests {

        @Test
        @DisplayName("should map an active account to enabled user details")
        void shouldMapActiveAccountToEnabledUserDetails() {
            UserAccount account = UserAccount.builder()
                    .id(7L).username("alice").passwordHash("hash").role("USER").status(1).build();
            when(userAccountRepository.findByUsername("alice")).thenReturn(Mono.just(account));

            UserDetails details = new UserDetailsServiceImpl(userAccountRepository)
                    .findByUsername("alice").block();

            assertEquals("alice", details.getUsername());
            assertEquals("hash", details.getPassword());
            assertTrue(details.isEnabled());
            assertTrue(details.getAuthorities().stream().anyMatch(value -> "ROLE_USER".equals(value.getAuthority())));
        }

        @Test
        @DisplayName("should map inactive account to disabled user details")
        void shouldMapInactiveAccountToDisabledUserDetails() {
            UserAccount account = UserAccount.builder()
                    .id(8L).username("disabled").passwordHash("hash").role("USER").status(0).build();
            when(userAccountRepository.findByUsername("disabled")).thenReturn(Mono.just(account));

            UserDetails details = new UserDetailsServiceImpl(userAccountRepository)
                    .findByUsername("disabled").block();

            assertFalse(details.isEnabled());
        }

        @Test
        @DisplayName("should return empty when account does not exist")
        void shouldReturnEmptyWhenAccountDoesNotExist() {
            when(userAccountRepository.findByUsername("missing")).thenReturn(Mono.empty());

            UserDetails details = new UserDetailsServiceImpl(userAccountRepository)
                    .findByUsername("missing").block();

            assertNull(details);
        }
    }
}
