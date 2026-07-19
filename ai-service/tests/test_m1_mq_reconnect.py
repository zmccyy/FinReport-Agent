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
    """Fake publisher channel that can reject its first publish."""

    def __init__(self, should_fail: bool) -> None:
        self.should_fail = should_fail
        self.published: list[dict[str, Any]] = []

    def basic_publish(self, **kwargs: Any) -> None:
        """Raise a transport error once, then record the durable message."""
        if self.should_fail:
            raise FakeAmqpError("publisher transport dropped")
        self.published.append(kwargs)


class FakePublisherConnection:
    """Minimal publisher connection facade."""

    def __init__(self, should_fail: bool) -> None:
        self.channel_instance = FakePublisherChannel(should_fail)
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
        exceptions=SimpleNamespace(AMQPError=FakeAmqpError),
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
    connections = [
        FakePublisherConnection(should_fail=True),
        FakePublisherConnection(should_fail=False),
    ]

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
