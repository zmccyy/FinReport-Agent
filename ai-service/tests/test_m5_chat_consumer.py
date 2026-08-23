"""M5.05 L3 ChatConsumer 单测：合法消息 ack、非法消息 nack 进 DLQ。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.core.config import Settings
from app.mq.chat_consumer import ChatConsumer


def _settings() -> Settings:
    return Settings(mq_consumer_enabled=False, rabbitmq_host="unused")


def _properties(trace_id: str = "trace-1") -> SimpleNamespace:
    return SimpleNamespace(headers={"traceId": trace_id})


def _method(tag: int = 1) -> SimpleNamespace:
    return SimpleNamespace(delivery_tag=tag)


class FakeChannel:
    """记录 ack/nack 的假 channel。"""

    def __init__(self) -> None:
        self.acks: list[int] = []
        self.nacks: list[tuple[int, bool]] = []

    def basic_ack(self, delivery_tag: int) -> None:
        self.acks.append(delivery_tag)

    def basic_nack(self, delivery_tag: int, requeue: bool) -> None:
        self.nacks.append((delivery_tag, requeue))


def _valid_body() -> bytes:
    return json.dumps(
        {
            "sessionId": "1",
            "messageId": "m-1",
            "reportId": 17,
            "question": "营收多少？",
            "companyContext": "贵州茅台（600519）",
            "timestamp": "2026-08-23T00:00:00Z",
        }
    ).encode("utf-8")


def test_valid_message_acknowledged() -> None:
    """合法控制流消息 → ack（不 nack）。"""
    channel = FakeChannel()
    consumer = ChatConsumer(_settings())
    consumer.on_message(channel, _method(1), _properties("trace-1"), _valid_body())

    assert channel.acks == [1]
    assert channel.nacks == []


def test_malformed_message_nacked_to_dlq() -> None:
    """缺关键字段 → nack(requeue=False) 进 DLQ。"""
    channel = FakeChannel()
    consumer = ChatConsumer(_settings())
    body = json.dumps({"sessionId": "1"}).encode("utf-8")
    consumer.on_message(channel, _method(2), _properties(), body)

    assert channel.acks == []
    assert channel.nacks == [(2, False)]


def test_invalid_json_nacked_to_dlq() -> None:
    """非 JSON 消息体 → nack(requeue=False) 进 DLQ。"""
    channel = FakeChannel()
    consumer = ChatConsumer(_settings())
    consumer.on_message(channel, _method(3), _properties(), b"not-json{{")

    assert channel.acks == []
    assert channel.nacks == [(3, False)]
