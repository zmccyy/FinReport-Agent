"""M1.13-M1.14 FastAPI and RabbitMQ worker contract tests."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.mq.consumer import TaskConsumer
from app.schemas.task import TaskMessage


class FakeChannel:
    """Captures RabbitMQ acknowledgement calls without a broker."""

    def __init__(self) -> None:
        self.acks: list[int] = []
        self.nacks: list[tuple[int, bool]] = []
        self.qos_calls: list[int] = []

    def basic_ack(self, delivery_tag: int) -> None:
        """Record a positive acknowledgement."""
        self.acks.append(delivery_tag)

    def basic_nack(self, delivery_tag: int, requeue: bool) -> None:
        """Record a negative acknowledgement."""
        self.nacks.append((delivery_tag, requeue))

    def basic_qos(self, prefetch_count: int) -> None:
        """Record the requested prefetch count."""
        self.qos_calls.append(prefetch_count)


class FakeProperties:
    """Minimal AMQP properties used by the consumer."""

    def __init__(self, headers: dict[str, Any]) -> None:
        self.headers = headers


class FakeMethod:
    """Minimal AMQP delivery method used by the consumer."""

    def __init__(self, delivery_tag: int, routing_key: str) -> None:
        self.delivery_tag = delivery_tag
        self.routing_key = routing_key


class FakeProducer:
    """Captures emitted progress messages."""

    def __init__(self) -> None:
        self.messages: list[tuple[dict[str, Any], str]] = []

    def publish_progress(self, message: dict[str, Any], trace_id: str) -> None:
        """Record a progress message."""
        self.messages.append((message, trace_id))


class FailingProducer(FakeProducer):
    """Simulates a publisher whose broker confirmation is unavailable."""

    def publish_progress(self, message: dict[str, Any], trace_id: str) -> None:
        """Reject a progress publish after the worker has completed its handler."""
        del message, trace_id
        raise OSError("progress publisher confirmation failed")


def build_message(step: str = "parse") -> bytes:
    """Build a valid task message body."""
    return json.dumps(
        {
            "taskId": "task-123",
            "step": step,
            "payload": {"pdfObjectKey": "uploads/demo.pdf"},
            "idempotencyKey": f"task-123:{step}",
        }
    ).encode()


def test_health_and_parse_mock_endpoints_are_available() -> None:
    """M1.13 exposes health, Swagger, and the mock parse endpoint."""
    app = create_app(Settings(mq_consumer_enabled=False))

    with TestClient(app) as client:
        health = client.get("/internal/health")
        parse = client.post("/parse", json={"pdfObjectKey": "uploads/demo.pdf"})
        docs = client.get("/docs")

    assert health.status_code == 200
    assert health.json()["status"] == "UP"
    assert parse.status_code == 200
    assert parse.json()["document"]["source"] == "uploads/demo.pdf"
    assert docs.status_code == 200


def test_task_consumer_acknowledges_after_success_and_preserves_trace_id() -> None:
    """A successful mock step reports progress then manually acknowledges delivery."""
    producer = FakeProducer()
    consumer = TaskConsumer(Settings(mq_consumer_enabled=False), producer)
    channel = FakeChannel()

    consumer.on_message(
        channel,
        FakeMethod(delivery_tag=17, routing_key="parse"),
        FakeProperties({"traceId": "trace-abc"}),
        build_message(),
    )

    assert channel.acks == [17]
    assert channel.nacks == []
    assert producer.messages[0][1] == "trace-abc"
    progress = producer.messages[0][0]
    assert progress["taskId"] == "task-123"
    assert progress["step"] == "PARSE"
    assert progress["status"] == "SUCCESS"
    assert progress["idempotencyKey"] == "task-123:PARSE"


def test_task_consumer_reports_handler_failure_before_acknowledging_delivery() -> None:
    """A handler failure must report FAILED progress before the delivery is acknowledged."""
    producer = FakeProducer()
    consumer = TaskConsumer(Settings(mq_consumer_enabled=False), producer)
    channel = FakeChannel()

    async def failing_handler(message: TaskMessage) -> dict[str, Any]:
        """Simulate an unrecoverable mock processing failure."""
        del message
        raise RuntimeError("mock handler failed")

    consumer.handlers["parse"] = failing_handler
    consumer.on_message(
        channel,
        FakeMethod(delivery_tag=18, routing_key="parse"),
        FakeProperties({"traceId": "trace-def"}),
        build_message(),
    )

    assert channel.acks == [18]
    assert channel.nacks == []
    assert producer.messages == [
        (
            {
                "taskId": "task-123",
                "step": "PARSE",
                "status": "FAILED",
                "progress": 15,
                "result": {"error": "mock handler failed"},
                "idempotencyKey": "task-123:PARSE",
            },
            "trace-def",
        )
    ]


def test_task_consumer_nacks_when_progress_publication_is_unconfirmed() -> None:
    """An unconfirmed progress publish must keep the source task out of false success."""
    consumer = TaskConsumer(Settings(mq_consumer_enabled=False), FailingProducer())
    channel = FakeChannel()

    consumer.on_message(
        channel,
        FakeMethod(delivery_tag=19, routing_key="parse"),
        FakeProperties({"traceId": "trace-publish-failure"}),
        build_message(),
    )

    assert channel.acks == []
    assert channel.nacks == [(19, False)]


def test_task_consumer_declares_prefetch_count_one() -> None:
    """M1.14 enforces GPU-safe single-message consumer prefetch."""
    consumer = TaskConsumer(Settings(mq_consumer_enabled=False), FakeProducer())
    channel = FakeChannel()

    consumer.configure_channel(channel)

    assert channel.qos_calls == [1]


def test_task_message_rejects_idempotency_key_for_different_task_or_step() -> None:
    """The idempotency key must stay taskId plus original routing step."""
    with pytest.raises(ValueError, match="idempotencyKey"):
        TaskMessage(
            taskId="task-123",
            step="parse",
            payload={},
            idempotencyKey="task-123:extract.bs",
        )
