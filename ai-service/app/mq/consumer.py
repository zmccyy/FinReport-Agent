"""RabbitMQ task consumer for the M1 mock processing chain."""

import asyncio
from threading import Event, Thread
from typing import Any, Awaitable, Callable

from app.core.config import Settings
from app.schemas.task import TaskMessage
from app.modules.extractor.handler import handle as extract_handler
from app.modules.parser.handler import handle as parse_handler
from app.modules.reasoner.handler import handle as reason_handler
from app.mq.producer import ProgressProducer
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)
PREFETCH_COUNT = 1
TASK_QUEUES = ("q.parse.requests", "q.extract.requests", "q.reason.requests")
STEP_NAMES = {
    "parse": "PARSE",
    "extract.bs": "EXTRACT_BS",
    "extract.is": "EXTRACT_IS",
    "extract.cf": "EXTRACT_CF",
    "check": "CHECK",
    "report": "REPORT",
}
STEP_PROGRESS = {
    "PARSE": 15,
    "EXTRACT_BS": 30,
    "EXTRACT_IS": 40,
    "EXTRACT_CF": 55,
    "CHECK": 75,
    "REPORT": 100,
}
Handler = Callable[[TaskMessage], Awaitable[dict[str, Any]]]


class TaskConsumer:
    """Consumes M1 task queues with manual acknowledgements and DLQ routing."""

    def __init__(self, settings: Settings, producer: ProgressProducer) -> None:
        """Create a broker consumer.

        Args:
            settings: RabbitMQ connection configuration.
            producer: Producer used to emit successful progress.
        """
        self.settings = settings
        self.producer = producer
        self.handlers: dict[str, Handler] = {
            "parse": parse_handler,
            "extract.bs": extract_handler,
            "extract.is": extract_handler,
            "extract.cf": extract_handler,
            "check": reason_handler,
            "report": reason_handler,
        }
        self.stop_event = Event()
        self.thread: Thread | None = None
        self.connection: Any | None = None

    def configure_channel(self, channel: Any) -> None:
        """Apply the fixed single-message prefetch policy.

        Args:
            channel: Pika channel object.
        """
        channel.basic_qos(prefetch_count=PREFETCH_COUNT)

    def start(self) -> None:
        """Start the broker loop on a daemon thread when enabled."""
        if not self.settings.mq_consumer_enabled or self.thread is not None:
            return
        self.thread = Thread(
            target=self._consume, name="finreport-mq-consumer", daemon=True
        )
        self.thread.start()

    def _consume(self) -> None:
        """Reconnect and consume all M1 task queues until shutdown."""
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
                for queue in TASK_QUEUES:
                    channel.basic_consume(
                        queue=queue, on_message_callback=self.on_message, auto_ack=False
                    )
                LOGGER.info("M1 task consumer started queues=%s", TASK_QUEUES)
                while not self.stop_event.is_set():
                    self.connection.process_data_events(time_limit=1)
            except Exception:
                if not self.stop_event.is_set():
                    LOGGER.exception(
                        "M1 task consumer lost broker connection; reconnecting"
                    )
                    self.stop_event.wait(self.settings.rabbitmq_reconnect_delay_seconds)
            finally:
                if self.connection is not None and self.connection.is_open:
                    self.connection.close()
                self.connection = None

    def on_message(
        self, channel: Any, method: Any, properties: Any, body: bytes
    ) -> None:
        """Process one delivery and acknowledge it only after its terminal progress is durable.

        Malformed deliveries cannot be correlated safely and therefore go directly to the DLQ.
        Handler failures are correlated task failures: they first publish a durable ``FAILED``
        progress event so L2 can retry or terminally fail the task, then acknowledge the input.

        Args:
            channel: Pika channel used for acknowledgement.
            method: Delivery metadata containing routing key and tag.
            properties: AMQP properties containing traceId.
            body: Serialized task JSON.
        """
        trace_id = str((getattr(properties, "headers", None) or {}).get("traceId", ""))
        try:
            task = TaskMessage.model_validate_json(body)
            handler = self.handlers.get(method.routing_key)
            if handler is None:
                raise ValueError(f"Unsupported routing key: {method.routing_key}")
            if task.step != method.routing_key:
                raise ValueError("Message step does not match delivery routing key")
            step_name = STEP_NAMES[task.step]
        except Exception:
            LOGGER.exception("Invalid task delivery routingKey=%s", method.routing_key)
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        try:
            result = asyncio.run(handler(task))
        except Exception as error:
            LOGGER.exception("Task handler failed routingKey=%s", method.routing_key)
            try:
                self._publish_progress(
                    task,
                    step_name,
                    "FAILED",
                    {"error": str(error)},
                    trace_id,
                )
            except Exception:
                LOGGER.exception(
                    "Failed to publish task failure progress routingKey=%s",
                    method.routing_key,
                )
                channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                return
            channel.basic_ack(delivery_tag=method.delivery_tag)
            return

        try:
            self._publish_progress(task, step_name, "SUCCESS", result, trace_id)
        except Exception:
            LOGGER.exception(
                "Failed to publish task success progress routingKey=%s",
                method.routing_key,
            )
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return
        channel.basic_ack(delivery_tag=method.delivery_tag)

    def _publish_progress(
        self,
        task: TaskMessage,
        step_name: str,
        status: str,
        result: dict[str, Any],
        trace_id: str,
    ) -> None:
        """Publish one terminal step progress event through the confirmed producer.

        Args:
            task: Validated input task.
            step_name: L2 step name corresponding to the routing key.
            status: Terminal step status, either ``SUCCESS`` or ``FAILED``.
            result: Handler result or failure details.
            trace_id: Correlation identifier propagated from the input delivery.
        """
        self.producer.publish_progress(
            {
                "taskId": task.task_id,
                "step": step_name,
                "status": status,
                "progress": STEP_PROGRESS[step_name],
                "result": result,
                "idempotencyKey": f"{task.task_id}:{step_name}",
            },
            trace_id,
        )

    def stop(self) -> None:
        """Stop consumption and close broker connections."""
        self.stop_event.set()
        if self.connection is not None and self.connection.is_open:
            self.connection.add_callback_threadsafe(self.connection.close)
        if self.thread is not None:
            self.thread.join(timeout=5)
        self.producer.close()
