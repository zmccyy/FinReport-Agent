# M1.12 Reliability Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the M1.12 upload persistence defect and address the approved task/MQ, SSE, upload I/O, trace, JWT, and Maven quality findings.

**Architecture:** Task creation becomes a single R2DBC transaction while dispatch applies explicit compensation on MQ failure. Uploads move from a heap buffer to a temporary-file stream. Request trace information is held in Reactor Context and emitted in MQ headers. SSE event emission is serialized per task.

**Tech Stack:** Java 21, Spring Boot 3.2 WebFlux/R2DBC, Reactor, RabbitMQ, MinIO, JUnit 5, Mockito, Maven.

---

### Task 1: Atomic task initialization and visible dispatch failures

**Files:**
- Modify: `backend/src/main/java/com/finreport/service/orchestrator/TaskOrchestrator.java`
- Modify: `backend/src/test/java/com/finreport/service/orchestrator/TaskOrchestratorTest.java`

- [x] Add task persistence, nullable bind, transaction invocation, and MQ failure compensation tests.
- [x] Run `mvn test -Dtest=TaskOrchestratorTest` after the implementation.
- [x] Add `TransactionalOperator` around task plus initial steps, and add a compensating FAILED transition when `publishTaskStep` fails.
- [x] Verify the task orchestrator tests pass.

### Task 2: Trace context from HTTP to MQ

**Files:**
- Create: `backend/src/main/java/com/finreport/config/TraceIdWebFilter.java`
- Modify: `backend/src/main/java/com/finreport/mq/TaskMessageProducer.java`
- Modify: `backend/src/test/java/com/finreport/mq/TaskMessageProducerTest.java`
- Create: `backend/src/test/java/com/finreport/config/TraceIdWebFilterTest.java`

- [x] Add tests for preserving/generating HTTP trace IDs and propagating them to MQ headers.
- [x] Implement the WebFilter and `Mono.deferContextual` trace lookup in the producer.
- [x] Run the trace context tests successfully.

### Task 3: Lossless concurrent SSE emission

**Files:**
- Modify: `backend/src/main/java/com/finreport/service/sse/SseEmitterPool.java`
- Modify: `backend/src/test/java/com/finreport/service/sse/SseEmitterPoolTest.java`

- [x] Add concurrent and emit-before-subscribe replay tests that require every event to arrive.
- [x] Replace finite busy retries with synchronized emission scoped to one task sink.
- [x] Run `mvn test -Dtest=SseEmitterPoolTest`; 10 tests pass.

### Task 4: Stream PDF upload and isolate MinIO I/O

**Files:**
- Modify: `backend/src/main/java/com/finreport/service/file/FileService.java`
- Modify: `backend/src/test/java/com/finreport/service/file/FileServiceTest.java`

- [x] Add upload tests for stream handling, cleanup, and MinIO errors.
- [x] Write incoming DataBuffers to a temporary file, calculate MD5 from the file, and upload with an input stream in `Schedulers.boundedElastic()`.
- [x] Verify `FileServiceTest` passes.

### Task 5: JWT validation and quality tooling

**Files:**
- Modify: `backend/src/main/resources/application.yml`
- Modify: `backend/src/main/java/com/finreport/security/JwtUtil.java`
- Modify: `backend/src/test/java/com/finreport/security/JwtUtilTest.java`
- Modify: `backend/pom.xml`
- Create: `backend/.mvn/wrapper/maven-wrapper.properties`
- Create: `backend/mvnw`, `backend/mvnw.cmd`
- Create: `backend/config/checkstyle/checkstyle.xml`

- [x] Add JWT configuration tests for unsafe non-local startup configuration.
- [x] Implement profile-aware key validation and configure wrapper plus Maven quality plugins.
- [x] Run backend unit tests and static checks. Coverage output needs ASCII CI verification (see decision record).

### Task 6: Final verification and delivery

**Files:**
- Modify: `docs/decisions/2026-07-15-r2dbc-upload-fix-and-repository-review.md`
- Modify: `docs/progress/m1.md` if task status changes

- [x] Run unit tests, targeted Maven quality goals, and a curl upload smoke test.
- [x] Record exact results and residual risks in the decision record.
- [ ] Review the diff, commit in Conventional Commits units, and push `feature/M1.12-reliability-hardening`.
