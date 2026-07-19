"""RabbitMQ task and progress message schemas."""

from typing import Any

from pydantic import BaseModel, Field, model_validator


class TaskMessage(BaseModel):
    """Task message emitted by the L2 TaskMessageProducer."""

    task_id: str = Field(alias="taskId", min_length=1)
    step: str = Field(min_length=1)
    payload: dict[str, Any]
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1)

    @model_validator(mode="after")
    def validate_idempotency_key(self) -> "TaskMessage":
        """Require the stable taskId plus original routing step key."""
        expected = f"{self.task_id}:{self.step}"
        if self.idempotency_key != expected:
            raise ValueError(f"idempotencyKey must equal {expected}")
        return self


class ProgressMessage(BaseModel):
    """Progress update consumed by L2 ProgressConsumer."""

    task_id: str = Field(alias="taskId")
    step: str
    status: str
    progress: int = Field(ge=0, le=100)
    result: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(alias="idempotencyKey")
