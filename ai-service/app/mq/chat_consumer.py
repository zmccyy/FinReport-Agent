"""RabbitMQ 问答控制流消费者 — spec §3.3 / M5.05。

问答链路数据面走 L2 → L3 HTTP SSE 拉流（/internal/chat/stream），
控制面（L2 发布的会话请求）落本队列记账：本消费者校验消息、记录
INFO 日志（会话/消息/报表/问题），并 ack。消息体含
``idempotency_key = messageId``；格式错误 nack(requeue=False) 进 DLQ。
"""

from __future__ import annotations

import json
from threading import Event, Thread
from typing import Any

from app.core.config import Settings
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)
PREFETCH_COUNT = 1
CHAT_QUEUE = "q.chat.requests"

#: 校验通过后记录的日志字段（不记录完整问题，控制日志体积）。
_REQUIRED_FIELDS = ("sessionId", "messageId", "reportId", "question")


class ChatConsumer:
    """Consumes the chat control-flow queue with manual acks and DLQ routing."""

    def __init__(self, settings: Settings) -> None:
        """Create a broker consumer for q.chat.requests.

        Args:
            settings: RabbitMQ connection configuration.
        """
        self.settings = settings
        self.stop_event = Event()
        self.thread: Thread | None = None
        self.connection: Any | None = None

    def configure_channel(self, channel: Any) -> None:
        """Apply the fixed single-message prefetch policy."""
        channel.basic_qos(prefetch_count=PREFETCH_COUNT)

    def start(self) -> None:
        """Start the broker loop when enabled."""
        if not self.settings.mq_consumer_enabled or self.thread is not None:
            return
        self.thread = Thread(
            target=self._consume, name="finreport-chat-consumer", daemon=True
        )
        self.thread.start()

    def _consume(self) -> None:
        """Reconnect and consume q.chat.requests until shutdown."""
        import pika

        credentials = pika.PlainCredentials(
            self.settings.rabbitmq_user, self.settings.rabbitmq_pass
        )
        parameters = pika.ConnectionParameters(
            host=self.settings.rabbitmq_host,
            port=self.settings.rabbitmq_port,
            virtual_host=self.settings.rabbitmq_vhost,
            credentials=credentials,
            heartbeat=self.settings.rabbitmq_heartbeat,
        )
        while not self.stop_event.is_set():
            try:
                self.connection = pika.BlockingConnection(parameters)
                channel = self.connection.channel()
                self.configure_channel(channel)
                channel.basic_consume(
                    queue=CHAT_QUEUE,
                    on_message_callback=self.on_message,
                    auto_ack=False,
                )
                LOGGER.info("Chat consumer started queue=%s", CHAT_QUEUE)
                while not self.stop_event.is_set():
                    self.connection.process_data_events(time_limit=1)
            except Exception:
                if not self.stop_event.is_set():
                    LOGGER.exception(
                        "Chat consumer lost broker connection; reconnecting"
                    )
                    self.stop_event.wait(self.settings.rabbitmq_reconnect_delay_seconds)
            finally:
                if self.connection is not None and self.connection.is_open:
                    self.connection.close()
                self.connection = None

    def on_message(
        self, channel: Any, method: Any, properties: Any, body: bytes
    ) -> None:
        """Validate one chat control message, log the receipt, and ack.

        Args:
            channel: Pika channel used for acknowledgement.
            method: Delivery metadata (delivery tag).
            properties: AMQP properties containing traceId / idempotencyKey.
            body: Serialized chat control message JSON.
        """
        trace_id = str((getattr(properties, "headers", None) or {}).get("traceId", ""))
        try:
            payload = json.loads(body.decode("utf-8"))
            if not all(payload.get(field) for field in _REQUIRED_FIELDS):
                raise ValueError("chat control message missing required fields")
            LOGGER.info(
                "Chat control receipt sessionId=%s messageId=%s reportId=%s questionLength=%d traceId=%s",
                payload["sessionId"],
                payload["messageId"],
                payload["reportId"],
                len(str(payload["question"])),
                trace_id,
            )
            channel.basic_ack(delivery_tag=method.delivery_tag)
        except Exception:
            LOGGER.exception("Invalid chat control delivery")
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    def stop(self) -> None:
        """Stop consumption and close broker connections."""
        self.stop_event.set()
        if self.connection is not None and self.connection.is_open:
            self.connection.add_callback_threadsafe(self.connection.close)
        if self.thread is not None:
            self.thread.join(timeout=5)
