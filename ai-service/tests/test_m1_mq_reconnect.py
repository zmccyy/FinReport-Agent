"""Regression tests for RabbitMQ reconnection in the M1 mock worker."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import Settings
from app.mq.consumer import TaskConsumer
from app.mq.producer import ProgressProducer


class FakeAmqpError(Exception):
    """Stand-in for a Pika transport failure."""


class FakeConsumerChannel:
    """Minimal consumer channel used to record setup calls."""

    def __init__(self) -> None:
        self.qos_calls: list[int] = []
        self.consumed_queues: list[str] = []

    def basic_qos(self, prefetch_count: int) -> None:
        """Record prefetch configuration."""
        self.qos_calls.append(prefetch_count)

    def basic_consume(self, queue: str, **_: Any) -> None:
        """Record queue registration."""
        self.consumed_queues.append(queue)


class FakeConsumerConnection:
    """A connection that fails once or stops the consumer after reconnection."""

    def __init__(self, consumer: TaskConsumer, should_fail: bool) -> None:
        self.channel_instance = FakeConsumerChannel()
        self.consumer = consumer
        self.should_fail = should_fail
        self.is_open = True
        self.closed = False

    def channel(self) -> FakeConsumerChannel:
        """Return the fake AMQP channel."""
        return self.channel_instance

    def process_data_events(self, time_limit: int) -> None:
        """Simulate a lost connection followed by a clean shutdown."""
        assert time_limit == 1
        if self.should_fail:
            self.is_open = False
            raise FakeAmqpError("consumer transport dropped")
        self.consumer.stop_event.set()

    def close(self) -> None:
        """Close the fake connection."""
        self.is_open = False
        self.closed = True


class FakePublisherChannel:
    """Fake publisher channel that can fail or negatively confirm a publish."""

    def __init__(self, should_fail: bool, publish_result: bool | None = None) -> None:
        self.should_fail = should_fail
        self.publish_result = publish_result
        self.confirm_calls = 0
        self.published: list[dict[str, Any]] = []

    def confirm_delivery(self) -> None:
        """Record publisher-confirm activation."""
        self.confirm_calls += 1

    def basic_publish(self, **kwargs: Any) -> bool | None:
        """Raise a transport error or return the configured broker confirmation result."""
        if self.should_fail:
            raise FakeAmqpError("publisher transport dropped")
        self.published.append(kwargs)
        return self.publish_result


class FakePublisherConnection:
    """Minimal publisher connection facade."""

    def __init__(self, should_fail: bool, publish_result: bool | None = None) -> None:
        self.channel_instance = FakePublisherChannel(should_fail, publish_result)
        self.is_open = True
        self.closed = False

    def channel(self) -> FakePublisherChannel:
        """Return the fake publisher channel."""
        return self.channel_instance

    def close(self) -> None:
        """Close the fake publisher connection."""
        self.is_open = False
        self.closed = True


def install_fake_pika(monkeypatch: pytest.MonkeyPatch, connection_factory: Any) -> None:
    """Install a minimal Pika module with a deterministic connection factory."""
    fake_pika = SimpleNamespace(
        PlainCredentials=lambda *_: object(),
        ConnectionParameters=lambda **_: object(),
        BlockingConnection=connection_factory,
        BasicProperties=lambda **kwargs: kwargs,
        exceptions=SimpleNamespace(AMQPError=FakeAmqpError, NackError=FakeAmqpError),
    )
    monkeypatch.setitem(sys.modules, "pika", fake_pika)


def test_task_consumer_reconnects_after_a_broker_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost AMQP consumer connection must not permanently stop the worker."""
    consumer = TaskConsumer(
        Settings(mq_consumer_enabled=False), ProgressProducer(Settings())
    )
    connections = [
        FakeConsumerConnection(consumer, should_fail=True),
        FakeConsumerConnection(consumer, should_fail=False),
    ]

    def connect(_: Any) -> FakeConsumerConnection:
        """Return the failing then recovered fake connection."""
        return connections.pop(0)

    install_fake_pika(monkeypatch, connect)

    consumer._consume()

    assert connections == []


def test_progress_producer_reconnects_once_after_a_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale idle publisher connection must be replaced before progress is lost."""
    producer = ProgressProducer(Settings())
    first_connection = FakePublisherConnection(should_fail=True)
    recovered_connection = FakePublisherConnection(should_fail=False)
    connections = [first_connection, recovered_connection]

    def connect(_: Any) -> FakePublisherConnection:
        """Return the failed connection followed by the replacement."""
        return connections.pop(0)

    install_fake_pika(monkeypatch, connect)

    producer.publish_progress(
        {
            "taskId": "task-reconnect",
            "step": "PARSE",
            "status": "SUCCESS",
            "progress": 15,
            "result": {},
            "idempotencyKey": "task-reconnect:PARSE",
        },
        "trace-reconnect",
    )

    assert connections == []
    assert first_connection.channel_instance.confirm_calls == 1
    assert recovered_connection.channel_instance.confirm_calls == 1
    assert recovered_connection.channel_instance.published[0]["mandatory"] is True


def test_progress_producer_rejects_unconfirmed_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broker negative confirmation must not be reported as a published progress event."""
    producer = ProgressProducer(Settings())
    first_connection = FakePublisherConnection(should_fail=False, publish_result=False)
    retry_connection = FakePublisherConnection(should_fail=False, publish_result=False)
    connections = [first_connection, retry_connection]

    def connect(_: Any) -> FakePublisherConnection:
        """Return two broker channels that both negatively confirm the publish."""
        return connections.pop(0)

    install_fake_pika(monkeypatch, connect)

    with pytest.raises(FakeAmqpError):
        producer.publish_progress(
            {
                "taskId": "task-unconfirmed",
                "step": "PARSE",
                "status": "SUCCESS",
                "progress": 15,
                "result": {},
                "idempotencyKey": "task-unconfirmed:PARSE",
            },
            "trace-unconfirmed",
        )

    assert connections == []
    assert first_connection.channel_instance.confirm_calls == 1
    assert retry_connection.channel_instance.confirm_calls == 1
    assert first_connection.channel_instance.published[0]["mandatory"] is True
    assert retry_connection.channel_instance.published[0]["mandatory"] is True
