"""RabbitMQ producer for L3 progress events."""

from datetime import UTC, datetime
import json
from typing import Any

from app.core.config import Settings
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)
PROGRESS_EXCHANGE = "progress.exchange"


class ProgressProducer:
    """Publishes durable L3 processing progress to the fanout exchange."""

    def __init__(self, settings: Settings) -> None:
        """Initialize the producer without opening a broker connection.

        Args:
            settings: RabbitMQ connection configuration.
        """
        self.settings = settings
        self.connection: Any | None = None
        self.channel: Any | None = None

    def connect(self) -> None:
        """Open one synchronous RabbitMQ publisher connection."""
        if self.connection is not None and self.connection.is_open:
            return
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
        self.connection = pika.BlockingConnection(parameters)
        self.channel = self.connection.channel()

    def publish_progress(self, message: dict[str, Any], trace_id: str) -> None:
        """Publish a persistent progress message with trace and idempotency headers.

        A single reconnect-and-retry handles an idle Pika connection closed by the broker.
        The stable idempotency key allows downstream consumers to safely handle a duplicated
        delivery when the original publish outcome is unknown.

        Args:
            message: JSON-compatible progress body.
            trace_id: Upstream HTTP/MQ trace identifier.
        """
        import pika

        body = {**message, "timestamp": datetime.now(UTC).isoformat()}
        properties = pika.BasicProperties(
            content_type="application/json",
            delivery_mode=2,
            headers={
                "traceId": trace_id,
                "taskId": body["taskId"],
                "step": body["step"],
                "idempotencyKey": body["idempotencyKey"],
            },
        )
        try:
            self._publish(body, properties)
        except (pika.exceptions.AMQPError, OSError):
            LOGGER.warning("Progress publisher lost broker connection; retrying once")
            self._reset_connection()
            self._publish(body, properties)
        LOGGER.info(
            "Published progress taskId=%s step=%s", body["taskId"], body["step"]
        )

    def _publish(self, body: dict[str, Any], properties: Any) -> None:
        """Send one already-normalized progress payload through the active channel.

        Args:
            body: Progress payload including timestamp.
            properties: Pika persistence and header properties.
        """
        self.connect()
        if self.channel is None:
            raise RuntimeError("RabbitMQ channel is unavailable")
        self.channel.basic_publish(
            exchange=PROGRESS_EXCHANGE,
            routing_key="",
            body=json.dumps(body).encode("utf-8"),
            properties=properties,
        )

    def _reset_connection(self) -> None:
        """Discard a broken connection so the next publish opens a fresh channel."""
        if self.connection is not None and self.connection.is_open:
            self.connection.close()
        self.connection = None
        self.channel = None

    def close(self) -> None:
        """Close the publisher connection when the application stops."""
        self._reset_connection()
