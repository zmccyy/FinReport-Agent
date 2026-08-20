"""RabbitMQ task consumer for the L3 processing chain (M4 起全链路真实实现).

路由：parse → parser handler；extract.{bs,is,cf} → extractor handler
（DeepSeek json_mode 抽取）；check → reasoner handler（勾稽/异常/复核）；
report → generator handler（报告/图表/PDF）。数据通路见 spec §4.4：
parse 产物走 MinIO，check/report 只读 MySQL，进度经 ProgressProducer
回报 L2。
"""

import asyncio
import queue
from threading import Event, Thread
from typing import Any, Awaitable, Callable

from app.core.config import Settings
from app.schemas.task import TaskMessage
from app.modules.extractor.handler import handle as extract_handler
from app.modules.generator.handler import handle as generator_handler
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
#: 消费者 I/O 线程转交工作线程的一条已验证投递。
WorkItem = tuple[Any, Any, TaskMessage, str, str]


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
            "report": generator_handler,
        }
        self.stop_event = Event()
        self.thread: Thread | None = None
        self.connection: Any | None = None
        # 工作线程队列：I/O 线程只负责收包/心跳/ack，handler 转交工作线程
        # 执行（M4.10 修复：parse 单条 handler 可阻塞 >7min，若在 I/O 线程
        # 同步执行，30s heartbeat 停发 → broker 断连 → ack 丢失 → 消息无限
        # 重投；拆线程后心跳持续流动）。
        self._work_queue: queue.Queue[WorkItem] = queue.Queue()
        self._worker_thread: Thread | None = None
        # 工作线程内复用的事件循环——避免每条消息 asyncio.run() 反复
        # 创建/销毁 loop（含 to_thread 线程池无法跨消息复用）。
        self._loop: asyncio.AbstractEventLoop | None = None

    def configure_channel(self, channel: Any) -> None:
        """Apply the fixed single-message prefetch policy.

        Args:
            channel: Pika channel object.
        """
        channel.basic_qos(prefetch_count=PREFETCH_COUNT)

    def start(self) -> None:
        """Start the broker loop and worker thread when enabled."""
        if not self.settings.mq_consumer_enabled or self.thread is not None:
            return
        self._worker_thread = Thread(
            target=self._process_loop, name="finreport-mq-worker", daemon=True
        )
        self._worker_thread.start()
        self.thread = Thread(target=self._consume, name="finreport-mq-consumer", daemon=True)
        self.thread.start()

    def _consume(self) -> None:
        """Reconnect and consume all M1 task queues until shutdown.

        This thread only pumps AMQP I/O (heartbeats included). Handlers run on
        the worker thread so a long parse never starves heartbeats; see
        ``_process_loop``.
        """
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
                    LOGGER.exception("M1 task consumer lost broker connection; reconnecting")
                    self.stop_event.wait(self.settings.rabbitmq_reconnect_delay_seconds)
            finally:
                if self.connection is not None and self.connection.is_open:
                    self.connection.close()
                self.connection = None

    def _process_loop(self) -> None:
        """Worker thread: execute validated deliveries with a reused event loop.

        Runs until shutdown; each queued item executes its handler to terminal
        progress, then schedules the acknowledgement back onto the consumer
        I/O thread via ``add_callback_threadsafe``.
        """
        self._loop = asyncio.new_event_loop()
        try:
            while not self.stop_event.is_set():
                try:
                    item = self._work_queue.get(timeout=1)
                except queue.Empty:
                    continue
                channel, method, task, step_name, trace_id = item
                self._process(channel, method, task, step_name, trace_id)
        finally:
            self._loop.close()
            self._loop = None

    def _process(
        self,
        channel: Any,
        method: Any,
        task: TaskMessage,
        step_name: str,
        trace_id: str,
    ) -> None:
        """Run one validated delivery to terminal progress and schedule its ack.

        Args:
            channel: Consumer channel the delivery arrived on (ack target).
            method: Delivery metadata (routing key, delivery tag).
            task: Validated input task.
            step_name: L2 step name (e.g. ``PARSE``).
            trace_id: Correlation identifier from delivery headers.
        """
        handler = self.handlers[method.routing_key]
        assert self._loop is not None
        try:
            result = self._loop.run_until_complete(handler(task))
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
                self._nack_threadsafe(channel, method.delivery_tag)
                return
        else:
            try:
                self._publish_progress(task, step_name, "SUCCESS", result, trace_id)
            except Exception:
                LOGGER.exception(
                    "Failed to publish task success progress routingKey=%s",
                    method.routing_key,
                )
                self._nack_threadsafe(channel, method.delivery_tag)
                return
        self._ack_threadsafe(channel, method.delivery_tag)

    def on_message(self, channel: Any, method: Any, properties: Any, body: bytes) -> None:
        """Validate one delivery and hand it to the worker thread.

        Malformed deliveries cannot be correlated safely and therefore go directly to the DLQ.
        Valid deliveries are enqueued for ``_process``; the I/O thread stays free to pump
        heartbeats during long handlers.

        Args:
            channel: Pika channel used for acknowledgement.
            method: Delivery metadata containing routing key and tag.
            properties: AMQP properties containing traceId.
            body: Serialized task JSON.
        """
        trace_id = str((getattr(properties, "headers", None) or {}).get("traceId", ""))
        try:
            task = TaskMessage.model_validate_json(body)
            if method.routing_key not in self.handlers:
                raise ValueError(f"Unsupported routing key: {method.routing_key}")
            if task.step != method.routing_key:
                raise ValueError("Message step does not match delivery routing key")
            step_name = STEP_NAMES[task.step]
        except Exception:
            LOGGER.exception("Invalid task delivery routingKey=%s", method.routing_key)
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return
        self._work_queue.put((channel, method, task, step_name, trace_id))

    def _ack_threadsafe(self, channel: Any, delivery_tag: Any) -> None:
        """Schedule ``basic_ack`` on the consumer I/O thread.

        Pika channels are not thread-safe; the callback runs inside the I/O
        thread's next ``process_data_events`` cycle. When the connection has
        died in the meantime, the broker redelivers the message and the task
        is reprocessed — progress idempotency keys make that safe.
        """
        self._schedule_threadsafe(channel, lambda: channel.basic_ack(delivery_tag=delivery_tag))

    def _nack_threadsafe(self, channel: Any, delivery_tag: Any) -> None:
        """Schedule ``basic_nack(requeue=False)`` (DLQ routing) on the I/O thread."""
        self._schedule_threadsafe(
            channel, lambda: channel.basic_nack(delivery_tag=delivery_tag, requeue=False)
        )

    def _schedule_threadsafe(self, channel: Any, operation: Callable[[], Any]) -> None:
        """Run ``operation`` on the consumer I/O thread, swallowing late failures.

        Args:
            channel: The channel whose lifetime guards the operation.
            operation: A zero-argument callback issuing one channel operation.
        """
        connection = self.connection
        if connection is None or not connection.is_open:
            LOGGER.warning(
                "Broker connection lost before ack; delivery will be redelivered"
            )
            return

        def guarded() -> None:
            try:
                operation()
            except Exception:
                LOGGER.warning(
                    "Channel operation failed after reconnect; delivery will be redelivered"
                )

        try:
            connection.add_callback_threadsafe(guarded)
        except Exception:
            LOGGER.warning("Could not schedule channel operation; delivery will be redelivered")

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
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=5)
        self.producer.close()
