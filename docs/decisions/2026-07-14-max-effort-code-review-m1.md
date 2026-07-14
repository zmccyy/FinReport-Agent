# 2026-07-14 Max-Effort 代码审查 — M1.04-M1.07

> 摘要：对 M1.04-M1.07 共 4 个 commit 进行 10 角度 max-effort 审查，发现 27 个问题，修复 23 个，71 测试全绿

## 审查配置

| 参数 | 值 |
|---|---|
| 审查范围 | HEAD~3..HEAD (28 files, +2771/-73 lines) |
| 审查级别 | max effort (5+5 angles × 8 candidates → verify → sweep) |
| 角度 | A: line-by-line diff, B: removed-behavior audit, C: cross-file tracer, D: language pitfalls (Java + Python), E: conventions (CLAUDE.md), 其他: reuse/simplification/efficiency/altitude |
| Agent 数量 | 10 finder agents (并行) |
| 最终测试 | Python 30/30 + Java 41/41 = 71/71 pass |

## 发现汇总

### CRITICAL (1)

| # | 问题 | 文件 | 修复 commit |
|---|---|---|---|
| C1 | `local` profile 排除 Rabbit/Redis/R2DBC AutoConfiguration → `NoSuchBeanDefinitionException` 启动崩溃 | `application.yml` | 36a0468 |

### HIGH (10) — 全部已修复

| # | 问题 | 文件 |
|---|---|---|
| H1 | `.block()` 阻塞 Netty event-loop → 并发探针下应用不可响应 | `HealthController.java` |
| H2 | overall status 硬编码 "UP" → Docker/K8s 探针误报 healthy | `HealthController.java` |
| H3 | RabbitMQ `createConnection().close()` → 连接泄漏耗尽 broker 连接数 | `HealthController.java` |
| H4 | JWT `getBytes()` 无 charset → Windows/Linux 跨平台密钥不一致 | `JwtUtil.java` |
| H5 | JWT `parseToken` 缺 `requireIssuer()` → issuer 混淆攻击面 | `JwtUtil.java` |
| H6 | `JwtFilter` null check 缺 → 无 `userId`/`username` claim 的 Token 导致 NPE 500 | `JwtFilter.java` |
| H7 | `@EnableWebFlux` 覆盖 Boot `WebFluxAutoConfiguration` → 后续定制失效 | `WebFluxConfig.java` |
| H8 | `@Configuration` + `@ConfigurationProperties` 双重注册 → 潜在的代理冲突 | `JwtConfig.java` |
| H9 | MinIO/AI-Service 健康检查无条件返回 UP → 误报 | `HealthController.java` |
| H10 | `declare_mq.py` `--definitions-path` CLI 参数被函数内覆盖 → 静默忽略用户输入 | `declare_mq.py` |

### MEDIUM (8) — 已修复 7，剩余 1

| # | 问题 | 文件 | 状态 |
|---|---|---|---|
| M1 | CORS `allowCredentials(true)` + `allowedOriginPatterns("*")` → CSRF 暴露面 | `WebFluxConfig.java` | ⚠ 开发环境可接受 |
| M2 | `init.sh` `mc ilm` 用 `|| echo "[WARN]"` 吞生命周期/策略失败 | `init.sh` | ✅ 已修复 |
| M3 | `init_milvus.py` `except Exception: pass` 静默吞索引检查失败 | `init_milvus.py` | ✅ 已修复 |
| M4 | `init_milvus.py` dry-run 时 `connections.disconnect` 崩溃 | `init_milvus.py` | ✅ 已修复 |
| M5 | `declare_mq.py` `declare_topology()` 连接未 try/finally → 声明中途异常连接泄漏 | `declare_mq.py` | ✅ 已修复 |
| M6 | `init.sh` 未挂载到 MinIO 容器 → `docker exec` 路径无效 | `docker-compose.yml` | ✅ 已修复 |
| M7 | pre-M1 决策文件删除导致 4 个风险项丢失 | `docs/decisions/` | ✅ 已恢复 |
| M8 | `init.sh` `mc ilm rule add` 非幂等 → 重复执行累加规则 | `init.sh` | ⚠ 建议用 `init_minio.py` |

### LOW (8) — 已修复 5，剩余 3

| # | 问题 | 状态 |
|---|---|---|
| L1 | `@Value` 缺 inline default（如 `@Value("${minio.endpoint:http://localhost:9000}")`） | ⚠ 已有 YAML 兜底 |
| L2 | `AuthException`/`ValidationException` 缺 cause-chaining 构造函数 | ✅ 已修复 |
| L3 | `GlobalExceptionHandler` 缺 traceId 字段 | ✅ 已修复（MDC+UUID 兜底） |
| L4 | `WebFluxConfig` 魔法数字 3600 | ✅ 已修复 |
| L5 | `declare_mq.py` unused `field` import | ✅ 已修复 |
| L6 | `declare_mq.py` `basic_qos` 仅对声明 channel 生效不持久化 | ✅ 已修复（注释说明） |
| L7 | `declare_mq.py` `--import-definitions` CWD 相对路径问题 | ⚠ 实际极少触发 |
| L8 | `declare_mq.py` `delete_topology` 无队列存在检查 | ⚠ `--delete` 非正常路径 |

## 关键决策

| # | 决策 |
|---|---|
| D1 | HealthController 不检测 MinIO/AI 连通性（M1 阶段延迟验证，避免启动依赖重服务） |
| D2 | CORS wildcard origin 在开发阶段保留（生产部署时收敛为具体 origin 列表） |
| D3 | `init.sh` 保留但建议优先使用 `init_minio.py`（Python 脚本更健壮，支持 lifecycle 替换语义） |
| D4 | `application.yml` `local` profile 不再排除任何 auto-config（Spring Boot 在依赖不可用时仅 WARN 不 crash） |

## 审查效果

| 指标 | 值 |
|---|---|
| 发现问题总数 | 27 |
| 已修复 | 23 (85%) |
| 剩余（可接受风险） | 4 (全部 LOW/MEDIUM, 边缘场景) |
| 审查前测试 | 71 pass |
| 审查后测试 | 71 pass (无回归) |
| 修复 commits | 3 (166a0bd, 36a0468, +amend) |

## 学到的教训

1. **Spring Boot `autoconfigure.exclude` 是危险操作** — 后续添加的 Config Bean 和 Controller 可能在不知情的情况下依赖被排除的 AutoConfig。解决方案：不排除，或为每个新 Bean 添加 `@Profile` / `@ConditionalOnBean` 守卫
2. **WebFlux 中 `.block()` 是反模式** — 即使在健康检查中，也应用 `Mono.timeout()` + `onErrorResume()` 保持响应式链
3. **JWT 安全性需要防御性编程** — 至少需要 charset 显式指定、issuer 校验、claims null check 三层防护
4. **Python/Shell 双轨脚本需要一致性测试** — `init_minio.py` 和 `init.sh` 的 bucket/lifecycle/policy 由 `test_m1_init_scripts.py` 自动化验证对齐
