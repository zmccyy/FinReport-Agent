# M1.12 Reliability Hardening Design

> **Date:** 2026-07-15
> **Status:** Approved for implementation
> **Scope:** Backend reliability and security findings from the M1.12 upload review

## Goals

1. Make task and step creation atomic in R2DBC.
2. Never report successful dispatch when RabbitMQ publish fails.
3. Preserve all concurrent SSE events for a single task sink.
4. Avoid holding an entire uploaded PDF in heap memory and isolate blocking MinIO SDK work.
5. Carry one trace ID from HTTP through Reactor and MQ headers.
6. Reject an unsafe JWT configuration outside the local profile.
7. Restore executable Maven quality gates without changing locked runtime stack versions.

## Decisions

### Task creation and dispatch

Task and all initial task_step rows will execute inside a `TransactionalOperator`. Dispatch remains outside
that transaction: a successful transaction creates a valid pending task; dispatch updates the step, publishes
the message, and compensates to FAILED when publication fails. This avoids silent RUNNING tasks while avoiding
a false claim of distributed DB/MQ atomicity.

### SSE serialization

Each task retains a single unicast/multicast event sink. Concurrent `tryEmitNext` calls are serialized with a
per-task lock. No event is dropped solely because Reactor reports `FAIL_NON_SERIALIZED`; terminal sink outcomes
are logged and handled explicitly.

### Upload storage strategy

The upload body is streamed into a temporary file while calculating MD5. MinIO receives a `Files.newInputStream`
from a bounded-elastic worker. The temporary file is deleted on success and failure. The implementation does not
introduce a new streaming bridge dependency.

### Trace context

A WebFilter receives or generates `X-Trace-Id`, returns it to callers, and stores it in Reactor Context. MQ
publication resolves it from Reactor Context; an MDC fallback is retained only for non-request execution paths.

### JWT startup validation

The default development key is allowed only when the `local` profile is active. All other profiles must supply
`JWT_SECRET` of at least 32 characters; startup fails otherwise.

### Quality enforcement

Maven Wrapper is added. JaCoCo reporting is wired through Surefire for CI verification; Checkstyle, SpotBugs,
and integration-test wiring are available as Maven goals. The current Windows workspace path prevents local JaCoCo
execution data from being produced, so the >=80% threshold must be confirmed and enforced in CI before M1 is
accepted as fully complete. No production library version is changed.

## Non-goals

- A persistent transactional outbox table and relay worker.
- Cross-service distributed transactions.
- A new object storage SDK.
- Altering API response contracts.
