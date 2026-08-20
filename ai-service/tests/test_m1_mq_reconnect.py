"""Regression tests for RabbitMQ reconnection in the M1 mock worker."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import Settings
from app.mq.consumer import TaskConsumer
from app.mq.producer import ProgressProducer
from app.schemas.task import TaskMessage


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


class FakeAckChannel:
    """Records ack/nack calls issued through the worker's threadsafe scheduling."""

    def __init__(self) -> None:
        self.acked: list[Any] = []
        self.nacked: list[tuple[Any, bool]] = []

    def basic_ack(self, delivery_tag: Any) -> None:
        """Record an acknowledgement."""
        self.acked.append(delivery_tag)

    def basic_nack(self, delivery_tag: Any, requeue: bool) -> None:
        """Record a negative acknowledgement."""
        self.nacked.append((delivery_tag, requeue))


class FakeThreadsafeConnection:
    """A connection whose threadsafe callback scheduler executes immediately."""

    def __init__(self) -> None:
        self.channel_instance = FakeAckChannel()
        self.is_open = True
        self.callbacks: list[Any] = []

    def add_callback_threadsafe(self, callback: Any) -> None:
        """Run the scheduled callback synchronously (single-threaded test)."""
        self.callbacks.append(callback)
        callback()


class RecordingProducer:
    """Records progress publish calls instead of touching a broker."""

    def __init__(self) -> None:
        self.published: list[tuple[dict[str, Any], str]] = []

    def publish_progress(self, message: dict[str, Any], trace_id: str) -> None:
        """Record the publish call."""
        self.published.append((message, trace_id))

    def close(self) -> None:
        """No-op close."""


def _valid_task_message(step: str = "parse") -> TaskMessage:
    """Build a schema-valid TaskMessage for handler dispatch tests."""
    return TaskMessage(
        taskId="task-worker",
        step=step,
        payload={"pdfKey": "uploads/x.pdf"},
        idempotencyKey=f"task-worker:{step}",
    )


def _consumer_with_recording_producer() -> tuple[TaskConsumer, RecordingProducer]:
    """Create a consumer with a stub producer for isolated _process tests."""
    producer = RecordingProducer()
    consumer = TaskConsumer(Settings(mq_consumer_enabled=False), producer)  # type: ignore[arg-type]
    consumer._loop = asyncio.new_event_loop()
    return consumer, producer


def test_on_message_dispatches_to_worker_queue() -> None:
    """A validated delivery must be enqueued, not executed on the I/O thread.

    M4.10 回归：parse handler 可阻塞 >7min，若在 I/O 线程同步执行则
    30s heartbeat 停发 → broker 断连 → 消息无限重投。on_message 只允许
    校验 + 入队，handler 由工作线程执行。
    """
    consumer, _ = _consumer_with_recording_producer()

    async def slow_handler(_: TaskMessage) -> dict[str, Any]:
        await asyncio.sleep(30)
        return {}

    consumer.handlers["parse"] = slow_handler
    channel = FakeAckChannel()
    method = SimpleNamespace(routing_key="parse", delivery_tag=7)
    properties = SimpleNamespace(headers={"traceId": "trace-worker"})

    consumer.on_message(
        channel,
        method,
        properties,
        _valid_task_message().model_dump_json(by_alias=True).encode(),
    )

    # 立即返回且未同步执行 handler：工作队列恰好一条待处理投递。
    assert consumer._work_queue.qsize() == 1
    assert channel.acked == [] and channel.nacked == []


def test_process_publishes_success_then_acks_threadsafe() -> None:
    """A successful handler must publish SUCCESS progress and schedule the ack."""
    consumer, producer = _consumer_with_recording_producer()

    async def ok_handler(_: TaskMessage) -> dict[str, Any]:
        return {"rows": 1}

    consumer.handlers["parse"] = ok_handler
    connection = FakeThreadsafeConnection()
    consumer.connection = connection
    task = _valid_task_message()
    method = SimpleNamespace(routing_key="parse", delivery_tag=3)

    consumer._process(connection.channel_instance, method, task, "PARSE", "trace-worker")

    assert producer.published[0][0]["status"] == "SUCCESS"
    assert producer.published[0][0]["idempotencyKey"] == "task-worker:PARSE"
    assert connection.channel_instance.acked == [3]


def test_process_publishes_failure_then_acks_threadsafe() -> None:
    """A failing handler must publish FAILED progress (L2 terminal failure) then ack."""
    consumer, producer = _consumer_with_recording_producer()

    async def failing_handler(_: TaskMessage) -> dict[str, Any]:
        raise RuntimeError("extraction exploded")

    consumer.handlers["parse"] = failing_handler
    connection = FakeThreadsafeConnection()
    consumer.connection = connection
    task = _valid_task_message()
    method = SimpleNamespace(routing_key="parse", delivery_tag=9)

    consumer._process(connection.channel_instance, method, task, "PARSE", "trace-worker")

    assert producer.published[0][0]["status"] == "FAILED"
    assert "extraction exploded" in producer.published[0][0]["result"]["error"]
    assert connection.channel_instance.acked == [9]


def test_process_nacks_to_dlq_when_progress_publish_fails() -> None:
    """When progress publish itself fails, the delivery must be DLQ-routed (no requeue)."""
    consumer, _ = _consumer_with_recording_producer()

    class FailingProducer(RecordingProducer):
        def publish_progress(self, message: dict[str, Any], trace_id: str) -> None:
            raise FakeAmqpError("publisher down")

    consumer.producer = FailingProducer()  # type: ignore[assignment]

    async def ok_handler(_: TaskMessage) -> dict[str, Any]:
        return {"rows": 1}

    consumer.handlers["parse"] = ok_handler
    connection = FakeThreadsafeConnection()
    consumer.connection = connection
    method = SimpleNamespace(routing_key="parse", delivery_tag=5)

    consumer._process(
        connection.channel_instance, method, _valid_task_message(), "PARSE", "t"
    )

    assert connection.channel_instance.nacked == [(5, False)]


def test_process_skips_ack_when_connection_already_lost() -> None:
    """A dead connection means redelivery; no threadsafe scheduling must be attempted."""
    consumer, _ = _consumer_with_recording_producer()

    async def ok_handler(_: TaskMessage) -> dict[str, Any]:
        return {"rows": 1}

    consumer.handlers["parse"] = ok_handler
    connection = FakeThreadsafeConnection()
    connection.is_open = False
    consumer.connection = connection
    method = SimpleNamespace(routing_key="parse", delivery_tag=11)

    consumer._process(
        connection.channel_instance, method, _valid_task_message(), "PARSE", "t"
    )

    assert connection.callbacks == []
    assert connection.channel_instance.acked == []
