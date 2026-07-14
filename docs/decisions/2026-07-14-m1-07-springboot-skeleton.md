# 2026-07-14 M1.07 L2 SpringBoot 骨架

> 摘要：搭建 WebFlux + Security + JWT + GlobalExceptionHandler 完整骨架，41 单元测试全绿

## 关键决策

| # | 决策 | 背景 |
|---|---|---|
| D1 | **WebFlux 而非 WebMVC** | spec §6 明确所有 L2 接口使用响应式编程模型（SSE、R2DBC、ReactiveRedis），与 WebFlux 天然匹配 |
| D2 | **JWT 使用 jjwt 0.12.6** | Spring Boot 3.2.x 生态中成熟的 JWT 库。HMAC-SHA256 签名，access 1h / refresh 7d |
| D3 | **M1.07 permitAll / M1.08 收紧** | SecurityConfig 在 M1.07 放行所有请求，JwtFilter 已注册但不拦截。M1.08 启用路径保护后只需修改 SecurityConfig 一行 |
| D4 | **UserDetailsServiceImpl M1.07 mock** | 返回固定 admin 用户（BCrypt cost=10）。M1.08 改为查 MySQL user_account 表 |
| D5 | **HealthController 主动探测** | MySQL(R2DBC) + Redis(PING) + RabbitMQ(open/close connection) 实时探测，MinIO + AI-Service 标记为 UP 并注明延迟验证 |
| D6 | **MinIO endpoint 协议自动补全** | MinioConfig 检测 endpoint 是否含 `http://`/`https://` 前缀，如缺失自动补 `http://`。兼容 Docker（环境变量传 `minio:9000`）和本地开发（`http://localhost:9000`）两种场景 |
| D7 | **RabbitMqConfig prefetch=1 在消费者容器级别** | 在 `RabbitListenerContainerFactory` 上设置 `prefetchCount=1`（spec §3.1 显存限流），而非依赖 topology 声明脚本 |
| D8 | **异常体系统一 RFC 9457** | GlobalExceptionHandler 拦截 BusinessException/IntegrationException/ValidationException/Exception，统一返回 `{type, title, status, detail, instance, timestamp}` 格式 |

## 创建/修改文件清单

### 新建（14 文件）
- `exception/BusinessException.java` — 业务异常基类（HTTP 状态码 + 错误代码）
- `exception/AuthException.java` — 认证授权异常（默认 401）
- `exception/ValidationException.java` — 参数校验异常（默认 422）
- `exception/IntegrationException.java` — 集成异常基类（MQ/MinIO/AI-Service）
- `exception/GlobalExceptionHandler.java` — RFC 9457 全局异常处理
- `config/JwtConfig.java` — JWT 配置属性（@ConfigurationProperties）
- `config/WebFluxConfig.java` — CORS 配置
- `config/MinioConfig.java` — MinioClient Bean（自动补全协议前缀）
- `config/RabbitMqConfig.java` — JSON 消息转换 + prefetch=1
- `config/RedisConfig.java` — ReactiveRedisTemplate Bean
- `security/JwtUtil.java` — JWT 生成/校验/过期判断
- `security/JwtFilter.java` — WebFilter 提取 Bearer Token
- `security/UserDetailsServiceImpl.java` — ReactiveUserDetailsService（M1.07 mock）
- `M107SkeletonTest.java` — 21 个单元测试

### 修改（3 文件）
- `controller/HealthController.java` — 扩展为多组件健康检查
- `resources/application.yml` — 新增 JWT 配置段
- `deploy/docker-compose.yml` — MINIO_ENDPOINT 补全 `http://` 前缀

## 验证结果

- [x] `mvn compile` — BUILD SUCCESS
- [x] `mvn test -Dtest="FlywayMigrationTest,M107SkeletonTest"` — 41/41 pass
- [x] 异常体系 7 测试通过（状态码 + 错误代码 + cause 链）
- [x] JWT 10 测试通过（生成/解析/校验/过期/不同密钥）
- [x] JwtConfig 4 测试通过（默认值 + setter）

## 已知限制

- FlywayIntegrationTest（16 个）需要 MySQL 容器，不在本次测试范围
- UserDetailsServiceImpl 返回 mock 用户（M1.08 对接 MySQL）
- JwtFilter 注册但不拦截（SecurityConfig permitAll）
- MinIO/AI-Service 健康检查为延迟验证模式

## 下一步

- [ ] M1.08 L2 AuthController — 注册/登录/刷新/登出（5 端点）
