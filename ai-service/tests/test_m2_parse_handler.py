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
    """In-memory object store used to mock MinIO fetches."""

    def __init__(
        self,
        *,
        data: bytes | None = None,
        error: Exception | None = None,
        empty: bool = False,
    ) -> None:
        self.data = b"" if empty else (data or b"%PDF-mock")
        self.error = error
        self.requests: list[tuple[str, str | None]] = []

    def fetch_bytes(self, object_key: str, bucket: str | None = None) -> bytes:
        """Return configured bytes or raise the configured error."""
        self.requests.append((object_key, bucket))
        if self.error is not None:
            raise self.error
        return self.data


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

    assert channel.acks == [18]
    assert producer.messages[0][0]["status"] == "FAILED"
    assert "MinIO unavailable" in producer.messages[0][0]["result"]["error"]
