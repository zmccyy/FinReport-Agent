package com.finreport.service;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.time.Duration;
import java.time.LocalDateTime;
import java.util.Date;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;
import org.springframework.data.redis.core.ReactiveRedisTemplate;
import org.springframework.data.redis.core.ReactiveValueOperations;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.security.crypto.password.PasswordEncoder;

import com.finreport.config.JwtConfig;
import com.finreport.domain.dto.LoginRequest;
import com.finreport.domain.dto.RefreshRequest;
import com.finreport.domain.dto.RegisterRequest;
import com.finreport.domain.dto.TokenResponse;
import com.finreport.domain.dto.UserInfoResponse;
import com.finreport.domain.entity.UserAccount;
import com.finreport.exception.AuthException;
import com.finreport.repository.UserAccountRepository;
import com.finreport.security.JwtUtil;

import reactor.core.publisher.Mono;
import reactor.test.StepVerifier;

/**
 * AuthService 单元测试。
 */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
@DisplayName("AuthService")
class AuthServiceTest {

    @Mock
    private UserAccountRepository userRepo;

    @Mock
    private ReactiveRedisTemplate<String, String> redisTemplate;

    @Mock
    private ReactiveValueOperations<String, String> valueOps;

    private JwtUtil jwtUtil;
    private JwtConfig jwtConfig;
    private PasswordEncoder passwordEncoder;
    private AuthService authService;

    @BeforeEach
    void setUp() {
        jwtConfig = new JwtConfig();
        jwtConfig.setSecret("test-jwt-secret-at-least-256-bits-long-for-hmac-sha256!!!");
        jwtConfig.setAccessExpiration(3600);
        jwtConfig.setRefreshExpiration(604800);
        jwtConfig.setIssuer("finreport-test");

        jwtUtil = new JwtUtil(jwtConfig);
        passwordEncoder = new BCryptPasswordEncoder(10);

        when(redisTemplate.opsForValue()).thenReturn(valueOps);

        authService = new AuthService(userRepo, passwordEncoder, jwtUtil, jwtConfig, redisTemplate);
    }

    @Nested
    @DisplayName("register")
    class Register {

        @Test
        @DisplayName("should create user and return tokens")
        void shouldRegisterSuccessfully() {
            RegisterRequest req = new RegisterRequest("newuser", "password123", "user@test.com");
            when(userRepo.existsByUsername("newuser")).thenReturn(Mono.just(false));
            when(userRepo.save(any(UserAccount.class))).thenAnswer(inv -> {
                UserAccount u = inv.getArgument(0);
                u.setId(10L);
                u.setCreatedAt(LocalDateTime.now());
                return Mono.just(u);
            });

            StepVerifier.create(authService.register(req))
                    .assertNext(response -> {
                        assertNotNull(response.accessToken());
                        assertNotNull(response.refreshToken());
                        assertEquals(3600, response.expiresIn());
                        assertEquals("Bearer", response.tokenType());
                        assertTrue(jwtUtil.validate(response.accessToken()));
                        assertTrue(jwtUtil.validate(response.refreshToken()));
                    })
                    .verifyComplete();

            verify(userRepo).existsByUsername("newuser");
            verify(userRepo).save(any(UserAccount.class));
        }

        @Test
        @DisplayName("should reject duplicate username")
        void shouldRejectDuplicateUsername() {
            RegisterRequest req = new RegisterRequest("existing", "password123", null);
            when(userRepo.existsByUsername("existing")).thenReturn(Mono.just(true));

            StepVerifier.create(authService.register(req))
                    .expectErrorMatches(ex -> ex instanceof AuthException
                            && "USERNAME_EXISTS".equals(((AuthException) ex).getErrorCode()))
                    .verify();

            verify(userRepo, never()).save(any());
        }

        @Test
        @DisplayName("should hash password with BCrypt")
        void shouldHashPassword() {
            RegisterRequest req = new RegisterRequest("user1", "secret123", null);
            when(userRepo.existsByUsername("user1")).thenReturn(Mono.just(false));
            when(userRepo.save(any(UserAccount.class))).thenAnswer(inv -> {
                UserAccount u = inv.getArgument(0);
                u.setId(1L);
                return Mono.just(u);
            });

            StepVerifier.create(authService.register(req))
                    .expectNextCount(1)
                    .verifyComplete();

            ArgumentCaptor<UserAccount> captor = ArgumentCaptor.forClass(UserAccount.class);
            verify(userRepo).save(captor.capture());
            UserAccount saved = captor.getValue();
            assertTrue(passwordEncoder.matches("secret123", saved.getPasswordHash()));
            assertEquals("USER", saved.getRole());
        }
    }

    @Nested
    @DisplayName("login")
    class Login {

        @Test
        @DisplayName("should return tokens on valid credentials")
        void shouldLoginSuccessfully() {
            String hashedPw = passwordEncoder.encode("correct");
            UserAccount user = UserAccount.builder()
                    .id(5L).username("tester").passwordHash(hashedPw)
                    .status(1).role("USER").build();

            when(userRepo.findByUsername("tester")).thenReturn(Mono.just(user));

            StepVerifier.create(authService.login(new LoginRequest("tester", "correct")))
                    .assertNext(response -> {
                        assertNotNull(response.accessToken());
                        assertEquals("tester", jwtUtil.getUsername(response.accessToken()));
                    })
                    .verifyComplete();
        }

        @Test
        @DisplayName("should reject wrong password")
        void shouldRejectWrongPassword() {
            String hashedPw = passwordEncoder.encode("correct");
            UserAccount user = UserAccount.builder()
                    .id(5L).username("tester").passwordHash(hashedPw)
                    .status(1).build();

            when(userRepo.findByUsername("tester")).thenReturn(Mono.just(user));

            StepVerifier.create(authService.login(new LoginRequest("tester", "wrong")))
                    .expectErrorMatches(ex -> ex instanceof AuthException
                            && "BAD_CREDENTIALS".equals(((AuthException) ex).getErrorCode()))
                    .verify();
        }

