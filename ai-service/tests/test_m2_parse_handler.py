"""Unit tests for the M2 parse MQ handler and MinIO client."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.core.config import Settings
from app.core.exceptions import AiException
from app.core.minio_client import MinioObjectClient
from app.modules.parser.document_parser import DocumentParser
from app.modules.parser.handler import configure_handler, handle, reset_handler
from app.mq.consumer import TaskConsumer
from app.schemas.document import Document, Page
from app.schemas.task import TaskMessage


class FakeObjectStore:
    """In-memory object store used to mock MinIO fetches and puts."""

    def __init__(
        self,
        *,
        data: bytes | None = None,
        error: Exception | None = None,
        put_error: Exception | None = None,
        empty: bool = False,
    ) -> None:
        self.data = b"" if empty else (data or b"%PDF-mock")
        self.error = error
        self.put_error = put_error
        self.requests: list[tuple[str, str | None]] = []
        self.puts: list[tuple[bytes, str, str | None, str]] = []

    def fetch_bytes(self, object_key: str, bucket: str | None = None) -> bytes:
        """Return configured bytes or raise the configured error."""
        self.requests.append((object_key, bucket))
        if self.error is not None:
            raise self.error
        return self.data

    def put_bytes(
        self,
        data: bytes,
        object_key: str,
        bucket: str | None = None,
        content_type: str = "application/octet-stream",
    ) -> None:
        """Record the upload or raise the configured error (M4.03)."""
        if self.put_error is not None:
            raise self.put_error
        self.puts.append((data, object_key, bucket, content_type))


class FakeChannel:
    """Captures RabbitMQ acknowledgement calls without a broker."""

    def __init__(self) -> None:
        self.acks: list[int] = []
        self.nacks: list[tuple[int, bool]] = []

    def basic_ack(self, delivery_tag: int) -> None:
        """Record a positive acknowledgement."""
        self.acks.append(delivery_tag)

    def basic_nack(self, delivery_tag: int, requeue: bool) -> None:
        """Record a negative acknowledgement."""
        self.nacks.append((delivery_tag, requeue))


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


class FakeConnection:
    """Simulates the pika connection for threadsafe ack scheduling."""

    is_open = True

    def add_callback_threadsafe(self, callback: Any) -> None:
        """Execute the scheduled channel operation synchronously."""
        callback()


def _run_queued_delivery(consumer: TaskConsumer) -> None:
    """Execute the single queued delivery to its terminal progress.

    M4.10 双线程模型：``on_message`` 只负责校验 + 入队，handler 与 ack
    由工作线程执行。单测将队列中的唯一投递取出、内联执行 ``_process``
    （配假 connection 同步调度 ack），使断言可见终态进度与 ack。
    """
    consumer.connection = FakeConnection()
    consumer._loop = asyncio.new_event_loop()
    channel, method, task, step_name, trace_id = consumer._work_queue.get_nowait()
    try:
        consumer._process(channel, method, task, step_name, trace_id)
    finally:
        consumer._loop.close()
        consumer._loop = None


class FakeParser(DocumentParser):
    """DocumentParser stub that returns a deterministic Document."""

    def __init__(self, document: Document) -> None:
        super().__init__()
        self.document = document
        self.calls: list[tuple[bytes, str]] = []

    def parse_bytes(self, pdf_bytes: bytes, source: str) -> Document:
        """Record inputs and return the configured document."""
        self.calls.append((pdf_bytes, source))
        return self.document


@pytest.fixture(autouse=True)
def _reset_parse_handler() -> None:
    """Ensure each test starts with default handler wiring."""
    reset_handler()
    yield
    reset_handler()


def build_task_message(
    *,
    task_id: str = "task-parse",
    object_key: str = "uploads/1/demo.pdf",
    payload: dict[str, Any] | None = None,
) -> TaskMessage:
    """Build a valid parse TaskMessage."""
    body = payload if payload is not None else {"pdfObjectKey": object_key}
    return TaskMessage(
        taskId=task_id,
        step="parse",
        payload=body,
        idempotencyKey=f"{task_id}:parse",
    )


def test_handle_fetches_pdf_and_returns_real_counts(text_pdf_bytes: bytes) -> None:
    """The handler must fetch MinIO bytes and expose page/table counts."""
    document = Document(
        source="uploads/1/demo.pdf",
        page_count=2,
        pages=[
            Page(page_index=0, width=595, height=842, table_blocks=[]),
            Page(page_index=1, width=595, height=842, table_blocks=[]),
        ],
    )
    store = FakeObjectStore(data=text_pdf_bytes)
    parser = FakeParser(document)
    configure_handler(parser=parser, object_store=store)

    result = asyncio.run(handle(build_task_message()))

    assert store.requests == [("uploads/1/demo.pdf", None)]
    assert parser.calls == [(text_pdf_bytes, "uploads/1/demo.pdf")]
    assert result["document"]["source"] == "uploads/1/demo.pdf"
    assert result["document"]["pageCount"] == 2
    assert result["document"]["tableCount"] == 0
    assert result["document"]["extra"]["page_count"] == 2


def test_parsed_object_key_format() -> None:
    """M4.03: parse artifacts use the parsed/{taskId}.json key convention."""
    from app.modules.parser.handler import parsed_object_key

    assert parsed_object_key("task-abc") == "parsed/task-abc.json"


def test_handle_uploads_parse_artifact_to_minio(text_pdf_bytes: bytes) -> None:
    """M4.03: the parsed Document payload must be persisted to MinIO."""
    document = Document(
        source="uploads/1/demo.pdf",
        page_count=1,
        pages=[Page(page_index=0, width=595, height=842)],
    )
    store = FakeObjectStore(data=text_pdf_bytes)
    configure_handler(parser=FakeParser(document), object_store=store)

    asyncio.run(handle(build_task_message(task_id="task-artifact")))

    assert len(store.puts) == 1
    payload_bytes, key, bucket, content_type = store.puts[0]
    assert key == "parsed/task-artifact.json"
    assert bucket is None  # 默认 finreport-artifacts
    assert content_type == "application/json"
    parsed = json.loads(payload_bytes.decode("utf-8"))
    assert parsed["source"] == "uploads/1/demo.pdf"
    assert parsed["page_count"] == 1


def test_handle_raises_when_artifact_put_fails(text_pdf_bytes: bytes) -> None:
    """M4.03: artifact upload failure must fail the step (retry via DLQ)."""
    document = Document(source="x", page_count=1, pages=[])
    store = FakeObjectStore(data=text_pdf_bytes, put_error=AiException("put failed"))
    configure_handler(parser=FakeParser(document), object_store=store)

    with pytest.raises(AiException, match="put failed"):
        asyncio.run(handle(build_task_message()))


def test_handle_raises_when_pdf_object_key_missing() -> None:
    """Missing pdfObjectKey must fail fast before touching MinIO."""
    with pytest.raises(AiException, match="Missing pdfObjectKey"):
        asyncio.run(handle(build_task_message(payload={})))


def test_handle_raises_when_minio_unavailable() -> None:
    """MinIO failures must surface as AiException for DLQ routing."""
    configure_handler(
        parser=FakeParser(Document(source="x", page_count=1, pages=[])),
        object_store=FakeObjectStore(error=AiException("MinIO down")),
    )

    with pytest.raises(AiException, match="MinIO down"):
        asyncio.run(handle(build_task_message()))


def test_minio_client_wraps_transport_errors() -> None:
    """Low-level SDK failures must be converted to AiException."""

    class BrokenClient:
        def get_object(self, bucket: str, key: str) -> Any:
            del bucket, key
            raise OSError("connection refused")

    client = MinioObjectClient(Settings(), client=BrokenClient())

    with pytest.raises(AiException, match="MinIO fetch failed"):
        client.fetch_bytes("uploads/1/report.pdf")


def test_minio_client_put_bytes_uploads_to_artifact_bucket() -> None:
    """M4.03: put_bytes uploads via the SDK to the artifact bucket by default."""

    class RecordingClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, int, str]] = []

        def put_object(
            self, bucket: str, key: str, stream: Any, length: int, content_type: str
        ) -> None:
            data = stream.read()
            self.calls.append((bucket, key, len(data), content_type))

    sdk = RecordingClient()
    client = MinioObjectClient(Settings(), client=sdk)

    client.put_bytes(
        b'{"page_count": 1}', "parsed/t1.json", content_type="application/json"
    )

    assert sdk.calls == [
        ("finreport-artifacts", "parsed/t1.json", 17, "application/json")
    ]


def test_minio_client_put_bytes_wraps_errors_and_rejects_empty() -> None:
    """M4.03: put failures convert to AiException; empty payloads rejected."""

    class BrokenClient:
        def put_object(
            self, bucket: str, key: str, stream: Any, length: int, content_type: str
        ) -> None:
            del bucket, key, stream, length, content_type
            raise OSError("quota exceeded")

    client = MinioObjectClient(Settings(), client=BrokenClient())

    with pytest.raises(AiException, match="MinIO put failed"):
        client.put_bytes(b"x", "parsed/t2.json")

    with pytest.raises(AiException, match="empty object"):
        client.put_bytes(b"", "parsed/t3.json")


def test_minio_client_rejects_empty_object() -> None:
    """An empty object must not be treated as a valid PDF."""

    class EmptyClient:
        def get_object(self, bucket: str, key: str) -> Any:
            del bucket, key

            class Response:
                def read(self) -> bytes:
                    return b""

                def close(self) -> None:
                    return None

                def release_conn(self) -> None:
                    return None

            return Response()

    client = MinioObjectClient(Settings(), client=EmptyClient())

    with pytest.raises(AiException, match="MinIO object is empty"):
        client.fetch_bytes("uploads/1/report.pdf")


def test_task_consumer_acknowledges_real_parse_handler(text_pdf_bytes: bytes) -> None:
    """M1 MQ contract stays green when the parse handler uses injected deps."""
    document = Document(
        source="uploads/demo.pdf",
        page_count=2,
        pages=[
            Page(page_index=0, width=595, height=842),
            Page(page_index=1, width=595, height=842),
        ],
    )
    configure_handler(
        parser=FakeParser(document),
        object_store=FakeObjectStore(data=text_pdf_bytes),
    )
    producer = FakeProducer()
    consumer = TaskConsumer(Settings(mq_consumer_enabled=False), producer)
    channel = FakeChannel()
    body = json.dumps(
        {
            "taskId": "task-123",
            "step": "parse",
            "payload": {"pdfObjectKey": "uploads/demo.pdf"},
            "idempotencyKey": "task-123:parse",
        }
    ).encode()

    consumer.on_message(
        channel,
        FakeMethod(delivery_tag=17, routing_key="parse"),
        FakeProperties({"traceId": "trace-abc"}),
        body,
    )
    _run_queued_delivery(consumer)

    assert channel.acks == [17]
    assert channel.nacks == []
    progress = producer.messages[0][0]
    assert progress["taskId"] == "task-123"
    assert progress["step"] == "PARSE"
    assert progress["status"] == "SUCCESS"
    assert progress["idempotencyKey"] == "task-123:PARSE"
    assert progress["result"]["document"]["pageCount"] == 2


def test_task_consumer_reports_minio_failure_before_acknowledging() -> None:
    """MinIO failures must publish FAILED progress then ack the delivery."""
    configure_handler(
        parser=FakeParser(Document(source="uploads/demo.pdf", page_count=1, pages=[])),
        object_store=FakeObjectStore(error=AiException("MinIO unavailable")),
    )
    producer = FakeProducer()
    consumer = TaskConsumer(Settings(mq_consumer_enabled=False), producer)
    channel = FakeChannel()
    body = json.dumps(
        {
            "taskId": "task-456",
            "step": "parse",
            "payload": {"pdfObjectKey": "uploads/demo.pdf"},
            "idempotencyKey": "task-456:parse",
        }
    ).encode()

    consumer.on_message(
        channel,
        FakeMethod(delivery_tag=18, routing_key="parse"),
        FakeProperties({"traceId": "trace-def"}),
        body,
    )
    _run_queued_delivery(consumer)

    assert channel.acks == [18]
    assert producer.messages[0][0]["status"] == "FAILED"
    assert "MinIO unavailable" in producer.messages[0][0]["result"]["error"]