        @Test
        @DisplayName("should reject disabled account")
        void shouldRejectDisabledAccount() {
            UserAccount user = UserAccount.builder()
                    .id(5L).username("locked").passwordHash("hash")
                    .status(0).build();

            when(userRepo.findByUsername("locked")).thenReturn(Mono.just(user));

            StepVerifier.create(authService.login(new LoginRequest("locked", "any")))
                    .expectErrorMatches(ex -> ex instanceof AuthException
                            && "ACCOUNT_DISABLED".equals(((AuthException) ex).getErrorCode()))
                    .verify();
        }

        @Test
        @DisplayName("should reject nonexistent user")
        void shouldRejectNonexistentUser() {
            when(userRepo.findByUsername("ghost")).thenReturn(Mono.empty());

            StepVerifier.create(authService.login(new LoginRequest("ghost", "any")))
                    .expectErrorMatches(ex -> ex instanceof AuthException
                            && "BAD_CREDENTIALS".equals(((AuthException) ex).getErrorCode()))
                    .verify();
        }
    }

    @Nested
    @DisplayName("refresh")
    class Refresh {

        @Test
        @DisplayName("should issue new tokens on valid refresh token")
        void shouldRefreshSuccessfully() {
            String refreshToken = jwtUtil.generateRefreshToken(7L, "user7");
            when(redisTemplate.hasKey(anyString())).thenReturn(Mono.just(false));
            when(valueOps.set(anyString(), eq("1"), any(Duration.class)))
                    .thenReturn(Mono.just(true));

            StepVerifier.create(authService.refresh(new RefreshRequest(refreshToken)))
                    .assertNext(response -> {
                        assertNotNull(response.accessToken());
                        assertNotNull(response.refreshToken());
                        assertTrue(jwtUtil.validate(response.accessToken()));
                    })
                    .verifyComplete();
        }

        @Test
        @DisplayName("should reject blacklisted token")
        void shouldRejectBlacklistedToken() {
            String refreshToken = jwtUtil.generateRefreshToken(7L, "user7");
            when(redisTemplate.hasKey(anyString())).thenReturn(Mono.just(true));
            when(valueOps.set(anyString(), anyString(), any(Duration.class)))
                    .thenReturn(Mono.just(true));

            StepVerifier.create(authService.refresh(new RefreshRequest(refreshToken)))
                    .expectErrorMatches(ex -> ex instanceof AuthException
                            && "TOKEN_REVOKED".equals(((AuthException) ex).getErrorCode()))
                    .verify();
        }

        @Test
        @DisplayName("should reject expired token")
        void shouldRejectExpiredToken() {
            JwtConfig shortConfig = new JwtConfig();
            shortConfig.setSecret("test-jwt-secret-at-least-256-bits-long-for-hmac-sha256!!!");
            shortConfig.setAccessExpiration(3600);
            shortConfig.setRefreshExpiration(-1);
            shortConfig.setIssuer("finreport-test");
            JwtUtil shortJwt = new JwtUtil(shortConfig);
            String expiredToken = shortJwt.generateRefreshToken(7L, "user7");

            StepVerifier.create(authService.refresh(new RefreshRequest(expiredToken)))
                    .expectErrorMatches(ex -> ex instanceof AuthException
                            && "TOKEN_INVALID".equals(((AuthException) ex).getErrorCode()))
                    .verify();
        }
    }

    @Nested
    @DisplayName("logout")
    class Logout {

        @Test
        @DisplayName("should blacklist valid refresh token")
        void shouldBlacklistToken() {
            String token = jwtUtil.generateRefreshToken(3L, "user3");
            when(valueOps.set(anyString(), eq("1"), any(Duration.class)))
                    .thenReturn(Mono.just(true));

            StepVerifier.create(authService.logout(token))
                    .verifyComplete();

            verify(valueOps).set(anyString(), eq("1"), any(Duration.class));
        }

        @Test
        @DisplayName("should silently ignore null token")
        void shouldIgnoreNullToken() {
            StepVerifier.create(authService.logout(null))
                    .verifyComplete();

            verify(valueOps, never()).set(anyString(), anyString(), any());
        }

        @Test
        @DisplayName("should silently ignore invalid token")
        void shouldIgnoreInvalidToken() {
            StepVerifier.create(authService.logout("not.a.jwt"))
                    .verifyComplete();
        }
    }

    @Nested
    @DisplayName("getUserInfo")
    class GetUserInfo {

        @Test
        @DisplayName("should return user info for existing user")
        void shouldReturnUserInfo() {
            UserAccount user = UserAccount.builder()
                    .id(9L).username("alice").email("alice@test.com")
                    .role("USER").createdAt(LocalDateTime.now()).build();
            when(userRepo.findById(9L)).thenReturn(Mono.just(user));

            StepVerifier.create(authService.getUserInfo(9L))
                    .assertNext(info -> {
                        assertEquals(9L, info.id());
                        assertEquals("alice", info.username());
                        assertEquals("alice@test.com", info.email());
                        assertEquals("USER", info.role());
                    })
                    .verifyComplete();
        }

        @Test
        @DisplayName("should throw for nonexistent user")
        void shouldThrowForNonexistentUser() {
            when(userRepo.findById(999L)).thenReturn(Mono.empty());

            StepVerifier.create(authService.getUserInfo(999L))
                    .expectErrorMatches(ex -> ex instanceof AuthException
                            && "USER_NOT_FOUND".equals(((AuthException) ex).getErrorCode()))
                    .verify();
        }
    }
}
